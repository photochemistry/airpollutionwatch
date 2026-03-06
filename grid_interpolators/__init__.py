"""空間内挿アルゴリズムパッケージ（andersan-grid より移植）."""

from __future__ import annotations

from typing import Literal

from ._linear import interpolate_linear
from ._tps import interpolate_atps, interpolate_tps

MethodName = Literal["linear", "tps", "atps"]

__all__ = [
    "MethodName",
    "interpolate_linear",
    "interpolate_tps",
    "interpolate_atps",
]
