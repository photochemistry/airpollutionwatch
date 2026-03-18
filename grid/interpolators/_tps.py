"""Thin Plate Spline 補間（andersan-grid より移植）."""

from __future__ import annotations

import numpy as np
from scipy.interpolate import RBFInterpolator
from scipy.spatial import cKDTree


def interpolate_tps(
    lon: np.ndarray,
    lat: np.ndarray,
    values: np.ndarray,
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    smoothing: float = 1.0,
) -> np.ndarray:
    """
    Thin Plate Spline (TPS) による空間内挿（一律 smoothing）.

    TPS は次のエネルギーを最小化する滑らかな曲面を求める:

        Σ_i smoothing · (観測値_i - f(x_i))²  +  ∫∫ (曲げエネルギー) dx dy

    smoothing=0 なら各局を厳密に通過、大きいほど滑らか。
    全域で同じ平滑化強度を使う。

    Parameters
    ----------
    smoothing:
        平滑化強度（全局一律）。0 で厳密補間。
    """
    points = np.column_stack([lon, lat])
    query = np.column_stack([lon2d.ravel(), lat2d.ravel()])
    rbf = RBFInterpolator(
        points,
        values,
        kernel="thin_plate_spline",
        smoothing=smoothing,
    )
    return rbf(query).reshape(lon2d.shape)


def interpolate_atps(
    lon: np.ndarray,
    lat: np.ndarray,
    values: np.ndarray,
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    smoothing: float = 1.0,
    k: int = 5,
    power: float = 2.0,
) -> np.ndarray:
    """
    密度適応型 Thin Plate Spline (adaptive TPS) による空間内挿.

    各観測局の smoothing を局所的な観測局間距離に比例させる:

        smoothing_i = smoothing × (d_i / median_d)^power

    ここで d_i は i 番局の第 k 近傍距離（局所スペーシングの代理）。
    疎な地域の局ほど縛りが緩まり、曲げエネルギーが自然に小さくなる。
    密な地域は観測値に忠実に追従する。

    Parameters
    ----------
    smoothing:
        中央値密度の局に対する基準 smoothing。
    k:
        局所密度推定に使う近傍局数。
    power:
        局間距離のべき乗。2 で面積スケールに対応（2D 問題の標準）。
    """
    points = np.column_stack([lon, lat])
    query = np.column_stack([lon2d.ravel(), lat2d.ravel()])
    smoothing_arr = _adaptive_smoothing(points, smoothing, k, power)
    rbf = RBFInterpolator(
        points,
        values,
        kernel="thin_plate_spline",
        smoothing=smoothing_arr,
    )
    return rbf(query).reshape(lon2d.shape)


def _adaptive_smoothing(
    points: np.ndarray,
    base_smoothing: float,
    k: int,
    power: float,
) -> np.ndarray:
    """
    各観測点の局所密度に応じた per-point smoothing 配列を計算する.

    k 近傍距離を局間スペーシングの代理として使い、
    中央値スペーシングで正規化する。これにより base_smoothing の
    絶対値がデータセットの密度によらず同じ意味を持つ。
    """
    n = len(points)
    k_actual = min(k, n - 1)
    if k_actual < 1:
        return np.full(n, base_smoothing)

    tree = cKDTree(points)
    # 自分自身（dist=0）を除くため k_actual+1 個取得して末尾を使う
    dists, _ = tree.query(points, k=k_actual + 1)
    nn_dist = dists[:, k_actual]  # 第 k 近傍距離

    # ゼロ割り回避（同一座標の局がある場合）
    nn_dist = np.where(nn_dist == 0.0, np.finfo(float).eps, nn_dist)

    median_dist = float(np.median(nn_dist))
    normalized = nn_dist / median_dist          # 1.0 が「標準的な局間距離」
    smoothing_arr = base_smoothing * normalized ** power
    return smoothing_arr
