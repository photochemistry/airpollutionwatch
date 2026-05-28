#!/usr/bin/env python3
"""全国計算と部分計算の同値性を比較するベンチマーク。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AIRPOLLUTIONWATCH_LIB = Path("/AIR/airpollutionwatch")
if AIRPOLLUTIONWATCH_LIB.exists() and str(AIRPOLLUTIONWATCH_LIB) not in sys.path:
    sys.path.insert(0, str(AIRPOLLUTIONWATCH_LIB))

from routers.grid import _compute_grid_field_body, _compute_grid_field_body_local_bbox


@dataclass
class DiffMetrics:
    max_abs: float
    p99_abs: float
    rmse: float
    mean_abs: float
    exceed_0_1: int
    exceed_1_0: int
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_abs": self.max_abs,
            "p99_abs": self.p99_abs,
            "rmse": self.rmse,
            "mean_abs": self.mean_abs,
            "exceed_0_1": self.exceed_0_1,
            "exceed_1_0": self.exceed_1_0,
            "count": self.count,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="全国計算と部分計算の同値性を比較する",
    )
    parser.add_argument("--datetime", required=True, help="基準時刻 ISO8601")
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="基準時刻から遡る時間数（1以上）。1なら単一時刻のみ",
    )
    parser.add_argument("--z", type=int, default=12, help="ズームレベル")
    parser.add_argument("--bbox", required=True, help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--items", default="ox", help="カンマ区切り項目（例: ox,nox,nmhc）")
    parser.add_argument("--method", default="idw", help="補間メソッド")
    parser.add_argument("--smoothing", type=float, default=0.001, help="atps/tps smoothing")
    parser.add_argument(
        "--margins",
        default="0,2,4,8,16,24",
        help="station_margin_tiles 候補（カンマ区切り）",
    )
    parser.add_argument(
        "--min-station-count",
        type=int,
        default=16,
        help="部分計算時の最低測定局数",
    )
    parser.add_argument(
        "--output",
        default="",
        help="結果JSONの保存先（省略時は保存しない）",
    )
    return parser.parse_args()


def _to_hour(dt_iso: str) -> dt.datetime:
    base = dt.datetime.fromisoformat(dt_iso)
    return base.replace(minute=0, second=0, microsecond=0)


def _float_array(values_2d: list[list[float | None]]) -> np.ndarray:
    arr = np.array(values_2d, dtype=float)
    return arr


def _calc_metrics(ref: np.ndarray, test: np.ndarray) -> DiffMetrics:
    mask = np.isfinite(ref) & np.isfinite(test)
    if not np.any(mask):
        return DiffMetrics(
            max_abs=float("nan"),
            p99_abs=float("nan"),
            rmse=float("nan"),
            mean_abs=float("nan"),
            exceed_0_1=0,
            exceed_1_0=0,
            count=0,
        )
    diffs = np.abs(ref[mask] - test[mask])
    return DiffMetrics(
        max_abs=float(np.max(diffs)),
        p99_abs=float(np.percentile(diffs, 99)),
        rmse=float(np.sqrt(np.mean(np.square(diffs)))),
        mean_abs=float(np.mean(diffs)),
        exceed_0_1=int(np.sum(diffs > 0.1)),
        exceed_1_0=int(np.sum(diffs > 1.0)),
        count=int(diffs.size),
    )


def _parse_body(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))


def _field_from_payload(payload: dict[str, Any], item: str) -> list[list[float | None]]:
    fields = payload.get("fields")
    if isinstance(fields, dict) and item in fields:
        return fields[item]
    if payload.get("item") == item and isinstance(payload.get("values"), list):
        return payload["values"]
    raise ValueError(f"item={item} の field を payload から取得できません")


def _aggregate_metrics(rows: list[DiffMetrics]) -> DiffMetrics:
    valid = [r for r in rows if r.count > 0]
    if not valid:
        return DiffMetrics(
            max_abs=float("nan"),
            p99_abs=float("nan"),
            rmse=float("nan"),
            mean_abs=float("nan"),
            exceed_0_1=0,
            exceed_1_0=0,
            count=0,
        )
    total_count = sum(r.count for r in valid)
    mean_abs = float(sum(r.mean_abs * r.count for r in valid) / total_count)
    rmse = float(np.sqrt(sum((r.rmse**2) * r.count for r in valid) / total_count))
    return DiffMetrics(
        max_abs=max(r.max_abs for r in valid),
        p99_abs=max(r.p99_abs for r in valid),
        rmse=rmse,
        mean_abs=mean_abs,
        exceed_0_1=sum(r.exceed_0_1 for r in valid),
        exceed_1_0=sum(r.exceed_1_0 for r in valid),
        count=total_count,
    )


def main() -> None:
    args = _parse_args()
    items = [t.strip().lower() for t in args.items.split(",") if t.strip()]
    margins = [int(t.strip()) for t in args.margins.split(",") if t.strip()]

    base_hour = _to_hour(args.datetime)
    target_hours = [
        (base_hour - dt.timedelta(hours=h)).isoformat()
        for h in range(max(args.hours, 1))
    ]

    results: dict[str, Any] = {
        "config": {
            "base_datetime": base_hour.isoformat(),
            "hours": args.hours,
            "z": args.z,
            "bbox": args.bbox,
            "items": items,
            "method": args.method,
            "smoothing": args.smoothing,
            "margins": margins,
            "min_station_count": args.min_station_count,
        },
        "by_margin": {},
    }

    for margin in margins:
        per_case: list[dict[str, Any]] = []
        per_case_metrics: list[DiffMetrics] = []
        fallback_count = 0

        for target_iso in target_hours:
            national_body = _compute_grid_field_body(
                target_iso,
                args.z,
                args.method,
                items,
                args.bbox,
                args.smoothing,
            )
            local_body = _compute_grid_field_body_local_bbox(
                target_iso,
                args.z,
                args.method,
                items,
                args.bbox,
                args.smoothing,
                margin,
                args.min_station_count,
            )
            national = _parse_body(national_body)
            local = _parse_body(local_body)

            case_item_metrics: dict[str, dict[str, Any]] = {}
            for item in items:
                ref_arr = _float_array(_field_from_payload(national, item))
                test_arr = _float_array(_field_from_payload(local, item))
                m = _calc_metrics(ref_arr, test_arr)
                case_item_metrics[item] = m.to_dict()
                per_case_metrics.append(m)

            if local.get("fallback_level") != "bbox":
                fallback_count += 1

            per_case.append(
                {
                    "datetime": target_iso,
                    "fallback_level": local.get("fallback_level"),
                    "used_station_count": local.get("used_station_count"),
                    "item_metrics": case_item_metrics,
                }
            )

        aggregate = _aggregate_metrics(per_case_metrics)
        results["by_margin"][str(margin)] = {
            "summary": aggregate.to_dict(),
            "fallback_cases": fallback_count,
            "total_cases": len(target_hours),
            "cases": per_case,
        }

    print("# Domain Equivalence Summary")
    print("margin_tiles | count | max_abs | p99_abs | rmse | mean_abs | >0.1 | >1.0 | fallback")
    for margin in margins:
        row = results["by_margin"][str(margin)]
        s = row["summary"]
        print(
            f"{margin:>12} | "
            f"{s['count']:>5} | "
            f"{s['max_abs']:.6f} | "
            f"{s['p99_abs']:.6f} | "
            f"{s['rmse']:.6f} | "
            f"{s['mean_abs']:.6f} | "
            f"{s['exceed_0_1']:>5} | "
            f"{s['exceed_1_0']:>5} | "
            f"{row['fallback_cases']}/{row['total_cases']}"
        )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()

