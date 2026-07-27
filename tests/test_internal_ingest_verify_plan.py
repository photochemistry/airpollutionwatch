"""internal ingest verify-plan / verify-report エンドポイントのテスト。"""
from __future__ import annotations

import datetime
import math
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


def test_verify_plan_parallel_layer_rotation(ingest_db):
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

    assert plan0.cursor == 0
    assert plan0.layer == 0
    assert len(plan0.jobs) == 3
    assert {j.prefecture for j in plan0.jobs} == {"alpha", "beta", "gamma"}
    for job in plan0.jobs:
        assert job.layer == 0
        assert job.lookback_hours == 48

    assert plan1.cursor == 1
    assert plan1.layer == 1
    assert len(plan1.jobs) == 3
    for job in plan1.jobs:
        assert job.layer == 1
        assert job.lookback_hours == 49


def test_verify_plan_no_duplicate_target_on_hour_boundary(ingest_db):
    """正時跨ぎで base+1h / layer+1 となっても target が重複しない。"""
    base22 = datetime.datetime(2026, 7, 3, 22, 0, tzinfo=ing.JST)
    base23 = datetime.datetime(2026, 7, 3, 23, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        for _ in range(96):
            ing.build_verify_plan(
                conn,
                base_target=base22,
                min_verify_age_hours=48,
                max_verify_lookback_hours=168,
                prefectures=["aichi"],
            )
        plan_a = ing.build_verify_plan(
            conn,
            base_target=base22,
            min_verify_age_hours=48,
            max_verify_lookback_hours=168,
            prefectures=["aichi"],
        )
        plan_b = ing.build_verify_plan(
            conn,
            base_target=base23,
            min_verify_age_hours=48,
            max_verify_lookback_hours=168,
            prefectures=["aichi"],
        )

    target_a = plan_a.jobs[0].target_datetime
    target_b = plan_b.jobs[0].target_datetime
    assert target_a != target_b


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

    jobs_by_pref = {j.prefecture: j for j in plan.jobs}
    assert jobs_by_pref["nara"].has_baseline is True
    assert jobs_by_pref["aichi"].has_baseline is False


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


def test_compare_measurements_null_incoming_row():
    baseline = {
        29201020: {"PM25": 12.0},
    }
    incoming = [
        ing.MeasurementRow(
            station_code=29201020,
            observed_datetime="2026-06-02T10:00:00+09:00",
        )
    ]
    diffs = ing.compare_measurements(baseline, incoming)
    assert len(diffs) == 1
    assert diffs[0].new_value is None


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

    def fake_notify(**kwargs):
        sent.append(kwargs)
        return ["discord"]

    monkeypatch.setattr(ing.notify_channels, "notify_revision", fake_notify)

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


def test_verify_report_apply_changes_upserts_nullable_rows(ingest_db, monkeypatch):
    monkeypatch.setenv("VERIFY_APPLY_CHANGES", "true")
    monkeypatch.setattr(ing.notify_channels, "notify_revision", lambda **kwargs: [])

    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    target_iso = (base - datetime.timedelta(hours=48)).isoformat()
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=target_iso,
            pm25=12.0,
        )
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
                    )
                ],
            ),
            attempted_at_iso=datetime.datetime.now(ing.JST).isoformat(),
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT PM25 FROM measurements WHERE prefecture = ? AND target_datetime = ?",
            ("nara", target_iso),
        )
        pm25 = cur.fetchone()[0]

    assert result.changed_count == 1
    assert result.applied_rows == 1
    assert pm25 is None


def test_verify_report_apply_changes_normalizes_nan_to_null(ingest_db, monkeypatch):
    monkeypatch.setenv("VERIFY_APPLY_CHANGES", "true")
    monkeypatch.setattr(ing.notify_channels, "notify_revision", lambda **kwargs: [])

    target_iso = datetime.datetime(2026, 6, 2, 10, 0, tzinfo=ing.JST).isoformat()
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=target_iso,
            pm25=12.0,
        )
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
                        pm25=math.nan,
                    )
                ],
            ),
            attempted_at_iso=datetime.datetime.now(ing.JST).isoformat(),
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT PM25 FROM measurements WHERE prefecture = ? AND target_datetime = ?",
            ("nara", target_iso),
        )
        pm25 = cur.fetchone()[0]

    assert result.changed_count == 1
    assert result.applied_rows == 1
    assert pm25 is None


def test_verify_report_suppresses_duplicate_notification(ingest_db, monkeypatch):
    monkeypatch.setenv("REVISION_ALERT_COOLDOWN_MINUTES", "10")
    calls: list[str] = []

    def fake_notify(**kwargs):
        calls.append("notify")
        return ["discord"]

    monkeypatch.setattr(ing.notify_channels, "notify_revision", fake_notify)

    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    target_iso = (base - datetime.timedelta(hours=48)).isoformat()
    t0 = datetime.datetime(2026, 7, 3, 22, 59, 0, tzinfo=ing.JST).isoformat()
    t1 = datetime.datetime(2026, 7, 3, 23, 4, 0, tzinfo=ing.JST).isoformat()
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(conn, prefecture="aichi", target_iso=target_iso)
        req = ing.VerifyReportRequest(
            prefecture="aichi",
            target_datetime=target_iso,
            status="ok",
            rows=[
                ing.MeasurementRow(
                    station_code=29201020,
                    observed_datetime=target_iso,
                    pm25=20.0,
                )
            ],
        )
        first = ing.process_verify_report(conn, req, attempted_at_iso=t0)
        second = ing.process_verify_report(conn, req, attempted_at_iso=t1)

    assert first.discord_notified is True
    assert second.revision_notify_suppressed is True
    assert second.discord_notified is False
    assert len(calls) == 1


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
