"""
API v1: 都道府県・測定局・測定データ・収集ログなどのエンドポイント。
"""
import datetime
import sqlite3
from pathlib import Path
from typing import List, Union, Literal, Dict, Any

import pandas as pd
import numpy as np

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict

from airpollutionwatch import prefecture_retrievers, ITEMSPECS
from stations_loader import get_stations_df, PREF_NAME_TO_ID

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

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "airpollutionwatch.sqlite3"
COLLECT_LOG_PATH = ROOT / "collect.log"
AI_DOC_PATH = ROOT / "docs" / "ai-clients.md"

router = APIRouter(prefix="/v1", tags=["v1"])

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


POLLUTANT_PARAM_TO_COL = {
    "so2": "SO2", "no": "NO", "no2": "NO2", "nox": "NOX", "ox": "OX",
    "spm": "SPM", "pm25": "PM25", "co": "CO", "nmhc": "NMHC", "ch4": "CH4",
    "thc": "THC", "wd": "WD", "ws": "WS", "temp": "TEMP", "hum": "HUM",
}


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
    pollutant: str = Field(..., description="測定項目名 (PM25, OX など)")
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

@router.get("/prefectures", response_model=List[PrefectureInfo])
async def prefectures():
    """都道府県一覧を返す。全 47 都道府県について、ID・日本語名・データ取得可否・地域ブロック。"""
    has_data_set = set(prefecture_retrievers.keys())
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


@router.get("/stations", response_model=List[StationListItem])
async def list_stations(
    pref: str | None = None,
    has: str | None = None,
):
    """測定局メタデータの一覧。クエリ `pref`・`has`（例: pm25,ox）で絞り込み可能。"""
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


@router.get("/stations/{station_id}", response_model=StationDetail)
async def get_station(station_id: str):
    """指定した測定局の詳細情報。"""
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
    responses={200: {"description": "format=series のときは時系列リスト、format=snapshot のときは局単位のスナップショット"}},
)
async def get_measurements(
    station_ids: str | None = None,
    pref: str | None = None,
    from_: datetime.datetime = Query(..., alias="from"),
    to: datetime.datetime = Query(...),
    pollutants: str = Query("pm25,ox,no2"),
    interval: str = Query("1h"),
    format: Literal["series", "snapshot"] = Query("series", description="series=時系列 / snapshot=1時刻・局単位（from=to の場合のみ）"),
):
    """局（または県）・期間を指定して測定データ。format で時系列／スナップショットを切り替え。"""
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

    if interval not in ("raw", "1h"):
        raise HTTPException(status_code=400, detail="interval は raw または 1h のみ対応しています。")

    cols = []
    for p in pollutants.lower().replace(" ", "").split(","):
        p = p.strip()
        if p and p in POLLUTANT_PARAM_TO_COL:
            cols.append(POLLUTANT_PARAM_TO_COL[p])
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
        with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
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
            pollutant=col,
            values=[TimeSeriesPoint(datetime=dt, value=v) for dt, v in points],
        ))
    return TimeSeriesResponse(timeseries=out)


@router.get("/latest", response_model=LatestResponse)
async def get_latest(
    station_ids: str | None = None,
    pref: str | None = None,
    pollutants: str = "pm25,ox,no2",
):
    """指定した局（または都道府県内の全局）の直近の最新値。"""
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
    for p in pollutants.lower().replace(" ", "").split(","):
        p = p.strip()
        if p and p in POLLUTANT_PARAM_TO_COL:
            cols.append(POLLUTANT_PARAM_TO_COL[p])
    if not cols:
        cols = ["PM25", "OX", "NO2"]

    placeholders = ",".join("?" * len(codes))
    col_list = ", ".join(["station_code", "target_datetime", "observed_datetime"] + cols)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"SELECT MAX(target_datetime) AS latest FROM measurements WHERE station_code IN ({placeholders})", tuple(codes))
        row = cur.fetchone()
        latest = row["latest"] if row and row["latest"] else None
    if not latest:
        return LatestResponse(datetime="", stations=[])

    with sqlite3.connect(DB_PATH) as conn:
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


@router.get("/coverage", response_class=Response)
async def coverage():
    """県ごとに「どこまで過去にさかのぼって連続データがあるか」を HTML テーブルで返す。"""
    now = datetime.datetime.now().astimezone()
    base_hour = now.replace(minute=0, second=0, microsecond=0)
    rows: list[tuple[str, str, str]] = []

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        for pref in sorted(prefecture_retrievers.keys()):
            cur.execute("SELECT DISTINCT target_datetime FROM measurements WHERE prefecture = ?", (pref,))
            result = [r[0] for r in cur.fetchall()]
            if not result:
                rows.append((pref, "データなし", "—"))
                continue
            dts = sorted(datetime.datetime.fromisoformat(s) for s in result)
            dt_set = set(dts)
            latest = max(dts)
            cur_dt = latest
            oldest = latest
            one_hour = datetime.timedelta(hours=1)
            while cur_dt in dt_set:
                oldest = cur_dt
                cur_dt -= one_hour
            delta_days = (base_hour - oldest).total_seconds() / 86400.0
            rows.append((pref, oldest.isoformat(), f"{delta_days:.1f} 日前"))

    html_parts = [
        "<!DOCTYPE html>", "<html lang='ja'>", "<head>", "  <meta charset='utf-8'>",
        "  <title>県別データ連続期間</title>",
        "  <style>body { font-family: system-ui, sans-serif; margin: 1.5rem; } table { border-collapse: collapse; width: 100%; max-width: 960px; } th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; font-size: 0.9rem; } th { background: #f0f0f0; } tbody tr:nth-child(odd) { background: #fafafa; }</style>",
        "</head>", "<body>",
        f"  <h1>県別の連続取得開始時刻（{base_hour.isoformat()} 時点）</h1>",
        "  <table>", "    <thead>", "      <tr><th>県</th><th>連続区間の最古の target_datetime</th><th>現在からの距離</th></tr>", "    </thead>", "    <tbody>",
    ]
    for pref, oldest_iso, days_str in rows:
        html_parts.append(f"      <tr><td>{pref}</td><td>{oldest_iso}</td><td>{days_str}</td></tr>")
    html_parts += ["    </tbody>", "  </table>", "</body>", "</html>"]
    return Response(content="\n".join(html_parts), media_type="text/html; charset=utf-8")


@router.get("/collect.log", response_class=Response)
async def collect_log():
    """収集ジョブのログファイル collect.log の内容をプレーンテキストで返す。"""
    if not COLLECT_LOG_PATH.exists():
        raise HTTPException(status_code=404, detail="collect.log not found")
    try:
        content = COLLECT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read collect.log: {e}")
    return Response(content=content, media_type="text/plain; charset=utf-8")


COLLECT_LOG_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>collect.log</title>
  <style>
    body { font-family: monospace; margin: 1rem; background: #1e1e1e; color: #d4d4d4; }
    #meta { margin-bottom: 0.5rem; font-size: 0.9rem; color: #858585; }
    #log { white-space: pre-wrap; word-break: break-all; }
  </style>
</head>
<body>
  <div id="meta">collect.log — <span id="updated">読み込み中…</span>（5分ごとに自動更新）</div>
  <pre id="log"></pre>
  <script>
    const PRELOAD_MINUTES = 5;
    function load() {
      fetch('collect.log')
        .then(r => { if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(t => {
          document.getElementById('log').textContent = t;
          document.getElementById('updated').textContent = '最終更新: ' + new Date().toLocaleString('ja-JP');
        })
        .catch(e => {
          document.getElementById('log').textContent = '取得できませんでした: ' + e.message;
          document.getElementById('updated').textContent = 'エラー';
        });
    }
    load();
    setInterval(load, PRELOAD_MINUTES * 60 * 1000);
  </script>
</body>
</html>
"""


@router.get("/log", response_class=Response)
async def collect_log_view():
    """collect.log をブラウザで閲覧するための簡易 HTML ビュー。5 分ごとに自動再取得。"""
    return Response(content=COLLECT_LOG_HTML, media_type="text/html; charset=utf-8")


@router.get("/meta/ai-docs", response_class=Response)
async def ai_docs():
    """LLM / AI クライアント向けガイド (docs/ai-clients.md) を Markdown として返す。"""
    if not AI_DOC_PATH.exists():
        raise HTTPException(status_code=404, detail="ai-clients.md not found")
    try:
        content = AI_DOC_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read ai-clients.md: {e}")
    return Response(content=content, media_type="text/markdown; charset=utf-8")
