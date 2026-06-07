"""internal ingest collect-plan エンドポイントのテスト。"""
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
) -> None:
    now_iso = datetime.datetime.now(ing.JST).isoformat()
    conn.execute(
        """
        INSERT INTO measurements (
            prefecture, station_code, target_datetime, observed_datetime,
            PM25, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (prefecture, 29201020, target_iso, target_iso, 12.0, now_iso),
    )
    conn.commit()


def test_collect_plan_latest_when_no_data(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        plan = ing.build_collect_plan(
            conn,
            base_target=base,
            prefectures=["nara", "aichi"],
        )

    assert plan.base_target == base.isoformat()
    assert len(plan.jobs) == 2
    for job in plan.jobs:
        assert job.target_datetime == base.isoformat()
        assert job.kind == "latest"
        assert job.lookback_hours == 0


def test_collect_plan_backfill_missing_hour(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    missing = datetime.datetime(2026, 6, 2, 10, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=base.isoformat(),
        )
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=(base - datetime.timedelta(hours=1)).isoformat(),
        )
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=(base - datetime.timedelta(hours=2)).isoformat(),
        )
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=(base - datetime.timedelta(hours=4)).isoformat(),
        )
        plan = ing.build_collect_plan(
            conn,
            base_target=base,
            prefectures=["nara"],
        )

    assert len(plan.jobs) == 1
    job = plan.jobs[0]
    assert job.target_datetime == missing.isoformat()
    assert job.kind == "backfill"
    assert job.lookback_hours == 3


def test_collect_plan_respects_max_lookback(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=base.isoformat(),
        )
        plan = ing.build_collect_plan(
            conn,
            base_target=base,
            max_lookback_hours=0,
            prefectures=["nara"],
        )

    assert plan.jobs[0].target_datetime == base.isoformat()
    assert plan.jobs[0].kind == "latest"


def test_collect_plan_skips_backfill_when_disabled(ingest_db):
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(
            conn,
            prefecture="nara",
            target_iso=base.isoformat(),
        )
        plan = ing.build_collect_plan(
            conn,
            base_target=base,
            past_collection_enabled=False,
            prefectures=["nara"],
        )

    assert plan.jobs == []


def _insert_attempt(
    conn,
    *,
    prefecture: str,
    target_iso: str,
    count: int = 1,
) -> None:
    for _ in range(count):
        conn.execute(
            """
            INSERT INTO ingest_attempts (
                prefecture, target_datetime, status, error_message, attempted_at
            ) VALUES (?, ?, 'failed', 'test', ?)
            """,
            (
                prefecture,
                target_iso,
                datetime.datetime.now(ing.JST).isoformat(),
            ),
        )
    conn.commit()


def test_collect_plan_rotates_among_gaps(ingest_db):
    """連続欠損ブロック内でローテーションする（最新の欠損に張り付かない）。"""
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    gap_old = datetime.datetime(2026, 6, 2, 8, 0, tzinfo=ing.JST)
    gap_mid = datetime.datetime(2026, 6, 2, 9, 0, tzinfo=ing.JST)
    gap_new = datetime.datetime(2026, 6, 2, 10, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        for hours_back in (0, 1, 2, 6):
            _insert_usable_measurement(
                conn,
                prefecture="nara",
                target_iso=(base - datetime.timedelta(hours=hours_back)).isoformat(),
            )

        plan0 = ing.build_collect_plan(
            conn, base_target=base, prefectures=["nara"], backfill_strategy="rotate"
        )
        _insert_attempt(conn, prefecture="nara", target_iso=gap_old.isoformat(), count=1)
        plan1 = ing.build_collect_plan(
            conn, base_target=base, prefectures=["nara"], backfill_strategy="rotate"
        )

    assert plan0.jobs[0].target_datetime == gap_old.isoformat()
    assert plan1.jobs[0].target_datetime == gap_mid.isoformat()


def test_collect_plan_newest_strategy(ingest_db):
    """newest は常に最も新しい欠損を選ぶ（従来動作）。"""
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    gap_new = datetime.datetime(2026, 6, 2, 10, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        _insert_usable_measurement(conn, prefecture="nara", target_iso=base.isoformat())
        _insert_usable_measurement(
            conn, prefecture="nara", target_iso=(base - datetime.timedelta(hours=1)).isoformat()
        )
        _insert_usable_measurement(
            conn, prefecture="nara", target_iso=(base - datetime.timedelta(hours=2)).isoformat()
        )
        _insert_usable_measurement(
            conn, prefecture="nara", target_iso=(base - datetime.timedelta(hours=4)).isoformat()
        )
        _insert_attempt(conn, prefecture="nara", target_iso=gap_new.isoformat(), count=5)
        plan = ing.build_collect_plan(
            conn, base_target=base, prefectures=["nara"], backfill_strategy="newest"
        )

    assert plan.jobs[0].target_datetime == gap_new.isoformat()


def test_collect_plan_unknown_prefecture_uses_db(ingest_db):
    """prefecture_retrievers 未登録でも measurements から計画できる。"""
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        plan = ing.build_collect_plan(
            conn,
            base_target=base,
            prefectures=["not_in_retrievers_xyz"],
        )

    assert len(plan.jobs) == 1
    assert plan.jobs[0].prefecture == "not_in_retrievers_xyz"
    assert plan.jobs[0].kind == "latest"


def test_collect_plan_ignores_empty_rows(ingest_db):
    """測定値が無い行だけでは「取得済み」とみなさない。"""
    base = datetime.datetime(2026, 6, 2, 13, 0, tzinfo=ing.JST)
    import config

    with config.connect_db() as conn:
        now_iso = datetime.datetime.now(ing.JST).isoformat()
        conn.execute(
            """
            INSERT INTO measurements (
                prefecture, station_code, target_datetime, observed_datetime,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("nara", 29201020, base.isoformat(), base.isoformat(), now_iso),
        )
        conn.commit()
        plan = ing.build_collect_plan(
            conn,
            base_target=base,
            prefectures=["nara"],
        )

    assert plan.jobs[0].target_datetime == base.isoformat()
    assert plan.jobs[0].kind == "latest"
