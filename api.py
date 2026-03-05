import datetime
import os
import sqlite3
from pathlib import Path
from typing import List, Union, Literal, Dict, Any
from logging import getLogger, basicConfig, INFO, DEBUG
import pandas as pd
import numpy as np

import uvicorn
from fastapi import Depends, FastAPI, Request, status, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from fastapi_mcp import FastApiMCP
from airpollutionwatch import prefecture_retrievers, ITEMSPECS
import json
import tomllib

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "airpollutionwatch.sqlite3"
COLLECT_LOG_PATH = ROOT / "collect.log"

with open("pyproject.toml", "rb") as f:
    project = tomllib.load(f)

APP_DESCRIPTION = """
airpollutionwatch は、日本全国の都道府県が公開している大気汚染データを
1時間ごとに収集し、共通スキーマで提供するための Web API です。

## 主なエンドポイント

- `GET /prefectures`  
  取得可能な都道府県 ID の一覧（例: `"tokyo"`, `"kanagawa"`）を返します。

- `GET /items/{prefecture}/{datehour}`  
  指定した都道府県・時刻の大気環境データを返します。  
  - `prefecture`: 県 ID（`/prefectures` で確認できます）
  - `datehour`: ISO8601 形式の日時（例: `2024-09-03T06:00+09:00`）  
    分以下は切り捨てて正時にそろえられ、指定時刻にデータがない場合は
    1時間前までを自動でフォールバック検索します。

  レスポンスは次の2部から構成されます。
  - `data`: 各測定局ごとの値（`station_code` をキーにした辞書）
  - `spec`: 各測定項目の単位・型などのメタ情報

- `GET /coverage`  
  各都道府県ごとに「どこまで過去にさかのぼって連続データがあるか」を
  HTML テーブルで返します。

- `GET /collect.log` / `GET /log`  
  バックグラウンド収集ジョブのログをテキスト／HTML で表示します。

## 利用方法の概要

1. `/prefectures` で利用可能な都道府県 ID を確認します。
2. `/items/{prefecture}/{datehour}` に対して HTTP GET を投げて、
   指定時刻の大気環境データを取得します。
3. 測定項目の意味や単位は `spec` セクションを参照してください。
"""

app = FastAPI(
    title="airpollutionwatch API",
    description=APP_DESCRIPTION,
    version=project.get("project", {}).get("version", "0.0.0"),
)

origins = [
    "*",
    "http://localhost:8087",
    "http://172.23.78.207:8087",
    "http://192.168.3.234:8087",
    "http://172.23.78.44:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 今のところ、MCPには接続できるが応答がない。無限に待たされるようだ。

# Add MCP server to the FastAPI app
mcp = FastApiMCP(app)

# Mount the MCP server to the FastAPI app
mcp.mount()


# class StationFloatValue(BaseModel):
#     station: Union[str, int] = Field(..., description="測定局名")
#     value: Union[float, int] = Field(..., description="測定値")

# class StationIntValue(BaseModel):
#     station: Union[str, int] = Field(..., description="測定局名")
#     value: int = Field(..., description="測定値")

# class StationStringValue(BaseModel):
#     station: Union[str, int] = Field(..., description="測定局名")
#     value: Union[int, str] = Field(..., description="国環研コード")

# 測定項目の仕様を定義するモデル
class ItemSpecRec(BaseModel):
    name: str = Field(..., description="測定項目の名前")
    unit: str = Field(..., description="測定値の単位")
    description: str = Field(..., description="測定項目の説明")
    type: Literal["int", "float"] = Field(..., description="測定値の型")
    default: Union[int, float] = Field(..., description="デフォルト値")



# 測定局のデータを定義するモデル
class ItemData(BaseModel):
    station_code: Dict[Union[str, int], Union[str, int]] = Field(..., description="測定局名")
    observed_datetime: Dict[Union[str, int], str] = Field(default_factory=dict, description="各測定局ごとの観測時刻(ISO8601)")
    SO2: Dict[Union[str, int], float] = Field(default_factory=dict, description="二酸化硫黄")
    NO: Dict[Union[str, int], float] = Field(default_factory=dict, description="一酸化窒素")
    NO2: Dict[Union[str, int], float] = Field(default_factory=dict, description="二酸化窒素")
    NOX: Dict[Union[str, int], float] = Field(default_factory=dict, description="窒素酸化物")
    OX: Dict[Union[str, int], float] = Field(default_factory=dict, description="光化学オキシダント")
    SPM: Dict[Union[str, int], float] = Field(default_factory=dict, description="浮遊粒子状物質")
    PM25: Dict[Union[str, int], float] = Field(default_factory=dict, description="微小粒子状物質")
    NMHC: Dict[Union[str, int], float] = Field(default_factory=dict, description="非メタン炭化水素")
    CH4: Dict[Union[str, int], float] = Field(default_factory=dict, description="メタン")
    THC: Dict[Union[str, int], float] = Field(default_factory=dict, description="全炭化水素")
    CO: Dict[Union[str, int], float] = Field(default_factory=dict, description="一酸化炭素")
    WD: Dict[Union[str, int], int] = Field(default_factory=dict, description="風向")
    WS: Dict[Union[str, int], float] = Field(default_factory=dict, description="風速")
    TEMP: Dict[Union[str, int], float] = Field(default_factory=dict, description="気温")
    HUM: Dict[Union[str, int], float] = Field(default_factory=dict, description="湿度")

class ItemSpec(BaseModel):
    station_code: ItemSpecRec = Field(default_factory=dict, description="測定局の仕様")
    SO2: ItemSpecRec = Field(default_factory=dict, description="二酸化硫黄の仕様")
    NO: ItemSpecRec = Field(default_factory=dict, description="一酸化窒素の仕様")
    NO2: ItemSpecRec = Field(default_factory=dict, description="二酸化窒素の仕様")
    NOX: ItemSpecRec = Field(default_factory=dict, description="窒素酸化物の仕様")
    OX: ItemSpecRec = Field(default_factory=dict, description="光化学オキシダントの仕様")
    SPM: ItemSpecRec = Field(default_factory=dict, description="浮遊粒子状物質の仕様")
    PM25: ItemSpecRec = Field(default_factory=dict, description="微小粒子状物質の仕様")
    NMHC: ItemSpecRec = Field(default_factory=dict, description="非メタン炭化水素の仕様")
    CH4: ItemSpecRec = Field(default_factory=dict, description="メタンの仕様")
    THC: ItemSpecRec = Field(default_factory=dict, description="全炭化水素の仕様")
    CO: ItemSpecRec = Field(default_factory=dict, description="一酸化炭素の仕様")
    WD: ItemSpecRec = Field(default_factory=dict, description="風向の仕様")
    WS: ItemSpecRec = Field(default_factory=dict, description="風速の仕様")
    TEMP: ItemSpecRec = Field(default_factory=dict, description="気温の仕様")
    HUM: ItemSpecRec = Field(default_factory=dict, description="湿度の仕様")
    
# APIレスポンスを定義するモデル
class AirQualityResponse(BaseModel):
    data: ItemData = Field(..., description="測定データ")
    spec: ItemSpec = Field(..., description="測定項目ごとの仕様")


@app.get("/prefectures", response_model=List[str])
async def prefectures():
    """利用可能な都道府県 ID の一覧を返すエンドポイント。

    # 概要
    `airpollutionwatch` が対応している都道府県の ID（例: `tokyo`, `kanagawa`）を配列で返します。
    この ID は `GET /items/{prefecture}/{datehour}` の `prefecture` パスパラメータとして利用します。
    """
    return list(prefecture_retrievers.keys())


@app.get("/coverage", response_class=Response)
async def coverage():
    """県ごとに「現在まで連続な最も古い target_datetime が何日前か」を表示する簡易 HTML。

    # 概要
    - `measurements` テーブルから、各県の `target_datetime` を集計します。
    - もっとも新しい時刻から 1 時間ずつさかのぼり、連続してデータが存在する最古の時刻を求めます。
    - 現在の正時から見た日数差も計算し、HTML テーブルとして返します。

    ブラウザでアクセスすると、どの県の履歴がどこまで埋まっているかを一目で確認できます。
    """
    # 現在の正時（ローカルタイムゾーンベース）
    now = datetime.datetime.now().astimezone()
    base_hour = now.replace(minute=0, second=0, microsecond=0)

    rows: list[tuple[str, str, str]] = []

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        for pref in sorted(prefecture_retrievers.keys()):
            cur.execute(
                """
                SELECT DISTINCT target_datetime
                FROM measurements
                WHERE prefecture = ?
                """,
                (pref,),
            )
            result = [r[0] for r in cur.fetchall()]
            if not result:
                rows.append((pref, "データなし", "—"))
                continue

            dts = sorted(datetime.datetime.fromisoformat(s) for s in result)
            dt_set = set(dts)
            latest = max(dts)

            # latest から1時間ずつさかのぼり、連続して存在する最も古い時刻を探す
            cur_dt = latest
            oldest = latest
            one_hour = datetime.timedelta(hours=1)
            while cur_dt in dt_set:
                oldest = cur_dt
                cur_dt -= one_hour

            # 「現在の正時」から見て何日前か（少数日）を計算
            delta_days = (base_hour - oldest).total_seconds() / 86400.0
            days_str = f"{delta_days:.1f} 日前"
            rows.append((pref, oldest.isoformat(), days_str))

    # シンプルなHTMLテーブルで返す
    html_parts = [
        "<!DOCTYPE html>",
        "<html lang='ja'>",
        "<head>",
        "  <meta charset='utf-8'>",
        "  <title>県別データ連続期間</title>",
        "  <style>",
        "    body { font-family: system-ui, sans-serif; margin: 1.5rem; }",
        "    table { border-collapse: collapse; width: 100%; max-width: 960px; }",
        "    th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; font-size: 0.9rem; }",
        "    th { background: #f0f0f0; }",
        "    tbody tr:nth-child(odd) { background: #fafafa; }",
        "    caption { text-align: left; margin-bottom: 0.5rem; font-weight: bold; }",
        "  </style>",
        "</head>",
        "<body>",
        f"  <h1>県別の連続取得開始時刻（{base_hour.isoformat()} 時点）</h1>",
        "  <table>",
        "    <thead>",
        "      <tr><th>県</th><th>連続区間の最古の target_datetime</th><th>現在からの距離</th></tr>",
        "    </thead>",
        "    <tbody>",
    ]

    for pref, oldest_iso, days_str in rows:
        html_parts.append(
            f"      <tr><td>{pref}</td><td>{oldest_iso}</td><td>{days_str}</td></tr>"
        )

    html_parts += [
        "    </tbody>",
        "  </table>",
        "</body>",
        "</html>",
    ]

    html = "\n".join(html_parts)
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/collect.log", response_class=Response)
async def collect_log():
    """収集ジョブのログファイル `collect.log` の内容をプレーンテキストで返すエンドポイント。

    # 概要
    - `collect_hourly.py` の実行ログ（INFO / WARNING / ERROR）をそのままテキストとして返します。
    - cron などで 5 分ごとに収集ジョブを動かしている場合の成否確認に利用できます。
    """
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
      fetch('/collect.log')
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


@app.get("/log", response_class=Response)
async def collect_log_view():
    """`collect.log` をブラウザで閲覧するための簡易 HTML ビューを返すエンドポイント。

    # 概要
    - ページ読み込み時に `/collect.log` を取得して表示します。
    - JavaScript により 5 分ごとに自動で再取得し、最新ログに更新します。
    - ターミナルを開かずに収集状況をモニタリングしたい場合に便利です。
    """
    return Response(content=COLLECT_LOG_HTML, media_type="text/html; charset=utf-8")


@app.get("/items/{prefecture}/{datehour}", response_model=AirQualityResponse)
# @FastApiMCP()
async def raw_data(
    prefecture: Literal[tuple(prefecture_retrievers.keys())],
    datehour: datetime.datetime,
):
    """指定した都道府県・時刻の大気環境データ（1 時間値）を返すエンドポイント。

    # パスパラメータ
    - `prefecture`: 都道府県 ID（例: `tokyo`, `kanagawa`）。`/prefectures` で確認できる値のみ有効です。
    - `datehour`: ISO8601 形式の日時文字列（例: `2024-09-03T06:00+09:00`）。
      分以下は切り捨てて正時に丸められます。

    # 動作
    - 内部の SQLite (`measurements` テーブル) から、指定した `prefecture` のデータを検索します。
    - 検索順は「指定時刻 → 1 時間前」で、最初にヒットした `target_datetime` を採用します。
    - 見つからない場合は `404 Not Found` を返します。

    # レスポンス
    - `data`: 各測定局ごとの値（`station_code` と `observed_datetime` を含む辞書）。
    - `spec`: 測定項目ごとの単位・説明・型などのメタ情報（`ITEMSPECS` 由来）。
    """
    if prefecture not in prefecture_retrievers:
        raise HTTPException(status_code=404, detail="Out of the cover area")

    # 分以下は切り捨てて正時にそろえる
    datehour = datehour.replace(minute=0, second=0, microsecond=0)

    # 指定時刻 → 1時間前の順に、最初に見つかったほうを採用する
    candidate_hours = [
        datehour,
        datehour - datetime.timedelta(hours=1),
    ]

    df: pd.DataFrame | None = None
    with sqlite3.connect(DB_PATH) as conn:
        for dt in candidate_hours:
            target_iso = dt.isoformat()
            query = """
                SELECT
                    station_code,
                    observed_datetime,
                    SO2, NO, NO2, NOX, OX,
                    SPM, PM25, CO, NMHC, CH4, THC,
                    WD, WS, TEMP, HUM
                FROM measurements
                WHERE prefecture = ?
                  AND target_datetime = ?
            """
            tmp = pd.read_sql_query(
                query,
                conn,
                params=(prefecture, target_iso),
            )
            if not tmp.empty:
                df = tmp
                break

    # 指定された時刻・その1時間前のどちらにもデータがなければ何も返さない
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="Data not available.")

    data_dict: Dict[str, Dict[Union[str, int], Any]] = {}
    for key, value in df.to_dict().items():
        new_value = {}
        for k, v in value.items():
            if pd.notna(v):
                new_value[k] = v
        data_dict[key] = new_value

    # 測定項目の仕様を取得（ITEMSPECS に存在するキーのみ）
    specs = {key: ITEMSPECS[key] for key in data_dict if key in ITEMSPECS}

    result = dict(data=ItemData(**data_dict), spec=specs)
    return result

if __name__ == "__main__":
    import os


    basicConfig(level=DEBUG)
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"][
        "fmt"
    ] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["default"][
        "fmt"
    ] = "%(asctime)s - %(levelname)s - %(message)s"
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8089,
        reload=True,
    ) 