"""
アメダス補間フィールドのキャッシュ DB（amedas_cache.sqlite3）管理モジュール.

キャッシュキー: "{datetime_hour}|{z}|{method}|{item}|{smoothing}"
フィールドデータは float32 numpy 配列を BLOB として保存する。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import TypedDict

import numpy as np


CACHE_DB_PATH = Path(__file__).resolve().parent / "amedas_cache.sqlite3"
CACHE_TTL_HOURS = 72

_DDL = """
CREATE TABLE IF NOT EXISTS amedas_cache (
    cache_key     TEXT PRIMARY KEY,
    z             INTEGER NOT NULL,
    method        TEXT NOT NULL,
    datetime_hour TEXT NOT NULL,
    item          TEXT NOT NULL,
    smoothing     REAL NOT NULL,
    shape_ny      INTEGER NOT NULL,
    shape_nx      INTEGER NOT NULL,
    tile_x_min    INTEGER NOT NULL,
    tile_x_max    INTEGER NOT NULL,
    tile_y_min    INTEGER NOT NULL,
    tile_y_max    INTEGER NOT NULL,
    field         BLOB NOT NULL,
    generated_at  TEXT NOT NULL
)
"""


class AmedasCacheEntry(TypedDict):
    tile_x_min: int
    tile_x_max: int
    tile_y_min: int
    tile_y_max: int
    field: np.ndarray  # shape (ny, nx), dtype float32
    generated_at: str


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(_DDL)
    conn.commit()
    return conn


def get_cache(datetime_hour: str, z: int, method: str, item: str, smoothing: float) -> AmedasCacheEntry | None:
    key = f"{datetime_hour}|{z}|{method}|{item}|{smoothing:.6g}"
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT shape_ny, shape_nx,
                   tile_x_min, tile_x_max, tile_y_min, tile_y_max,
                   field, generated_at
            FROM amedas_cache WHERE cache_key = ?
            """,
            (key,),
        ).fetchone()
    if row is None:
        return None
    ny, nx = row[0], row[1]
    field = np.frombuffer(row[6], dtype=np.float32).reshape(ny, nx).copy()
    return AmedasCacheEntry(
        tile_x_min=row[2],
        tile_x_max=row[3],
        tile_y_min=row[4],
        tile_y_max=row[5],
        field=field,
        generated_at=row[7],
    )


def put_cache(
    datetime_hour: str,
    z: int,
    method: str,
    item: str,
    smoothing: float,
    tile_x_min: int,
    tile_x_max: int,
    tile_y_min: int,
    tile_y_max: int,
    field: np.ndarray,
    generated_at: str,
) -> None:
    key = f"{datetime_hour}|{z}|{method}|{item}|{smoothing:.6g}"
    field_f32 = np.asarray(field, dtype=np.float32)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO amedas_cache
            (cache_key, z, method, datetime_hour, item, smoothing,
             shape_ny, shape_nx,
             tile_x_min, tile_x_max, tile_y_min, tile_y_max,
             field, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                z,
                method,
                datetime_hour,
                item,
                float(smoothing),
                field_f32.shape[0],
                field_f32.shape[1],
                tile_x_min,
                tile_x_max,
                tile_y_min,
                tile_y_max,
                field_f32.tobytes(),
                generated_at,
            ),
        )
        conn.commit()


def evict_old_cache() -> int:
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=CACHE_TTL_HOURS)
    ).isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM amedas_cache WHERE generated_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount

