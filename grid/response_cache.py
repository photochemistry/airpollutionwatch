"""
HTTP レスポンス body の短期 SQLite キャッシュ（grid/field 用）.

クライアントがタイムアウトしても、バックグラウンド計算完了後にここへ保存すれば
次回リクエストは補間・JSON 組み立てをスキップできる。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

RESPONSE_CACHE_DB_PATH = (
    Path(__file__).resolve().parent.parent / "grid_response_cache.sqlite3"
)
RESPONSE_CACHE_TTL_HOURS = int(
    __import__("os").getenv("GRID_RESPONSE_CACHE_TTL_HOURS", "168")
)

_DDL = """
CREATE TABLE IF NOT EXISTS grid_response_cache (
    cache_key       TEXT PRIMARY KEY,
    body            BLOB NOT NULL,
    data_version    TEXT,
    generated_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL
)
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(RESPONSE_CACHE_DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_DDL)
    conn.commit()
    return conn


def get_response_cache(cache_key: str) -> bytes | None:
    """キャッシュヒット時は body を返す。期限切れのみ None（ヒット時は DB 再照会しない）。"""
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT body, expires_at
            FROM grid_response_cache WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
    if row is None:
        return None
    body, expires_at = row
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        exp = datetime.datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    if now >= exp:
        return None
    return body


def put_response_cache(
    cache_key: str,
    body: bytes,
    *,
    data_version: str | None = None,
    generated_at: str | None = None,
) -> None:
    """レスポンス body を保存する。"""
    if generated_at is None:
        generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    expires_at = (
        datetime.datetime.fromisoformat(generated_at)
        + datetime.timedelta(hours=RESPONSE_CACHE_TTL_HOURS)
    ).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO grid_response_cache
            (cache_key, body, data_version, generated_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cache_key, body, data_version, generated_at, expires_at),
        )
        conn.commit()


def evict_old_response_cache() -> int:
    """期限切れエントリを削除し、削除件数を返す。"""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM grid_response_cache WHERE expires_at < ?", (now,)
        )
        conn.commit()
        return cur.rowcount
