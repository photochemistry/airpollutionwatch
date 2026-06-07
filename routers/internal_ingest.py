"""
内部向け ingest API。

将来の webhook 連携で crawler から DB へ投入する窓口として利用する。
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Literal

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from airpollutionwatch import prefecture_retrievers

from auth_tokens import (
    INGEST_TOKEN_ENV,
    INGEST_TOKEN_FILE_ENV,
    resolve_expected_token,
    verify_ingest_token,
)
from config import ROOT, connect_db

router = APIRouter(prefix="/internal/ingest", tags=["internal"])
logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
ALERT_FAILURE_HOURS_ENV = "ALERT_FAILURE_HOURS"
ALERT_DISCORD_WEBHOOK_ENV = "ALERT_DISCORD_WEBHOOK_URL"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def _auth_value(name: str) -> str:
    from auth_tokens import AUTH_INFO

    raw = AUTH_INFO.get(name)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()

_POLLUTANT_ATTRS = (
    "so2",
    "no",
    "no2",
    "nox",
    "ox",
    "spm",
    "pm25",
    "co",
    "nmhc",
    "ch4",
    "thc",
)

_POLLUTANT_DB_COLS = tuple(a.upper() for a in _POLLUTANT_ATTRS)

_EMPTY_DATA_ERROR = "no measurement data ingested (empty)"


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


CollectPlanKind = Literal["latest", "backfill"]
BackfillStrategy = Literal["rotate", "newest"]


class CollectPlanJob(BaseModel):
    prefecture: str = Field(..., description="都道府県 ID")
    target_datetime: str = Field(..., description="収集対象の正時（ISO8601）")
    kind: CollectPlanKind = Field(
        ...,
        description="latest=最新時刻の取得、backfill=欠損時刻の穴埋め",
    )
    lookback_hours: int = Field(
        ...,
        description="base_target から何時間前の時刻か（latest は 0）",
    )


class CollectPlanResponse(BaseModel):
    base_target: str = Field(..., description="基準とした現在の正時（ISO8601）")
    max_lookback_hours: int = Field(..., description="穴埋めでさかのぼる上限（時間）")
    past_collection_enabled: bool = Field(
        ...,
        description="False のとき欠損時刻の県は jobs に含めない",
    )
    backfill_strategy: BackfillStrategy = Field(
        ...,
        description="欠損時刻の選び方: rotate=キュー巡回, newest=最も新しい欠損のみ",
    )
    jobs: list[CollectPlanJob] = Field(..., description="県ごとの収集計画")


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


def _row_has_usable_pollutant(row: MeasurementRow) -> bool:
    for attr in _POLLUTANT_ATTRS:
        if _normalize_pollutant(getattr(row, attr)) is not None:
            return True
    return False


def _target_has_usable_measurements(conn, prefecture: str, target_iso: str) -> bool:
    cols_or = " OR ".join(f"{col} IS NOT NULL" for col in _POLLUTANT_DB_COLS)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT 1 FROM measurements
        WHERE prefecture = ? AND target_datetime = ?
          AND ({cols_or})
        LIMIT 1
        """,
        (prefecture, target_iso),
    )
    return cur.fetchone() is not None


def _payload_has_usable_rows(rows: list[MeasurementRow]) -> bool:
    """ingest payload のいずれかの行に usable な測定値があるか。"""
    return any(_row_has_usable_pollutant(r) for r in rows)


def _all_pollutants_null_sql(prefix: str = "") -> str:
    """全測定項目が NULL（usable なし）である SQL 条件。"""
    col = f"{prefix}." if prefix else ""
    return " AND ".join(f"{col}{c} IS NULL" for c in _POLLUTANT_DB_COLS)


def purge_empty_measurements(
    conn,
    *,
    prefecture: str | None = None,
    dry_run: bool = False,
) -> int:
    """測定値が一切ない measurements 行を削除する。戻り値は削除（予定）件数。"""
    where = _all_pollutants_null_sql()
    params: list[str] = []
    if prefecture is not None:
        where = f"{where} AND prefecture = ?"
        params.append(prefecture)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM measurements WHERE {where}", params)
    count = int(cur.fetchone()[0])
    if not dry_run and count > 0:
        cur.execute(f"DELETE FROM measurements WHERE {where}", params)
        conn.commit()
    return count


def _insert_measurements(conn, req: IngestRequest, now_iso: str) -> int:
    if req.status != "ok" or not req.rows:
        return 0
    if not _payload_has_usable_rows(req.rows):
        return 0
    written = 0
    cur = conn.cursor()
    data_cols = (
        "target_datetime",
        "observed_datetime",
        *_POLLUTANT_DB_COLS,
        "WD",
        "WS",
        "TEMP",
        "HUM",
        "created_at",
    )
    update_assignments = ", ".join(f"{c}=excluded.{c}" for c in data_cols)
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
            f"""
            INSERT INTO measurements (
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
            ON CONFLICT(prefecture, station_code, observed_datetime) DO UPDATE SET
                {update_assignments}
            """,
            values,
        )
        if cur.rowcount > 0:
            written += 1
    conn.commit()
    return written


def _record_attempt(
    conn,
    req: IngestRequest,
    attempted_at_iso: str,
    *,
    error_message: str | None = None,
) -> None:
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
            error_message if error_message is not None else req.error_message,
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
    alert_kind: Literal["failure", "no_data"],
) -> None:
    webhook = _discord_webhook_url()
    if not webhook:
        return

    if alert_kind == "no_data":
        headline = "【ALERT】airpollutionwatch 測定データが取得できていません（空欄）"
        kind_line = "- 状態: 収集処理は動いているが、当該時刻の測定値が DB にありません"
    else:
        headline = "【ALERT】airpollutionwatch 収集失敗が継続しています"
        kind_line = "- 状態: 収集失敗（status=failed）"

    text = (
        f"{headline}\n"
        f"- 都道府県: {prefecture}\n"
        f"- 対象時刻: {target_iso}\n"
        f"{kind_line}\n"
        f"- 初回検知: {since_iso}\n"
        f"- 継続時間: {failure_hours}時間以上\n"
        f"- 代表メッセージ: {error}"
    )
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(webhook, json={"content": text})
        resp.raise_for_status()


def _attempt_error_message(
    req: IngestRequest,
    *,
    has_usable_measurements: bool,
    incoming_rows_usable: bool,
) -> str | None:
    if req.status == "failed":
        return req.error_message
    if has_usable_measurements:
        return req.error_message
    if req.error_message:
        return req.error_message
    if not req.rows or not incoming_rows_usable:
        return _EMPTY_DATA_ERROR
    return req.error_message


def _first_deficient_attempt(
    cur,
    *,
    prefecture: str,
    target_iso: str,
) -> tuple[str, str, Literal["failure", "no_data"]] | None:
    """当該 target に測定値が無い状態の先頭試行を返す。"""
    cur.execute(
        """
        SELECT attempted_at, status, error_message
        FROM ingest_attempts
        WHERE prefecture = ? AND target_datetime = ?
        ORDER BY attempted_at ASC
        LIMIT 1
        """,
        (prefecture, target_iso),
    )
    row = cur.fetchone()
    if row is None:
        return None
    first_at, first_status, first_error = row
    if first_status == "failed":
        return first_at, first_error or "failed to collect", "failure"
    return first_at, first_error or _EMPTY_DATA_ERROR, "no_data"


def _maybe_notify_ingest_issue(
    conn,
    *,
    prefecture: str,
    target_iso: str,
    status: str,
    error_message: str | None,
    attempted_at_iso: str,
) -> None:
    cur = conn.cursor()

    if _target_has_usable_measurements(conn, prefecture, target_iso):
        cur.execute(
            """
            DELETE FROM ingest_alert_sent
            WHERE prefecture = ? AND target_datetime = ?
            """,
            (prefecture, target_iso),
        )
        conn.commit()
        return

    if status not in ("ok", "failed"):
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

    deficient = _first_deficient_attempt(
        cur, prefecture=prefecture, target_iso=target_iso
    )
    if deficient is None:
        return

    first_at_str, first_error, alert_kind = deficient
    failure_hours = _alert_failure_hours()
    now_dt = datetime.datetime.fromisoformat(attempted_at_iso)
    first_dt = datetime.datetime.fromisoformat(first_at_str)
    if now_dt - first_dt < datetime.timedelta(hours=failure_hours):
        return

    try:
        _send_discord_alert(
            prefecture=prefecture,
            target_iso=target_iso,
            since_iso=first_at_str,
            error=first_error or error_message or "",
            failure_hours=failure_hours,
            alert_kind=alert_kind,
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


GRID_API_URL_ENV = "AIRPOLLUTIONWATCH_GRID_API_URL"
_DEFAULT_GRID_API_URL = "http://127.0.0.1:8090"


def _evict_caches(target_datetime: str) -> tuple[int, int]:
    """airpollutionwatch-grid の /internal/invalidate-cache を呼ぶ。"""
    base = os.environ.get(GRID_API_URL_ENV, _DEFAULT_GRID_API_URL).strip().rstrip("/")
    if not base:
        return 0, 0
    token = resolve_expected_token(INGEST_TOKEN_ENV, INGEST_TOKEN_FILE_ENV)
    if not token:
        logger.warning("grid キャッシュ無効化をスキップ: ingest トークン未設定")
        return 0, 0
    url = f"{base}/internal/invalidate-cache"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                url,
                json={"target_datetime": target_datetime},
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        body = resp.json()
        return int(body.get("evicted_grid_cache", 0)), int(body.get("evicted_response_cache", 0))
    except httpx.HTTPError as e:
        logger.warning("grid キャッシュ無効化に失敗 (%s): %s", url, e)
        return 0, 0


def _compute_target_hour(now: datetime.datetime | None = None) -> datetime.datetime:
    """現在の正時（JST）を返す。"""
    if now is None:
        now = datetime.datetime.now(JST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    else:
        now = now.astimezone(JST)
    return now.replace(minute=0, second=0, microsecond=0)


def _parse_base_target_iso(base_target: str | None) -> datetime.datetime:
    if base_target is None:
        return _compute_target_hour()
    try:
        dt = datetime.datetime.fromisoformat(base_target.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="base_target の形式が不正です（ISO 8601 で指定してください）",
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    else:
        dt = dt.astimezone(JST)
    return dt.replace(minute=0, second=0, microsecond=0)


def _collect_prefecture_ids() -> list[str]:
    """crawler と同様、prefecture_retrievers に登録された県 ID を返す。"""
    return sorted(prefecture_retrievers.keys())


def _list_missing_hours(
    conn,
    *,
    prefecture: str,
    base_target: datetime.datetime,
    max_lookback_hours: int,
) -> list[datetime.datetime]:
    """lookback 窓内で usable な measurements が無い正時を列挙（新しい順）。"""
    missing: list[datetime.datetime] = []
    target = base_target
    while True:
        lookback_hours = int((base_target - target).total_seconds() // 3600)
        if lookback_hours > max_lookback_hours:
            break
        if not _target_has_usable_measurements(conn, prefecture, target.isoformat()):
            missing.append(target)
        target -= datetime.timedelta(hours=1)
    return missing


def _list_actionable_missing_hours(
    conn,
    *,
    prefecture: str,
    base_target: datetime.datetime,
    max_lookback_hours: int,
) -> list[datetime.datetime]:
    """最新側から見た連続欠損ブロックを古い順で返す（途中にデータがあればそこで打ち切り）。"""
    segment: list[datetime.datetime] = []
    in_segment = False
    target = base_target
    while True:
        lookback_hours = int((base_target - target).total_seconds() // 3600)
        if lookback_hours > max_lookback_hours:
            break
        if not _target_has_usable_measurements(conn, prefecture, target.isoformat()):
            segment.append(target)
            in_segment = True
        elif in_segment:
            break
        target -= datetime.timedelta(hours=1)
    segment.reverse()
    return segment


def _newest_missing_hour(
    conn,
    *,
    prefecture: str,
    base_target: datetime.datetime,
    max_lookback_hours: int,
) -> datetime.datetime:
    """欠損のうち最も新しい正時（従来動作）。"""
    missing = _list_missing_hours(
        conn,
        prefecture=prefecture,
        base_target=base_target,
        max_lookback_hours=max_lookback_hours,
    )
    if not missing:
        return base_target
    return missing[0]


def _rotation_index(conn, *, prefecture: str, base_target: datetime.datetime, max_lookback_hours: int) -> int:
    """lookback 窓内の ingest 試行回数でローテーション用インデックスを返す。"""
    window_start = base_target - datetime.timedelta(hours=max_lookback_hours)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM ingest_attempts
        WHERE prefecture = ? AND attempted_at >= ?
        """,
        (prefecture, window_start.isoformat()),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _pick_missing_hour_rotating(
    conn,
    *,
    prefecture: str,
    base_target: datetime.datetime,
    max_lookback_hours: int,
) -> datetime.datetime:
    """連続欠損ブロック内をキュー巡回（最新正時が欠けていればそこを優先）。"""
    if not _target_has_usable_measurements(conn, prefecture, base_target.isoformat()):
        return base_target
    queue = _list_actionable_missing_hours(
        conn,
        prefecture=prefecture,
        base_target=base_target,
        max_lookback_hours=max_lookback_hours,
    )
    if not queue:
        return base_target
    if len(queue) == 1:
        return queue[0]
    idx = _rotation_index(
        conn,
        prefecture=prefecture,
        base_target=base_target,
        max_lookback_hours=max_lookback_hours,
    )
    return queue[idx % len(queue)]


def _pick_collect_target(
    conn,
    *,
    prefecture: str,
    base_target: datetime.datetime,
    max_lookback_hours: int,
    backfill_strategy: BackfillStrategy = "rotate",
) -> datetime.datetime:
    if backfill_strategy == "newest":
        return _newest_missing_hour(
            conn,
            prefecture=prefecture,
            base_target=base_target,
            max_lookback_hours=max_lookback_hours,
        )
    return _pick_missing_hour_rotating(
        conn,
        prefecture=prefecture,
        base_target=base_target,
        max_lookback_hours=max_lookback_hours,
    )


def build_collect_plan(
    conn,
    *,
    base_target: datetime.datetime,
    max_lookback_hours: int = 72,
    past_collection_enabled: bool = True,
    prefectures: list[str] | None = None,
    backfill_strategy: BackfillStrategy = "rotate",
) -> CollectPlanResponse:
    """DB 上の欠損から県ごとの収集 target を決める。"""
    if max_lookback_hours < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_lookback_hours は 0 以上で指定してください",
        )

    pref_ids = prefectures if prefectures is not None else _collect_prefecture_ids()
    jobs: list[CollectPlanJob] = []
    for prefecture in pref_ids:
        if prefecture not in prefecture_retrievers:
            logger.warning(
                "collect-plan: %s は prefecture_retrievers に未登録。"
                "measurements の欠損状況のみから計画します",
                prefecture,
            )
        target = _pick_collect_target(
            conn,
            prefecture=prefecture,
            base_target=base_target,
            max_lookback_hours=max_lookback_hours,
            backfill_strategy=backfill_strategy,
        )
        if target < base_target and not past_collection_enabled:
            continue
        lookback_hours = int((base_target - target).total_seconds() // 3600)
        kind: CollectPlanKind = "latest" if lookback_hours == 0 else "backfill"
        jobs.append(
            CollectPlanJob(
                prefecture=prefecture,
                target_datetime=target.isoformat(),
                kind=kind,
                lookback_hours=lookback_hours,
            )
        )

    return CollectPlanResponse(
        base_target=base_target.isoformat(),
        max_lookback_hours=max_lookback_hours,
        past_collection_enabled=past_collection_enabled,
        backfill_strategy=backfill_strategy,
        jobs=jobs,
    )


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
    verify_ingest_token(authorization)

    now_iso = datetime.datetime.now(JST).isoformat()
    attempted_at_iso = req.attempted_at or now_iso

    with connect_db(timeout=30.0) as conn:
        _ensure_tables(conn)
        inserted_rows = _insert_measurements(conn, req, now_iso)
        has_usable = _target_has_usable_measurements(
            conn, req.prefecture, req.target_datetime
        )
        incoming_rows_usable = any(_row_has_usable_pollutant(r) for r in req.rows)
        attempt_error = _attempt_error_message(
            req,
            has_usable_measurements=has_usable,
            incoming_rows_usable=incoming_rows_usable,
        )
        _record_attempt(conn, req, attempted_at_iso, error_message=attempt_error)
        _maybe_notify_ingest_issue(
            conn,
            prefecture=req.prefecture,
            target_iso=req.target_datetime,
            status=req.status,
            error_message=attempt_error,
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


@router.get(
    "/collect-plan",
    response_model=CollectPlanResponse,
    summary="内部: crawler 向け収集計画",
    include_in_schema=False,
)
async def get_collect_plan(
    authorization: str | None = Header(None),
    base_target: str | None = Query(
        None,
        description="基準とする正時（ISO8601）。省略時は現在の正時（JST）",
    ),
    max_lookback_hours: int = Query(
        72,
        ge=0,
        description="穴埋めでさかのぼる上限（時間）。超える欠損は base_target を返す",
    ),
    past_collection_enabled: bool = Query(
        True,
        description="False のとき、最新取得済みで過去欠損のみの県は jobs から除外",
    ),
    prefectures: str | None = Query(
        None,
        description="対象県 ID をカンマ区切りで指定（省略時は全収集対象県）",
    ),
    backfill_strategy: BackfillStrategy = Query(
        "rotate",
        description="欠損時刻の選び方: rotate=複数欠損をキュー巡回, newest=最も新しい欠損のみ",
    ),
):
    """measurements の欠損状況から、県ごとに次に取得すべき正時を返す。"""
    verify_ingest_token(authorization)

    base_dt = _parse_base_target_iso(base_target)
    pref_list: list[str] | None = None
    if prefectures is not None:
        pref_list = [p.strip() for p in prefectures.split(",") if p.strip()]
        if not pref_list:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="prefectures が空です",
            )

    with connect_db(timeout=30.0) as conn:
        _ensure_tables(conn)
        return build_collect_plan(
            conn,
            base_target=base_dt,
            max_lookback_hours=max_lookback_hours,
            past_collection_enabled=past_collection_enabled,
            prefectures=pref_list,
            backfill_strategy=backfill_strategy,
        )
