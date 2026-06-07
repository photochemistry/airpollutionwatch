"""
airpollutionwatch API エントリポイント。
v1 エンドポイントは routers/v1 に定義し、/v1 にマウントしています。
同一ポートで dashboard（Svelte）を配信するため、/ に静的ファイルをマウントし、
API に該当しないパスは SPA 用に index.html を返します。

空間補間グリッド（/v1/grid, /v1/amedas）は別プロジェクト airpollutionwatch-grid（ポート 8090）で提供します。
"""
from pathlib import Path
import tomllib

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from routers.v1 import router as v1_router
from routers.internal_ingest import router as internal_ingest_router

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIST = ROOT / "dashboard" / "dist"
with open(ROOT / "pyproject.toml", "rb") as f:
    project = tomllib.load(f)

APP_DESCRIPTION = """
日本全国の都道府県が公開する大気環境データ（1 時間ごと）を収集し、共通スキーマ（そらまめ互換）で提供する Web API です。

- **リポジトリ**: https://github.com/photochemistry/airpollutionwatch
- **簡易ガイド**: リポジトリ内 `README.md`
- **グリッド API**: 別サービス [airpollutionwatch-grid](../airpollutionwatch-grid)（ポート 8090）

## v1 — 基本 API

| メソッド | パス | 概要 |
|----------|------|------|
| GET | `/v1/prefectures` | 都道府県一覧（id・日本語名・has_data・region） |
| GET | `/v1/stations` | 測定局メタデータ一覧（`pref`・`has` で絞り込み） |
| GET | `/v1/stations/{station_id}` | 測定局詳細（住所・観測項目の有無など） |
| GET | `/v1/measurements` | 局または県・期間の測定データ（`format=series` / `snapshot`） |
| GET | `/v1/latest` | 局または県の直近最新値 |
| GET | `/v1/log` | 収集ジョブログ概要（県別 `status_items` + `ingest_attempts` 由来の `collect_log`） |
| GET | `/v1/log/prefectures/{pref_id}/history` | 指定県の収集履歴（日×24 時間: 〇/△/×） |
| GET | `/v1/geojson/outline/{pref_id}` | 都道府県輪郭（地図表示用 GeoJSON rings） |
| GET | `/v1/meta/ai-docs` | LLM 向け利用ガイド（Markdown） |

## 典型的な利用の流れ

1. `/v1/prefectures` で都道府県 ID を確認
2. `/v1/stations?pref=...` で局一覧を取得
3. `/v1/measurements?pref=...&from=...&to=...&items=pm25` で時系列を取得
4. 収集状況の確認には `/v1/log` の `status_items` を参照
"""

OPENAPI_TAGS = [
    {
        "name": "v1",
        "description": "都道府県・測定局・観測値・収集ログなど、大気汚染データの基本 API（`/v1/...`）",
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
app.include_router(internal_ingest_router)

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
