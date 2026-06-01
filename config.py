"""アプリケーション共通設定（環境変数）。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH_ENV = "AIRPOLLUTIONWATCH_DB_PATH"
_DEFAULT_DB_PATH = ROOT / "airpollutionwatch.sqlite3"


def resolve_db_path() -> Path:
    """測定データ SQLite のパス。環境変数未設定時はリポジトリ直下。"""
    raw = os.environ.get(DB_PATH_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_DB_PATH.resolve()


DB_PATH = resolve_db_path()


def connect_db(*, timeout: float | None = None) -> sqlite3.Connection:
    """測定データ DB へ接続し、WAL モードを有効にする。"""
    kwargs: dict[str, object] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    conn = sqlite3.connect(DB_PATH, **kwargs)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
