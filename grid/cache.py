"""
グリッド補間結果のキャッシュ DB（grid_cache.sqlite3）管理モジュール.

キャッシュキー: "{datetime_hour}|{z}|{method}|{pollutant}"
フィールドデータは float32 numpy 配列を BLOB として保存する。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import TypedDict

import numpy as np

CACHE_DB_PATH = Path(__file__).resolve().parent.parent / "grid_cache.sqlite3"
CACHE_TTL_HOURS = 72

_DDL = """
CREATE TABLE IF NOT EXISTS grid_cache (
    cache_key             TEXT PRIMARY KEY,
    z                     INTEGER NOT NULL,
    method                TEXT NOT NULL,
    datetime_hour         TEXT NOT NULL,
    pollutant             TEXT NOT NULL,
    shape_ny              INTEGER NOT NULL,
    shape_nx              INTEGER NOT NULL,
    tile_x_min            INTEGER NOT NULL,
    tile_x_max            INTEGER NOT NULL,
    tile_y_min            INTEGER NOT NULL,
    tile_y_max            INTEGER NOT NULL,
    field                 BLOB NOT NULL,
    apw_snapshot_at       TEXT,
    apw_oldest_station_at TEXT,
    generated_at          TEXT NOT NULL,
    apw_station_count     INTEGER
)
"""


class GridCacheEntry(TypedDict):
    tile_x_min: int
    tile_x_max: int
    tile_y_min: int
    tile_y_max: int
    field: np.ndarray           # shape (ny, nx), dtype float32
    apw_snapshot_at: str | None
    apw_oldest_station_at: str | None
    generated_at: str
    apw_station_count: int | None


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB_PATH)
    # テーブル作成（存在しない場合のみ）
    conn.execute(_DDL)
    # 既存 DB に apw_station_count 列がなければ追加する
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(grid_cache)")
    }
    if "apw_station_count" not in cols:
        conn.execute(
            "ALTER TABLE grid_cache ADD COLUMN apw_station_count INTEGER"
        )
    conn.commit()
    return conn


def get_cache(
    datetime_hour: str, z: int, method: str, pollutant: str, smoothing: float = 0.001
) -> GridCacheEntry | None:
    """キャッシュからグリッドデータを取得。なければ None を返す。"""
    key = f"{datetime_hour}|{z}|{method}|{pollutant}|{smoothing:.6g}"
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT shape_ny, shape_nx,
                   tile_x_min, tile_x_max, tile_y_min, tile_y_max,
                   field, apw_snapshot_at, apw_oldest_station_at, generated_at,
                   apw_station_count
            FROM grid_cache WHERE cache_key = ?
            """,
            (key,),
        ).fetchone()
    if row is None:
        return None
    ny, nx = row[0], row[1]
    field = np.frombuffer(row[6], dtype=np.float32).reshape(ny, nx).copy()
    return GridCacheEntry(
        tile_x_min=row[2],
        tile_x_max=row[3],
        tile_y_min=row[4],
        tile_y_max=row[5],
        field=field,
        apw_snapshot_at=row[7],
        apw_oldest_station_at=row[8],
        generated_at=row[9],
        apw_station_count=row[10],
    )


def put_cache(
    datetime_hour: str,
    z: int,
    method: str,
    pollutant: str,
    tile_x_min: int,
    tile_x_max: int,
    tile_y_min: int,
    tile_y_max: int,
    field: np.ndarray,
    apw_snapshot_at: str | None,
    apw_oldest_station_at: str | None,
    generated_at: str,
    smoothing: float = 0.001,
    apw_station_count: int | None = None,
) -> None:
    """グリッドデータをキャッシュに保存する。同一キーがあれば上書き。"""
    key = f"{datetime_hour}|{z}|{method}|{pollutant}|{smoothing:.6g}"
    field_f32 = np.asarray(field, dtype=np.float32)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO grid_cache
            (cache_key, z, method, datetime_hour, pollutant,
             shape_ny, shape_nx,
             tile_x_min, tile_x_max, tile_y_min, tile_y_max,
             field, apw_snapshot_at, apw_oldest_station_at, generated_at,
             apw_station_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                z,
                method,
                datetime_hour,
                pollutant,
                field_f32.shape[0],
                field_f32.shape[1],
                tile_x_min,
                tile_x_max,
                tile_y_min,
                tile_y_max,
                field_f32.tobytes(),
                apw_snapshot_at,
                apw_oldest_station_at,
                generated_at,
                apw_station_count,
            ),
        )
        conn.commit()


def evict_old_cache() -> int:
    """CACHE_TTL_HOURS より古いエントリを削除し、削除件数を返す。"""
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=CACHE_TTL_HOURS)
    ).isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM grid_cache WHERE generated_at < ?", (cutoff,)
        )
        conn.commit()
        return cur.rowcount


def get_latest_info() -> tuple[str | None, str | None]:
    """最新エントリの (generated_at, apw_snapshot_at) を返す。なければ (None, None)。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT generated_at, apw_snapshot_at FROM grid_cache ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]
