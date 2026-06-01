"""
内部向け ingest API。

将来の webhook 連携で crawler から DB へ投入する窓口として利用する。
"""
from __future__ import annotations

import datetime
import os
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from config import connect_db
from grid.cache import evict_cache_by_datetime_hour
from grid.response_cache import evict_response_cache_by_data_version

router = APIRouter(prefix="/internal/ingest", tags=["internal"])

JST = datetime.timezone(datetime.timedelta(hours=9))
INGEST_TOKEN_ENV = "AIRPOLLUTIONWATCH_INGEST_TOKEN"
INGEST_TOKEN_FILE_ENV = "AIRPOLLUTIONWATCH_INGEST_TOKEN_FILE"


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
        token_file = os.environ.get(INGEST_TOKEN_FILE_ENV, "").strip()
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
