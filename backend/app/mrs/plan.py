"""MRS 计划层（规格书 §6.1-6.2）：Signal → OrderBundle 或 REJECT。

口径登记（v1）：
  1) tick 对齐：所有价格先量化到 tick；平台期（多）向下/（空）向上取整 = 一律取不利方向
     （§6.6 算例：多单止损 64,905.75→64,905.7、止盈 65,365.75→65,365.7）；trigger 同规则。
  2) 强平距离模型：liq_dist ≈ e/lev（逐仓、忽略维持保证金，§6.6 "3x≈20,000" 同口径）。
     迭代 8 次仍不足 → REJECT("LIQ")；lev 降到 2 后按 qty×0.8 重截断。
  3) min_qty = lot_step（1 张）；最小名义额不在本层校验（OKX 实测 minNotional 见 §11 checklist）。
  4) 反向最近区：取同侧（多→R、空→S）中 d×(edge−e) > 0 里最近者；无 → REJECT("ROOM")。
  5) R0 ≤ 0 → REJECT("BAD_R0")（脏数据，§6.1 ">0，否则视为脏数据"）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import CFG
from .types import Signal, Zone


@dataclass(frozen=True, slots=True)
class Bundle:
    e_sig: str
    now_ts: int
    dir: str
    zone_id: str
    trigger: float
    stop: float
    tp: float
    qty: float
    R: float
    seg1_px: float
    be_px_target: float
    leverage: int
    fee_bps: float
    slip_bps: float


@dataclass(frozen=True, slots=True)
class Reject:
    reason: str  # STOP_FAR|COST|ATR_ANOMALY|ROOM|TP_GAP|QTY_MIN|LIQ|BAD_R0


def _floor_tick(x: float, tick: float) -> float:
    return math.floor(round(x / tick, 9)) * tick


def _ceil_tick(x: float, tick: float) -> float:
    return math.ceil(round(x / tick, 9)) * tick


def _align(x: float, tick: float, d: int) -> float:
    """多方向下（floor）、空方向向上（ceil）= 一律不利方向（v1 口径 1）。"""
    return _floor_tick(x, tick) if d > 0 else _ceil_tick(x, tick)


def _floor_lot(qty: float, lot: float) -> float:
    return math.floor(round(qty / lot, 9)) * lot


def _opposite_edge_dist(zones: list[Zone], d: int, e: float) -> float | None:
    """反向最近区边缘距（v1 口径 4）。多→R 区下边缘、空→S 区上边缘。"""
    best = None
    for z in zones:
        if d > 0 and z.side != "R":
            continue
        if d < 0 and z.side != "S":
            continue
        edge = z.center - z.width / 2 if d > 0 else z.center + z.width / 2
        dist = d * (edge - e)
        if dist > 0 and (best is None or dist < best):
            best = dist
    return best


def make_plan(sig: Signal, ctx: dict, cfg: dict = CFG) -> Bundle | Reject:
    """§6.1：ctx = {atr1, atr5, atr15, equity, zones, regime, fees=(fee_bps, slip_bps)}。"""
    d = 1 if sig.dir == "LONG" else -1
    tick = cfg["venue"]["tick_size"]
    lot = cfg["venue"]["lot_step"]
    risk = cfg["risk"]
    mg = cfg["manage"]
    e = _align(sig.trigger_px, tick, d)

    # --- 1. R 定界（§6.2）---
    raw_stop = _align(sig.invalid_px - d * 0.15 * ctx["atr5"], tick, d)
    r0 = d * (e - raw_stop)
    if r0 <= 0:
        return Reject("BAD_R0")
    lo_mult, hi_mult = risk["atr_bounds_mult"]
    if r0 > hi_mult * ctx["atr15"]:
        return Reject("STOP_FAR")
    r = max(r0, lo_mult * ctx["atr15"])
    stop = _align(e - d * r, tick, d)

    # --- 2. 闸门 ---
    fee_bps, slip_bps = ctx["fees"]
    rt = 2 * e * (fee_bps + slip_bps) / 1e4
    if r < max(4 * rt, risk["min_r_ticks_mult"] * tick):
        return Reject("COST")
    if r > 0.5 * e:
        return Reject("ATR_ANOMALY")
    edge_dist = _opposite_edge_dist(ctx["zones"], d, e)
    if edge_dist is None or edge_dist < cfg["entry"]["room_rr_min"] * r:
        return Reject("ROOM")

    # --- 3. 止盈 ---
    tp_rr = e + d * mg["tp_r_base"] * r
    tp_zone = e + d * (edge_dist - 0.15 * ctx["atr5"])
    tp = tp_zone if d * (tp_zone - e) < d * (tp_rr - e) else tp_rr
    if d * (tp - e) < mg["seg1_r"] * r:
        return Reject("TP_GAP")
    tp = _align(tp, tick, d)

    # --- 4. 数量 ---
    qty = ctx["equity"] * risk["risk_pct"] / 100 / r
    if ctx["regime"] == "HALF":
        qty *= cfg["regime"]["half_size_factor"]
    qty = min(qty, ctx["equity"] * risk["notional_max_pct"] / 100 / e)
    qty = _floor_lot(qty, lot)
    if qty < lot:
        return Reject("QTY_MIN")

    # --- 5. 强平缓冲（v1 口径 2）---
    lev = risk["leverage_default"]
    iters = 0
    while e / lev < risk["liq_buffer_r"] * r:
        iters += 1
        if iters > 8:
            return Reject("LIQ")
        if lev > 2:
            lev -= 1
        else:
            qty = _floor_lot(qty * 0.8, lot)
            if qty < lot:
                return Reject("QTY_MIN")

    seg1 = _align(e + d * mg["seg1_r"] * r, tick, d)
    be = _align(e + d * max(2 * rt, mg["be_lock_min_r"] * r), tick, d)
    return Bundle(e_sig=sig.id, now_ts=sig.now_ts, dir=sig.dir, zone_id=sig.zone_id,
                  trigger=sig.trigger_px, stop=stop, tp=tp, qty=qty, R=r, seg1_px=seg1,
                  be_px_target=be, leverage=lev, fee_bps=fee_bps, slip_bps=slip_bps)
