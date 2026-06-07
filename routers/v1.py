"""
API v1: 都道府県・測定局・測定データ・収集ログなどのエンドポイント。
"""
import datetime
import json
import sqlite3
from pathlib import Path
from typing import List, Union, Literal, Dict, Any

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except ImportError:
    JST = datetime.timezone(datetime.timedelta(hours=9))  # Python 3.8 用

UTC = datetime.timezone.utc

import pandas as pd
import numpy as np

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict

import airpollutionwatch
from airpollutionwatch import prefecture_retrievers, ITEMSPECS
from data.stations import get_stations_df, PREF_NAME_TO_ID

# 都道府県 ID → 日本語名
PREF_ID_TO_NAME = {v: k for k, v in PREF_NAME_TO_ID.items()}

# 都道府県 ID → 地域ブロック
PREF_ID_TO_REGION: Dict[str, str] = {
    "hokkaido": "北海道",
    "aomori": "東北", "iwate": "東北", "miyagi": "東北", "akita": "東北",
    "yamagata": "東北", "fukushima": "東北",
    "ibaraki": "関東", "tochigi": "関東", "gunma": "関東", "saitama": "関東",
    "chiba": "関東", "tokyo": "関東", "kanagawa": "関東",
    "niigata": "中部", "toyama": "中部", "ishikawa": "中部", "fukui": "中部",
    "yamanashi": "中部", "nagano": "中部", "gifu": "中部", "shizuoka": "中部",
    "aichi": "中部",
    "mie": "近畿", "shiga": "近畿", "kyoto": "近畿", "osaka": "近畿",
    "hyogo": "近畿", "nara": "近畿", "wakayama": "近畿",
    "tottori": "中国", "shimane": "中国", "okayama": "中国", "hiroshima": "中国",
    "yamaguchi": "中国",
    "tokushima": "四国", "kagawa": "四国", "ehime": "四国", "kochi": "四国",
    "fukuoka": "九州", "saga": "九州", "nagasaki": "九州", "kumamoto": "九州",
    "oita": "九州", "miyazaki": "九州", "kagoshima": "九州",
    "okinawa": "沖縄",
}

from config import ROOT, connect_db
from data.item_mapping import ITEM_PARAM_TO_COL

GEOJSON_OUTLINES_DIR = ROOT / "geojson_outlines"
AI_DOC_PATH = ROOT / "docs" / "ai-clients.md"
INGEST_LOG_LIMIT = 500

router = APIRouter(prefix="/v1", tags=["v1"])


def _pref_links_path() -> Path:
    """pref-links.md のパスを探索して返す（見つからない場合は API 直下）。"""
    candidates: list[Path] = []
    try:
        pkg_root = Path(airpollutionwatch.__file__).resolve().parent.parent
        candidates.append(pkg_root / "pref-links.md")
    except Exception:
        # 実行環境によっては airpollutionwatch が import できないことがある
        pass
    candidates.extend([
        ROOT / "pref-links.md",
        ROOT / "airpollutionwatch" / "pref-links.md",
        ROOT.parent / "airpollutionwatch" / "pref-links.md",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return ROOT / "pref-links.md"


def collection_prefecture_ids() -> list[str]:
    """収集状況・has_data 判定に使う都道府県 ID 一覧。"""
    return sorted(prefecture_retrievers.keys())


def log_overview_prefecture_ids() -> list[str]:
    """巡回ログ画面用: 全47都道府県（公式サイトリンク表示のため）。"""
    return sorted(PREF_ID_TO_NAME.keys())


def _ensure_measurements_log_indexes(cur: sqlite3.Cursor) -> None:
    """/v1/log 用: 県×時刻の集計を速くするインデックス。"""
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_measurements_pref_target
        ON measurements (prefecture, target_datetime)
        """
    )


def _ensure_ingest_attempts_log_indexes(cur: sqlite3.Cursor) -> None:
    """/v1/log 用: ingest_attempts の最新行・ログ取得を速くするインデックス。"""
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ingest_pref_attempted
        ON ingest_attempts (prefecture, attempted_at DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ingest_attempted
        ON ingest_attempts (attempted_at DESC)
        """
    )

# --- モデル ---
class StationListItem(BaseModel):
    station_id: str = Field(..., description="国環研局番（8桁）")
    pref: str = Field(..., description="都道府県 ID")
    name: str | None = Field(None, description="測定局名")
    name_short: str | None = Field(None, description="8文字名")
    municipality: str | None = Field(None, description="市区町村名")
    lat: float | None = Field(None, description="緯度")
    lon: float | None = Field(None, description="経度")
    has_pm25: bool = Field(False, description="PM2.5 測定の有無")
    has_ox: bool = Field(False, description="OX 測定の有無")


class StationDetail(StationListItem):
    address: str | None = Field(None, description="住所")
    building: str | None = Field(None, description="建物名等")
    elevation_m: float | None = Field(None, description="標高 m")
    station_category: int | None = Field(None, description="測定局区分")
    station_type: int | None = Field(None, description="測定局種別")
    has_so2: bool = False
    has_no: bool = False
    has_no2: bool = False
    has_nox: bool = False
    has_co: bool = False
    has_spm: bool = False
    has_nmhc: bool = False
    has_ch4: bool = False
    has_thc: bool = False
    has_wd: bool = False
    has_ws: bool = False
    has_temp: bool = False
    has_hum: bool = False


class PrefectureInfo(BaseModel):
    id: str = Field(..., description="都道府県 ID（API のパス等で使用）")
    name_ja: str = Field(..., description="都道府県の日本語名")
    has_data: bool = Field(..., description="当該県のデータが API で取得可能か")
    region: str = Field(..., description="地域ブロック（北海道・東北・関東・中部・近畿・中国・四国・九州・沖縄）")


class HourlyStationPoint(BaseModel):
    model_config = ConfigDict(extra="allow")
    station_id: str = Field(..., description="国環研局番（8桁）")
    target_datetime: str = Field(..., description="収集ジョブが対象とした正時 (ISO8601)。局ごとに異なる場合がある")
    observed_datetime: str = Field(..., description="観測時刻 ISO8601")


class HourlyResponse(BaseModel):
    target_datetime: str = Field(..., description="リクエストで指定した時刻 (ISO8601)")
    data: List[HourlyStationPoint] = Field(..., description="局ごとの測定値（各局の target_datetime はデータ内を参照）")
    spec: Dict[str, Any] = Field(..., description="測定項目ごとの仕様（返した項目のみ）")


class TimeSeriesPoint(BaseModel):
    datetime: str = Field(..., description="観測時刻 (ISO 8601)")
    value: float | None = Field(None, description="測定値（欠損は null）")


class TimeSeriesSeries(BaseModel):
    station_id: str = Field(..., description="国環研局番 8 桁")
    item: str = Field(..., description="測定項目名 (PM25, OX など)")
    values: List[TimeSeriesPoint] = Field(default_factory=list)


class TimeSeriesResponse(BaseModel):
    timeseries: List[TimeSeriesSeries] = Field(default_factory=list)


class LatestStationValues(BaseModel):
    station_id: str
    values: Dict[str, float | None] = Field(default_factory=dict)


class LatestResponse(BaseModel):
    datetime: str = Field(..., description="対象とした最新の target_datetime (ISO 8601)")
    stations: List[LatestStationValues] = Field(default_factory=list)


def _station_id_to_code(sid: str) -> int:
    try:
        return int(sid)
    except ValueError:
        return int(sid.zfill(8) if len(sid) <= 8 else sid)


def _station_code_to_id(code: int) -> str:
    return str(code).zfill(8)


def _series_to_station_item(s: pd.Series) -> StationListItem:
    return StationListItem(
        station_id=str(s["station_id"]),
        pref=str(s["pref"]),
        name=s.get("name"),
        name_short=s.get("name_short"),
        municipality=s.get("municipality"),
        lat=s.get("lat") if pd.notna(s.get("lat")) else None,
        lon=s.get("lon") if pd.notna(s.get("lon")) else None,
        has_pm25=bool(s.get("has_pm25", 0)),
        has_ox=bool(s.get("has_ox", 0)),
    )


def _series_to_station_detail(s: pd.Series) -> StationDetail:
    return StationDetail(
        station_id=str(s["station_id"]),
        pref=str(s["pref"]),
        name=s.get("name"),
        name_short=s.get("name_short"),
        municipality=s.get("municipality"),
        lat=s.get("lat") if pd.notna(s.get("lat")) else None,
        lon=s.get("lon") if pd.notna(s.get("lon")) else None,
        has_pm25=bool(s.get("has_pm25", 0)),
        has_ox=bool(s.get("has_ox", 0)),
        address=s.get("address"),
        building=s.get("building"),
        elevation_m=s.get("elevation_m") if pd.notna(s.get("elevation_m")) else None,
        station_category=int(s["station_category"]) if pd.notna(s.get("station_category")) else None,
        station_type=int(s["station_type"]) if pd.notna(s.get("station_type")) else None,
        has_so2=bool(s.get("has_so2", 0)),
        has_no=bool(s.get("has_no", 0)),
        has_no2=bool(s.get("has_no2", 0)),
        has_nox=bool(s.get("has_nox", 0)),
        has_co=bool(s.get("has_co", 0)),
        has_spm=bool(s.get("has_spm", 0)),
        has_nmhc=bool(s.get("has_nmhc", 0)),
        has_ch4=bool(s.get("has_ch4", 0)),
        has_thc=bool(s.get("has_thc", 0)),
        has_wd=bool(s.get("has_wd", 0)),
        has_ws=bool(s.get("has_ws", 0)),
        has_temp=bool(s.get("has_temp", 0)),
        has_hum=bool(s.get("has_hum", 0)),
    )


REGION_ORDER = ("北海道", "東北", "関東", "中部", "近畿", "中国", "四国", "九州", "沖縄")


# --- エンドポイント ---


@router.get(
    "/geojson/outline/{pref_id}",
    summary="都道府県輪郭",
)
async def geojson_outline(pref_id: str):
    """指定都道府県の境界線（簡略化済み）を GeoJSON rings 形式で返します。

    地図上での県単位ハイライト表示に使用します。
    データは `geojson_outlines/{pref_id}.json`（`scripts/generate_prefecture_outlines.py` で生成）。
    """
    if pref_id not in PREF_ID_TO_NAME:
        raise HTTPException(status_code=404, detail=f"未知の都道府県 ID: {pref_id}")
    path = GEOJSON_OUTLINES_DIR / f"{pref_id}.json"
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"輪郭データがありません。scripts/generate_prefecture_outlines.py を実行してください: {pref_id}",
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"輪郭の読み込みに失敗しました: {e!s}")
    if "rings" not in data:
        raise HTTPException(status_code=500, detail="輪郭 JSON に rings が含まれていません")
    return data


@router.get("/prefectures", response_model=List[PrefectureInfo], summary="都道府県一覧")
async def prefectures():
    """全 47 都道府県の ID・日本語名・データ取得可否（has_data）・地域ブロック（region）を返します。

    地域ブロック順・日本語名順でソートされています。
    `has_data=true` の県のみ `/v1/measurements?pref=...` で観測値を取得できます。
    """
    has_data_set = set(collection_prefecture_ids())
    items = []
    for pref_id in PREF_ID_TO_NAME:
        items.append(
            PrefectureInfo(
                id=pref_id,
                name_ja=PREF_ID_TO_NAME.get(pref_id, pref_id),
                has_data=pref_id in has_data_set,
                region=PREF_ID_TO_REGION.get(pref_id, ""),
            )
        )
    region_rank = {r: i for i, r in enumerate(REGION_ORDER)}
    items.sort(key=lambda x: (region_rank.get(x.region, 99), x.name_ja))
    return items


@router.get("/stations", response_model=List[StationListItem], summary="測定局一覧")
async def list_stations(
    pref: str | None = Query(None, description="都道府県 ID（例: tokyo）。省略時は全国"),
    has: str | None = Query(
        None,
        description="観測項目のカンマ区切り（例: pm25,ox）。指定した項目を観測する局のみ返す",
    ),
):
    """国環研測定局メタデータの一覧を返します。

    データソースは TM20210000（メモリ上の DataFrame）。地図表示や局選択 UI のマスターデータとして利用します。
    """
    df = get_stations_df()
    if df.empty:
        return []
    mask = pd.Series(True, index=df.index)
    if pref is not None:
        mask &= df["pref"] == pref
    if has is not None:
        for token in has.lower().replace(" ", "").split(","):
            t = token.strip()
            if t and t in ("pm25", "ox", "so2", "no", "no2", "nox", "co", "spm", "nmhc", "ch4", "thc", "wd", "ws", "temp", "hum"):
                mask &= df[f"has_{t}"] == 1
    subset = df.loc[mask]
    return [_series_to_station_item(subset.loc[i]) for i in subset.index]


@router.get("/stations/{station_id}", response_model=StationDetail, summary="測定局詳細")
async def get_station(station_id: str):
    """指定した測定局の詳細情報（住所・局種別・各観測項目の有無など）を返します。

    `station_id` は国環研局番（8 桁。先頭 0 は省略可）。
    """
    try:
        sid = str(int(station_id)).zfill(8)
    except ValueError:
        sid = station_id.zfill(8) if len(station_id) <= 8 else station_id
    df = get_stations_df()
    if df.empty:
        raise HTTPException(status_code=404, detail="Stations metadata not available")
    row = df[df["station_id"] == sid]
    if row.empty:
        raise HTTPException(status_code=404, detail="Station not found")
    return _series_to_station_detail(row.iloc[0])


@router.get(
    "/measurements",
    response_model=Union[TimeSeriesResponse, HourlyResponse],
    summary="測定データ取得",
    responses={200: {"description": "format=series のときは時系列、format=snapshot のときは局単位スナップショット"}},
)
async def get_measurements(
    station_ids: str | None = Query(
        None,
        description="国環研局番のカンマ区切り（例: 13114010,13114510）。pref と同時指定不可",
    ),
    pref: str | None = Query(
        None,
        description="都道府県 ID（例: tokyo）。指定時はその県の全局を対象",
    ),
    from_: datetime.datetime = Query(..., alias="from", description="期間開始（ISO 8601。分以下は正時に丸め）"),
    to: datetime.datetime = Query(..., description="期間終了（ISO 8601。分以下は正時に丸め）"),
    items: str = Query(
        "pm25,ox,no2",
        description="測定項目のカンマ区切り（例: pm25,ox,no2）。so2,no,no2,nox,ox,spm,pm25,co,nmhc,ch4,thc,wd,ws,temp,hum",
    ),
    format: Literal["series", "snapshot"] = Query(
        "series",
        description="series=時系列（既定） / snapshot=1 時刻・局単位（from=to 必須）",
    ),
):
    """局（または都道府県）・期間を指定して測定データを取得します。

    - **format=series**: (局, 測定項目) ごとの時系列配列。グラフ描画向け。
    - **format=snapshot**: 指定時刻以前の局ごと最新値。地図・表の 1 時刻表示向け。

    `station_ids` と `pref` のどちらか一方を指定してください。
    """
    if station_ids and pref:
        raise HTTPException(status_code=400, detail="station_ids と pref は同時に指定できません")
    if not station_ids and not pref:
        raise HTTPException(status_code=400, detail="station_ids または pref を指定してください")

    if pref is not None:
        if pref == "japan":
            # retrieve all prefectures
            df_st = get_stations_df()
            codes = [_station_id_to_code(str(s)) for pref in prefecture_retrievers.keys() for s in df_st[df_st["pref"] == pref]["station_id"]]
        else:
            if pref not in prefecture_retrievers:
                raise HTTPException(status_code=404, detail="都道府県が見つかりません")
            df_st = get_stations_df()
            if df_st.empty:
                raise HTTPException(status_code=404, detail="局メタデータを取得できません")
            pref_stations = df_st[df_st["pref"] == pref]
            codes = [_station_id_to_code(str(s)) for s in pref_stations["station_id"]] if not pref_stations.empty else []
    else:
        try:
            codes = [_station_id_to_code(s.strip()) for s in station_ids.split(",") if s.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="station_ids は整数または 8 桁以内の数字で指定してください")
        if not codes:
            raise HTTPException(status_code=400, detail="station_ids を 1 つ以上指定してください")

    cols = []
    for p in items.lower().replace(" ", "").split(","):
        p = p.strip()
        if p and p in ITEM_PARAM_TO_COL:
            cols.append(ITEM_PARAM_TO_COL[p])
    if not cols:
        cols = ["PM25", "OX", "NO2"]

    from_rounded = from_.replace(minute=0, second=0, microsecond=0)
    to_rounded = to.replace(minute=0, second=0, microsecond=0)

    if format == "snapshot":
        if from_rounded != to_rounded:
            raise HTTPException(status_code=400, detail="format=snapshot のときは from と to に同一時刻（同一正時）を指定してください。")

        # 局ごとに from_rounded 以前の最新 target_datetime を取得し、そのデータを返す
        placeholders = ",".join("?" * len(codes))
        m_cols = ", ".join(["m.station_code", "m.target_datetime", "m.observed_datetime"] + [f"m.{c}" for c in cols])
        query = f"""
            SELECT {m_cols}
            FROM measurements m
            INNER JOIN (
                SELECT station_code, MAX(target_datetime) AS max_dt
                FROM measurements
                WHERE station_code IN ({placeholders}) AND target_datetime <= ?
                GROUP BY station_code
            ) latest ON m.station_code = latest.station_code AND m.target_datetime = latest.max_dt
            WHERE m.station_code IN ({placeholders})
        """
        with connect_db() as conn:
            df = pd.read_sql_query(query, conn, params=(*codes, from_rounded.isoformat(), *codes))

        if df.empty:
            raise HTTPException(status_code=404, detail="Data not available.")

        data_list: List[HourlyStationPoint] = []
        for _, row in df.iterrows():
            point_dict = {
                "station_id": _station_code_to_id(int(row["station_code"])),
                "target_datetime": str(row["target_datetime"]),
                "observed_datetime": str(row["observed_datetime"]),
            }
            for col in cols:
                val = row.get(col)
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    point_dict[col] = None
                elif col == "WD":
                    point_dict[col] = int(val)
                else:
                    point_dict[col] = float(val)
            data_list.append(HourlyStationPoint(**point_dict))
        spec = {k: ITEMSPECS[k] for k in cols if k in ITEMSPECS}
        return HourlyResponse(
            target_datetime=from_rounded.isoformat(),
            data=data_list,
            spec=spec,
        )

    from_iso = from_rounded.isoformat()
    to_iso = to_rounded.isoformat()
    placeholders = ",".join("?" * len(codes))
    col_list = ", ".join(["station_code", "target_datetime", "observed_datetime"] + cols)
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            f"SELECT {col_list} FROM measurements WHERE station_code IN ({placeholders}) AND target_datetime >= ? AND target_datetime <= ? ORDER BY station_code, target_datetime",
            (*codes, from_iso, to_iso),
        )
        rows = cur.fetchall()

    series: Dict[tuple[int, str], List[tuple[str, float | None]]] = {}
    for r in rows:
        code = r["station_code"]
        dt = r["observed_datetime"] or r["target_datetime"]
        for col in cols:
            key = (code, col)
            if key not in series:
                series[key] = []
            val = r[col]
            series[key].append((dt, float(val) if val is not None and not (isinstance(val, float) and np.isnan(val)) else None))

    out = []
    for (code, col), points in sorted(series.items()):
        out.append(TimeSeriesSeries(
            station_id=_station_code_to_id(code),
            item=col,
            values=[TimeSeriesPoint(datetime=dt, value=v) for dt, v in points],
        ))
    return TimeSeriesResponse(timeseries=out)


@router.get("/latest", response_model=LatestResponse, summary="最新値")
async def get_latest(
    station_ids: str | None = Query(
        None,
        description="国環研局番のカンマ区切り。pref と同時指定不可",
    ),
    pref: str | None = Query(None, description="都道府県 ID。指定時はその県の全局の最新値"),
    items: str = Query(
        "pm25,ox,no2",
        description="測定項目のカンマ区切り（例: pm25,ox）",
    ),
):
    """指定した局、または都道府県内の全局について、直近の最新値を返します。

    対象局集合内で最も新しい `target_datetime` のデータを返します。
    ダッシュボードや「いま」の状況表示ウィジェット向け。
    """
    if station_ids and pref:
        raise HTTPException(status_code=400, detail="station_ids と pref は同時に指定できません")
    if not station_ids and not pref:
        raise HTTPException(status_code=400, detail="station_ids または pref を指定してください")

    if pref is not None:
        if pref not in prefecture_retrievers:
            raise HTTPException(status_code=404, detail="都道府県が見つかりません")
        df_st = get_stations_df()
        if df_st.empty:
            raise HTTPException(status_code=404, detail="局メタデータを取得できません")
        pref_stations = df_st[df_st["pref"] == pref]
        if pref_stations.empty:
            return LatestResponse(datetime="", stations=[])
        codes = [_station_id_to_code(str(s)) for s in pref_stations["station_id"]]
    else:
        try:
            codes = [_station_id_to_code(s.strip()) for s in station_ids.split(",") if s.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="station_ids の形式が不正です")
        if not codes:
            raise HTTPException(status_code=400, detail="station_ids を 1 つ以上指定してください")

    cols = []
    for p in items.lower().replace(" ", "").split(","):
        p = p.strip()
        if p and p in ITEM_PARAM_TO_COL:
            cols.append(ITEM_PARAM_TO_COL[p])
    if not cols:
        cols = ["PM25", "OX", "NO2"]

    placeholders = ",".join("?" * len(codes))
    col_list = ", ".join(["station_code", "target_datetime", "observed_datetime"] + cols)
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"SELECT MAX(target_datetime) AS latest FROM measurements WHERE station_code IN ({placeholders})", tuple(codes))
        row = cur.fetchone()
        latest = row["latest"] if row and row["latest"] else None
    if not latest:
        return LatestResponse(datetime="", stations=[])

    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            f"SELECT {col_list} FROM measurements WHERE station_code IN ({placeholders}) AND target_datetime = ? ORDER BY station_code",
            (*codes, latest),
        )
        rows = cur.fetchall()

    stations_out = []
    for r in rows:
        code = r["station_code"]
        values = {}
        for col in cols:
            val = r[col]
            values[col] = float(val) if val is not None and not (isinstance(val, float) and np.isnan(val)) else None
        stations_out.append(LatestStationValues(station_id=_station_code_to_id(code), values=values))
    return LatestResponse(datetime=latest, stations=stations_out)


class CollectionStatusItem(BaseModel):
    """県ごとの収集巡回状況（1件）"""
    pref_id: str = Field(..., description="都道府県 ID")
    name_ja: str = Field(..., description="都道府県名")
    region: str = Field("", description="地域ブロック")
    latest_datetime: str | None = Field(None, description="直近の target_datetime (ISO8601)。データなしのとき null")
    hours_ago: float | None = Field(
        None,
        description="「最新1件」の target_datetime が今から何時間前か（鮮度）。収集が全県一括のため県どうしで同じになりやすい。",
    )
    oldest_continuous_datetime: str | None = Field(
        None,
        description="連続している区間の最古の target_datetime（1時間刻みで欠けなしにさかのぼれる限界）。データなしのとき null",
    )
    continuous_days_ago: float | None = Field(
        None,
        description="連続データが何日前までさかのぼれるか（日数）。県ごとに取得状況で異なる。",
    )
    has_data: bool = Field(..., description="当該県に測定データが1件以上あるか")
    log_status: str = Field("ok", description="直近の ingest_attempts に基づく状態: ok / warning / error")
    log_message: str | None = Field(None, description="直近の ingest_attempts の error_message（代表1件）")
    pref_url: str | None = Field(None, description="当該県の公式データページ URL（pref-links.md 由来）")


class LogOverviewResponse(BaseModel):
    """収集ジョブの概要（県別ステータス + ingest_attempts 由来のログテキスト）"""

    status_items: List[CollectionStatusItem] = Field(
        ..., description="県ごとの収集巡回状況（直近 target_datetime・経過時間・log_status 等）"
    )
    collect_log: str | None = Field(
        None,
        description="ingest_attempts から生成した収集ログテキスト（後方互換のフィールド名。データなしは null）",
    )


PrefHistoryCellStatus = Literal["ok", "missing", "empty"]

# internal_ingest._POLLUTANT_DB_COLS と同じ（usable 判定）
_MEASUREMENT_POLLUTANT_COLS = (
    "SO2",
    "NO",
    "NO2",
    "NOX",
    "OX",
    "SPM",
    "PM25",
    "CO",
    "NMHC",
    "CH4",
    "THC",
)


class PrefHistoryCell(BaseModel):
    hour: int = Field(..., ge=0, le=23, description="時刻（0-23）")
    has_data: bool = Field(
        ...,
        description="当該正時に usable な測定値があるか（status=ok のとき true）",
    )
    status: PrefHistoryCellStatus = Field(
        ...,
        description="ok=測定値あり, empty=行のみ（全項目欠測）, missing=行なし",
    )


class PrefHistoryDayRow(BaseModel):
    date: str = Field(..., description="日付（YYYY-MM-DD, JST）")
    cells: List[PrefHistoryCell] = Field(..., description="0-23時のセル")
    ok_count: int = Field(..., description="当日の測定値ありスロット数")
    empty_count: int = Field(..., description="当日の行のみ（全項目欠測）スロット数")
    missing_count: int = Field(..., description="当日の行なしスロット数")


class PrefHistorySummary(BaseModel):
    total_slots: int = Field(..., description="対象スロット総数（days*24）")
    ok_slots: int = Field(..., description="測定値ありスロット数")
    empty_slots: int = Field(..., description="行のみ（全項目欠測）スロット数")
    missing_slots: int = Field(..., description="行なしスロット数")
    coverage_ratio: float = Field(
        ...,
        description="usable 充足率（ok_slots/total_slots）。empty は含めない",
    )
    oldest_continuous_datetime: str | None = Field(
        None,
        description="最新から1時間刻みで欠けなしに遡れる最古の時刻（JST）",
    )


class PrefLogHistoryResponse(BaseModel):
    pref_id: str = Field(..., description="都道府県 ID")
    name_ja: str = Field(..., description="都道府県名")
    days: int = Field(..., description="対象日数")
    start_datetime: str = Field(..., description="対象開始時刻（JST, ISO8601）")
    end_datetime: str = Field(..., description="対象終了時刻（JST, ISO8601）")
    rows: List[PrefHistoryDayRow] = Field(..., description="日ごとの〇×表データ")
    summary: PrefHistorySummary


def _load_pref_links() -> Dict[str, str]:
    """pref-links.md をパースし、県 ID → URL の辞書を返す。1行目はヘッダ、2行目以降は「県ID URL ...」形式。"""
    path = _pref_links_path().resolve()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    result: Dict[str, str] = {}
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if i == 0 and parts and parts[0] == "県名":
            continue
        if len(parts) >= 2:
            result[parts[0]] = parts[1]
    return result


def _ingest_attempts_table_exists(cur: sqlite3.Cursor) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_attempts'"
    )
    return cur.fetchone() is not None


def _ingest_status_from_row(
    status: str | None,
    error_message: str | None,
) -> tuple[str, str | None]:
    """ingest_attempts の1行から log_status / log_message を決める。"""
    if status == "failed":
        msg = error_message or "failed to collect"
        return "warning", f"failed to collect: {msg}"
    if error_message:
        return "warning", error_message
    return "ok", None


def _ingest_status_per_prefecture(cur: sqlite3.Cursor) -> Dict[str, tuple[str, str | None]]:
    """ingest_attempts の最新行から県ごとの log_status / log_message を返す。"""
    if not _ingest_attempts_table_exists(cur):
        return {}
    result: Dict[str, tuple[str, str | None]] = {
        pref: ("ok", None) for pref in log_overview_prefecture_ids()
    }
    cur.execute(
        """
        SELECT ia.prefecture, ia.status, ia.error_message
        FROM ingest_attempts AS ia
        INNER JOIN (
            SELECT prefecture, MAX(attempted_at) AS max_at
            FROM ingest_attempts
            GROUP BY prefecture
        ) AS latest
            ON ia.prefecture = latest.prefecture
           AND ia.attempted_at = latest.max_at
        """
    )
    for pref, status, error_message in cur.fetchall():
        if pref in result:
            result[pref] = _ingest_status_from_row(status, error_message)
    return result


def _format_attempted_at_for_log(attempted_at: str) -> str:
    """ingest_attempts.attempted_at をログ表示用に整形する。"""
    try:
        dt = datetime.datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(JST)
        else:
            dt = dt.replace(tzinfo=JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return attempted_at.replace("T", " ")[:19]


def _build_ingest_log_text(cur: sqlite3.Cursor, *, limit: int = INGEST_LOG_LIMIT) -> str | None:
    """ingest_attempts から collect_log 互換のテキストログを生成する。"""
    if not _ingest_attempts_table_exists(cur):
        return None
    cur.execute(
        """
        SELECT prefecture, target_datetime, status, error_message, attempted_at
        FROM ingest_attempts
        ORDER BY attempted_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    lines: list[str] = []
    for pref, target_dt, status, error_message, attempted_at in reversed(rows):
        ts = _format_attempted_at_for_log(attempted_at)
        if status == "ok" and not error_message:
            lines.append(f"{ts} INFO Collecting {pref} at {target_dt}")
        elif status == "ok" and error_message:
            lines.append(f"{ts} WARNING Prefecture {pref}: {error_message}")
        else:
            lines.append(
                f"{ts} WARNING Prefecture {pref}: failed to collect: {error_message or 'unknown'}"
            )
    return "\n".join(lines)


def _parse_db_datetime_utc(iso_str: str) -> datetime.datetime | None:
    """DB の target_datetime を UTC の aware datetime で返す。ナイーブなら JST と解釈する。"""
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST).astimezone(UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _oldest_continuous_from_datetimes(dts_utc: list[datetime.datetime]) -> tuple[str | None, float | None]:
    """正時 datetime の集合から、最新から1時間刻みで連続している区間の最古を求める。"""
    if not dts_utc:
        return None, None
    dt_set = set(dts_utc)
    latest = max(dts_utc)
    cur_dt = latest
    oldest = latest
    one_hour = datetime.timedelta(hours=1)
    while cur_dt in dt_set:
        oldest = cur_dt
        cur_dt -= one_hour
    now = datetime.datetime.now(UTC)
    delta_days = (now - oldest).total_seconds() / 86400.0
    oldest_jst = oldest.astimezone(JST)
    return oldest_jst.isoformat(), round(delta_days, 1)


def _usable_pollutant_sql(prefix: str = "") -> str:
    """measurements 行に usable な測定値があるかの SQL 条件（OR 連結）。"""
    col = f"{prefix}." if prefix else ""
    return " OR ".join(f"{col}{c} IS NOT NULL" for c in _MEASUREMENT_POLLUTANT_COLS)


def _load_measurement_slot_status(
    cur: sqlite3.Cursor,
    *,
    prefecture: str,
    start_hour_utc: datetime.datetime,
    end_hour_utc: datetime.datetime,
) -> dict[datetime.datetime, PrefHistoryCellStatus]:
    """正時 UTC → ok（usable あり） / empty（行のみ）。"""
    cols_or = _usable_pollutant_sql()
    cur.execute(
        f"""
        SELECT target_datetime,
               MAX(CASE WHEN ({cols_or}) THEN 1 ELSE 0 END) AS has_usable
        FROM measurements
        WHERE prefecture = ?
        GROUP BY target_datetime
        """,
        (prefecture,),
    )
    out: dict[datetime.datetime, PrefHistoryCellStatus] = {}
    for target_iso, has_usable in cur.fetchall():
        dt = _parse_db_datetime_utc(target_iso)
        if dt is None:
            continue
        dt = dt.replace(minute=0, second=0, microsecond=0)
        if not (start_hour_utc <= dt <= end_hour_utc):
            continue
        out[dt] = "ok" if has_usable else "empty"
    return out


def _oldest_continuous_target(cur: sqlite3.Cursor, pref: str) -> tuple[str | None, float | None]:
    """県の target_datetime 一覧から、最新から1時間刻みで連続している区間の最古を求め、その ISO 文字列と「何日前か」を返す。"""
    cur.execute("SELECT DISTINCT target_datetime FROM measurements WHERE prefecture = ?", (pref,))
    dts_utc: list[datetime.datetime] = []
    for row in cur.fetchall():
        dt = _parse_db_datetime_utc(row[0])
        if dt is not None:
            dts_utc.append(dt.replace(minute=0, second=0, microsecond=0))
    return _oldest_continuous_from_datetimes(dts_utc)


def _latest_by_prefecture(cur: sqlite3.Cursor) -> Dict[str, str]:
    """県ごとの最新 target_datetime を一括取得する。"""
    cur.execute(
        "SELECT prefecture, MAX(target_datetime) FROM measurements GROUP BY prefecture"
    )
    return {row[0]: row[1] for row in cur.fetchall() if row[0] and row[1]}


def _datetimes_by_prefecture(cur: sqlite3.Cursor) -> Dict[str, list[datetime.datetime]]:
    """県ごとの distinct target_datetime（正時 UTC）を一括取得する。"""
    cur.execute(
        """
        SELECT prefecture, target_datetime
        FROM measurements
        GROUP BY prefecture, target_datetime
        """
    )
    out: Dict[str, list[datetime.datetime]] = {}
    for pref, iso in cur.fetchall():
        if not pref:
            continue
        dt = _parse_db_datetime_utc(iso)
        if dt is None:
            continue
        out.setdefault(pref, []).append(dt.replace(minute=0, second=0, microsecond=0))
    return out


def _parse_end_hour_utc(end_iso: str | None) -> datetime.datetime:
    """
    クエリ end を UTC の正時 datetime に正規化する。
    未指定時は現在時刻（UTC）を使用。
    """
    if end_iso is None:
        return datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="end の形式が不正です（ISO 8601 で指定してください）",
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST).astimezone(UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


def _get_collection_status_items_from_cursor(
    cur: sqlite3.Cursor,
) -> List[CollectionStatusItem]:
    """connect_db 済みのカーソルから県別収集状況を組み立てる。"""
    now = datetime.datetime.now(UTC)
    pref_links = _load_pref_links()
    result: List[CollectionStatusItem] = []

    log_status_map = _ingest_status_per_prefecture(cur)
    latest_map = _latest_by_prefecture(cur)
    datetimes_map = _datetimes_by_prefecture(cur)
    for pref in log_overview_prefecture_ids():
        latest_iso = latest_map.get(pref)
        has_data = latest_iso is not None

        hours_ago: float | None = None
        if latest_iso:
            latest_dt = _parse_db_datetime_utc(latest_iso)
            if latest_dt is not None:
                delta = now - latest_dt
                hours_ago = round(delta.total_seconds() / 3600.0, 1)

        oldest_iso, continuous_days_ago = _oldest_continuous_from_datetimes(
            datetimes_map.get(pref, [])
        )

        level, message = log_status_map.get(pref, ("ok", None))

        result.append(
            CollectionStatusItem(
                pref_id=pref,
                name_ja=PREF_ID_TO_NAME.get(pref, pref),
                region=PREF_ID_TO_REGION.get(pref, ""),
                latest_datetime=latest_iso,
                hours_ago=hours_ago,
                oldest_continuous_datetime=oldest_iso,
                continuous_days_ago=continuous_days_ago,
                has_data=has_data,
                log_status=level,
                log_message=message,
                pref_url=pref_links.get(pref),
            )
        )

    return result


def _get_collection_status_items() -> List[CollectionStatusItem]:
    """各県の収集巡回状況を一覧で返す。ingest_attempts があれば県ごとの log_status / log_message も付与する。"""
    with connect_db() as conn:
        cur = conn.cursor()
        _ensure_measurements_log_indexes(cur)
        _ensure_ingest_attempts_log_indexes(cur)
        conn.commit()
        return _get_collection_status_items_from_cursor(cur)


@router.get("/log", response_model=LogOverviewResponse, summary="収集ログ概要")
async def collect_log_overview():
    """収集ジョブの概要を JSON で返します。

    - **status_items**: 県ごとの収集状況（最新時刻・連続データ期間・ingest_attempts 由来の log_status 等）
    - **collect_log**: `ingest_attempts` から生成したログテキスト（データなしの場合は null）

    データ欠損の原因調査や運用監視に使用します。
    """
    collect_text: str | None = None
    try:
        with connect_db() as conn:
            cur = conn.cursor()
            _ensure_measurements_log_indexes(cur)
            _ensure_ingest_attempts_log_indexes(cur)
            conn.commit()
            status_items = _get_collection_status_items_from_cursor(cur)
            collect_text = _build_ingest_log_text(cur)
    except sqlite3.Error:
        status_items = _get_collection_status_items()
        collect_text = None

    return LogOverviewResponse(status_items=status_items, collect_log=collect_text)


@router.get(
    "/log/prefectures/{pref_id}/history",
    response_model=PrefLogHistoryResponse,
    summary="県別収集履歴",
)
async def collect_log_pref_history(
    pref_id: str,
    days: int = Query(30, ge=1, le=90, description="表示する過去日数（1〜90）"),
    end: str | None = Query(None, description="対象終了時刻（ISO 8601）。省略時は現在時刻の正時"),
):
    """指定都道府県の過去収集履歴を、日×24 時間の充足表として返します。

    各セルの status:
    - ok: いずれかの測定項目に値がある
    - empty: measurements 行はあるが全項目欠測（再取得で埋まる可能性あり）
    - missing: 行なし

    `summary.coverage_ratio` は usable（ok）のみの充足率です。
    """
    if pref_id not in PREF_ID_TO_NAME:
        raise HTTPException(status_code=404, detail=f"未知の都道府県 ID: {pref_id}")

    end_hour_utc = _parse_end_hour_utc(end)
    start_hour_utc = end_hour_utc - datetime.timedelta(hours=(days * 24 - 1))

    with connect_db() as conn:
        cur = conn.cursor()
        slot_status = _load_measurement_slot_status(
            cur,
            prefecture=pref_id,
            start_hour_utc=start_hour_utc,
            end_hour_utc=end_hour_utc,
        )
        oldest_continuous_iso, _ = _oldest_continuous_target(cur, pref_id)

    rows: List[PrefHistoryDayRow] = []
    total_slots = days * 24
    ok_slots = 0
    empty_slots = 0
    missing_slots = 0

    # 新しい日付から順に返す（ダッシュボードの最新監視に合わせる）
    for day_offset in range(days):
        day_jst = (end_hour_utc.astimezone(JST).date() - datetime.timedelta(days=day_offset))
        cells: List[PrefHistoryCell] = []
        day_ok = 0
        day_empty = 0
        day_missing = 0
        for hour in range(24):
            dt_jst = datetime.datetime.combine(day_jst, datetime.time(hour=hour, tzinfo=JST))
            dt_utc = dt_jst.astimezone(UTC)
            cell_status = slot_status.get(dt_utc, "missing")
            if cell_status == "ok":
                day_ok += 1
            elif cell_status == "empty":
                day_empty += 1
            else:
                day_missing += 1
            cells.append(
                PrefHistoryCell(
                    hour=hour,
                    has_data=cell_status == "ok",
                    status=cell_status,
                )
            )
        ok_slots += day_ok
        empty_slots += day_empty
        missing_slots += day_missing
        rows.append(
            PrefHistoryDayRow(
                date=day_jst.isoformat(),
                cells=cells,
                ok_count=day_ok,
                empty_count=day_empty,
                missing_count=day_missing,
            )
        )

    coverage_ratio = (ok_slots / total_slots) if total_slots > 0 else 0.0

    return PrefLogHistoryResponse(
        pref_id=pref_id,
        name_ja=PREF_ID_TO_NAME.get(pref_id, pref_id),
        days=days,
        start_datetime=start_hour_utc.astimezone(JST).isoformat(),
        end_datetime=end_hour_utc.astimezone(JST).isoformat(),
        rows=rows,
        summary=PrefHistorySummary(
            total_slots=total_slots,
            ok_slots=ok_slots,
            empty_slots=empty_slots,
            missing_slots=missing_slots,
            coverage_ratio=round(coverage_ratio, 4),
            oldest_continuous_datetime=oldest_continuous_iso,
        ),
    )


@router.get("/meta/ai-docs", response_class=Response, summary="AI クライアント向けガイド")
async def ai_docs():
    """LLM / AI クライアント向けの利用ガイド（`docs/ai-clients.md`）を Markdown として返します。

    ChatGPT や Cursor などが API を呼び出す際の参照用ドキュメントです。
    """
    if not AI_DOC_PATH.exists():
        raise HTTPException(status_code=404, detail="ai-clients.md not found")
    try:
        content = AI_DOC_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read ai-clients.md: {e}")
    return Response(content=content, media_type="text/markdown; charset=utf-8")
