"""
アメダスグリッドAPI: JMAアメダス観測データを空間補間し、地理院タイル座標系のグリッド値を提供する。

データソースは大気測定局（/v1/grid）とは独立しており、混在しない。

エンドポイント:
  GET /v1/amedas  — bbox 内全タイルの補間グリッド値（気象要素、複数同時取得可）
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Dict, List

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from amedas_cache import evict_old_cache, get_cache, put_cache
from data.amedas import AVAILABLE_ITEMS, fetch_amedas_df
from routers.grid import AVAILABLE_METHODS, JAPAN_BBOX
from grid.interpolators import (
    interpolate_atps,
    interpolate_idw,
    interpolate_linear,
    interpolate_nnatural,
    interpolate_tps,
)
from grid.utils import (
    _webmercator_lonlat_to_tile_xy,
    get_tile_bounds,
    make_lonlat_grid_tiles,
)

logger = logging.getLogger(__name__)

DEFAULT_METHOD = "idw"
# アメダスはズームが小さいほど計算量が減るため、下限は広く許可する（上限は安全のため固定）
AVAILABLE_ZOOM_LEVELS_AMEDAS = list(range(0, 14))  # 0..13

router = APIRouter(prefix="/v1/amedas", tags=["amedas"])


class AmedasFieldResponse(BaseModel):
    datetime: str = Field(..., description="対象時刻（ISO 8601、正時）")
    method: str = Field(..., description="使用した補間メソッド")
    items: List[str] = Field(..., description="返却した気象要素名のリスト")
    z: int = Field(..., description="ズームレベル")
    tile_x_min: int = Field(..., description="出力タイル X の最小値")
    tile_x_max: int = Field(..., description="出力タイル X の最大値")
    tile_y_min: int = Field(..., description="出力タイル Y の最小値")
    tile_y_max: int = Field(..., description="出力タイル Y の最大値")
    fields: Dict[str, List[List[float | None]]] = Field(
        ...,
        description="気象要素ごとの 2 次元補間値配列",
    )


@router.get("", response_model=AmedasFieldResponse, summary="アメダス補間グリッド")
async def amedas_field(
    z: int = Query(..., description=f"ズームレベル（{AVAILABLE_ZOOM_LEVELS_AMEDAS[0]}〜{AVAILABLE_ZOOM_LEVELS_AMEDAS[-1]}）"),
    items: str = Query(
        "temp,hum,wx,wy",
        description=f"気象要素のカンマ区切り。指定可能: {', '.join(AVAILABLE_ITEMS)}",
    ),
    datetime_: str = Query(..., alias="datetime", description="対象時刻（ISO 8601。正時に丸め）"),
    bbox: str | None = Query(
        None,
        description="出力範囲 min_lon,min_lat,max_lon,max_lat。省略時は全国",
    ),
    method: str = Query(
        DEFAULT_METHOD,
        description=f"補間メソッド: {', '.join(AVAILABLE_METHODS)}",
    ),
    smoothing: float = Query(
        0.001,
        description="atps / tps の平滑化強度（0=厳密補間）",
    ),
):
    """JMA アメダス観測データを空間補間し、地理院タイル座標系の 2 次元グリッドで返します。

    大気測定局データ（`/v1/grid`）とは**独立したデータソース**です。混在しません。

    主な気象要素:
    - **temp**: 気温（℃）
    - **hum**: 湿度（%）
    - **wx / wy**: 風ベクトル成分（m/s）

    地図への気象レイヤー重ね合わせに使用します。
    """
    if z not in AVAILABLE_ZOOM_LEVELS_AMEDAS:
        raise HTTPException(
            status_code=400,
            detail=f"z は {AVAILABLE_ZOOM_LEVELS_AMEDAS} のいずれかにしてください",
        )
    if method not in AVAILABLE_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"method は {AVAILABLE_METHODS} のいずれかにしてください",
        )

    # ついでに古いキャッシュを掃除（失敗してもAPIは継続）
    try:
        evict_old_cache()
    except Exception:
        pass

    item_list = [v.strip().lower() for v in items.split(",") if v.strip()]
    if not item_list:
        raise HTTPException(status_code=400, detail="items を 1 つ以上指定してください")
    unknown = [v for v in item_list if v not in AVAILABLE_ITEMS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不明な気象要素: {unknown}。指定可能: {AVAILABLE_ITEMS}",
        )

    try:
        dt = datetime.datetime.fromisoformat(datetime_)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="datetime の形式が不正（ISO 8601 で指定してください）",
        )
    dt_hour = dt.replace(minute=0, second=0, microsecond=0)
    dt_hour_iso = dt_hour.isoformat()

    try:
        amedas_df = await run_in_threadpool(fetch_amedas_df, dt_hour_iso)
    except Exception as e:
        logger.warning("amedas fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=f"アメダスデータ取得失敗: {e}")

    # 全国グリッドを生成
    lon2d, lat2d = make_lonlat_grid_tiles(JAPAN_BBOX, z)
    tx_min, tx_max, ty_min, ty_max = get_tile_bounds(JAPAN_BBOX, z)

    # bbox による出力範囲の絞り込み
    out_tx_min, out_tx_max = tx_min, tx_max
    out_ty_min, out_ty_max = ty_min, ty_max

    if bbox is not None:
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

        bx_min = max(int(math.floor(min(bx_min_f, bx_max_f))), tx_min)
        bx_max = min(int(math.floor(max(bx_min_f, bx_max_f))), tx_max)
        by_min = max(int(math.floor(min(by_min_f, by_max_f))), ty_min)
        by_max = min(int(math.floor(max(by_min_f, by_max_f))), ty_max)

        if bx_min > bx_max or by_min > by_max:
            return AmedasFieldResponse(
                datetime=dt_hour_iso,
                method=method,
                items=item_list,
                z=z,
                tile_x_min=bx_min, tile_x_max=bx_max,
                tile_y_min=by_min, tile_y_max=by_max,
                fields={v: [] for v in item_list},
            )

        out_tx_min, out_tx_max = bx_min, bx_max
        out_ty_min, out_ty_max = by_min, by_max

    # フィールド配列の切り出しインデックス
    col_start = out_tx_min - tx_min
    col_end   = out_tx_max - tx_min + 1
    row_start = ty_max - out_ty_max
    row_end   = ty_max - out_ty_min + 1

    lon_all = amedas_df["lon"].to_numpy(dtype=float)
    lat_all = amedas_df["lat"].to_numpy(dtype=float)

    fields: dict[str, list[list[float | None]]] = {}
    for item in item_list:
        # キャッシュ（全国フィールド）から取得できれば bbox で切り出すだけ
        cached = get_cache(dt_hour_iso, z, method, item, smoothing)
        if cached is not None:
            full_field = cached["field"]
            sub = full_field[row_start:row_end, col_start:col_end]
            fields[item] = [
                [None if np.isnan(v) else float(v) for v in row_arr]
                for row_arr in sub
            ]
            logger.info("amedas grid cache hit: %s z=%d method=%s item=%s", dt_hour_iso, z, method, item)
            continue

        if item not in amedas_df.columns:
            logger.warning("amedas column missing: %s", item)
            fields[item] = []
            continue

        values_all = amedas_df[item].to_numpy(dtype=float)
        valid = ~np.isnan(values_all)
        if valid.sum() < 3:
            logger.warning("amedas: too few valid stations for %s (%d)", item, valid.sum())
            fields[item] = []
            continue

        lon = lon_all[valid]
        lat = lat_all[valid]
        values = values_all[valid]

        if method == "atps":
            full_field = interpolate_atps(lon, lat, values, lon2d, lat2d, smoothing=smoothing)
        elif method == "tps":
            full_field = interpolate_tps(lon, lat, values, lon2d, lat2d, smoothing=smoothing)
        elif method == "linear":
            full_field = interpolate_linear(lon, lat, values, lon2d, lat2d)
        elif method == "idw":
            full_field = interpolate_idw(lon, lat, values, lon2d, lat2d)
        elif method == "nnatural":
            full_field = interpolate_nnatural(lon, lat, values, lon2d, lat2d)
        else:
            raise HTTPException(status_code=400, detail=f"不明なメソッド: {method}")

        # 全国フィールドを保存（次回以降は bbox 切り出しのみ）
        generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            put_cache(
                dt_hour_iso,
                z,
                method,
                item,
                smoothing,
                tx_min,
                tx_max,
                ty_min,
                ty_max,
                full_field,
                generated_at,
            )
            logger.info("amedas grid cached: %s z=%d method=%s item=%s", dt_hour_iso, z, method, item)
        except Exception as e:
            logger.warning("amedas grid cache write failed: %s", e)

        sub = full_field[row_start:row_end, col_start:col_end]
        fields[item] = [
            [None if np.isnan(v) else float(v) for v in row_arr]
            for row_arr in sub
        ]

    return AmedasFieldResponse(
        datetime=dt_hour_iso,
        method=method,
        items=item_list,
        z=z,
        tile_x_min=out_tx_min,
        tile_x_max=out_tx_max,
        tile_y_min=out_ty_min,
        tile_y_max=out_ty_max,
        fields=fields,
    )
