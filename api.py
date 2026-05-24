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
from fastapi_mcp import FastApiMCP

import logging

from routers.v1 import router as v1_router
from routers.grid import router as v1_grid_router
from routers.amedas import router as v1_amedas_router

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIST = ROOT / "dashboard" / "dist"
with open(ROOT / "pyproject.toml", "rb") as f:
    project = tomllib.load(f)

APP_DESCRIPTION = """
airpollutionwatch は、日本全国の都道府県が公開している大気汚染データを
1時間ごとに収集し、共通スキーマで提供するための Web API です。

- **リポジトリ**: https://github.com/vitroid/airpollutionwatch-api

## API バージョン

- **v1** (`/v1/...`): 現行のエンドポイント（都道府県・測定局・測定データ・収集ログなど）
- **v1/grid** (`/v1/grid/...`): グリッドAPI（空間補間による地理院タイル単位の測定値）

## 主なエンドポイント（v1）

- `GET /v1/prefectures` — 都道府県一覧（id・日本語名・has_data・region）
- `GET /v1/measurements` — 局（または県）・期間で測定データ。`format=series` / `format=snapshot`
- `GET /v1/stations` — 測定局メタデータ一覧。`pref`・`has` で絞り込み
- `GET /v1/stations/{station_id}` — 局詳細
- `GET /v1/latest` — 指定局または県内の直近最新値
- `GET /v1/coverage` — 県別の連続データ期間（HTML）
- `GET /v1/log` — 収集ジョブログの概要を **JSON** で返す（県別巡回状況 `status_items` と `collect_log` 本文）

## グリッドエンドポイント（v1/grid）

- `GET /v1/grid/info` — メタ情報・キャッシュ状況
- `GET /v1/grid/snapshot` — 指定タイル群・1時刻の補間値（z, tiles, pollutants, datetime）
- `GET /v1/grid/field` — bbox 内全タイルの補間値・地図描画用（z, item/pollutant/items/pollutants, datetime, bbox）
  - 複数項目はカンマ区切りで指定可能（例: `items=ox,pm25,no2`）
  - レスポンスは `items` と `fields`（項目ごとの2次元配列）を返却
  - 完成 JSON は grid_response_cache.sqlite3 に最大7日保持

## 利用の流れ

1. `/v1/prefectures` で都道府県一覧を確認
2. `/v1/measurements?pref=...&from=...&to=...` で測定データを取得
3. 測定項目の意味・単位はレスポンスの `spec` を参照
"""

app = FastAPI(
    title="airpollutionwatch API",
    description=APP_DESCRIPTION,
    version=project.get("project", {}).get("version", "0.0.0"),
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

mcp = FastApiMCP(app)
mcp.mount()

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
