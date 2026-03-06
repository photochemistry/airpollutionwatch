"""
測定局メタデータを CSV から読み込み、pandas DataFrame で保持するモジュール。

TM20210000.py の STATIONS（CSV 文字列）をパースし、検索用の列（pref, lat, lon, has_*）
を付与した DataFrame をキャッシュします。データが約 1MB と小さいため、
SQLite ではなくメモリ上の DataFrame で pandas 検索する方式にしています。
"""

import io
from functools import lru_cache
from typing import Optional

import pandas as pd

# 国環研「都道府県名」→ API の pref ID（/prefectures で返す値）
PREF_NAME_TO_ID = {
    "北海道": "hokkaido",
    "青森県": "aomori",
    "岩手県": "iwate",
    "宮城県": "miyagi",
    "秋田県": "akita",
    "山形県": "yamagata",
    "福島県": "fukushima",
    "茨城県": "ibaraki",
    "栃木県": "tochigi",
    "群馬県": "gunma",
    "埼玉県": "saitama",
    "千葉県": "chiba",
    "東京都": "tokyo",
    "神奈川県": "kanagawa",
    "新潟県": "niigata",
    "富山県": "toyama",
    "石川県": "ishikawa",
    "福井県": "fukui",
    "山梨県": "yamanashi",
    "長野県": "nagano",
    "岐阜県": "gifu",
    "静岡県": "shizuoka",
    "愛知県": "aichi",
    "三重県": "mie",
    "滋賀県": "shiga",
    "京都府": "kyoto",
    "大阪府": "osaka",
    "兵庫県": "hyogo",
    "奈良県": "nara",
    "和歌山県": "wakayama",
    "鳥取県": "tottori",
    "島根県": "shimane",
    "岡山県": "okayama",
    "広島県": "hiroshima",
    "山口県": "yamaguchi",
    "徳島県": "tokushima",
    "香川県": "kagawa",
    "愛媛県": "ehime",
    "高知県": "kochi",
    "福岡県": "fukuoka",
    "佐賀県": "saga",
    "長崎県": "nagasaki",
    "熊本県": "kumamoto",
    "大分県": "oita",
    "宮崎県": "miyazaki",
    "鹿児島県": "kagoshima",
    "沖縄県": "okinawa",
}

HAS_COLUMNS = (
    "pm25", "ox", "so2", "no", "no2", "nox", "co", "spm",
    "nmhc", "ch4", "thc", "wd", "ws", "temp", "hum",
)


def _dms_to_decimal(d: object, m: object, s: object) -> Optional[float]:
    try:
        return float(d) + float(m) / 60 + float(s) / 3600
    except (TypeError, ValueError):
        return None


def _has_flag(val: object) -> bool:
    try:
        return int(val) == 1
    except (TypeError, ValueError):
        return False


def _str_or_none(x: object) -> Optional[str]:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    return s if s else None


@lru_cache(maxsize=1)
def get_stations_df() -> pd.DataFrame:
    """
    TM20210000 の CSV を読み、pref / lat / lon / has_* を付与した DataFrame を返す。
    初回のみパースし、以降はキャッシュを返す。
    """
    try:
        from TM20210000 import STATIONS
    except ImportError:
        return pd.DataFrame()

    raw = pd.read_csv(io.StringIO(STATIONS), dtype=str, encoding="utf-8")

    # 国環研局番 → 8桁文字列
    raw["station_id"] = raw["国環研局番"].apply(
        lambda x: str(int(float(x))).zfill(8)
        if pd.notna(x) and str(x).strip() else ""
    )
    raw = raw[raw["station_id"] != ""].copy()

    # 都道府県名 → pref
    raw["pref"] = raw["都道府県名"].apply(
        lambda x: PREF_NAME_TO_ID.get(str(x).strip()) if pd.notna(x) else None
    )
    raw = raw[raw["pref"].notna()].copy()

    # 緯度・経度（度分秒 → 十進数）
    raw["lat"] = raw.apply(
        lambda r: _dms_to_decimal(r.get("緯度_度"), r.get("緯度_分"), r.get("緯度_秒")),
        axis=1,
    )
    raw["lon"] = raw.apply(
        lambda r: _dms_to_decimal(r.get("経度_度"), r.get("経度_分"), r.get("経度_秒")),
        axis=1,
    )

    # 標高・区分・種別
    def safe_float(col: str):
        def f(r):
            x = r.get(col)
            if pd.isna(x) or str(x).strip() == "":
                return None
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        return f

    def safe_int(col: str):
        def f(r):
            x = r.get(col)
            if pd.isna(x) or str(x).strip() == "":
                return None
            try:
                return int(float(x))
            except (TypeError, ValueError):
                return None
        return f

    raw["elevation_m"] = raw.apply(safe_float("標高(m)"), axis=1)
    raw["station_category"] = raw.apply(safe_int("測定局区分"), axis=1)
    raw["station_type"] = raw.apply(safe_int("測定局種別"), axis=1)

    # 測定有無
    for col, src in [
        ("has_so2", "SO2_測定有無"),
        ("has_no", "NO_測定有無"),
        ("has_no2", "NO2_測定有無"),
        ("has_nox", "NOX_測定有無"),
        ("has_co", "CO_測定有無"),
        ("has_ox", "OX_測定有無"),
        ("has_spm", "SPM_測定有無"),
        ("has_pm25", "PM25_測定有無"),
        ("has_nmhc", "NMHC_測定有無"),
        ("has_ch4", "CH4_測定有無"),
        ("has_thc", "THC_測定有無"),
        ("has_wd", "WD_測定有無"),
        ("has_ws", "WS_測定有無"),
        ("has_temp", "TEMP_測定有無"),
        ("has_hum", "HUM_測定有無"),
    ]:
        raw[col] = raw[src].apply(lambda x: 1 if _has_flag(x) else 0)

    # テキスト列（API で使う名前を統一）
    raw["name"] = raw["測定局名"].apply(_str_or_none)
    raw["name_short"] = raw["８文字名"].apply(_str_or_none)
    raw["municipality"] = raw["市区町村名"].apply(_str_or_none)
    raw["address"] = raw["住所"].apply(_str_or_none)
    raw["building"] = raw["建物名等"].apply(_str_or_none)

    return raw
