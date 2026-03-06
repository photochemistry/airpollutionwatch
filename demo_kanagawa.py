"""
神奈川県全域の大気汚染グリッドをプロットするデモスクリプト.

使い方:
    poetry run python demo_kanagawa.py [--datetime ISO8601] [--smoothing 0.007]
"""

import argparse
import datetime

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import requests

BASE_URL = "http://localhost:8089"

# 神奈川県をカバーする bbox
KANAGAWA_BBOX = "138.9,35.1,139.85,35.7"

POLLUTANTS = ["nox", "ox", "pm25"]
TITLES     = ["NOX (ppb)", "OX (ppb)", "PM2.5 (µg/m³)"]
CMAPS      = ["OrRd", "PuBu", "YlOrBr"]


def fetch_field(pollutant: str, dt_iso: str, smoothing: float, z: int = 12) -> dict:
    r = requests.get(
        f"{BASE_URL}/v1/grid/field",
        params={
            "z": z,
            "pollutant": pollutant,
            "datetime": dt_iso,
            "bbox": KANAGAWA_BBOX,
            "method": "atps",
            "smoothing": smoothing,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def response_to_array(data: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """レスポンスを (lon2d, lat2d, field) に変換する."""
    from grid_utils import (
        BoundingBox,
        make_lonlat_grid_tiles,
        _webmercator_lonlat_to_tile_xy,
    )
    import math

    z = data["z"]
    # タイル範囲から lon2d / lat2d を再構築
    tx_min = data["tile_x_min"]
    tx_max = data["tile_x_max"]
    ty_min = data["tile_y_min"]
    ty_max = data["tile_y_max"]

    from grid_utils import _webmercator_tile_xy_to_lonlat
    # 列: tx_min ~ tx_max（西→東）
    lon_centers = [
        _webmercator_tile_xy_to_lonlat(tx + 0.5, (ty_min + ty_max) / 2.0 + 0.5, z)[0]
        for tx in range(tx_min, tx_max + 1)
    ]
    # 行: ty_max ~ ty_min → lat 昇順（南→北）に反転済みの順
    lat_centers = [
        _webmercator_tile_xy_to_lonlat((tx_min + tx_max) / 2.0 + 0.5, ty + 0.5, z)[1]
        for ty in range(ty_min, ty_max + 1)
    ]
    lat_centers = lat_centers[::-1]  # 南→北

    lons = np.array(lon_centers)
    lats = np.array(lat_centers)
    lon2d, lat2d = np.meshgrid(lons, lats)

    values = data["values"]
    field = np.array(
        [[np.nan if v is None else v for v in row] for row in values],
        dtype=np.float32,
    )
    return lon2d, lat2d, field


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--datetime",
        default=None,
        help="対象時刻 ISO8601（デフォルト: 最新の正時）",
    )
    parser.add_argument("--smoothing", type=float, default=0.007)
    args = parser.parse_args()

    if args.datetime is None:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        dt_iso = now.replace(minute=0, second=0, microsecond=0).isoformat()
    else:
        dt_iso = args.datetime

    print(f"対象時刻: {dt_iso}  smoothing={args.smoothing}")

    # 3汚染物質を取得
    datasets = []
    for p in POLLUTANTS:
        print(f"  取得中: {p} ...", end="", flush=True)
        data = fetch_field(p, dt_iso, args.smoothing)
        lon2d, lat2d, field = response_to_array(data)
        datasets.append((lon2d, lat2d, field, data))
        valid = field[~np.isnan(field)]
        print(f" {field.shape[0]}×{field.shape[1]} tiles  "
              f"range=[{valid.min():.2f}, {valid.max():.2f}]")

    snapshot_at = datasets[0][3]["apw_snapshot_at"]

    # --- プロット ---
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle(
        f"Kanagawa — Air Pollution Grid\n"
        f"snapshot: {snapshot_at}  (method=atps, smoothing={args.smoothing})",
        fontsize=12,
    )

    for ax, (lon2d, lat2d, field, _), title, cmap in zip(axes, datasets, TITLES, CMAPS):
        # 0 クランプ（可視化用のみ。負値は海上外挿アーティファクト）
        disp = np.where(field < 0, 0, field)

        pcm = ax.pcolormesh(
            lon2d, lat2d, disp,
            cmap=cmap,
            shading="nearest",
        )
        fig.colorbar(pcm, ax=ax, orientation="vertical", pad=0.02, shrink=0.85)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
        ax.tick_params(labelsize=8)
        ax.set_aspect("equal")

    plt.tight_layout()
    out = "kanagawa_grid.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\nsaved: {out}")
    plt.close()


if __name__ == "__main__":
    main()
