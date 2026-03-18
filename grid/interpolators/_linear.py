"""Delaunay 三角分割による線形内挿（andersan-grid より移植）."""

from __future__ import annotations

import numpy as np
from scipy.interpolate import griddata


def interpolate_linear(
    lon: np.ndarray,
    lat: np.ndarray,
    values: np.ndarray,
    lon2d: np.ndarray,
    lat2d: np.ndarray,
) -> np.ndarray:
    """
    Delaunay 三角分割に基づく線形内挿 (scipy.interpolate.griddata).

    観測局の凸包の外側は NaN のまま返す（外挿しない）。
    """
    points = np.column_stack([lon, lat])
    return griddata(points, values, (lon2d, lat2d), method="linear")
