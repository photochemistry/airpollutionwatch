#!/usr/bin/env python3
"""
dataofjapan/land の japan.geojson から都道府県ごとに輪郭を抽出し、
簡略化して geojson_outlines/{pref_id}.json に保存する。
県単位で読み込む・表示する前提で軽量化する（ビルド時の OOM 防止）。

簡略化: 各リングを step 点ごとに間引き（先頭・末尾は閉じるため残す）。
"""
import json
import urllib.request
from pathlib import Path
from typing import List

# api_v1.PREF_ID_TO_NAME などと整合する都道府県名→API pref id
PREF_NAME_TO_ID = {
    "北海道": "hokkaido", "青森県": "aomori", "岩手県": "iwate", "宮城県": "miyagi",
    "秋田県": "akita", "山形県": "yamagata", "福島県": "fukushima", "茨城県": "ibaraki",
    "栃木県": "tochigi", "群馬県": "gunma", "埼玉県": "saitama", "千葉県": "chiba",
    "東京都": "tokyo", "神奈川県": "kanagawa", "新潟県": "niigata", "富山県": "toyama",
    "石川県": "ishikawa", "福井県": "fukui", "山梨県": "yamanashi", "長野県": "nagano",
    "岐阜県": "gifu", "静岡県": "shizuoka", "愛知県": "aichi", "三重県": "mie",
    "滋賀県": "shiga", "京都府": "kyoto", "大阪府": "osaka", "兵庫県": "hyogo",
    "奈良県": "nara", "和歌山県": "wakayama", "鳥取県": "tottori", "島根県": "shimane",
    "岡山県": "okayama", "広島県": "hiroshima", "山口県": "yamaguchi", "徳島県": "tokushima",
    "香川県": "kagawa", "愛媛県": "ehime", "高知県": "kochi", "福岡県": "fukuoka",
    "佐賀県": "saga", "長崎県": "nagasaki", "熊本県": "kumamoto", "大分県": "oita",
    "宮崎県": "miyazaki", "鹿児島県": "kagoshima", "沖縄県": "okinawa",
}

URL = "https://raw.githubusercontent.com/dataofjapan/land/master/japan.geojson"
# 間引き: この点数ごとに残す（大きいほど軽いが形が粗くなる）
SIMPLIFY_STEP = 8
OUT_DIR = "geojson_outlines"


def extract_rings(feature):
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    rings = []
    if geom.get("type") == "MultiPolygon" and coords:
        for poly in coords:
            if poly and poly[0]:
                ring = [[c[0], c[1]] for c in poly[0]]
                if ring:
                    rings.append(ring)
    elif geom.get("type") == "Polygon" and coords and coords[0]:
        rings.append([[c[0], c[1]] for c in coords[0]])
    return rings


def simplify_ring(ring: list, step: int) -> list:
    """リングを step 点ごとに間引き。先頭・末尾を残して閉じる。"""
    if not ring or len(ring) <= step + 1:
        return ring
    n = len(ring)
    # 閉じているなら末尾は先頭と同一なので除外して間引き
    closed = n >= 2 and ring[0][0] == ring[-1][0] and ring[0][1] == ring[-1][1]
    pts = ring[:-1] if closed else ring
    indices = list(range(0, len(pts), step))
    if closed and (len(pts) - 1) % step != 0:
        indices.append(len(pts) - 1)
    out = [pts[i] for i in indices]
    if closed:
        out.append(out[0][:])
    return out


def bbox_from_rings(rings: list) -> List[float]:
    """全リングの全点から [minLon, minLat, maxLon, maxLat] を返す。"""
    lons: list[float] = []
    lats: list[float] = []
    for ring in rings:
        for c in ring:
            if len(c) >= 2:
                lons.append(float(c[0]))
                lats.append(float(c[1]))
    if not lons or not lats:
        return [128.0, 30.0, 146.0, 46.0]  # 日本全体のフォールバック
    return [min(lons), min(lats), max(lons), max(lats)]


def main():
    root = Path(__file__).resolve().parent.parent
    out_dir = root / OUT_DIR
    out_dir.mkdir(exist_ok=True)

    with urllib.request.urlopen(URL, timeout=60) as r:
        fc = json.loads(r.read().decode())

    count = 0
    for f in fc.get("features") or []:
        p = (f or {}).get("properties") or {}
        nam_ja = p.get("nam_ja")
        pref_id = PREF_NAME_TO_ID.get(nam_ja) if nam_ja else None
        if not pref_id:
            continue
        rings = extract_rings(f)
        if not rings:
            continue
        simplified = [simplify_ring(r, SIMPLIFY_STEP) for r in rings]
        bbox = bbox_from_rings(simplified)
        out_path = out_dir / f"{pref_id}.json"
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump({"rings": simplified, "bbox": bbox}, fp, ensure_ascii=False)
        count += 1
        print(f"  {pref_id} -> {out_path.name}")

    print(f"OK: {count} prefectures -> {out_dir}")


if __name__ == "__main__":
    main()
