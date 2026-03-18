"""Inverse Distance Weighting (IDW) / Natural Neighbor 風の補間."""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def _idw_weights(distances: np.ndarray, power: float) -> np.ndarray:
    """距離配列に対する IDW 重みを計算する（ゼロ距離はその点の値を採用）。"""
    # 距離 0 の点があれば、その点だけを 1.0 にして他は 0.0
    zero_mask = distances == 0.0
    if np.any(zero_mask):
        w = np.zeros_like(distances)
        # 距離ゼロの点が複数あっても等分
        count = zero_mask.sum()
        w[zero_mask] = 1.0 / float(count)
        return w

    with np.errstate(divide="ignore"):
        w = 1.0 / np.power(distances, power)
    s = w.sum()
    if s == 0.0:
        # すべて無限大など異常な場合は等重み
        return np.full_like(w, 1.0 / float(len(w)))
    return w / s


def interpolate_idw(
    lon: np.ndarray,
    lat: np.ndarray,
    values: np.ndarray,
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    k: int = 8,
    power: float = 2.0,
) -> np.ndarray:
    """Inverse Distance Weighting (IDW) による補間.

    - 各グリッド点ごとに観測点からの距離に応じて重み付けする。
    - 重みは常に正で正規化されるため、補間値は観測値の凸結合となり、
      オーバーシュートしない。

    Parameters
    ----------
    k:
        各グリッド点の補間に使う近傍観測点数。
    power:
        距離のべき指数（大きいほど近傍の寄与が強くなる）。
    """
    points = np.column_stack([lon, lat])
    tree = cKDTree(points)

    query = np.column_stack([lon2d.ravel(), lat2d.ravel()])

    # 近傍数は観測点数を上限とする
    n_stations = len(points)
    k_eff = min(k, n_stations)
    if k_eff < 1:
        return np.full(lon2d.shape, np.nan, dtype=float)

    dists, idx = tree.query(query, k=k_eff)
    # k_eff == 1 のときも 2 次元配列に揃える
    if k_eff == 1:
        dists = dists[:, np.newaxis]
        idx = idx[:, np.newaxis]

    vals_flat = np.empty(len(query), dtype=float)

    for i in range(len(query)):
        di = dists[i]
        vi = values[idx[i]]
        w = _idw_weights(di, power=power)
        vals_flat[i] = float(np.sum(w * vi))

    return vals_flat.reshape(lon2d.shape)


def interpolate_nnatural(
    lon: np.ndarray,
    lat: np.ndarray,
    values: np.ndarray,
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    k: int = 16,
    power: float = 2.0,
) -> np.ndarray:
    """Natural Neighbor 風（近傍密度にやや配慮した IDW）補間.

    厳密なシブソン Natural Neighbor ではなく、実装コストと速度を優先し、
    - 近傍点数 k をやや多めに取り
    - pow を少し抑えめにする
    ような IDW で近似している。
    """
    return interpolate_idw(lon, lat, values, lon2d, lat2d, k=k, power=power)

