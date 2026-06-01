"""
内部向け ingest API。

将来の webhook 連携で crawler から DB へ投入する窓口として利用する。
"""
from __future__ import annotations

import datetime
import logging
import os
import runpy
from typing import Literal

import httpx
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from config import ROOT, connect_db
from grid.cache import evict_cache_by_datetime_hour
from grid.response_cache import evict_response_cache_by_data_version

router = APIRouter(prefix="/internal/ingest", tags=["internal"])
logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
INGEST_TOKEN_ENV = "AIRPOLLUTIONWATCH_INGEST_TOKEN"
INGEST_TOKEN_FILE_ENV = "AIRPOLLUTIONWATCH_INGEST_TOKEN_FILE"
ALERT_FAILURE_HOURS_ENV = "ALERT_FAILURE_HOURS"
ALERT_DISCORD_WEBHOOK_ENV = "ALERT_DISCORD_WEBHOOK_URL"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def _load_auth_info() -> dict[str, object]:
    """任意の auth_info.py を読み込む（存在しなければ空）。"""
    path = ROOT / "auth_info.py"
    if not path.is_file():
        return {}
    try:
        data = runpy.run_path(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("auth_info.py の読み込みに失敗: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


AUTH_INFO = _load_auth_info()


def _auth_value(name: str) -> str:
    raw = AUTH_INFO.get(name)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()

class MeasurementRow(BaseModel):
    station_code: int = Field(..., description="国環研局番（8桁整数）")
    observed_datetime: str = Field(..., description="観測時刻（ISO8601）")
    so2: float | None = None
    no: float | None = None
    no2: float | None = None
    nox: float | None = None
    ox: float | None = None
    spm: float | None = None
    pm25: float | None = None
    co: float | None = None
    nmhc: float | None = None
    ch4: float | None = None
    thc: float | None = None
    wd: float | None = None
    ws: float | None = None
    temp: float | None = None
    hum: float | None = None


class IngestRequest(BaseModel):
    prefecture: str = Field(..., description="都道府県 ID")
    target_datetime: str = Field(..., description="target_datetime（ISO8601）")
    status: Literal["ok", "failed"] = Field(..., description="収集結果")
    error_message: str | None = Field(None, description="失敗内容、または補足メッセージ")
    attempted_at: str | None = Field(None, description="試行時刻（ISO8601、省略時は現在）")
    rows: list[MeasurementRow] = Field(default_factory=list, description="status=ok のときに挿入する測定値")
    invalidate_cache: bool = Field(True, description="投入後に対象時刻の grid キャッシュを無効化する")


class IngestResponse(BaseModel):
    inserted_rows: int
    attempt_logged: bool
    evicted_grid_cache: int
    evicted_response_cache: int


def _require_ingest_token(authorization: str | None) -> None:
    expected = os.environ.get(INGEST_TOKEN_ENV, "").strip()
    if not expected:
        expected = _auth_value(INGEST_TOKEN_ENV)
    if not expected:
        token_file = os.environ.get(INGEST_TOKEN_FILE_ENV, "").strip()
        if not token_file:
            token_file = _auth_value(INGEST_TOKEN_FILE_ENV)
        if token_file:
            try:
                expected = open(token_file, encoding="utf-8").read().strip()
            except OSError as e:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"{INGEST_TOKEN_FILE_ENV} の読み取りに失敗しました: {e}",
                )
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{INGEST_TOKEN_ENV} または {INGEST_TOKEN_FILE_ENV} が未設定です",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <token> が必要です",
        )
    token = authorization[len("Bearer ") :].strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="トークンが不正です",
        )


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prefecture TEXT NOT NULL,
            station_code INTEGER NOT NULL,
            target_datetime TEXT NOT NULL,
            observed_datetime TEXT NOT NULL,
            SO2 REAL,
            NO REAL,
            NO2 REAL,
            NOX REAL,
            OX REAL,
            SPM REAL,
            PM25 REAL,
            CO REAL,
            NMHC REAL,
            CH4 REAL,
            THC REAL,
            WD REAL,
            WS REAL,
            TEMP REAL,
            HUM REAL,
            created_at TEXT NOT NULL,
            UNIQUE (prefecture, station_code, observed_datetime)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prefecture TEXT NOT NULL,
            target_datetime TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            attempted_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_alert_sent (
            prefecture TEXT NOT NULL,
            target_datetime TEXT NOT NULL,
            alerted_at TEXT NOT NULL,
            PRIMARY KEY (prefecture, target_datetime)
        )
        """
    )
    conn.commit()


def _normalize_pollutant(value: float | None) -> float | None:
    if value is None:
        return None
    v = float(value)
    return None if v >= 999 else v


def _insert_measurements(conn, req: IngestRequest, now_iso: str) -> int:
    if req.status != "ok" or not req.rows:
        return 0
    inserted = 0
    cur = conn.cursor()
    for row in req.rows:
        values = {
            "prefecture": req.prefecture,
            "station_code": row.station_code,
            "target_datetime": req.target_datetime,
            "observed_datetime": row.observed_datetime,
            "SO2": _normalize_pollutant(row.so2),
            "NO": _normalize_pollutant(row.no),
            "NO2": _normalize_pollutant(row.no2),
            "NOX": _normalize_pollutant(row.nox),
            "OX": _normalize_pollutant(row.ox),
            "SPM": _normalize_pollutant(row.spm),
            "PM25": _normalize_pollutant(row.pm25),
            "CO": _normalize_pollutant(row.co),
            "NMHC": _normalize_pollutant(row.nmhc),
            "CH4": _normalize_pollutant(row.ch4),
            "THC": _normalize_pollutant(row.thc),
            "WD": _normalize_pollutant(row.wd),
            "WS": _normalize_pollutant(row.ws),
            "TEMP": _normalize_pollutant(row.temp),
            "HUM": _normalize_pollutant(row.hum),
            "created_at": now_iso,
        }
        cur.execute(
            """
            INSERT OR IGNORE INTO measurements (
                prefecture, station_code, target_datetime, observed_datetime,
                SO2, NO, NO2, NOX, OX,
                SPM, PM25, CO, NMHC, CH4, THC,
                WD, WS, TEMP, HUM, created_at
            ) VALUES (
                :prefecture, :station_code, :target_datetime, :observed_datetime,
                :SO2, :NO, :NO2, :NOX, :OX,
                :SPM, :PM25, :CO, :NMHC, :CH4, :THC,
                :WD, :WS, :TEMP, :HUM, :created_at
            )
            """,
            values,
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted


def _record_attempt(conn, req: IngestRequest, attempted_at_iso: str) -> None:
    conn.execute(
        """
        INSERT INTO ingest_attempts (
            prefecture, target_datetime, status, error_message, attempted_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            req.prefecture,
            req.target_datetime,
            req.status,
            req.error_message,
            attempted_at_iso,
        ),
    )
    conn.commit()


def _alert_failure_hours() -> int:
    raw = os.environ.get(ALERT_FAILURE_HOURS_ENV, "").strip()
    if not raw:
        raw = _auth_value(ALERT_FAILURE_HOURS_ENV)
    if not raw:
        raw = "3"
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(value, 1)


def _discord_webhook_url() -> str:
    env_alert = os.environ.get(ALERT_DISCORD_WEBHOOK_ENV, "").strip()
    env_legacy = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip()
    if env_alert or env_legacy:
        return env_alert or env_legacy
    auth_alert = _auth_value(ALERT_DISCORD_WEBHOOK_ENV)
    auth_legacy = _auth_value(DISCORD_WEBHOOK_ENV)
    return auth_alert or auth_legacy


def _send_discord_alert(
    *,
    prefecture: str,
    target_iso: str,
    since_iso: str,
    error: str,
    failure_hours: int,
) -> None:
    webhook = _discord_webhook_url()
    if not webhook:
        return

    text = (
        "【ALERT】airpollutionwatch 収集失敗が継続しています\n"
        f"- 都道府県: {prefecture}\n"
        f"- 対象時刻: {target_iso}\n"
        f"- 初回失敗: {since_iso}\n"
        f"- 継続時間: {failure_hours}時間以上\n"
        f"- 代表エラー: {error}"
    )
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(webhook, json={"content": text})
        resp.raise_for_status()


def _maybe_notify_failure(
    conn,
    *,
    prefecture: str,
    target_iso: str,
    status: str,
    error_message: str | None,
    attempted_at_iso: str,
) -> None:
    cur = conn.cursor()

    if status == "ok":
        cur.execute(
            """
            DELETE FROM ingest_alert_sent
            WHERE prefecture = ? AND target_datetime = ?
            """,
            (prefecture, target_iso),
        )
        conn.commit()
        return

    if status != "failed":
        return

    cur.execute(
        """
        SELECT 1 FROM ingest_alert_sent
        WHERE prefecture = ? AND target_datetime = ?
        """,
        (prefecture, target_iso),
    )
    if cur.fetchone() is not None:
        return

    cur.execute(
        """
        SELECT MAX(attempted_at) FROM ingest_attempts
        WHERE prefecture = ? AND target_datetime = ? AND status = 'ok'
        """,
        (prefecture, target_iso),
    )
    last_ok_row = cur.fetchone()
    last_ok_at = last_ok_row[0] if last_ok_row else None

    if last_ok_at:
        cur.execute(
            """
            SELECT attempted_at, error_message
            FROM ingest_attempts
            WHERE prefecture = ? AND target_datetime = ? AND status = 'failed'
              AND attempted_at > ?
            ORDER BY attempted_at ASC
            LIMIT 1
            """,
            (prefecture, target_iso, last_ok_at),
        )
    else:
        cur.execute(
            """
            SELECT attempted_at, error_message
            FROM ingest_attempts
            WHERE prefecture = ? AND target_datetime = ? AND status = 'failed'
            ORDER BY attempted_at ASC
            LIMIT 1
            """,
            (prefecture, target_iso),
        )
    row = cur.fetchone()
    if row is None:
        return

    first_failed_at_str, first_error = row
    failure_hours = _alert_failure_hours()
    now_dt = datetime.datetime.fromisoformat(attempted_at_iso)
    first_failed_dt = datetime.datetime.fromisoformat(first_failed_at_str)
    if now_dt - first_failed_dt < datetime.timedelta(hours=failure_hours):
        return

    try:
        _send_discord_alert(
            prefecture=prefecture,
            target_iso=target_iso,
            since_iso=first_failed_at_str,
            error=first_error or error_message or "",
            failure_hours=failure_hours,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "failed to send discord alert: pref=%s target=%s err=%s",
            prefecture,
            target_iso,
            e,
        )
        return

    cur.execute(
        """
        INSERT INTO ingest_alert_sent (prefecture, target_datetime, alerted_at)
        VALUES (?, ?, ?)
        """,
        (prefecture, target_iso, attempted_at_iso),
    )
    conn.commit()


def _evict_caches(target_datetime: str) -> tuple[int, int]:
    evicted_grid = evict_cache_by_datetime_hour(target_datetime)
    evicted_response = evict_response_cache_by_data_version(target_datetime)
    return evicted_grid, evicted_response


@router.post(
    "/measurements",
    response_model=IngestResponse,
    summary="内部: 収集結果を DB へ投入",
    include_in_schema=False,
)
async def ingest_measurements(
    req: IngestRequest,
    authorization: str | None = Header(None),
):
    """crawler から収集結果を投入する内部 API（将来の webhook 用）。"""
    _require_ingest_token(authorization)

    now_iso = datetime.datetime.now(JST).isoformat()
    attempted_at_iso = req.attempted_at or now_iso

    with connect_db(timeout=30.0) as conn:
        _ensure_tables(conn)
        inserted_rows = _insert_measurements(conn, req, now_iso)
        _record_attempt(conn, req, attempted_at_iso)
        _maybe_notify_failure(
            conn,
            prefecture=req.prefecture,
            target_iso=req.target_datetime,
            status=req.status,
            error_message=req.error_message,
            attempted_at_iso=attempted_at_iso,
        )

    evicted_grid = 0
    evicted_response = 0
    if req.invalidate_cache:
        evicted_grid, evicted_response = _evict_caches(req.target_datetime)

    return IngestResponse(
        inserted_rows=inserted_rows,
        attempt_logged=True,
        evicted_grid_cache=evicted_grid,
        evicted_response_cache=evicted_response,
    )
