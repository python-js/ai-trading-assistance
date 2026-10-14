"""MRS 执行层（规格书 §6.3，纸面/回测版）：订单生命周期模拟 + 故障注入。

与真实交易所 API 的同构点（供 M7 换实盘 Broker 时对照）：
  A) 触发单 STOP_MARKET：bar[n+1] 起触发；TTL 至 bar[n+2] 收盘（§5.6；§6.3 的 5 分钟为通用口径，
     本标的使用 §5.6 专则）；未成交 → cancel + funnel NO_FILL。
  B) 触发成交后立即挂保护单（tp 限价 + stop 条件单），补偿式重试 ×3；全败 → UNPROTECTED，
     调用方须市价全平并计 error_stop_fail（§6.3-B / T12）。
  C) 成交价（§9.1 保守内插）：入 = trigger ± 不利滑点；止损出 = stop ∓ 不利滑点；
     市价出 = close ∓ 不利滑点；限价（tp/seg1）按挂单价成交、不加滑点。
  D) 幂等：clientOrderId = sig_id；同一 sig_id 单组恒存（含已关闭生命周期，open 不复活、
     不新建）；重复触价事件对已成交订单无副作用（T10）。动作驱动成交（seg1/market 平仓）
     由引擎状态保证单次（seg_done / pos 清空与 CLOSED 阶段），不在此重复设防。
  E) 部分成交模型：v1 纸面为全量成交（PARTIAL_SHALLOW 保留未启用，口径登记）。

费用：每笔成交按 fee_bps 计提（默认 taker），滑点 bps 按成交价比例、不利方向。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import CFG
from .plan import Bundle
from .types import Bar

_SLIP_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Fill:
    ts: int
    sig_id: str
    kind: str      # entry|seg1|tp|stop|market
    px: float      # 实际成交价（含滑点）
    qty: float
    fee_usdt: float
    reason: str = ""  # 离场类成交的 §8.3 reason（STOP|BE|KILL|...）


@dataclass
class OrderGroup:
    """一组同生命周期订单（trigger + 保护单），键 = sig_id。"""

    sig_id: str
    bundle: Bundle
    stage: str = "TRIGGER_SENT"  # TRIGGER_SENT|PROTECTED|UNPROTECTED|CLOSED|CANCELLED
    qty: float = 0.0
    stop_px: float = 0.0
    tp_px: float = 0.0
    seg1_px: float = 0.0
    protect_fills: int = 0
    stop_filled: bool = False
    tp_filled: bool = False
    order_log: list = field(default_factory=list)


class PaperBroker:
    def __init__(self, *, fee_bps: float, slip_bps: float, ledger=None,
                 fail_place_stop: int = 0, cfg: dict = CFG):
        self.fee_bps = fee_bps
        self.slip_bps = slip_bps
        self.ledger = ledger
        self.fail_place_stop = fail_place_stop  # 故障注入：保护单挂单失败次数（T12）
        self.cfg = cfg
        self.groups: dict[str, OrderGroup] = {}

    # ---------- 基础 ----------
    def _slip(self, px: float) -> float:
        return px * self.slip_bps / 1e4

    def _fee(self, px: float, qty: float) -> float:
        return px * qty * self.fee_bps / 1e4

    def _order(self, g: OrderGroup, oid: str, typ: str, state: str, px: float = None,
               qty: float = None, ts: int | None = None) -> None:
        rec = {"ts": ts if ts is not None else (g.order_log[-1]["ts"] if g.order_log else 0),
               "sig_id": g.sig_id, "client_oid": g.sig_id, "order_id": oid, "type": typ,
               "state": state, "px": px, "qty": qty}
        g.order_log.append(rec)
        if self.ledger is not None:
            self.ledger.order(rec)

    def active(self) -> list[OrderGroup]:
        return [g for g in self.groups.values() if g.stage in ("TRIGGER_SENT", "PROTECTED",
                                                               "UNPROTECTED")]

    def get(self, sig_id: str) -> OrderGroup | None:
        return self.groups.get(sig_id)

    # ---------- §6.3-A：开单（幂等） ----------
    def open(self, bundle: Bundle, ts: int) -> OrderGroup:
        g = self.groups.get(bundle.e_sig)
        if g is not None:
            return g  # 同 sig_id 单组恒存（含已关闭生命周期，不复活；§6.3-D）
        g = OrderGroup(sig_id=bundle.e_sig, bundle=bundle, qty=bundle.qty,
                       stop_px=bundle.stop, tp_px=bundle.tp, seg1_px=bundle.seg1_px)
        self.groups[bundle.e_sig] = g
        g.order_log.append({"ts": ts, "sig_id": g.sig_id, "client_oid": g.sig_id,
                            "order_id": f"{g.sig_id}:trig", "type": "TRIGGER",
                            "state": "SENT", "px": bundle.trigger, "qty": bundle.qty})
        if self.ledger is not None:
            self.ledger.order(g.order_log[-1])
        return g

    def cancel(self, sig_id: str, ts: int, why: str) -> OrderGroup | None:
        g = self.groups.get(sig_id)
        if g is None or g.stage not in ("TRIGGER_SENT", "PROTECTED", "UNPROTECTED"):
            return g
        g.order_log.append({"ts": ts, "sig_id": g.sig_id, "client_oid": g.sig_id,
                            "order_id": f"{g.sig_id}:all", "type": "CANCEL",
                            "state": "CANCELLED", "px": None, "qty": g.qty, "why": why})
        if self.ledger is not None:
            self.ledger.order(g.order_log[-1])
        g.stage = "CANCELLED"
        return g

    # ---------- §5.6 TTL：挂单在途处理 ----------
    def on_pending_bar(self, g: OrderGroup, bar: Bar) -> Fill | None:
        """挂单在途：填触发价则成交；至 bar[n+2] 收盘（sig 收盘 +120s）仍未成交 → 撤单（返回 None 后由引擎判 TTL）。"""
        if g.stage != "TRIGGER_SENT":
            return None  # 已成交/已撤/已关闭的订单不再触价（重复事件无副作用，§6.3-D）
        b = g.bundle
        d = 1 if b.dir == "LONG" else -1
        hit = bar.high >= b.trigger - _SLIP_EPS if d > 0 else bar.low <= b.trigger + _SLIP_EPS
        if not hit:
            return None
        px = b.trigger + d * self._slip(b.trigger)  # 不利滑点
        fill = Fill(ts=bar.ts, sig_id=g.sig_id, kind="entry", px=px, qty=g.qty,
                    fee_usdt=self._fee(px, g.qty))
        self._order(g, f"{g.sig_id}:trig", "TRIGGER", "FILLED", px=px, qty=g.qty, ts=bar.ts)
        self.place_protections(g, bar)
        return fill

    def ttl_expired(self, g: OrderGroup, bar: Bar) -> bool:
        """bar[n+2]（sig 收盘 ts + 60）收盘仍未成交 → TTL 撤单。"""
        return bar.ts >= g.bundle.now_ts + 60 and g.stage == "TRIGGER_SENT"

    # ---------- §6.3-B：保护单（补偿式重试 ×3 + 故障注入） ----------
    def place_protections(self, g: OrderGroup, bar: Bar) -> str:
        """成交后挂 tp/stop 保护单；返回 PROTECTED | UNPROTECTED。"""
        for attempt in range(3):
            if self.fail_place_stop > 0:
                self.fail_place_stop -= 1
                self._order(g, f"{g.sig_id}:stop", "STOP_MARKET", "FAIL", px=g.stop_px,
                            qty=g.qty, ts=bar.ts)
                continue
            self._order(g, f"{g.sig_id}:tp", "LIMIT_REDUCE", "PLACED", px=g.tp_px, qty=g.qty, ts=bar.ts)
            self._order(g, f"{g.sig_id}:stop", "STOP_MARKET_REDUCE", "PLACED", px=g.stop_px,
                        qty=g.qty, ts=bar.ts)
            g.protect_fills += 1
            g.stage = "PROTECTED"
            return "PROTECTED"
        g.stage = "UNPROTECTED"
        return "UNPROTECTED"

    def amend_stop(self, sig_id: str, px: float, ts: int) -> None:
        g = self.groups.get(sig_id)
        if g is None:
            return
        g.stop_px = px
        self._order(g, f"{g.sig_id}:stop", "STOP_MARKET_REDUCE", "AMENDED", px=px, qty=g.qty, ts=ts)

    # ---------- 成交模拟 ----------
    def fill_stop(self, g: OrderGroup, bar: Bar, be_done: bool) -> Fill | None:
        if g.stage != "PROTECTED" or g.stop_filled:
            return None  # 未挂保护 / 已止损成交 → 重复事件不重复成交（§6.3-D）
        b = g.bundle
        d = 1 if b.dir == "LONG" else -1
        hit = bar.low <= g.stop_px + _SLIP_EPS if d > 0 else bar.high >= g.stop_px - _SLIP_EPS
        if not hit:
            return None
        g.stop_filled = True
        px = g.stop_px - d * self._slip(g.stop_px)  # 止损成交 = stop ∓ 不利滑点（§9.1-c）
        reason = "BE" if be_done else "STOP"
        fill = Fill(ts=bar.ts, sig_id=g.sig_id, kind="stop", px=px, qty=g.qty,
                    fee_usdt=self._fee(px, g.qty), reason=reason)
        self._order(g, f"{g.sig_id}:stop", "STOP_MARKET_REDUCE", "FILLED", px=px, qty=g.qty, ts=bar.ts)
        return fill

    def fill_partial(self, g: OrderGroup, bar: Bar, px: float, qty: float,
                     kind: str = "seg1") -> Fill:
        fill = Fill(ts=bar.ts, sig_id=g.sig_id, kind=kind, px=px, qty=qty,
                    fee_usdt=self._fee(px, qty), reason="SEG1")
        g.qty -= qty
        self._order(g, f"{g.sig_id}:{kind}", "LIMIT_REDUCE", "FILLED", px=px, qty=qty, ts=bar.ts)
        return fill

    def fill_tp(self, g: OrderGroup, bar: Bar) -> Fill | None:
        if g.stage != "PROTECTED" or g.tp_filled:
            return None  # 重复事件不重复成交（§6.3-D）
        b = g.bundle
        d = 1 if b.dir == "LONG" else -1
        hit = bar.high >= g.tp_px - _SLIP_EPS if d > 0 else bar.low <= g.tp_px + _SLIP_EPS
        if not hit:
            return None
        g.tp_filled = True
        fill = Fill(ts=bar.ts, sig_id=g.sig_id, kind="tp", px=g.tp_px, qty=g.qty,
                    fee_usdt=self._fee(g.tp_px, g.qty), reason="TP")
        self._order(g, f"{g.sig_id}:tp", "LIMIT_REDUCE", "FILLED", px=g.tp_px, qty=g.qty, ts=bar.ts)
        return fill

    def market_close(self, g: OrderGroup, bar: Bar, reason: str) -> Fill:
        b = g.bundle
        d = 1 if b.dir == "LONG" else -1
        px = bar.close - d * self._slip(bar.close)  # 市价离场 = close ∓ 不利滑点
        fill = Fill(ts=bar.ts, sig_id=g.sig_id, kind="market", px=px, qty=g.qty,
                    fee_usdt=self._fee(px, g.qty), reason=reason)
        self._order(g, f"{g.sig_id}:mkt", "MARKET_REDUCE", "FILLED", px=px, qty=g.qty, ts=bar.ts)
        return fill

    def close(self, sig_id: str, ts: int) -> None:
        g = self.groups.get(sig_id)
        if g is not None and g.stage in ("PROTECTED", "UNPROTECTED", "TRIGGER_SENT"):
            g.stage = "CLOSED"
