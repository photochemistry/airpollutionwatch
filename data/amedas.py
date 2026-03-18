"""アメダスデータ取得モジュール.

JMA の bosai/amedas API から観測データを取得し、内挿に使いやすい DataFrame に変換する。

キャッシュ戦略:
  - 過去の正時データ（現在の正時より前）: 取得後は永続キャッシュ（データは変化しない）
  - 現在の正時データ: TTL = 1 時間（DATA_CACHE_TTL_CURRENT）でキャッシュ

風ベクトルの変換:
  JMA の ws（風速 m/s）と wd（風向 1-16、北=16）から wx・wy を計算する。
    θ = (wd % 16) * 22.5°  （気象学的方位、北=0°、時計回り、FROM方向）
    wx = -ws * sin(θ)       （東西成分、正=東向き）
    wy = -ws * cos(θ)       （南北成分、正=北向き）
  wd=0（静穏）は wx=wy=0 として扱う。
  wx・wy は連続スカラー量なので空間補間が正しく機能する。
"""

from __future__ import annotations

import datetime
import io
import logging
import time

import httpx
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# APIパラメータ名 → (JSONキー名, スケール係数)
# JMA JSON では気温は 0.1℃ 単位で格納されている
AMEDAS_COLUMN_MAP: dict[str, tuple[str, float]] = {
    "temp": ("temp", 0.1),          # 気温 [℃]
    "hum":  ("humidity", 1.0),      # 湿度 [%]
    "ws":   ("wind", 1.0),          # 風速 [m/s]
    "wd":   ("windDirection", 1.0), # 風向 [1-16方位、北=16]
}

# ws・wd は内部計算用。外部に公開するのは wx・wy（補間可能な風ベクトル成分）と temp・hum
AVAILABLE_ITEMS = ["temp", "hum", "wx", "wy"]

_AMEDAS_TABLE_CACHE: pd.DataFrame | None = None
# {dt_hour_iso: (fetch_monotonic_time | None, DataFrame)}
# fetch_monotonic_time が None の場合は永続キャッシュ（過去データ）
_DATA_CACHE: dict[str, tuple[float | None, pd.DataFrame]] = {}
DATA_CACHE_TTL_CURRENT = 3600  # 直近正時データの再取得間隔: 1時間


def _fetch_amedas_table() -> pd.DataFrame:
    """アメダス測定局テーブルを取得してキャッシュ（プロセス起動中は再取得しない）。"""
    global _AMEDAS_TABLE_CACHE
    if _AMEDAS_TABLE_CACHE is not None:
        return _AMEDAS_TABLE_CACHE

    with httpx.Client(timeout=30.0) as client:
        resp = client.get("https://www.jma.go.jp/bosai/amedas/const/amedastable.json")
        resp.raise_for_status()

    table = pd.read_json(io.StringIO(resp.text), orient="index")
    # 度分表記 [度, 分] → 十進度
    table["lon"] = [x[0] + x[1] / 60 for x in table["lon"]]
    table["lat"] = [x[0] + x[1] / 60 for x in table["lat"]]
    _AMEDAS_TABLE_CACHE = table[["lon", "lat"]]
    logger.info("amedas table loaded: %d stations", len(_AMEDAS_TABLE_CACHE))
    return _AMEDAS_TABLE_CACHE


def fetch_amedas_df(dt_hour_iso: str) -> pd.DataFrame:
    """
    指定時刻（正時）のアメダス観測データを DataFrame で返す。

    Parameters
    ----------
    dt_hour_iso : str
        ISO 8601 形式の正時（例: "2026-03-18T09:00:00+09:00"）

    Returns
    -------
    DataFrame
        index: アメダス局コード
        columns: lon, lat, temp[℃], hum[%], wx[m/s 東西], wy[m/s 南北]
        各カラムは測定していない局が NaN になる場合がある
    """
    now = time.monotonic()
    if dt_hour_iso in _DATA_CACHE:
        cached_at, df = _DATA_CACHE[dt_hour_iso]
        if cached_at is None:
            logger.info("amedas cache hit (permanent): %s", dt_hour_iso)
            return df
        if now - cached_at < DATA_CACHE_TTL_CURRENT:
            age_s = int(now - cached_at)
            logger.info("amedas cache hit (age=%ds): %s", age_s, dt_hour_iso)
            return df

    dt = datetime.datetime.fromisoformat(dt_hour_iso)
    date_time = dt.strftime("%Y%m%d%H0000")

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"https://www.jma.go.jp/bosai/amedas/data/map/{date_time}.json"
        )
        resp.raise_for_status()

    raw = pd.read_json(io.StringIO(resp.text), orient="index")
    table = _fetch_amedas_table()

    # 測定局テーブルと結合（lat/lon のない局は除外）
    df = raw.join(table, how="inner")

    result_cols: dict[str, pd.Series] = {
        "lon": df["lon"].astype(float),
        "lat": df["lat"].astype(float),
    }

    for param, (json_key, scale) in AMEDAS_COLUMN_MAP.items():
        if json_key not in df.columns:
            continue
        vals: list[float] = []
        for v in df[json_key]:
            if isinstance(v, list) and len(v) >= 1:
                # [値, フラグ] 形式 — 値だけ取り出す
                vals.append(float(v[0]) if v[0] is not None else np.nan)
            elif isinstance(v, (int, float)):
                vals.append(float(v))
            else:
                vals.append(np.nan)
        series = pd.Series(vals, index=df.index, dtype=float)
        if scale != 1.0:
            series = series * scale
        result_cols[param] = series

    result = pd.DataFrame(result_cols).dropna(subset=["lon", "lat"])

    # ws・wd → wx・wy 変換（wd=0 は静穏として wx=wy=0）
    if "ws" in result.columns and "wd" in result.columns:
        wd = result["wd"].to_numpy(dtype=float)
        ws = result["ws"].to_numpy(dtype=float)
        theta_rad = np.where(wd == 0, 0.0, (wd % 16) * 22.5 * np.pi / 180.0)
        calm = wd == 0
        result["wx"] = np.where(calm, 0.0, -ws * np.sin(theta_rad))
        result["wy"] = np.where(calm, 0.0, -ws * np.cos(theta_rad))
    result = result.drop(columns=[c for c in ("ws", "wd") if c in result.columns])

    current_hour = datetime.datetime.now(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    is_current_hour = dt.astimezone(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) >= current_hour
    _DATA_CACHE[dt_hour_iso] = (now if is_current_hour else None, result)
    logger.info(
        "amedas fetched: %s  stations=%d  cache=%s",
        dt_hour_iso,
        len(result),
        "ttl=1h" if is_current_hour else "permanent",
    )
    return result
