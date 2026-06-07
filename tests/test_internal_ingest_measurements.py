"""ingest の measurements 投入（UPSERT・空 payload 拒否・purge）のテスト。"""
from __future__ import annotations

import datetime
import os
import tempfile

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


def _post(conn, req: ing.IngestRequest, now_iso: str = "2026-06-02T12:00:00+09:00") -> int:
    return ing._insert_measurements(conn, req, now_iso)


def test_skip_insert_when_all_pollutants_null(ingest_db):
    target = "2026-06-02T10:00:00+09:00"
    req = ing.IngestRequest(
        prefecture="kochi",
        target_datetime=target,
        status="ok",
        rows=[
            ing.MeasurementRow(
                station_code=39201350,
                observed_datetime=target,
                ox=None,
                pm25=None,
            )
        ],
    )
    import config

    with config.connect_db() as conn:
        n = _post(conn, req)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM measurements WHERE prefecture = 'kochi'"
        )
        count = cur.fetchone()[0]
    assert n == 0
    assert count == 0


def test_upsert_overwrites_prior_null_row(ingest_db):
    target = "2026-06-02T10:00:00+09:00"
    now = "2026-06-02T12:00:00+09:00"
    import config

    with config.connect_db() as conn:
        conn.execute(
            """
            INSERT INTO measurements (
                prefecture, station_code, target_datetime, observed_datetime,
                OX, PM25, created_at
            ) VALUES ('kochi', 39201350, ?, ?, NULL, NULL, ?)
            """,
            (target, target, now),
        )
        conn.commit()
        req = ing.IngestRequest(
            prefecture="kochi",
            target_datetime=target,
            status="ok",
            rows=[
                ing.MeasurementRow(
                    station_code=39201350,
                    observed_datetime=target,
                    ox=22.0,
                    pm25=2.0,
                )
            ],
        )
        n = _post(conn, req, now_iso="2026-06-02T13:00:00+09:00")
        cur = conn.cursor()
        cur.execute(
            "SELECT OX, PM25 FROM measurements WHERE prefecture = 'kochi' AND station_code = 39201350"
        )
        ox, pm25 = cur.fetchone()
    assert n == 1
    assert ox == 22.0
    assert pm25 == 2.0


def test_purge_empty_measurements(ingest_db):
    target = "2026-06-02T11:00:00+09:00"
    now = "2026-06-02T12:00:00+09:00"
    import config

    with config.connect_db() as conn:
        conn.execute(
            """
            INSERT INTO measurements (
                prefecture, station_code, target_datetime, observed_datetime,
                OX, created_at
            ) VALUES ('kochi', 39201350, ?, ?, NULL, ?)
            """,
            (target, target, now),
        )
        conn.execute(
            """
            INSERT INTO measurements (
                prefecture, station_code, target_datetime, observed_datetime,
                OX, created_at
            ) VALUES ('kochi', 39210010, ?, ?, 30.0, ?)
            """,
            (target, target, now),
        )
        conn.commit()
        pending = ing.purge_empty_measurements(conn, prefecture="kochi", dry_run=True)
        assert pending == 1
        ing.purge_empty_measurements(conn, prefecture="kochi", dry_run=False)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM measurements WHERE prefecture = 'kochi'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT OX FROM measurements WHERE station_code = 39210010")
        assert cur.fetchone()[0] == 30.0
