#!/usr/bin/env python3
"""
測定値が一切ない measurements 行を削除する。

使い方:
  python scripts/purge_empty_measurements.py --dry-run
  python scripts/purge_empty_measurements.py --pref kochi
  python scripts/purge_empty_measurements.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import connect_db
from routers.internal_ingest import _ensure_tables, purge_empty_measurements


def main() -> int:
    parser = argparse.ArgumentParser(description="usable な測定値のない measurements 行を削除")
    parser.add_argument(
        "--pref",
        dest="prefecture",
        default=None,
        help="都道府県 ID で絞り込み（省略時は全県）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="削除せず件数のみ表示",
    )
    args = parser.parse_args()

    with connect_db() as conn:
        _ensure_tables(conn)
        n = purge_empty_measurements(
            conn,
            prefecture=args.prefecture,
            dry_run=args.dry_run,
        )

    scope = args.prefecture or "全県"
    if args.dry_run:
        print(f"[dry-run] 削除対象: {n} 件 ({scope})")
    else:
        print(f"削除しました: {n} 件 ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
