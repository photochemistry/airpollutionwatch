"""県別収集履歴 API のセル状態（ok / empty / missing）テスト。"""
from __future__ import annotations

import datetime
import os
import tempfile

import pytest

from routers import internal_ingest as ing
from routers import v1


@pytest.fixture()
def history_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.sqlite3")
        monkeypatch.setenv("AIRPOLLUTIONWATCH_DB_PATH", db_path)
        import config

        monkeypatch.setattr(config, "DB_PATH", config.resolve_db_path())
        conn = config.connect_db()
        ing._ensure_tables(conn)
        conn.close()
        yield db_path


def _insert_row(
    conn,
    *,
    prefecture: str,
    target: str,
    station_code: int,
    pm25: float | None = None,
) -> None:
    now = "2026-06-03T00:00:00+09:00"
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO measurements (
            prefecture, station_code, target_datetime, observed_datetime,
            SO2, NO, NO2, NOX, OX, SPM, PM25, CO, NMHC, CH4, THC,
            WD, WS, TEMP, HUM, created_at
        ) VALUES (
            ?, ?, ?, ?,
            NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL, NULL,
            NULL, NULL, NULL, NULL, ?
        )
        """,
        (prefecture, station_code, target, target, pm25, now),
    )
    conn.commit()


def test_load_measurement_slot_status_ok_empty(history_db):
    target_ok = "2026-06-02T10:00:00+09:00"
    target_empty = "2026-06-02T11:00:00+09:00"
    import config

    with config.connect_db() as conn:
        cur = conn.cursor()
        _insert_row(
            conn,
            prefecture="kochi",
            target=target_ok,
            station_code=39201350,
            pm25=12.0,
        )
        _insert_row(
            conn,
            prefecture="kochi",
            target=target_empty,
            station_code=39201350,
            pm25=None,
        )
        end_hour_utc = v1._parse_end_hour_utc("2026-06-02T23:00:00+09:00")
        start_hour_utc = end_hour_utc - datetime.timedelta(hours=23)
        slot_status = v1._load_measurement_slot_status(
            cur,
            prefecture="kochi",
            start_hour_utc=start_hour_utc,
            end_hour_utc=end_hour_utc,
        )

    dt_ok = v1._parse_db_datetime_utc(target_ok)
    dt_empty = v1._parse_db_datetime_utc(target_empty)
    assert dt_ok is not None and dt_empty is not None
    assert slot_status[dt_ok.replace(minute=0, second=0, microsecond=0)] == "ok"
    assert slot_status[dt_empty.replace(minute=0, second=0, microsecond=0)] == "empty"
