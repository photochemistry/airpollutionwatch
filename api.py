"""
airpollutionwatch API エントリポイント。
v1 エンドポイントは api_v1 に定義し、/v1 にマウントしています。
同一ポートで dashboard（Svelte）を配信するため、/ に静的ファイルをマウントし、
API に該当しないパスは SPA 用に index.html を返します。
"""
from pathlib import Path
import tomllib

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import logging

from routers.v1 import router as v1_router
from routers.grid import router as v1_grid_router
from routers.amedas import router as v1_amedas_router
from routers.grid import ensure_grid_db_indexes

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIST = ROOT / "dashboard" / "dist"
with open(ROOT / "pyproject.toml", "rb") as f:
    project = tomllib.load(f)

APP_DESCRIPTION = """
日本全国の都道府県が公開する大気環境データ（1 時間ごと）を収集し、共通スキーマ（そらまめ互換）で提供する Web API です。

- **リポジトリ**: https://github.com/vitroid/airpollutionwatch-api
- **簡易ガイド**: リポジトリ内 `README.md`

## タグ（API グループ）

| タグ | プレフィックス | 内容 |
|------|----------------|------|
| **v1** | `/v1/...` | 都道府県・測定局・観測値・収集ログなど基本 API |
| **grid** | `/v1/grid/...` | 大気測定局データの空間補間グリッド（地理院タイル座標） |
| **amedas** | `/v1/amedas` | JMA アメダス気象データの空間補間グリッド |

## v1 — 基本 API

| メソッド | パス | 概要 |
|----------|------|------|
| GET | `/v1/prefectures` | 都道府県一覧（id・日本語名・has_data・region） |
| GET | `/v1/stations` | 測定局メタデータ一覧（`pref`・`has` で絞り込み） |
| GET | `/v1/stations/{station_id}` | 測定局詳細（住所・観測項目の有無など） |
| GET | `/v1/measurements` | 局または県・期間の測定データ（`format=series` / `snapshot`） |
| GET | `/v1/latest` | 局または県の直近最新値 |
| GET | `/v1/log` | 収集ジョブログ概要（県別 `status_items` + `collect_log` 全文） |
| GET | `/v1/log/prefectures/{pref_id}/history` | 指定県の収集履歴（日×24 時間の充足表） |
| GET | `/v1/geojson/outline/{pref_id}` | 都道府県輪郭（地図表示用 GeoJSON rings） |
| GET | `/v1/meta/ai-docs` | LLM 向け利用ガイド（Markdown） |

## grid — 大気汚染グリッド API

観測局の点データを空間補間し、地理院タイル座標系の 2 次元配列で返します。地図への色塗り・ヒートマップ描画向け。

| メソッド | パス | 概要 |
|----------|------|------|
| GET | `/v1/grid/info` | 利用可能なズーム・補間法・キャッシュ状況 |
| GET | `/v1/grid/snapshot` | 指定タイル座標リスト・1 時刻の補間値 |
| GET | `/v1/grid/field` | bbox 内・1 時刻の補間グリッド（地図描画の主用途） |
| GET | `/v1/grid/range` | bbox 内・複数時刻の補間グリッド（最大 72 時間） |

**主なクエリ**: `z`（ズーム 0〜13）, `datetime`, `items`（例: `ox,pm25,no2`）, `bbox`, `method`（atps / tps / linear / idw / nnatural）

## amedas — 気象グリッド API

JMA アメダス観測データを `/v1/grid` と同様のタイル座標系で返します。大気データとは独立したデータソースです。

| メソッド | パス | 概要 |
|----------|------|------|
| GET | `/v1/amedas` | bbox 内の気象補間グリッド（temp / hum / wx / wy など） |

## 典型的な利用の流れ

1. `/v1/prefectures` で都道府県 ID を確認
2. `/v1/stations?pref=...` で局一覧を取得
3. `/v1/measurements?pref=...&from=...&to=...&items=pm25` で時系列を取得
4. 地図表示には `/v1/grid/field?z=12&datetime=...&items=ox&bbox=...` を使用
5. 収集状況の確認には `/v1/log` の `status_items` を参照
"""

OPENAPI_TAGS = [
    {
        "name": "v1",
        "description": "都道府県・測定局・観測値・収集ログなど、大気汚染データの基本 API（`/v1/...`）",
    },
    {
        "name": "grid",
        "description": "大気測定局データの空間補間グリッド。地理院タイル座標系で 2 次元配列を返す（`/v1/grid/...`）",
    },
    {
        "name": "amedas",
        "description": "JMA アメダス気象データの空間補間グリッド。大気 grid API とは独立（`/v1/amedas`）",
    },
]

app = FastAPI(
    title="airpollutionwatch API",
    description=APP_DESCRIPTION,
    version=project.get("project", {}).get("version", "0.0.0"),
    openapi_tags=OPENAPI_TAGS,
)

# favicon.ico（漢字「氣」）を返すエンドポイント
FAVICON_PATH = Path("/home/ubuntu/.cursor/projects/home-ubuntu-github-airpollutionwatch/assets/favicon-ki.png")
APPLE_TOUCH_ICON_PATH = Path("/home/ubuntu/.cursor/projects/home-ubuntu-github-airpollutionwatch/assets/apple-touch-icon.png")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(FAVICON_PATH)


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    return FileResponse(APPLE_TOUCH_ICON_PATH)


# CORS: API を呼び出す「ページ」のオリジン（スキーム+ホスト+ポート）。フロントを置くホストを列挙する。
origins = [
    "*",
    "https://andersan.net",
    "https://andersan.net:8089",
    "http://localhost:8089",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router)
app.include_router(v1_grid_router)
app.include_router(v1_amedas_router)

# dashboard（Svelte ビルド）を同一ポートで配信
if DASHBOARD_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DASHBOARD_DIST / "assets"), name="dashboard_assets")

    @app.get("/{full_path:path}")
    async def serve_dashboard(request: Request, full_path: str):
        """API 以外のパスは静的ファイルまたは SPA 用 index.html を返す"""
        if full_path.startswith("v1/") or full_path == "v1":
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path.startswith("assets/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Not Found")
        file_path = DASHBOARD_DIST / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        index_path = DASHBOARD_DIST / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Dashboard not built. Run: cd dashboard && npm run build")


def _patch_uvicorn_invalid_request_logging() -> None:
    """
    uvicorn が「Invalid HTTP request received.」をログする際に、
    例外の詳細（exc_info）も出すようにパッチする。
    プロトコルパーサーエラーの原因（不正なリクエスト行・ボディの混入など）を確認しやすくする。
    """
    try:
        import httptools
        import uvicorn.protocols.http.httptools_impl as httptools_impl
    except ImportError:
        pass
    else:

        def _data_received_httptools(self, data: bytes) -> None:
            self._unset_keepalive_if_required()
            try:
                self.parser.feed_data(data)
            except httptools.HttpParserError as e:
                msg = "Invalid HTTP request received."
                self.logger.warning("%s %s", msg, e, exc_info=True)
                self.send_400_response(msg)
                return
            except httptools.HttpParserUpgrade:
                if self._should_upgrade():
                    self.handle_websocket_upgrade()
                else:
                    self._unsupported_upgrade_warning()

        httptools_impl.HttpToolsProtocol.data_received = _data_received_httptools

    # h11_impl は handle_events が長いためここではパッチしない。
    # 標準的には httptools が使われるため、上記の httptools パッチで多くの場合詳細が出力される。


@app.on_event("startup")
async def _setup_logging() -> None:
    """uvicorn の logging 設定完了後に各モジュールのロガーを接続する."""
    ensure_grid_db_indexes()
    uvicorn_handlers = logging.getLogger("uvicorn").handlers
    for name in ("routers.grid", "routers.amedas", "data.amedas"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            for handler in uvicorn_handlers:
                lg.addHandler(handler)
            lg.propagate = False


# uvicorn が app をロードする前にプロトコルをパッチするため、uvicorn.run の直前に実行する
# （__main__ で run する場合）。reload 時は子プロセスで app が再読込されるので、
# モジュール読み込み時にパッチを当てておく。
_patch_uvicorn_invalid_request_logging()


if __name__ == "__main__":
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8089,
        reload=True,
    )
