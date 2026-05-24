"""
グリッドAPI: 観測局データを空間補間し、地理院タイル座標系のグリッド値を提供する.

エンドポイント:
  GET /v1/grid/info      — メタ情報・キャッシュ状況
  GET /v1/grid/snapshot  — 指定タイル群・1時刻の補間値
  GET /v1/grid/field     — bbox 内全タイルの補間値（後方互換）
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import sqlite3
import time
from typing import Dict, List

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
    get_tile_bounds,
    make_lonlat_grid_tiles,
)
from data.stations import get_stations_df

# 日本全体をカバーする bounding box（沖縄〜北海道、対馬〜国後）
JAPAN_BBOX = BoundingBox(min_lon=122.0, min_lat=24.0, max_lon=146.0, max_lat=46.0)

AVAILABLE_ZOOM_LEVELS = [10, 11, 12, 13]
AVAILABLE_METHODS = ["atps", "tps", "linear", "idw", "nnatural"]
AVAILABLE_ITEMS = ["no2", "ox", "pm25", "so2", "no", "nox", "spm", "co", "nmhc", "temp", "hum"]
DEFAULT_METHOD = "atps"

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
    item: str
    tile_x_min: int
    tile_x_max: int
    tile_y_min: int
    tile_y_max: int
    values: List[List[float | None]]


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
        # データ更新検知: 現在の DB の測定局数がキャッシュ作成時と異なれば無効化
        cached_count = cached.get("apw_station_count")
        if cached_count is not None:
            current_count = _fetch_station_count(datetime_hour, item_col)
            if current_count != cached_count:
                cached = None
    if cached is not None:
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


def _resolve_field_items(item: str | None, pollutant: str | None) -> list[str]:
    """item または pollutant（カンマ区切り可）から測定項目リストを得る。"""
    if item is not None and pollutant is not None:
        raise HTTPException(
            status_code=400,
            detail="item と pollutant は同時に指定できません",
        )
    raw = item if item is not None else pollutant
    if raw is None or not str(raw).strip():
        raise HTTPException(
            status_code=400,
            detail="item または pollutant を指定してください",
        )
    items = [p.strip().lower() for p in raw.split(",") if p.strip()]
    unknown = [p for p in items if p not in ITEM_PARAM_TO_COL]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不明な測定項目: {unknown}")
    if not items:
        raise HTTPException(
            status_code=400,
            detail="測定項目を 1 つ以上指定してください",
        )
    return items


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
        return resp.model_dump_json().encode("utf-8")

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
        "tile_x_min": out_tx_min,
        "tile_x_max": out_tx_max,
        "tile_y_min": out_ty_min,
        "tile_y_max": out_ty_max,
        "fields": fields,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


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
        dt = datetime.datetime.fromisoformat(datetime_)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="datetime の形式が不正（ISO 8601 で指定してください）",
        )
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
    item: str | None = Query(None, description="1 種類の測定項目名"),
    pollutant: str | None = Query(
        None,
        description="測定項目名（item の別名。カンマ区切りで複数可。例: nmhc,nox,ox）",
    ),
    datetime_: str = Query(..., alias="datetime", description="対象時刻 ISO 8601"),
    bbox: str | None = Query(
        None,
        description="min_lon,min_lat,max_lon,max_lat（省略時は全国）",
    ),
    method: str = Query(DEFAULT_METHOD, description="補間メソッド: atps / tps / linear"),
    smoothing: float = Query(0.001, description="atps / tps の平滑化強度。0 で厳密補間、大きいほど滑らか"),
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

    items = _resolve_field_items(item, pollutant)

    try:
        dt = datetime.datetime.fromisoformat(datetime_)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="datetime の形式が不正（ISO 8601 で指定してください）",
        )
    dt_hour = dt.replace(minute=0, second=0, microsecond=0)
    dt_hour_iso = dt_hour.isoformat()

    cache_key = _field_response_cache_key(
        dt_hour_iso, z, method, items, bbox, smoothing
    )

    def _compute() -> bytes:
        return _compute_grid_field_body(
            dt_hour_iso, z, method, items, bbox, smoothing
        )

    def _data_version() -> str:
        return _field_data_version(dt_hour_iso, items)

    body, status, detail = await asyncio.to_thread(
        lambda: get_or_compute_field_response(
            cache_key,
            _compute,
            data_version_fn=_data_version,
        )
    )
    if body is not None:
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
