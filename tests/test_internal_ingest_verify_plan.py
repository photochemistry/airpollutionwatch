"""internal ingest verify-plan / verify-report エンドポイントのテスト。"""
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


def _insert_usable_measurement(
    conn,
    *,
    prefecture: str,
    target_iso: str,
    pm25: float = 12.0,
    station_code: int = 29201020,
) -> None:
    now_iso = datetime.datetime.now(ing.JST).isoformat()
    conn.execute(
        """
        INSERT INTO measurements (
            prefecture, station_code, target_datetime, observed_datetime,
            PM25, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (prefecture, station_code, target_iso, target_iso, pm25, now_iso),
    )
    conn.commit()


def test_verify_plan_bfs_rotation(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    prefs = ["alpha", "beta", "gamma"]
    import config

    with config.connect_db() as conn:
        plan0 = ing.build_verify_plan(
            conn,
            base_target=base,
            min_verify_age_hours=48,
            max_verify_lookback_hours=2,
            prefectures=prefs,
        )
        plan1 = ing.build_verify_plan(
            conn,
            base_target=base,
            min_verify_age_hours=48,
            max_verify_lookback_hours=2,
            prefectures=prefs,
        )
        plan2 = ing.build_verify_plan(
            conn,
            base_target=base,
            min_verify_age_hours=48,
            max_verify_lookback_hours=2,
            prefectures=prefs,
        )
        plan3 = ing.build_verify_plan(
            conn,
            base_target=base,
            min_verify_age_hours=48,
            max_verify_lookback_hours=2,
            prefectures=prefs,
        )

    assert plan0.cursor == 0
    assert plan0.job.prefecture == "alpha"
    assert plan0.job.layer == 0
    assert plan0.job.lookback_hours == 48

    assert plan1.cursor == 1
    assert plan1.job.prefecture == "beta"
    assert plan1.job.layer == 0

    assert plan2.cursor == 2
    assert plan2.job.prefecture == "gamma"
    assert plan2.job.layer == 0

    assert plan3.cursor == 3
    assert plan3.job.prefecture == "alpha"
    assert plan3.job.layer == 1
    assert plan3.job.lookback_hours == 49


def test_verify_plan_has_baseline_flag(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    target = base - datetime.timedelta(hours=48)
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=target.isoformat(),
        )
        plan = ing.build_verify_plan(
            conn,
            base_target=base,
            prefectures=["nara", "aichi"],
        )

    assert plan.job.prefecture == "nara"
    assert plan.job.has_baseline is True


def test_compare_measurements_detects_change():
    baseline = {
        29201020: {"PM25": 12.0, "OX": 40.0},
    }
    incoming = [
        ing.MeasurementRow(
            station_code=29201020,
            observed_datetime="2026-06-02T10:00:00+09:00",
            pm25=14.0,
            ox=40.0,
        )
    ]
    diffs = ing.compare_measurements(baseline, incoming)
    assert len(diffs) == 1
    assert diffs[0].field == "PM25"
    assert diffs[0].old_value == 12.0
    assert diffs[0].new_value == 14.0


def test_verify_report_unchanged(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    target_iso = (base - datetime.timedelta(hours=48)).isoformat()
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(conn, prefecture="nara", target_iso=target_iso)
        result = ing.process_verify_report(
            conn,
            ing.VerifyReportRequest(
                prefecture="nara",
                target_datetime=target_iso,
                status="ok",
                rows=[
                    ing.MeasurementRow(
                        station_code=29201020,
                        observed_datetime=target_iso,
                        pm25=12.0,
                    )
                ],
            ),
            attempted_at_iso=datetime.datetime.now(ing.JST).isoformat(),
        )

    assert result.changed_count == 0
    assert result.discord_notified is False


def test_verify_report_changed_sends_discord(ingest_db, monkeypatch):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    target_iso = (base - datetime.timedelta(hours=48)).isoformat()
    sent: list[dict] = []

    def fake_discord(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(ing, "_send_discord_revision_alert", fake_discord)

    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(conn, prefecture="nara", target_iso=target_iso)
        result = ing.process_verify_report(
            conn,
            ing.VerifyReportRequest(
                prefecture="nara",
                target_datetime=target_iso,
                status="ok",
                rows=[
                    ing.MeasurementRow(
                        station_code=29201020,
                        observed_datetime=target_iso,
                        pm25=20.0,
                    )
                ],
            ),
            attempted_at_iso=datetime.datetime.now(ing.JST).isoformat(),
        )

    assert result.changed_count == 1
    assert result.discord_notified is True
    assert len(sent) == 1
    assert sent[0]["changed_count"] == 1


def test_verify_report_skipped_without_baseline(ingest_db):
    target_iso = datetime.datetime(2026, 6, 2, 10, 0, tzinfo=ing.JST).isoformat()
    import config

    with config.connect_db() as conn:
        result = ing.process_verify_report(
            conn,
            ing.VerifyReportRequest(
                prefecture="nara",
                target_datetime=target_iso,
                status="skipped",
                error_message="no baseline",
            ),
            attempted_at_iso=datetime.datetime.now(ing.JST).isoformat(),
        )

    assert result.attempt_logged is True
    assert result.changed_count == 0
