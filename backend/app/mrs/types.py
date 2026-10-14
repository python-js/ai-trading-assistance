"""MRS 共享数据结构（规格书 §3.3/§5.4/§5.8）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Bar:
    """已收盘 bar（ts=开盘时间，秒）。"""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Material:
    """关键区原料位（§5.4）：kind ∈ swing|ma|vwap|poc|fib|int|prevday。"""

    price: float
    kind: str


@dataclass(frozen=True, slots=True)
class Zone:
    """关键区（§5.4）：side S=现价下方支撑 / R=现价上方阻力。"""

    id: str
    center: float
    width: float
    side: str
    grade: float
    touches: int
    rebound: float
    types: tuple[str, ...]


@dataclass(slots=True)
class Signal:
    """触发器信号（§5.8）。sig_id 用 uuid5 确定性生成（§9.4 回放逐行 diff 需要）。"""

    id: str
    now_ts: int
    type: str
    dir: str
    zone_id: str
    trigger_px: float
    invalid_px: float
    close: float
    suppressed: str | None = None
    gates_passed: list[str] = field(default_factory=list)
