"""グリッド・タイル座標変換ユーティリティ（andersan-grid より移植）."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np


@dataclass
class BoundingBox:
    """緯度経度の矩形領域."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @classmethod
    def from_points(
        cls, lons: np.ndarray, lats: np.ndarray, margin: float = 0.0
    ) -> "BoundingBox":
        """観測点から自動的に bbox を決める."""
        min_lon = float(np.min(lons)) - margin
        max_lon = float(np.max(lons)) + margin
        min_lat = float(np.min(lats)) - margin
        max_lat = float(np.max(lats)) + margin
        return cls(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)


def _webmercator_lonlat_to_tile_xy(
    lon: float, lat: float, zoom: int
) -> Tuple[float, float]:
    """地理院タイル（Web メルカトル）の連続タイル座標 (x, y) を返す."""
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = np.deg2rad(lat)
    y = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / np.pi) / 2.0 * n
    return x, y


def _webmercator_tile_xy_to_lonlat(
    x: float, y: float, zoom: int
) -> Tuple[float, float]:
    """地理院タイル（Web メルカトル）のタイル座標 (x, y) からタイル中心の緯度経度を返す."""
    n = 2.0**zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = np.arctan(np.sinh(np.pi * (1.0 - 2.0 * y / n)))
    lat = np.rad2deg(lat_rad)
    return lon, lat


def make_lonlat_grid_tiles(
    bbox: BoundingBox,
    zoom: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    地理院タイル（Web メルカトル）のタイル中心位置に対応する 2D グリッドを生成する.

    与えられた bbox を含むタイル範囲を計算し、そのタイル中心の緯度経度を格子として用いる。

    Returns
    -------
    lon2d, lat2d:
        shape (ny, nx) の 2D 配列。行は南→北（lat 昇順）、列は西→東（lon 昇順）。
    """
    if zoom < 0:
        raise ValueError("zoom は 0 以上の整数である必要があります。")

    x_min_f, y_max_f = _webmercator_lonlat_to_tile_xy(bbox.min_lon, bbox.min_lat, zoom)
    x_max_f, y_min_f = _webmercator_lonlat_to_tile_xy(bbox.max_lon, bbox.max_lat, zoom)

    x_min = int(np.floor(min(x_min_f, x_max_f)))
    x_max = int(np.floor(max(x_min_f, x_max_f)))
    y_min = int(np.floor(min(y_min_f, y_max_f)))
    y_max = int(np.floor(max(y_min_f, y_max_f)))

    xs = np.arange(x_min, x_max + 1, dtype=float)
    ys = np.arange(y_min, y_max + 1, dtype=float)

    # タイル中心 (x+0.5, y+0.5) を緯度経度に変換
    lon_centers = []
    for xt in xs:
        lon, _ = _webmercator_tile_xy_to_lonlat(
            xt + 0.5, (y_min + y_max) / 2.0 + 0.5, zoom
        )
        lon_centers.append(lon)
    lon_centers = np.asarray(lon_centers, dtype=float)

    lat_centers = []
    for yt in ys:
        _, lat = _webmercator_tile_xy_to_lonlat(
            (x_min + x_max) / 2.0 + 0.5, yt + 0.5, zoom
        )
        lat_centers.append(lat)
    # ys は y_min→y_max（タイル y 軸は北が小さい）なので lat は降順（北→南）。
    # 南→北（昇順）に反転して make_lonlat_grid と挙動を揃える。
    lat_centers = np.asarray(lat_centers[::-1], dtype=float)

    lon2d, lat2d = np.meshgrid(lon_centers, lat_centers)
    return lon2d, lat2d


def get_tile_bounds(bbox: BoundingBox, zoom: int) -> Tuple[int, int, int, int]:
    """
    bbox をカバーするタイル座標範囲を返す.

    Returns
    -------
    tile_x_min, tile_x_max, tile_y_min, tile_y_max:
        make_lonlat_grid_tiles が内部で使うタイル範囲と同一の値。
    """
    x_min_f, y_max_f = _webmercator_lonlat_to_tile_xy(bbox.min_lon, bbox.min_lat, zoom)
    x_max_f, y_min_f = _webmercator_lonlat_to_tile_xy(bbox.max_lon, bbox.max_lat, zoom)

    tile_x_min = int(np.floor(min(x_min_f, x_max_f)))
    tile_x_max = int(np.floor(max(x_min_f, x_max_f)))
    tile_y_min = int(np.floor(min(y_min_f, y_max_f)))
    tile_y_max = int(np.floor(max(y_min_f, y_max_f)))
    return tile_x_min, tile_x_max, tile_y_min, tile_y_max
