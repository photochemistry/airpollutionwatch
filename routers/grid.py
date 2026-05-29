"""
グリッドAPI: 観測局データを空間補間し、地理院タイル座標系のグリッド値を提供する.

エンドポイント:
  GET /v1/grid/info      — メタ情報・キャッシュ状況
  GET /v1/grid/snapshot  — 指定タイル群・1時刻の補間値
  GET /v1/grid/field     — bbox 内全タイルの補間値（後方互換）
  GET /v1/grid/range     — bbox 内全タイルの補間値（複数時刻・推奨）
  GET /v1/grid/field/range — bbox 内全タイルの補間値（複数時刻）
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import sqlite3
import time
from typing import Callable, Dict, List, Literal

logger = logging.getLogger(__name__)

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from routers.v1 import DB_PATH, ITEM_PARAM_TO_COL
from grid.cache import (
    GridCacheEntry,
    evict_old_cache,
    get_cache,
    get_latest_info,
    put_cache,
    CACHE_TTL_HOURS,
)
from grid.field_jobs import get_or_compute_field_response
from grid.response_cache import evict_old_response_cache
from grid.interpolators import (
    interpolate_atps,
    interpolate_linear,
    interpolate_tps,
    interpolate_idw,
    interpolate_nnatural,
)
from grid.utils import (
    BoundingBox,
    _webmercator_lonlat_to_tile_xy,
    _webmercator_tile_xy_to_lonlat,
    get_tile_bounds,
    make_lonlat_grid_tiles,
)
from data.stations import get_stations_df

# 日本全体をカバーする bounding box（沖縄〜北海道、対馬〜国後）
JAPAN_BBOX = BoundingBox(min_lon=122.0, min_lat=24.0, max_lon=146.0, max_lat=46.0)

AVAILABLE_ZOOM_LEVELS = list(range(0, 14))  # 0..13
AVAILABLE_METHODS = ["atps", "tps", "linear", "idw", "nnatural"]
AVAILABLE_ITEMS = ["no2", "ox", "pm25", "so2", "no", "nox", "spm", "co", "nmhc", "temp", "hum"]
DEFAULT_METHOD = "atps"
AUTO_MARGIN_CANDIDATES = [2, 4, 8, 16, 24]

router = APIRouter(prefix="/v1/grid", tags=["grid"])


# ---------------------------------------------------------------------------
# Pydantic モデル
# ---------------------------------------------------------------------------

class GridInfo(BaseModel):
    available_zoom_levels: List[int]
    default_method: str
    available_methods: List[str]
    items: List[str]
    latest_grid_at: str | None
    latest_apw_snapshot_at: str | None
    cached_hours: int


class TileValue(BaseModel):
    x: int
    y: int
    values: Dict[str, float | None]


class GridSnapshotResponse(BaseModel):
    grid_generated_at: str | None
    apw_snapshot_at: str | None
    apw_oldest_station_at: str | None
    z: int
    datetime: str
    method: str
    tiles: List[TileValue]


class GridFieldResponse(BaseModel):
    grid_generated_at: str | None
    apw_snapshot_at: str | None
    apw_oldest_station_at: str | None
    z: int
    datetime: str
    method: str
    items: List[str] | None = None
    fields: Dict[str, List[List[float | None]]] | None = None
    # 後方互換: 単一項目時に従来形式も返す
    item: str | None = None
    tile_x_min: int
    tile_x_max: int
    tile_y_min: int
    tile_y_max: int
    values: List[List[float | None]] | None = None
    compute_domain: str | None = None
    fallback_level: str | None = None
    used_station_count: int | None = None


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------

def _fetch_station_snapshot(target_dt_iso: str, item_col: str) -> pd.DataFrame:
    """
    全国の測定局について、指定時刻以前の最新測定値を取得する.

    Returns
    -------
    DataFrame with columns: station_code, target_datetime, observed_datetime, {item_col}
    """
    query = f"""
        SELECT m.station_code, m.target_datetime, m.observed_datetime, m.{item_col}
        FROM measurements m
        INNER JOIN (
            SELECT station_code, MAX(target_datetime) AS max_dt
            FROM measurements
            WHERE target_datetime <= ?
            GROUP BY station_code
        ) latest
            ON  m.station_code    = latest.station_code
            AND m.target_datetime = latest.max_dt
        WHERE m.{item_col} IS NOT NULL
    """
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(query, conn, params=(target_dt_iso,))
    return df


def _prepare_station_arrays(
    datetime_hour: str,
    item: str,
    *,
    station_bbox: BoundingBox | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, str, int]:
    """補間に使う測定局配列を返す。station_bbox があれば局を空間フィルタする。"""
    item_col = ITEM_PARAM_TO_COL.get(item)
    if item_col is None:
        raise HTTPException(status_code=400, detail=f"不明な測定項目: {item}")

    meas_df = _fetch_station_snapshot(datetime_hour, item_col)
    if meas_df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"{datetime_hour} 時点の {item} データがありません",
        )

    stations_df = get_stations_df()
    meas_df["station_id"] = meas_df["station_code"].apply(lambda c: str(int(c)).zfill(8))
    merged = meas_df.merge(
        stations_df[["station_id", "lat", "lon"]],
        on="station_id",
        how="inner",
    )
    merged = merged.dropna(subset=["lat", "lon", item_col])
    merged[item_col] = pd.to_numeric(merged[item_col], errors="coerce")
    merged = merged.dropna(subset=[item_col])
    if station_bbox is not None:
        merged = merged[
            (merged["lon"] >= station_bbox.min_lon)
            & (merged["lon"] <= station_bbox.max_lon)
            & (merged["lat"] >= station_bbox.min_lat)
            & (merged["lat"] <= station_bbox.max_lat)
        ]

    if merged.empty:
        raise HTTPException(
            status_code=404,
            detail=f"{datetime_hour} 時点の {item} に有効な測定値がありません",
        )

    lon = merged["lon"].to_numpy(dtype=float)
    lat = merged["lat"].to_numpy(dtype=float)
    values = merged[item_col].to_numpy(dtype=float)
    apw_snapshot_at = str(merged["target_datetime"].max())
    apw_oldest_station_at = (
        str(merged["observed_datetime"].min())
        if "observed_datetime" in merged.columns
        else apw_snapshot_at
    )
    return lon, lat, values, apw_snapshot_at, apw_oldest_station_at, len(merged)


def _parse_bbox(bbox: str) -> BoundingBox:
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=400,
            detail="bbox は min_lon,min_lat,max_lon,max_lat の形式で指定してください",
        )
    try:
        bmin_lon, bmin_lat, bmax_lon, bmax_lat = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox の数値変換に失敗しました")
    return BoundingBox(
        min_lon=min(bmin_lon, bmax_lon),
        min_lat=min(bmin_lat, bmax_lat),
        max_lon=max(bmin_lon, bmax_lon),
        max_lat=max(bmin_lat, bmax_lat),
    )


def _expand_bbox_by_tiles(bbox: BoundingBox, z: int, margin_tiles: int) -> BoundingBox:
    """タイル数ベースで bbox を拡張する。"""
    if margin_tiles <= 0:
        return bbox
    tx_min, tx_max, ty_min, ty_max = get_tile_bounds(bbox, z)
    n = 2**z
    tx_min2 = max(0, tx_min - margin_tiles)
    tx_max2 = min(n - 1, tx_max + margin_tiles)
    ty_min2 = max(0, ty_min - margin_tiles)
    ty_max2 = min(n - 1, ty_max + margin_tiles)
    west_lon, north_lat = _webmercator_tile_xy_to_lonlat(tx_min2, ty_min2, z)
    east_lon, south_lat = _webmercator_tile_xy_to_lonlat(tx_max2 + 1, ty_max2 + 1, z)
    return BoundingBox(
        min_lon=min(west_lon, east_lon),
        min_lat=min(south_lat, north_lat),
        max_lon=max(west_lon, east_lon),
        max_lat=max(south_lat, north_lat),
    )


def _fetch_station_count(target_dt_iso: str, item_col: str) -> int:
    """指定時刻・物質の有効測定局数を返す（キャッシュ無効化判定用）。"""
    query = f"""
        SELECT COUNT(*) FROM measurements m
        INNER JOIN (
            SELECT station_code, MAX(target_datetime) AS max_dt
            FROM measurements
            WHERE target_datetime <= ?
            GROUP BY station_code
        ) latest
            ON  m.station_code    = latest.station_code
            AND m.target_datetime = latest.max_dt
        WHERE m.{item_col} IS NOT NULL
    """
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(query, (target_dt_iso,)).fetchone()[0]


def _fetch_station_snapshot_meta(target_dt_iso: str, item_col: str) -> tuple[int, str | None]:
    """
    指定時刻・物質の最新スナップショット要約を返す。

    Returns
    -------
    (count, latest_target_datetime)
      - count: 有効測定局数
      - latest_target_datetime: JOIN 後データの最大 target_datetime（データがなければ None）
    """
    query = f"""
        SELECT COUNT(*) AS count, MAX(m.target_datetime) AS latest_target_datetime
        FROM measurements m
        INNER JOIN (
            SELECT station_code, MAX(target_datetime) AS max_dt
            FROM measurements
            WHERE target_datetime <= ?
            GROUP BY station_code
        ) latest
            ON  m.station_code    = latest.station_code
            AND m.target_datetime = latest.max_dt
        WHERE m.{item_col} IS NOT NULL
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(query, (target_dt_iso,)).fetchone()
    if row is None:
        return 0, None
    return int(row[0] or 0), row[1]


def ensure_grid_db_indexes() -> None:
    """
    grid キャッシュ妥当性チェックで使う measurements クエリ向けインデックスを作成する。
    重複作成は IF NOT EXISTS で回避する。
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_measurements_target_station
                ON measurements (target_datetime, station_code)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_measurements_station_target
                ON measurements (station_code, target_datetime)
                """
            )
            conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("grid DB index creation skipped: %s", e)


def _get_or_compute_grid(
    datetime_hour: str,
    z: int,
    method: str,
    item: str,
    smoothing: float = 0.001,
) -> GridCacheEntry:
    """
    キャッシュから取得。なければ補間計算してキャッシュに保存し返す.
    collect で新データが入った場合は測定局数が変わるため、キャッシュを無効化して再計算する。
    """
    item_col = ITEM_PARAM_TO_COL.get(item)
    cached = get_cache(datetime_hour, z, method, item, smoothing)
    if cached is not None and item_col is not None:
        # データ更新検知:
        # - 測定局数が変わった
        # - 最新スナップショット時刻（MAX target_datetime）が進んだ
        # のいずれかでキャッシュを無効化する。
        cached_count = cached.get("apw_station_count")
        cached_snapshot_at = cached.get("apw_snapshot_at")
        current_count, current_snapshot_at = _fetch_station_snapshot_meta(datetime_hour, item_col)
        if cached_count is not None and current_count != cached_count:
            logger.info(
                "grid cache invalidated (station_count changed): %s z=%d method=%s item=%s cached=%s current=%s",
                datetime_hour, z, method, item, cached_count, current_count,
            )
            cached = None
        elif cached_snapshot_at != current_snapshot_at:
            logger.info(
                "grid cache invalidated (snapshot advanced): %s z=%d method=%s item=%s cached=%s current=%s",
                datetime_hour, z, method, item, cached_snapshot_at, current_snapshot_at,
            )
            cached = None
    if cached is not None:
        logger.info(
            "grid cache hit: %s z=%d method=%s item=%s",
            datetime_hour, z, method, item,
        )
        return cached

    if item_col is None:
        raise HTTPException(status_code=400, detail=f"不明な測定項目: {item}")

    # 全国スナップショットを DB から取得
    meas_df = _fetch_station_snapshot(datetime_hour, item_col)
    if meas_df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"{datetime_hour} 時点の {item} データがありません",
        )

    # 測定局の lat/lon と結合
    stations_df = get_stations_df()
    meas_df["station_id"] = meas_df["station_code"].apply(
        lambda c: str(int(c)).zfill(8)
    )
    merged = meas_df.merge(
        stations_df[["station_id", "lat", "lon"]],
        on="station_id",
        how="inner",
    )
    merged = merged.dropna(subset=["lat", "lon", item_col])
    merged[item_col] = pd.to_numeric(merged[item_col], errors="coerce")
    merged = merged.dropna(subset=[item_col])

    if merged.empty:
        raise HTTPException(
            status_code=404,
            detail=f"{datetime_hour} 時点の {item} に有効な測定値がありません",
        )

    lon = merged["lon"].to_numpy(dtype=float)
    lat = merged["lat"].to_numpy(dtype=float)
    values = merged[item_col].to_numpy(dtype=float)

    apw_snapshot_at = str(merged["target_datetime"].max())
    apw_oldest_station_at = (
        str(merged["observed_datetime"].min())
        if "observed_datetime" in merged.columns
        else apw_snapshot_at
    )

    # タイルグリッドを生成
    lon2d, lat2d = make_lonlat_grid_tiles(JAPAN_BBOX, z)

    # 補間
    smoothing_desc = f"smoothing={smoothing}" if method in ("atps", "tps") else ""
    logger.info(
        "grid cache miss – computing: %s z=%d %s %s %s stations=%d",
        datetime_hour, z, method, item, smoothing_desc, len(values),
    )
    t0 = time.perf_counter()

    if method == "atps":
        field = interpolate_atps(lon, lat, values, lon2d, lat2d, smoothing=smoothing)
    elif method == "tps":
        field = interpolate_tps(lon, lat, values, lon2d, lat2d, smoothing=smoothing)
    elif method == "linear":
        field = interpolate_linear(lon, lat, values, lon2d, lat2d)
    elif method == "idw":
        field = interpolate_idw(lon, lat, values, lon2d, lat2d)
    elif method == "nnatural":
        field = interpolate_nnatural(lon, lat, values, lon2d, lat2d)
    else:
        raise HTTPException(status_code=400, detail=f"不明なメソッド: {method}")

    elapsed = time.perf_counter() - t0
    tile_x_min, tile_x_max, tile_y_min, tile_y_max = get_tile_bounds(JAPAN_BBOX, z)
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    logger.info(
        "grid cache updated: %s z=%d %s %s %s  shape=%s  %.2fs",
        datetime_hour, z, method, item, smoothing_desc,
        f"{field.shape[0]}x{field.shape[1]}", elapsed,
    )

    put_cache(
        datetime_hour, z, method, item,
        tile_x_min, tile_x_max, tile_y_min, tile_y_max,
        field.astype(np.float32),
        apw_snapshot_at, apw_oldest_station_at, generated_at,
        smoothing=smoothing,
        apw_station_count=len(merged),
    )

    return GridCacheEntry(
        tile_x_min=tile_x_min,
        tile_x_max=tile_x_max,
        tile_y_min=tile_y_min,
        tile_y_max=tile_y_max,
        field=field.astype(np.float32),
        apw_snapshot_at=apw_snapshot_at,
        apw_oldest_station_at=apw_oldest_station_at,
        generated_at=generated_at,
        apw_station_count=len(merged),
    )


def _tile_to_index(
    tx: int, ty: int, entry: GridCacheEntry
) -> tuple[int, int] | tuple[None, None]:
    """
    タイル座標 (tx, ty) をフィールド配列の (row, col) インデックスに変換する.

    フィールドの行方向: 南→北（row 0 = tile y_max = 最南端）
    フィールドの列方向: 西→東（col 0 = tile x_min = 最西端）
    """
    col = tx - entry["tile_x_min"]
    row = entry["tile_y_max"] - ty
    ny, nx = entry["field"].shape
    if col < 0 or col >= nx or row < 0 or row >= ny:
        return None, None
    return row, col


def _parse_tiles_param(tiles: str) -> list[tuple[int, int]]:
    """tiles=x,y;x,y;... をパースしてタイル座標の配列を返す。"""
    tile_list: list[tuple[int, int]] = []
    for token in tiles.split(";"):
        token = token.strip()
        if not token:
            continue
        parts = token.split(",")
        if len(parts) != 2:
            raise HTTPException(
                status_code=400, detail=f"tiles の形式が不正: {token!r}"
            )
        try:
            tile_list.append((int(parts[0]), int(parts[1])))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"tiles の整数変換に失敗: {token!r}"
            )
    if not tile_list:
        raise HTTPException(status_code=400, detail="tiles を 1 つ以上指定してください")
    return tile_list


def _build_grid_field_response(
    entry: GridCacheEntry,
    z: int,
    datetime_hour_iso: str,
    method: str,
    item: str,
    out_tx_min: int,
    out_tx_max: int,
    out_ty_min: int,
    out_ty_max: int,
) -> GridFieldResponse:
    """全国フィールドから指定タイル範囲を切り出して返す。"""
    tx_min = entry["tile_x_min"]
    tx_max = entry["tile_x_max"]
    ty_min = entry["tile_y_min"]
    ty_max = entry["tile_y_max"]

    if (
        out_tx_min > out_tx_max
        or out_ty_min > out_ty_max
        or out_tx_max < tx_min
        or out_tx_min > tx_max
        or out_ty_max < ty_min
        or out_ty_min > ty_max
    ):
        return GridFieldResponse(
            grid_generated_at=entry["generated_at"],
            apw_snapshot_at=entry["apw_snapshot_at"],
            apw_oldest_station_at=entry["apw_oldest_station_at"],
            z=z,
            datetime=datetime_hour_iso,
            method=method,
            items=[item],
            fields={item: []},
            item=item,
            tile_x_min=out_tx_min,
            tile_x_max=out_tx_max,
            tile_y_min=out_ty_min,
            tile_y_max=out_ty_max,
            values=[],
        )

    clip_tx_min = max(out_tx_min, tx_min)
    clip_tx_max = min(out_tx_max, tx_max)
    clip_ty_min = max(out_ty_min, ty_min)
    clip_ty_max = min(out_ty_max, ty_max)

    # col: tile_x_min=0 ... tile_x_max=nx-1 (west→east)
    # row: tile_y_max=0 ... tile_y_min=ny-1 (south→north in field, tile y は北が小さい)
    col_start = clip_tx_min - tx_min
    col_end = clip_tx_max - tx_min + 1
    row_start = ty_max - clip_ty_max
    row_end = ty_max - clip_ty_min + 1

    sub_field = entry["field"][row_start:row_end, col_start:col_end]
    values_2d: list[list[float | None]] = [
        [None if np.isnan(v) else float(v) for v in row_arr]
        for row_arr in sub_field
    ]

    return GridFieldResponse(
        grid_generated_at=entry["generated_at"],
        apw_snapshot_at=entry["apw_snapshot_at"],
        apw_oldest_station_at=entry["apw_oldest_station_at"],
        z=z,
        datetime=datetime_hour_iso,
        method=method,
        items=[item],
        fields={item: values_2d},
        item=item,
        tile_x_min=clip_tx_min,
        tile_x_max=clip_tx_max,
        tile_y_min=clip_ty_min,
        tile_y_max=clip_ty_max,
        values=values_2d,
    )


def _extract_tile_value_map(
    entry: GridCacheEntry,
    out_tx_min: int,
    out_tx_max: int,
    out_ty_min: int,
    out_ty_max: int,
) -> dict[tuple[int, int], float | None]:
    """
    指定タイル範囲の値を辞書で返す。
    内部的には tiles/field と同じ切り出し処理を利用する。
    """
    sliced = _build_grid_field_response(
        entry=entry,
        z=0,  # 値抽出用途のため未使用
        datetime_hour_iso="",
        method="",
        item="",
        out_tx_min=out_tx_min,
        out_tx_max=out_tx_max,
        out_ty_min=out_ty_min,
        out_ty_max=out_ty_max,
    )
    if not sliced.values:
        return {}

    value_map: dict[tuple[int, int], float | None] = {}
    for row_idx, row_vals in enumerate(sliced.values):
        ty = sliced.tile_y_max - row_idx
        for col_idx, v in enumerate(row_vals):
            tx = sliced.tile_x_min + col_idx
            value_map[(tx, ty)] = v
    return value_map


def _resolve_field_items(
    item: str | None = None,
    pollutant: str | None = None,
    items: str | None = None,
    pollutants: str | None = None,
) -> list[str]:
    """item / pollutant / items / pollutants から測定項目リストを得る（順序維持・重複排除）。"""
    item_tokens: list[str] = []
    for raw in (item, pollutant, items, pollutants):
        if raw is None:
            continue
        for token in raw.split(","):
            t = token.strip().lower()
            if t:
                item_tokens.append(t)
    if not item_tokens:
        raise HTTPException(
            status_code=400,
            detail="item / pollutant / items / pollutants のいずれかを指定してください",
        )
    item_list: list[str] = []
    for t in item_tokens:
        if t not in item_list:
            item_list.append(t)
    unknown = [p for p in item_list if p not in ITEM_PARAM_TO_COL]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不明な測定項目: {unknown}")
    return item_list


def _field_data_version(dt_hour_iso: str, items: list[str]) -> str:
    """観測データ更新検知用。成分ごとの有効測定局数を連結する。"""
    parts: list[str] = []
    for item in items:
        col = ITEM_PARAM_TO_COL[item]
        parts.append(f"{item}:{_fetch_station_count(dt_hour_iso, col)}")
    return ",".join(parts)


def _field_response_cache_key(
    dt_hour_iso: str,
    z: int,
    method: str,
    items: list[str],
    bbox: str | None,
    smoothing: float,
) -> str:
    items_part = ",".join(sorted(items))
    bbox_part = bbox.strip() if bbox else "national"
    return (
        f"grid_field|{dt_hour_iso}|z={z}|m={method}|items={items_part}"
        f"|sm={smoothing:.6g}|bbox={bbox_part}"
    )


def _bbox_tile_range(
    bbox: str,
    z: int,
    tx_min: int,
    tx_max: int,
    ty_min: int,
    ty_max: int,
) -> tuple[int, int, int, int]:
    parsed = _parse_bbox(bbox)
    bmin_lon, bmin_lat, bmax_lon, bmax_lat = (
        parsed.min_lon,
        parsed.min_lat,
        parsed.max_lon,
        parsed.max_lat,
    )

    bx_min_f, by_max_f = _webmercator_lonlat_to_tile_xy(bmin_lon, bmin_lat, z)
    bx_max_f, by_min_f = _webmercator_lonlat_to_tile_xy(bmax_lon, bmax_lat, z)

    out_tx_min = max(int(math.floor(min(bx_min_f, bx_max_f))), tx_min)
    out_tx_max = min(int(math.floor(max(bx_min_f, bx_max_f))), tx_max)
    out_ty_min = max(int(math.floor(min(by_min_f, by_max_f))), ty_min)
    out_ty_max = min(int(math.floor(max(by_min_f, by_max_f))), ty_max)
    return out_tx_min, out_tx_max, out_ty_min, out_ty_max


def _compute_grid_field_body(
    dt_hour_iso: str,
    z: int,
    method: str,
    items: list[str],
    bbox: str | None,
    smoothing: float,
) -> bytes:
    """grid/field の JSON レスポンス body を生成する（同期・バックグラウンド用）。"""
    first_entry = _get_or_compute_grid(dt_hour_iso, z, method, items[0], smoothing)
    tx_min = first_entry["tile_x_min"]
    tx_max = first_entry["tile_x_max"]
    ty_min = first_entry["tile_y_min"]
    ty_max = first_entry["tile_y_max"]

    out_tx_min, out_tx_max = tx_min, tx_max
    out_ty_min, out_ty_max = ty_min, ty_max
    if bbox is not None:
        out_tx_min, out_tx_max, out_ty_min, out_ty_max = _bbox_tile_range(
            bbox, z, tx_min, tx_max, ty_min, ty_max
        )

    if len(items) == 1:
        resp = _build_grid_field_response(
            first_entry,
            z,
            dt_hour_iso,
            method,
            items[0],
            out_tx_min,
            out_tx_max,
            out_ty_min,
            out_ty_max,
        )
        if hasattr(resp, "model_dump_json"):
            return resp.model_dump_json().encode("utf-8")
        return resp.json(ensure_ascii=False).encode("utf-8")

    fields: dict[str, list[list[float | None]]] = {}
    grid_generated_at = first_entry["generated_at"]
    apw_snapshot_at = first_entry["apw_snapshot_at"]
    apw_oldest_station_at = first_entry["apw_oldest_station_at"]

    for item in items:
        entry = (
            first_entry
            if item == items[0]
            else _get_or_compute_grid(dt_hour_iso, z, method, item, smoothing)
        )
        sliced = _build_grid_field_response(
            entry,
            z,
            dt_hour_iso,
            method,
            item,
            out_tx_min,
            out_tx_max,
            out_ty_min,
            out_ty_max,
        )
        fields[item] = sliced.values
        if grid_generated_at is None:
            grid_generated_at = entry["generated_at"]
        if apw_snapshot_at is None:
            apw_snapshot_at = entry["apw_snapshot_at"]
        if apw_oldest_station_at is None:
            apw_oldest_station_at = entry["apw_oldest_station_at"]

    payload = {
        "grid_generated_at": grid_generated_at,
        "apw_snapshot_at": apw_snapshot_at,
        "apw_oldest_station_at": apw_oldest_station_at,
        "z": z,
        "datetime": dt_hour_iso,
        "method": method,
        "items": items,
        "tile_x_min": out_tx_min,
        "tile_x_max": out_tx_max,
        "tile_y_min": out_ty_min,
        "tile_y_max": out_ty_max,
        "fields": fields,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _compute_grid_field_body_local_bbox(
    dt_hour_iso: str,
    z: int,
    method: str,
    items: list[str],
    bbox: str,
    smoothing: float,
    station_margin_tiles: int,
    min_station_count: int,
) -> bytes:
    """grid/field の bbox 領域のみを直接補間して返す（高速化向け）。"""
    target_bbox = _parse_bbox(bbox)
    out_tx_min, out_tx_max, out_ty_min, out_ty_max = _bbox_tile_range(
        bbox, z, 0, (2**z) - 1, 0, (2**z) - 1
    )
    lon2d, lat2d = make_lonlat_grid_tiles(target_bbox, z)
    source_bbox = _expand_bbox_by_tiles(target_bbox, z, station_margin_tiles)

    fields: dict[str, list[list[float | None]]] = {}
    apw_snapshot_at = None
    apw_oldest_station_at = None
    used_station_count = None
    fallback_level = "bbox"

    for item in items:
        try:
            lon, lat, values, snap_at, oldest_at, station_count = _prepare_station_arrays(
                dt_hour_iso, item, station_bbox=source_bbox
            )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            lon, lat, values, snap_at, oldest_at, station_count = _prepare_station_arrays(
                dt_hour_iso, item, station_bbox=None
            )
            fallback_level = "national_station_set"
        if station_count < min_station_count:
            lon, lat, values, snap_at, oldest_at, station_count = _prepare_station_arrays(
                dt_hour_iso, item, station_bbox=None
            )
            fallback_level = "national_station_set"
        if method == "atps":
            field = interpolate_atps(lon, lat, values, lon2d, lat2d, smoothing=smoothing)
        elif method == "tps":
            field = interpolate_tps(lon, lat, values, lon2d, lat2d, smoothing=smoothing)
        elif method == "linear":
            field = interpolate_linear(lon, lat, values, lon2d, lat2d)
        elif method == "idw":
            field = interpolate_idw(lon, lat, values, lon2d, lat2d)
        elif method == "nnatural":
            field = interpolate_nnatural(lon, lat, values, lon2d, lat2d)
        else:
            raise HTTPException(status_code=400, detail=f"不明なメソッド: {method}")

        fields[item] = [
            [None if np.isnan(v) else float(v) for v in row_arr]
            for row_arr in field
        ]
        apw_snapshot_at = snap_at if apw_snapshot_at is None else apw_snapshot_at
        apw_oldest_station_at = oldest_at if apw_oldest_station_at is None else apw_oldest_station_at
        used_station_count = station_count if used_station_count is None else min(used_station_count, station_count)

    payload = {
        "grid_generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "apw_snapshot_at": apw_snapshot_at,
        "apw_oldest_station_at": apw_oldest_station_at,
        "z": z,
        "datetime": dt_hour_iso,
        "method": method,
        "items": items,
        "tile_x_min": out_tx_min,
        "tile_x_max": out_tx_max,
        "tile_y_min": out_ty_min,
        "tile_y_max": out_ty_max,
        "fields": fields,
        "compute_domain": "bbox",
        "fallback_level": fallback_level,
        "used_station_count": used_station_count,
    }
    if len(items) == 1:
        payload["item"] = items[0]
        payload["values"] = fields[items[0]]
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _compute_grid_field_body_auto_bbox(
    dt_hour_iso: str,
    z: int,
    method: str,
    items: list[str],
    bbox: str,
    smoothing: float,
    min_station_count: int,
    margin_candidates: list[int] | None = None,
) -> bytes:
    """bbox 指定時に、局数条件を満たす最小 margin を自動選択して補間する。"""
    target_bbox = _parse_bbox(bbox)
    candidates = margin_candidates or AUTO_MARGIN_CANDIDATES
    # 項目ごとに必要 margin を見積もり、全項目の最大値を採用する
    required_margin = 0
    for item in items:
        selected_for_item = candidates[-1]
        for margin in candidates:
            source_bbox = _expand_bbox_by_tiles(target_bbox, z, margin)
            try:
                _, _, _, _, _, station_count = _prepare_station_arrays(
                    dt_hour_iso, item, station_bbox=source_bbox
                )
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                station_count = 0
            if station_count >= min_station_count:
                selected_for_item = margin
                break
        required_margin = max(required_margin, selected_for_item)

    body = _compute_grid_field_body_local_bbox(
        dt_hour_iso=dt_hour_iso,
        z=z,
        method=method,
        items=items,
        bbox=bbox,
        smoothing=smoothing,
        station_margin_tiles=required_margin,
        min_station_count=min_station_count,
    )
    payload = _parse_body_json(body)
    payload["compute_domain"] = "auto-bbox"
    payload["auto_selected_margin_tiles"] = required_margin
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _parse_body_json(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def _build_grid_range_payload(
    *,
    dt_from: datetime.datetime,
    dt_to: datetime.datetime,
    z: int,
    method: str,
    item_list: list[str],
    bbox: str,
    compute_body_for_hour: Callable[[str], bytes],
) -> dict:
    """
    grid/range・grid/field 共通で使う時刻レンジ集約処理。
    compute_body_for_hour は「対象時刻1件ぶんの field JSON body」を返す関数。
    """
    total_hours = int((dt_to - dt_from).total_seconds() // 3600) + 1
    fields_by_datetime: dict[str, dict] = {}
    timestamps: list[str] = []
    stacked_values: dict[str, list[list[list[float | None]]]] = {
        item_name: [] for item_name in item_list
    }
    grid_meta: dict | None = None
    current = dt_from
    while current <= dt_to:
        dt_hour_iso = current.isoformat()
        body = compute_body_for_hour(dt_hour_iso)
        payload = _parse_body_json(body)
        fields_by_datetime[dt_hour_iso] = payload
        timestamps.append(dt_hour_iso)

        if grid_meta is None:
            gx_min = int(payload["tile_x_min"])
            gx_max = int(payload["tile_x_max"])
            gy_min = int(payload["tile_y_min"])
            gy_max = int(payload["tile_y_max"])
            grid_meta = {
                "z": z,
                "xMin": gx_min,
                "xMax": gx_max,
                "yMin": gy_min,
                "yMax": gy_max,
                "width": gx_max - gx_min + 1,
                "height": gy_max - gy_min + 1,
                # タイル格子の座標系を明示（地理院/OSM互換のWeb Mercator tile grid）
                "crs": "WebMercatorTile",
            }

        payload_fields = payload.get("fields") or {}
        if len(item_list) == 1 and (not payload_fields):
            single_item = item_list[0]
            payload_fields = {single_item: payload.get("values", [])}
        for item_name in item_list:
            stacked_values[item_name].append(payload_fields.get(item_name, []))
        current += datetime.timedelta(hours=1)

    return {
        "grid": grid_meta,
        "z": z,
        "from": dt_from.isoformat(),
        "to": dt_to.isoformat(),
        "method": method,
        "items": item_list,
        "bbox": bbox,
        "count_hours": total_hours,
        "timestamps": timestamps,
        "values": stacked_values,
        # 互換のため残す（従来実装を参照しているクライアント向け）
        "series": fields_by_datetime,
    }


def _single_field_payload_from_range_payload(
    *,
    range_payload: dict,
    dt_hour_iso: str,
) -> dict:
    """range ペイロード（1時刻）から field 互換ペイロードへ変換する。"""
    item_list = list(range_payload.get("items") or [])
    grid_meta = range_payload.get("grid") or {}
    series = range_payload.get("series") or {}
    src = series.get(dt_hour_iso) or {}
    stacked_values = range_payload.get("values") or {}

    fields: dict[str, list[list[float | None]]] = {}
    for item_name in item_list:
        item_stack = stacked_values.get(item_name) or []
        fields[item_name] = item_stack[0] if item_stack else []

    payload = {
        "grid_generated_at": src.get("grid_generated_at"),
        "apw_snapshot_at": src.get("apw_snapshot_at"),
        "apw_oldest_station_at": src.get("apw_oldest_station_at"),
        "z": int(range_payload.get("z")),
        "datetime": dt_hour_iso,
        "method": range_payload.get("method"),
        "items": item_list,
        "tile_x_min": int(grid_meta.get("xMin")),
        "tile_x_max": int(grid_meta.get("xMax")),
        "tile_y_min": int(grid_meta.get("yMin")),
        "tile_y_max": int(grid_meta.get("yMax")),
        "fields": fields,
    }
    # field 特有の付帯情報は series 側にあれば引き継ぐ
    for k in ("compute_domain", "fallback_level", "used_station_count", "auto_selected_margin_tiles"):
        if k in src:
            payload[k] = src[k]
    if len(item_list) == 1:
        payload["item"] = item_list[0]
        payload["values"] = fields[item_list[0]]
    return payload


def _ensure_field_response_info(
    body: bytes,
    *,
    status: str,
    requested_items: list[str],
    compute_domain: str,
    bbox: str | None,
) -> bytes:
    """
    grid/field の返却JSONに info 系キーを補完する。
    既存キャッシュが古い形式でも、items/fields/compute_domain/cache_status を安定提供する。
    """
    payload = _parse_body_json(body)
    changed = False

    items = payload.get("items")
    if not isinstance(items, list):
        single_item = payload.get("item")
        payload["items"] = [single_item] if isinstance(single_item, str) else list(requested_items)
        items = payload["items"]
        changed = True

    fields = payload.get("fields")
    if not isinstance(fields, dict):
        if len(items) == 1 and "values" in payload:
            payload["fields"] = {items[0]: payload.get("values") or []}
        else:
            payload["fields"] = {}
        changed = True

    if payload.get("compute_domain") is None:
        if compute_domain == "auto" and bbox is not None:
            payload["compute_domain"] = "auto-bbox"
        else:
            payload["compute_domain"] = compute_domain
        changed = True

    if payload.get("cache_status") != status:
        payload["cache_status"] = status
        changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _parse_iso_datetime_or_400(raw: str, *, field_name: str) -> datetime.datetime:
    """ISO8601 文字列を datetime に変換する（末尾 Z も許容）。"""
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(normalized)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} の形式が不正（ISO 8601 で指定してください）",
        )


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------

@router.get("/info", response_model=GridInfo)
async def grid_info():
    """グリッド API のメタ情報・キャッシュ状況を返す。"""
    evict_old_cache()
    evict_old_response_cache()
    latest_grid_at, latest_apw_snapshot_at = get_latest_info()
    return GridInfo(
        available_zoom_levels=AVAILABLE_ZOOM_LEVELS,
        default_method=DEFAULT_METHOD,
        available_methods=AVAILABLE_METHODS,
        items=AVAILABLE_ITEMS,
        latest_grid_at=latest_grid_at,
        latest_apw_snapshot_at=latest_apw_snapshot_at,
        cached_hours=CACHE_TTL_HOURS,
    )


@router.get("/snapshot", response_model=GridSnapshotResponse)
async def grid_snapshot(
    z: int = Query(..., description="ズームレベル（12 または 14）"),
    tiles: str = Query(
        ...,
        description="x,y ペアをセミコロン区切り。例: 3550,1520;3551,1520",
    ),
    items: str = Query(
        "no2,ox,pm25",
        description="カンマ区切りの測定項目名（no2, ox, pm25, so2, no, nox, spm, co など）",
    ),
    datetime_: str = Query(..., alias="datetime", description="対象時刻 ISO 8601"),
    method: str = Query(DEFAULT_METHOD, description="補間メソッド: atps / tps / linear"),
    smoothing: float = Query(0.001, description="atps / tps の平滑化強度。0 で厳密補間、大きいほど滑らか"),
):
    """指定タイル群・1 時刻の補間値スナップショットを返す。"""
    if z not in AVAILABLE_ZOOM_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"z は {AVAILABLE_ZOOM_LEVELS} のいずれかにしてください",
        )
    if method not in AVAILABLE_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"method は {AVAILABLE_METHODS} のいずれかにしてください",
        )

    tile_list = _parse_tiles_param(tiles)

    # datetime パース・正時丸め
    try:
        dt = _parse_iso_datetime_or_400(datetime_, field_name="datetime")
    except HTTPException:
        raise
    dt_hour = dt.replace(minute=0, second=0, microsecond=0)
    dt_hour_iso = dt_hour.isoformat()

    # 測定項目リスト
    item_list = [p.strip().lower() for p in items.split(",") if p.strip()]
    unknown = [p for p in item_list if p not in ITEM_PARAM_TO_COL]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不明な測定項目: {unknown}")

    out_tx_min = min(tx for tx, _ in tile_list)
    out_tx_max = max(tx for tx, _ in tile_list)
    out_ty_min = min(ty for _, ty in tile_list)
    out_ty_max = max(ty for _, ty in tile_list)

    # 各タイルの値を収集（内部的には tiles と同じ切り出し処理を利用）
    tile_values: dict[tuple[int, int], dict[str, float | None]] = {
        (tx, ty): {} for tx, ty in tile_list
    }
    grid_generated_at = None
    apw_snapshot_at = None
    apw_oldest_station_at = None

    for item in item_list:
        entry = _get_or_compute_grid(dt_hour_iso, z, method, item, smoothing)
        if grid_generated_at is None:
            grid_generated_at = entry["generated_at"]
            apw_snapshot_at = entry["apw_snapshot_at"]
            apw_oldest_station_at = entry["apw_oldest_station_at"]
        value_map = _extract_tile_value_map(
            entry,
            out_tx_min=out_tx_min,
            out_tx_max=out_tx_max,
            out_ty_min=out_ty_min,
            out_ty_max=out_ty_max,
        )
        for tx, ty in tile_list:
            tile_values[(tx, ty)][item] = value_map.get((tx, ty))

    tiles_out = [
        TileValue(x=tx, y=ty, values=tile_values[(tx, ty)])
        for tx, ty in tile_list
    ]
    return GridSnapshotResponse(
        grid_generated_at=grid_generated_at,
        apw_snapshot_at=apw_snapshot_at,
        apw_oldest_station_at=apw_oldest_station_at,
        z=z,
        datetime=dt_hour_iso,
        method=method,
        tiles=tiles_out,
    )


@router.get("/field")
async def grid_field(
    z: int = Query(..., description="ズームレベル（12 または 14）"),
    item: str | None = Query(None, description="測定項目名（カンマ区切り可）"),
    pollutant: str | None = Query(None, description="item のエイリアス（カンマ区切り可）"),
    items: str | None = Query(None, description="測定項目名（カンマ区切り）"),
    pollutants: str | None = Query(None, description="items のエイリアス（カンマ区切り）"),
    datetime_: str = Query(..., alias="datetime", description="対象時刻 ISO 8601"),
    bbox: str | None = Query(
        None,
        description="min_lon,min_lat,max_lon,max_lat（省略時は全国）",
    ),
    method: str = Query(DEFAULT_METHOD, description="補間メソッド: atps / tps / linear"),
    smoothing: float = Query(0.001, description="atps / tps の平滑化強度。0 で厳密補間、大きいほど滑らか"),
    compute_domain: Literal["auto", "national", "bbox"] = Query(
        "national",
        description="補間計算領域。national=全国計算（推奨）、auto=自動選択（bbox指定時は部分計算）、bbox=bbox領域のみ直接補間",
    ),
    station_margin_tiles: int | None = Query(
        None,
        ge=0,
        le=256,
        description="compute_domain=bbox のとき、観測局選定に使う bbox 拡張タイル数。未指定時は自動",
    ),
    min_station_count: int = Query(
        16,
        ge=1,
        le=10000,
        description="compute_domain=bbox のときの最小観測局数。未満なら全国局集合へ自動フォールバック",
    ),
):
    """bbox 内の全タイルの補間値を返す（地図描画用）。bbox 省略時は全国。"""
    if z not in AVAILABLE_ZOOM_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"z は {AVAILABLE_ZOOM_LEVELS} のいずれかにしてください",
        )
    if method not in AVAILABLE_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"method は {AVAILABLE_METHODS} のいずれかにしてください",
        )

    item_list = _resolve_field_items(item, pollutant, items, pollutants)

    try:
        dt = _parse_iso_datetime_or_400(datetime_, field_name="datetime")
    except HTTPException:
        raise
    dt_hour = dt.replace(minute=0, second=0, microsecond=0)
    dt_hour_iso = dt_hour.isoformat()

    if compute_domain == "bbox" and bbox is None:
        raise HTTPException(
            status_code=400,
            detail="compute_domain=bbox のときは bbox を指定してください",
        )

    cache_key = _field_response_cache_key(
        dt_hour_iso, z, method, item_list, bbox, smoothing
    ) + f"|domain={compute_domain}|smg={station_margin_tiles}|msc={min_station_count}"

    def _compute() -> bytes:
        resolved_bbox = bbox or (
            f"{JAPAN_BBOX.min_lon},{JAPAN_BBOX.min_lat},{JAPAN_BBOX.max_lon},{JAPAN_BBOX.max_lat}"
        )

        def _compute_one_hour(hour_iso: str) -> bytes:
            if compute_domain == "auto" and bbox is not None:
                return _compute_grid_field_body_auto_bbox(
                    dt_hour_iso=hour_iso,
                    z=z,
                    method=method,
                    items=item_list,
                    bbox=bbox,
                    smoothing=smoothing,
                    min_station_count=min_station_count,
                )
            if compute_domain == "bbox":
                resolved_margin = (
                    station_margin_tiles if station_margin_tiles is not None else 8
                )
                return _compute_grid_field_body_local_bbox(
                    hour_iso,
                    z,
                    method,
                    item_list,
                    bbox,
                    smoothing,
                    resolved_margin,
                    min_station_count,
                )
            return _compute_grid_field_body(
                hour_iso, z, method, item_list, bbox, smoothing
            )

        range_payload = _build_grid_range_payload(
            dt_from=dt_hour,
            dt_to=dt_hour,
            z=z,
            method=method,
            item_list=item_list,
            bbox=resolved_bbox,
            compute_body_for_hour=_compute_one_hour,
        )
        field_payload = _single_field_payload_from_range_payload(
            range_payload=range_payload,
            dt_hour_iso=dt_hour_iso,
        )
        return json.dumps(field_payload, ensure_ascii=False).encode("utf-8")

    def _data_version() -> str:
        return _field_data_version(dt_hour_iso, item_list)

    body, status, detail = await asyncio.to_thread(
        lambda: get_or_compute_field_response(
            cache_key,
            _compute,
            data_version_fn=_data_version,
        )
    )
    if body is not None:
        body = _ensure_field_response_info(
            body,
            status=status,
            requested_items=item_list,
            compute_domain=compute_domain,
            bbox=bbox,
        )
        return Response(content=body, media_type="application/json")
    if status == "pending":
        raise HTTPException(
            status_code=503,
            detail=detail or "grid field is being computed; retry shortly",
            headers={"Retry-After": "5"},
        )
    raise HTTPException(
        status_code=500,
        detail=detail or "grid field compute failed",
    )


@router.get("/range")
@router.get("/field/range")
async def grid_field_range(
    z: int = Query(..., description="ズームレベル（12 または 14）"),
    item: str | None = Query(None, description="測定項目名（カンマ区切り可）"),
    pollutant: str | None = Query(None, description="item のエイリアス（カンマ区切り可）"),
    items: str | None = Query(None, description="測定項目名（カンマ区切り）"),
    pollutants: str | None = Query(None, description="items のエイリアス（カンマ区切り）"),
    from_datetime: str = Query(..., alias="from", description="開始時刻 ISO 8601（含む）"),
    to_datetime: str = Query(..., alias="to", description="終了時刻 ISO 8601（含む）"),
    bbox: str = Query(
        ...,
        description="min_lon,min_lat,max_lon,max_lat（レスポンス縮小のため必須）",
    ),
    method: str = Query(DEFAULT_METHOD, description="補間メソッド: atps / tps / linear"),
    smoothing: float = Query(0.001, description="atps / tps の平滑化強度。0 で厳密補間、大きいほど滑らか"),
):
    """bbox 内グリッドを複数時刻まとめて返す。"""
    if z not in AVAILABLE_ZOOM_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"z は {AVAILABLE_ZOOM_LEVELS} のいずれかにしてください",
        )
    if method not in AVAILABLE_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"method は {AVAILABLE_METHODS} のいずれかにしてください",
        )
    item_list = _resolve_field_items(item, pollutant, items, pollutants)

    try:
        dt_from = _parse_iso_datetime_or_400(from_datetime, field_name="from")
        dt_to = _parse_iso_datetime_or_400(to_datetime, field_name="to")
    except HTTPException:
        raise
    dt_from = dt_from.replace(minute=0, second=0, microsecond=0)
    dt_to = dt_to.replace(minute=0, second=0, microsecond=0)
    if dt_from > dt_to:
        raise HTTPException(status_code=400, detail="from は to 以下にしてください")

    # 安全のため上限を設ける（1時間刻み）
    total_hours = int((dt_to - dt_from).total_seconds() // 3600) + 1
    if total_hours > 72:
        raise HTTPException(
            status_code=400,
            detail="取得期間が長すぎます。最大72時間までにしてください",
        )

    response = await asyncio.to_thread(
        lambda: _build_grid_range_payload(
            dt_from=dt_from,
            dt_to=dt_to,
            z=z,
            method=method,
            item_list=item_list,
            bbox=bbox,
            compute_body_for_hour=lambda dt_hour_iso: _compute_grid_field_body(
                dt_hour_iso,
                z,
                method,
                item_list,
                bbox,
                smoothing,
            ),
        )
    )
    return Response(
        content=json.dumps(response, ensure_ascii=False),
        media_type="application/json",
    )
