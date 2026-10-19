"""MRS 主流程（规格书 §7）：on_tf_close / on_1m_close 与账户状态装配。

同源约束（§9.0）：本引擎是回测与实盘唯一决策路径；DataHub（喂 bar）与 Broker 由外部替换。
on_1m_close 顺序（§7.2 固定）：1m 指标/HTF 合成 → funding 结算 → 持仓管理 → 挂单在途 → 闸门与触发。
每 1m 收盘恰写一行 funnel（§8.2）；指标样本不足按 REGIME 不通过处理（§4.1 None 语义）。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict

from .broker import PaperBroker
from .config import CFG, cfg_hash
from .decision import (build_materials, compute_rank, eval_entry,
                       rebuild_zones, regime_from_adx)
from .indicators import ATR, ADX, SMA, DayStats, SwingTracker, VolWindow, fib_levels
from .ledger import Ledger
from .manage import Amend, Exit, PartialExit, Position, manage_step
from .plan import Reject, _floor_lot, make_plan
from .state import MAX_TRADES_PER_DAY, AccountState
from .types import Bar, Zone

_TFS = (300, 900, 1800)


class Engine:
    def __init__(self, *, cfg: dict = CFG, ledger: Ledger | None = None,
                 equity: float | None = None, fees: tuple | None = None,
                 broker: PaperBroker | None = None, on_halt=None, funding_fn=None):
        self.cfg = cfg
        self.ledger = ledger if ledger is not None else Ledger()
        self.equity = float(equity if equity is not None else cfg["account"]["init_equity"])
        fee_bps, slip_bps = fees if fees else (cfg["venue"]["taker_bps"],
                                               cfg["venue"]["slip_base_bps"])
        self.fees = (fee_bps, slip_bps)
        self.broker = broker if broker is not None else PaperBroker(
            fee_bps=fee_bps, slip_bps=slip_bps, ledger=self.ledger, cfg=cfg)
        self.on_halt = on_halt
        self.funding_fn = funding_fn
        self.funding_imputed = 0
        self.funding_settlements = 0
        self.acct = AccountState()
        self.pos: Position | None = None
        self.pending_sig_id: str | None = None
        self.last_ts: int | None = None
        mp, ap = cfg["ma_periods"], cfg["atr_periods"]["m1"]
        self.tf = {tf: {"sma": (SMA(mp["fast"]), SMA(mp["slow"]), SMA(mp["long"])),
                        "atr": ATR(ap), "adx": ADX(cfg["adx_period"]),
                        "sw": SwingTracker(tf, cfg["swings"]["k_confirm"]),
                        "rank": 0, "bar_ts": None} for tf in _TFS}
        self.m1_atr = ATR(ap)
        self.volwin = VolWindow(cfg["entry"]["vol_pctile_n"], min_n=40)
        self.day = DayStats(bins=cfg["zones"]["poc"]["bins"])
        self.hist: deque[Bar] = deque(maxlen=4600)
        self._agg = {tf: None for tf in _TFS}
        self.zones: list[Zone] = []
        self.regime = "NO_TRADE"
        self._vol_pctile = None
        self._atr5 = None
        self._atr15 = None

    # ---------- 主入口 ----------
    def on_1m_close(self, bar: Bar) -> tuple[str, str | None]:
        """喂一根已收盘 1m bar（ts=开盘时间）。返回本 bar 的 (funnel 标签, plan_reject)。"""
        if self.last_ts is not None and bar.ts != self.last_ts + 60:
            if self.acct.state != "HALTED_DAY":
                self.acct.state = "NO_DATA"  # §3.2-H3：断供由 DataHub 回填，引擎拒处理不连续 bar
            self.ledger.funnel(bar.ts + 60, "NO_DATA")
            return "NO_DATA", None
        self.last_ts = bar.ts
        self.acct.roll_day(bar.ts // 86_400)
        self.m1_atr.update(bar.high, bar.low, bar.close)
        self._vol_pctile = self.volwin.update(bar.volume)
        self.day.update(bar.ts, bar.high, bar.low, bar.close, bar.volume)
        self.hist.append(bar)
        for tf in _TFS:
            self._feed_tf(tf, bar)
        if self.pos is not None and bar.ts % 28_800 == 0:
            self._settle_funding(bar)
        lab, pr = self._process_bar(bar)
        self.ledger.funnel(bar.ts + 60, lab, pr)
        return lab, pr

    # ---------- HTF 合成与收盘（§3.2 H-1 / §7.1） ----------
    def _feed_tf(self, tf: int, bar: Bar) -> None:
        a = self._agg[tf]
        w0 = bar.ts - (bar.ts % tf)
        if a is None or a["ts"] != w0:
            a = {"ts": w0, "o": bar.open, "h": bar.high, "l": bar.low,
                 "c": bar.close, "v": bar.volume}
            self._agg[tf] = a
        else:
            a["h"] = max(a["h"], bar.high)
            a["l"] = min(a["l"], bar.low)
            a["c"] = bar.close
            a["v"] += bar.volume
        if bar.ts % tf == tf - 60:  # 本 1m 为该周期最后一分钟 → 立即合成收盘
            self._on_tf_close(tf, Bar(a["ts"], a["o"], a["h"], a["l"], a["c"], a["v"]),
                              bar.ts + 60)

    def _on_tf_close(self, tf: int, b: Bar, now_ts: int) -> None:
        t = self.tf[tf]
        ma = tuple(s.update(b.close) for s in t["sma"])
        adx = t["adx"].update(b.high, b.low, b.close)
        t["atr"].update(b.high, b.low, b.close)
        t["sw"].update(b.ts, b.high, b.low)
        t["rank"] = compute_rank(b.close, ma[0], ma[1], ma[2], t["sw"].confirmed, now_ts)
        t["bar_ts"] = b.ts
        if tf == 900:
            self.regime = regime_from_adx(adx, self.cfg)
            self._rebuild_zones(b, now_ts)
        self.persist()

    def _rebuild_zones(self, b: Bar, now_ts: int) -> None:
        tick = self.cfg["venue"]["tick_size"]
        atr15 = self.tf[900]["atr"].v
        if atr15 is None:
            self.zones = []
            return
        lb = self.cfg["swings"]["lookback_tf"]
        sw = []
        for tf2 in _TFS:
            pts = [s for s in self.tf[tf2]["sw"].confirmed if s.confirm_ts <= now_ts]
            sw.extend(pts[-lb[tf2]:])
        fib = fib_levels(self.tf[900]["sw"].confirmed, now_ts, 900)
        ma_levels = [self.tf[900]["sma"][1].value, self.tf[900]["sma"][2].value,
                     self.tf[1800]["sma"][1].value, self.tf[1800]["sma"][2].value]
        mats = build_materials(day_snap=self.day.yesterday, ma_levels=ma_levels,
                               swings=sw, fib=fib, close=b.close, atr15=atr15,
                               tick=tick, cfg=self.cfg)
        self.zones = rebuild_zones(materials=mats, close=b.close, atr15=atr15,
                                   bars_1m_recent=list(self.hist), now_ts=now_ts,
                                   tick=tick, cfg=self.cfg)

    # ---------- funding（§9.2） ----------
    def _settle_funding(self, bar: Bar) -> None:
        pos = self.pos
        self.funding_settlements += 1
        rate = self.funding_fn(bar.ts) if self.funding_fn is not None else None
        if rate is None:
            rate = 0.0
            self.funding_imputed += 1  # 无数据按 0 并标记（§9.2），报表侧统计 imputed 占比
        d = 1 if pos.dir == "LONG" else -1
        amt = d * rate * pos.qty * bar.open
        pos.funding_paid += amt
        self.equity -= amt

    # ---------- §7.2 主处理 ----------
    def _process_bar(self, bar: Bar) -> tuple[str, str | None]:
        now = bar.ts + 60
        if self.acct.state == "NO_DATA":  # 断供恢复（v1：下一次连续 bar 自动复位）
            self.acct.state = ("IN_POS" if self.pos else
                               ("ARMED" if self.pending_sig_id else "IDLE"))
        if self.pos is not None:
            return self._manage_phase(bar)
        if self.pending_sig_id is not None:
            return self._pending_phase(bar)
        return self._entry_phase(bar, now)

    # ---- 1. 持仓管理（§6.4-6.5） ----
    def _manage_phase(self, bar: Bar) -> tuple[str, str | None]:
        pos = self.pos
        g = self.broker.get(pos.sig_id)
        f = self.broker.fill_stop(g, bar, pos.be_done)  # a) 止损命中优先（§9.1-b 先不利）
        if f is not None:
            self._close_position(f.reason, f, bar)
            return "BUSY", None
        atr5 = self.tf[300]["atr"].v
        acts = manage_step(pos, bar, atr5=atr5 if atr5 is not None else math.inf,
                           ma5m=self.tf[300]["sma"][1].value,
                           is_5m_close=(bar.ts % 300 == 240), cfg=self.cfg)
        exit_act = None
        for a in acts:
            if isinstance(a, Amend):
                pos.stop = a.px
                self.broker.amend_stop(pos.sig_id, a.px, bar.ts)
            elif isinstance(a, PartialExit):
                qty = _floor_lot(pos.qty0 * a.frac, self.cfg["venue"]["lot_step"])
                if qty > 0:
                    pf = self.broker.fill_partial(g, bar, a.px, qty)
                    pos.qty -= qty
                    pos.legs.append((pf.px, pf.qty, "SEG1"))
                    pos.fees_paid += pf.fee_usdt
            elif isinstance(a, Exit):
                exit_act = a
        f = self.broker.fill_tp(g, bar)  # c) 止盈（限价先于收盘类离场）
        if f is not None:
            self._close_position("TP", f, bar)
            return "BUSY", None
        if self._flatten_due(bar.ts + 60):  # d) 定时强平（§6.4 flatten_by_utc）
            f = self.broker.market_close(g, bar, "FLATTEN")
            self._close_position("FLATTEN", f, bar)
            return "BUSY", None
        if exit_act is not None:  # e) 收盘类离场（TIME/TRAIL）
            f = self.broker.market_close(g, bar, exit_act.reason)
            self._close_position(exit_act.reason, f, bar)
            return "BUSY", None
        if acts:
            self.persist()
        return "BUSY", None

    def _flatten_due(self, now: int) -> bool:
        hh, mm = self.cfg["manage"]["kill"]["flatten_by_utc"].split(":")
        return (now % 86_400) // 60 >= int(hh) * 60 + int(mm)

    # ---- 2. 挂单在途（§5.6 TTL / §6.3-A） ----
    def _pending_phase(self, bar: Bar) -> tuple[str, str | None]:
        g = self.broker.get(self.pending_sig_id)
        f = self.broker.on_pending_bar(g, bar)
        if f is not None:
            return self._on_entry_fill(g, f, bar)
        if self.broker.ttl_expired(g, bar):
            self.broker.cancel(g.sig_id, bar.ts + 60, "TTL")
            self.pending_sig_id = None
            self.acct.register_ttl_abort(bar.ts + 60, self.cfg)
            self.persist()
            return "NO_FILL", None
        return "BUSY", None

    def _on_entry_fill(self, g, f, bar: Bar) -> tuple[str, str | None]:
        b = g.bundle
        tick = self.cfg["venue"]["tick_size"]
        d = 1 if b.dir == "LONG" else -1
        self.pos = Position(sig_id=g.sig_id, dir=b.dir, open_ts=f.ts, entry=f.px,
                            qty0=f.qty, qty=f.qty, R=b.R, stop=b.stop, tp=b.tp,
                            seg1_px=b.seg1_px, be_px_target=b.be_px_target,
                            zone_id=b.zone_id, lev=b.leverage, peak=f.px)
        self.pos.legs.append((f.px, f.qty, "ENTRY"))
        self.pos.fees_paid += f.fee_usdt
        self.pending_sig_id = None
        self.acct.register_entry(b.zone_id, f.ts)
        self.ledger.trade({"ts": f.ts, "sig_id": g.sig_id, "trade_id": g.sig_id,
                           "event": "init", "qty": f.qty, "trigger": b.trigger,
                           "entry_px_real": f.px,
                           "slip_ticks": d * (f.px - b.trigger) / tick,
                           "stop": b.stop, "tp": b.tp, "R": b.R,
                           "risk_usdt": b.R * f.qty,
                           "fees_bps": {"fee": self.fees[0], "slip": self.fees[1]}})
        self.persist()
        if g.stage == "UNPROTECTED":  # §6.3-B：保护单 3 连败 → 市价全平 + 计数（T12）
            f2 = self.broker.market_close(g, bar, "KILL")
            self._close_position("KILL", f2, bar)
            if self.acct.note_error_stop_fail(self.cfg):
                self._alert("ERROR_STOP_FAIL")
        return "BUSY", None

    # ---- 3. 闸门与触发（§7.2 第 3-4 步） ----
    def _entry_phase(self, bar: Bar, now: int) -> tuple[str, str | None]:
        if self.acct.state == "HALTED_DAY":
            return "HALT", None
        self.acct.maybe_resume(now)
        if self.acct.trades_today >= MAX_TRADES_PER_DAY:
            return "TRADES_CAP", None
        atr1 = self.m1_atr.v
        self._atr5 = self.tf[300]["atr"].v
        self._atr15 = self.tf[900]["atr"].v
        if atr1 is None or self._atr5 is None or self._atr15 is None:
            return "REGIME", None  # v1：指标样本不足按 regime 不通过（§4.1）
        bars = list(self.hist)[-4:]
        if len(bars) < 4:
            return "NO_TRIGGER", None
        sig, reason = eval_entry(
            bars=bars, now_ts=now, rank30=self.tf[1800]["rank"], rank15=self.tf[900]["rank"],
            rank5=self.tf[300]["rank"], adx15=self.tf[900]["adx"].v,
            atr1=atr1, atr5=self._atr5, atr15=self._atr15, vol_pctile=self._vol_pctile,
            spread_bps=None, zones=self.zones, zone_touch_today=self.acct.zone_touches,
            zone_blacklist=self.acct.blacklist_set(), t1_used_today=self.acct.t1_used,
            tick=self.cfg["venue"]["tick_size"],
            session_windows=self.cfg["manage"]["session_windows_utc"],
            funding_hours=self.cfg["funding_hours_utc"],
            blackout_min=self.cfg["manage"]["event_blackout_min"],
            cooldowns=self.acct.cooldowns,
            cfg=self.cfg)
        if sig is None:
            return reason, None
        if sig.type == "T1":
            self.acct.t1_used.add(("T1", sig.zone_id))
        ctx = {"atr1": atr1, "atr5": self._atr5, "atr15": self._atr15,
               "equity": self.equity, "zones": self.zones, "regime": self.regime,
               "fees": self.fees}
        out = make_plan(sig, ctx, cfg=self.cfg)
        if isinstance(out, Reject):
            return "PLAN", out.reason
        self.broker.open(out, ts=bar.ts)
        self.pending_sig_id = out.e_sig
        self.acct.state = "ARMED"
        self.ledger.signal(self._signal_record(sig, out))
        self.persist()
        return "OK", None

    def _signal_record(self, sig, b) -> dict:
        z = next((x for x in self.zones if x.id == sig.zone_id), None)
        return {"ts": sig.now_ts, "cfg_hash": cfg_hash(self.cfg), "sig_id": sig.id,
                "sym": self.cfg["symbol"]["internal"], "dir": sig.dir, "type": sig.type,
                "zone_id": sig.zone_id, "suppressed": sig.suppressed,
                "gates_passed": sig.gates_passed,
                "ranks": [self.tf[1800]["rank"], self.tf[900]["rank"], self.tf[300]["rank"]],
                "regime": self.regime, "atr5": self._atr5, "atr15": self._atr15,
                "vol_pctl": self._vol_pctile, "spread_bps": None, "close": sig.close,
                "zone_center": z.center if z else None, "zone_width": z.width if z else None,
                "zone_grade": z.grade if z else None,
                "bundle": {"trigger": b.trigger, "stop": b.stop, "tp": b.tp,
                           "R": b.R, "qty": b.qty}}

    # ---------- 离场与落账 ----------
    def _close_position(self, reason: str, f, bar: Bar) -> str:
        pos = self.pos
        pos.legs.append((f.px, f.qty, reason))
        pos.fees_paid += f.fee_usdt
        d = 1 if pos.dir == "LONG" else -1
        pnl = -pos.fees_paid - pos.funding_paid
        for px, qty, _k in pos.legs:
            pnl += d * (px - pos.entry) * qty
        pnl_r = pnl / (pos.R * pos.qty0)
        self.broker.close(pos.sig_id, bar.ts + 60)
        self.ledger.trade({"ts": bar.ts + 60, "sig_id": pos.sig_id,
                           "trade_id": pos.sig_id, "event": "exit", "reason": reason,
                           "px": f.px, "qty": f.qty, "pnl_usdt": pnl, "pnl_R": pnl_r,
                           "mfe_R": pos.mfe_r, "mae_R": pos.mae_r,
                           "bars_held": pos.bars_held, "funding_paid": pos.funding_paid,
                           "be_done": pos.be_done, "seg_done": pos.seg_done})
        self.equity += pnl
        halt = self.acct.register_exit(ts=bar.ts + 60, direction=pos.dir,
                                       zone_id=pos.zone_id, reason=reason,
                                       pnl_r=pnl_r, cfg=self.cfg)
        self.pos = None
        self.persist()
        if halt:
            self._alert(halt)
        return halt

    def _alert(self, reason: str) -> None:
        if self.on_halt is not None:
            self.on_halt(reason)

    # ---------- 状态快照（§8.5） ----------
    def persist(self) -> None:
        """持仓字段为完整可重建集（recover.to_position 直接装配：含 qty0/legs/fees 等）。

        v1 扩展 runtime：活动订单组（TRIGGER_SENT/PROTECTED/UNPROTECTED）全字段随快照落盘，
        M7 Runner 重启时据此重建 PaperBroker 组（spec §8.5 字段之外的本地扩展，recover 忽略）。
        """
        pos = self.pos
        self.ledger.dump_state({
            "ts": (self.last_ts or 0) + 60, "state": self.acct.state, "day": self.acct.day,
            "cooldowns": dict(self.acct.cooldowns),
            "trades_today": self.acct.trades_today, "consec_losses": self.acct.consec_losses,
            "day_pnl_r": self.acct.day_pnl_r, "error_stop_fail": self.acct.error_stop_fail,
            "halt_reason": self.acct.halt_reason, "equity": self.equity,
            "cfg_hash": cfg_hash(self.cfg), "pending": self.pending_sig_id,
            "position": None if pos is None else {
                "sig_id": pos.sig_id, "dir": pos.dir, "open_ts": pos.open_ts,
                "entry": pos.entry, "qty0": pos.qty0, "qty": pos.qty, "R": pos.R,
                "stop": pos.stop, "tp": pos.tp, "seg1_px": pos.seg1_px,
                "be_px_target": pos.be_px_target, "zone_id": pos.zone_id, "lev": pos.lev,
                "peak": pos.peak, "be_done": pos.be_done, "seg_done": pos.seg_done,
                "bars_held": pos.bars_held, "mfe_r": pos.mfe_r, "mae_r": pos.mae_r,
                "funding_paid": pos.funding_paid, "fees_paid": pos.fees_paid,
                "legs": [list(x) for x in pos.legs]},
            "runtime": {"groups": [
                {"sig_id": g.sig_id, "bundle": asdict(g.bundle), "stage": g.stage,
                 "qty": g.qty, "stop_px": g.stop_px, "tp_px": g.tp_px,
                 "seg1_px": g.seg1_px, "protect_fills": g.protect_fills,
                 "stop_filled": g.stop_filled, "tp_filled": g.tp_filled}
                for g in self.broker.active()]},
        })
