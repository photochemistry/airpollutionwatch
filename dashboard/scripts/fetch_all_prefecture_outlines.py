#!/usr/bin/env python3
"""japan.geojson から全47都道府県の輪郭を抽出し prefecture-outlines.ts を生成する。"""
import json
import urllib.request

# stations_loader.PREF_NAME_TO_ID と同一（都道府県名→API pref ID）
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
OUT = "src/data/prefecture-outlines.ts"


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


def main():
    with urllib.request.urlopen(URL, timeout=60) as r:
        fc = json.loads(r.read().decode())
    result = {}
    for f in fc.get("features") or []:
        p = (f or {}).get("properties") or {}
        nam_ja = p.get("nam_ja")
        pref_id = PREF_NAME_TO_ID.get(nam_ja) if nam_ja else None
        if not pref_id:
            continue
        rings = extract_rings(f)
        if rings:
            result[pref_id] = rings
    lines = [
        "// 全47都道府県輪郭（dataofjapan/land japan.geojson から抽出）。fetch_all_prefecture_outlines.py で再生成。",
        "export const prefectureOutlines: Record<string, [number, number][][]> = {",
    ]
    for pref_id in sorted(result.keys()):
        lines.append(f"  {json.dumps(pref_id)}: {json.dumps(result[pref_id], ensure_ascii=False)},")
    lines.append("};")
    out_path = __file__.replace("scripts/fetch_all_prefecture_outlines.py", OUT)
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"OK: {len(result)} prefectures -> {out_path}")


if __name__ == "__main__":
    main()
