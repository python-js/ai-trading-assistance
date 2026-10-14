"""MRS 持仓管理（规格书 §6.4）：每 1m 收盘顺序执行 TIME/TRAIL/BE/SEG。

纯函数 manage_step：只读 bar + ctx，输出 Action 列表（Amend/PartialExit/Exit），
副作用（改单/成交/落账）由 Engine 执行。Action 价格规则（v1 口径，登录于下）：
  1) Exit.px = bar.close（收盘市价离场）；stop 命中类离场由 Broker 检查、价格由 Broker 定。
  2) TRAIL 检查用"上一 bar 收盘时的峰值"（不含当前 close），避免同 bar 冲高回落的伪回撤；
     uR 用 close 计算，要求 ≥ +1R（§6.4-2a "当前uR≥1R"）。
  3) SEG1 只挂一次、不随价改单、不重挂（§6.4-4 "回落到+0.6R 内不再改单" 在 v1 退化为不动）。
  4) seg_done 后 TRAIL_ATR/TRAIL_MA 离场 reason 记 SEG1_THEN_TRAIL；同 bar 先 seg 后 trail
     的场景按评估时点状态取值。
  5) bars_held/mfe/mae 统计到"最后一次完整管理的 bar"（止损盘中出局的 bar 不计）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import CFG
from .types import Bar


@dataclass
class Position:
    """持仓管理状态（engine 持有；broker 持有订单侧价格）。"""

    sig_id: str
    dir: str            # LONG | SHORT
    open_ts: int
    entry: float        # 实际成交价（含不利滑点）
    qty0: float         # 建仓量
    qty: float          # 剩余量（seg 部分止盈后减少）
    R: float
    stop: float         # 当前保护止损（BE 触发后同步改价）
    tp: float
    seg1_px: float
    be_px_target: float
    zone_id: str = ""
    lev: int = 3
    peak: float = 0.0
    be_done: bool = False
    seg_done: bool = False
    bars_held: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    funding_paid: float = 0.0
    fees_paid: float = 0.0
    legs: list = field(default_factory=list)  # (px, qty, kind) 依成交序


@dataclass(frozen=True, slots=True)
class Amend:
    px: float
    reason: str  # BE


@dataclass(frozen=True, slots=True)
class PartialExit:
    px: float
    frac: float
    reason: str  # SEG1


@dataclass(frozen=True, slots=True)
class Exit:
    reason: str
    px: float
    qty: float | None = None  # None = 全部剩余


def _seg_reason(pos: Position, reason: str) -> str:
    if pos.seg_done and reason in ("TRAIL_ATR", "TRAIL_MA"):
        return "SEG1_THEN_TRAIL"
    return reason


def manage_step(pos: Position, bar: Bar, *, atr5: float, ma5m: float | None,
                is_5m_close: bool, cfg: dict = CFG) -> list[Amend | PartialExit | Exit]:
    """§6.4 顺序：1) TIME → 2) TRAIL（a ATR 回撤 / b 5m 破 MA，先触发先）→ 3) BE → 4) SEG1。

    返回动作列表；返回含 Exit 时发动机应离场（同 bar 若发生止损命中，止损优先于本表）。
    """
    d = 1 if pos.dir == "LONG" else -1
    mg = cfg["manage"]
    pos.bars_held += 1
    # mfe/mae 取极值按方向：有利极值（多=high、空=low）/ 不利极值（多=low、空=high）。
    fav0 = bar.high if d > 0 else bar.low
    adv0 = bar.low if d > 0 else bar.high
    pos.mfe_r = max(pos.mfe_r, d * (fav0 - pos.entry) / pos.R)
    pos.mae_r = min(pos.mae_r, d * (adv0 - pos.entry) / pos.R)
    u_r = d * (bar.close - pos.entry) / pos.R

    # 1) 时间止损
    if pos.bars_held >= mg["time_stop_bars"] and pos.mfe_r < mg["time_stop_mfe_r"]:
        return [Exit("TIME_STOP", bar.close)]

    # 2) 追踪离场（按 v1 口径 2：回撤对比"上一收盘峰值"，随后再更新峰值）
    adverse = bar.low if d > 0 else bar.high
    if d * (pos.peak - adverse) >= mg["trail_atr_mult"] * atr5 and u_r >= 1.0:
        return [Exit(_seg_reason(pos, "TRAIL_ATR"), bar.close)]
    if is_5m_close and ma5m is not None and d * (bar.close - ma5m) < 0:
        return [Exit(_seg_reason(pos, "TRAIL_MA"), bar.close)]
    pos.peak = max(pos.peak, bar.close) if d > 0 else min(pos.peak, bar.close)

    acts: list[Amend | PartialExit | Exit] = []
    # 3) 保本（一次生效）
    if not pos.be_done and u_r >= mg["be_trigger_r"]:
        acts.append(Amend(pos.be_px_target, "BE"))
        pos.be_done = True
    # 4) seg1 部分止盈（限价，只挂一次）
    if not pos.seg_done:
        fav = bar.high if d > 0 else bar.low
        if d * (fav - pos.seg1_px) >= 0:
            acts.append(PartialExit(pos.seg1_px, mg["seg1_frac"], "SEG1"))
            pos.seg_done = True
    return acts
