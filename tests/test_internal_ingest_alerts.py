"""internal ingest のアラート判定テスト。"""
from __future__ import annotations

import datetime
import os
import tempfile
from unittest.mock import patch

import pytest

from routers import internal_ingest as ing


@pytest.fixture()
def ingest_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.sqlite3")
        monkeypatch.setenv("AIRPOLLUTIONWATCH_DB_PATH", db_path)
        import config

        monkeypatch.setattr(config, "DB_PATH", config.resolve_db_path())
        conn = config.connect_db()
        ing._ensure_tables(conn)
        conn.close()
        yield db_path


def _post_ingest(conn, req: ing.IngestRequest, attempted_at: str) -> None:
    now_iso = datetime.datetime.now(ing.JST).isoformat()
    ing._insert_measurements(conn, req, now_iso)
    has_usable = ing._target_has_usable_measurements(
        conn, req.prefecture, req.target_datetime
    )
    incoming_usable = any(ing._row_has_usable_pollutant(r) for r in req.rows)
    attempt_error = ing._attempt_error_message(
        req,
        has_usable_measurements=has_usable,
        incoming_rows_usable=incoming_usable,
    )
    ing._record_attempt(conn, req, attempted_at, error_message=attempt_error)
    ing._maybe_notify_ingest_issue(
        conn,
        prefecture=req.prefecture,
        target_iso=req.target_datetime,
        status=req.status,
        error_message=attempt_error,
        attempted_at_iso=attempted_at,
    )


def test_empty_ok_records_warning_message(ingest_db):
    target = "2026-06-02T09:00:00+09:00"
    req = ing.IngestRequest(
        prefecture="nara",
        target_datetime=target,
        status="ok",
        rows=[],
    )
    import config

    with config.connect_db() as conn:
        _post_ingest(conn, req, "2026-06-02T10:00:00+09:00")
        cur = conn.cursor()
        cur.execute(
            "SELECT error_message FROM ingest_attempts WHERE prefecture = 'nara'"
        )
        assert ing._EMPTY_DATA_ERROR in (cur.fetchone()[0] or "")
        cur.execute("SELECT 1 FROM ingest_alert_sent WHERE prefecture = 'nara'")
        assert cur.fetchone() is None


def test_empty_ok_sends_discord_after_threshold(ingest_db):
    target = "2026-06-02T09:00:00+09:00"
    req = ing.IngestRequest(
        prefecture="nara",
        target_datetime=target,
        status="ok",
        rows=[],
    )
    import config

    with patch.object(ing, "_send_discord_alert") as send_alert:
        with config.connect_db() as conn:
            _post_ingest(conn, req, "2026-06-02T06:00:00+09:00")
            send_alert.assert_not_called()
            _post_ingest(conn, req, "2026-06-02T10:00:00+09:00")
            send_alert.assert_called_once()
            assert send_alert.call_args.kwargs["alert_kind"] == "no_data"


def test_usable_data_clears_deficient_state(ingest_db):
    target = "2026-06-02T09:00:00+09:00"
    empty_req = ing.IngestRequest(
        prefecture="nara",
        target_datetime=target,
        status="ok",
        rows=[],
    )
    ok_req = ing.IngestRequest(
        prefecture="nara",
        target_datetime=target,
        status="ok",
        rows=[
            ing.MeasurementRow(
                station_code=29201020,
                observed_datetime=target,
                pm25=15.0,
            )
        ],
    )
    import config

    with config.connect_db() as conn:
        _post_ingest(conn, empty_req, "2026-06-02T10:00:00+09:00")
        _post_ingest(conn, ok_req, "2026-06-02T10:05:00+09:00")
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM ingest_alert_sent WHERE prefecture = 'nara'")
        assert cur.fetchone() is None
        assert ing._target_has_usable_measurements(conn, "nara", target)
