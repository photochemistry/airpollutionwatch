#!/usr/bin/env python3
"""
指定した pollutant のグリッドキャッシュを削除する。

利用例:
  宮崎の Ox を ppm→ppb に修正したあと、ヒートマップだけ古い値のままになる場合:
    1. 必要なら clear_pref_data.py で宮崎を抹消してから再 collect
    2. 本スクリプトで ox のキャッシュを削除する:
       python clear_grid_cache.py ox
  これで次回の /v1/grid リクエスト時に ox が再計算され、正しい ppb で描画される。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from grid.cache import evict_cache_for_pollutant

def main():
    if len(sys.argv) < 2:
        print("usage: python clear_grid_cache.py <pollutant>", file=sys.stderr)
        print("  pollutant: no2, ox, pm25, so2, no, nox, spm, co, nmhc, temp, hum のいずれか", file=sys.stderr)
        sys.exit(1)
    pollutant = sys.argv[1].strip().lower()
    n = evict_cache_for_pollutant(pollutant)
    print(f"Deleted {n} grid cache entries for pollutant={pollutant}")

if __name__ == "__main__":
    main()
