"""
airpollutionwatch API エントリポイント。
v1 エンドポイントは api_v1 に定義し、/v1 にマウントしています。
"""
from pathlib import Path
import tomllib

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi_mcp import FastApiMCP

from api_v1 import router as v1_router

ROOT = Path(__file__).resolve().parent
with open(ROOT / "pyproject.toml", "rb") as f:
    project = tomllib.load(f)

APP_DESCRIPTION = """
airpollutionwatch は、日本全国の都道府県が公開している大気汚染データを
1時間ごとに収集し、共通スキーマで提供するための Web API です。

## API バージョン

- **v1** (`/v1/...`): 現行のエンドポイント（都道府県・測定局・測定データ・収集ログなど）

## 主なエンドポイント（v1）

- `GET /v1/prefectures` — 都道府県一覧（id・日本語名・has_data・region）
- `GET /v1/measurements` — 局（または県）・期間で測定データ。`format=series` / `format=snapshot`
- `GET /v1/stations` — 測定局メタデータ一覧。`pref`・`has` で絞り込み
- `GET /v1/stations/{station_id}` — 局詳細
- `GET /v1/latest` — 指定局または県内の直近最新値
- `GET /v1/coverage` — 県別の連続データ期間（HTML）
- `GET /v1/collect.log` / `GET /v1/log` — 収集ジョブログ（テキスト／HTML）

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


if __name__ == "__main__":
    from logging import basicConfig, DEBUG
    basicConfig(level=DEBUG)
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8089,
        reload=True,
    )
