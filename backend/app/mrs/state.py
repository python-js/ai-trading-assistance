"""MRS 账户状态机与频控（规格书 §1.4/§6.5/§7.3）：全部按 UTC 日重置。

v1 口径（规格书留白处登记）：
  1) 日最大笔数 10 为模块常量（§6.5 文本值，§2 配置块未含）。
  2) 连损计数 = 离场 pnl_usdt < 0 的连续笔数（§6.5 "连损3笔"），达 ≥ consec_stops → HALTED_DAY。
  3) 止损离场计数 = reason ∈ {STOP, BE}（BE 为挪至保本的止损单成交）；第 2 次同 (zone,dir) → 当日拉黑。
  4) 撤单/过期（无持仓）→ COOLDOWN 5 根 1m 双向禁开（§7.3 "COOLDOWN(5)"）；
     真仓离场 → 25 根同向禁开（§6.5 risk.cooldown_min_bars）。冷却窗口按方向分存
     （cooldowns: dir→截止 ts），新事件取 max 只延长不缩短既有窗口——否则反向单离场/
     TTL 撤单会覆盖并"提前解除"既有同向冷却（T14：无冷却的连续同向开仓必须不可达）。
  5) 熔断三源：日内累计 pnl_r ≤ -daily_loss_r / 连损 ≥ consec_stops / error_stop_fail ≥ 2。
  6) HALTED_DAY 于 UTC 日切自动解除；NO_DATA 由数据层置入/解除（§7.3 只出不进、保留交易所侧保护）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_TRADES_PER_DAY = 10
COOLDOWN_BARS_TTL = 5
_STOP_REASONS = ("STOP", "BE")


@dataclass
class AccountState:
    state: str = "IDLE"  # IDLE|ARMED|IN_POS|COOLDOWN|HALTED_DAY|NO_DATA
    day: int | None = None
    cooldowns: dict = field(default_factory=dict)     # dir -> 禁开截止 ts（§6.5，按方向分存）
    trades_today: int = 0
    consec_losses: int = 0
    day_pnl_r: float = 0.0
    error_stop_fail: int = 0
    stop_outs: dict = field(default_factory=dict)     # (zone_id, dir) -> 次数
    zone_touches: dict = field(default_factory=dict)  # zone_id -> 当日开仓数
    t1_used: set = field(default_factory=set)         # ("T1", zone_id)
    halt_reason: str = ""

    # ---------- 日切 ----------
    def roll_day(self, day: int) -> None:
        if self.day == day:
            return
        self.day = day
        self.trades_today = 0
        self.consec_losses = 0
        self.day_pnl_r = 0.0
        self.error_stop_fail = 0
        self.stop_outs.clear()
        self.zone_touches.clear()
        self.t1_used.clear()
        if self.state == "HALTED_DAY":
            self.state = "IDLE"
            self.halt_reason = ""

    # ---------- 门 ----------
    def in_cooldown(self, now_ts: int, direction: str) -> bool:
        return now_ts < self.cooldowns.get(direction, 0)

    def can_enter(self, now_ts: int, direction: str) -> tuple[bool, str]:
        """返回 (允许, 拒因)。拒因为空串表示允许。"""
        if self.state in ("HALTED_DAY", "NO_DATA", "ARMED", "IN_POS"):
            return False, self.state
        if self.in_cooldown(now_ts, direction):
            return False, "COOLDOWN"
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return False, "TRADES_CAP"
        return True, ""

    def zone_blacklisted(self, zone_id: str, direction: str) -> bool:
        return self.stop_outs.get((zone_id, direction), 0) >= 2  # §1.4 第 2 次止损当日拉黑

    def blacklist_set(self) -> set:
        return {k for k, n in self.stop_outs.items() if n >= 2}

    def maybe_resume(self, now_ts: int) -> None:
        """全部冷却窗口到期 → COOLDOWN 态翻回 IDLE（禁令本身按方向由 cooldowns 控制）。"""
        if self.state == "COOLDOWN" and all(v <= now_ts for v in self.cooldowns.values()):
            self.state = "IDLE"

    # ---------- 事件 ----------
    def register_entry(self, zone_id: str, ts: int) -> None:
        self.trades_today += 1
        self.zone_touches[zone_id] = self.zone_touches.get(zone_id, 0) + 1
        self.state = "IN_POS"

    def register_ttl_abort(self, ts: int, cfg=None) -> None:
        until = ts + COOLDOWN_BARS_TTL * 60
        for d in ("LONG", "SHORT"):
            self.cooldowns[d] = max(self.cooldowns.get(d, 0), until)
        self.state = "COOLDOWN"

    def register_exit(self, *, ts: int, direction: str, zone_id: str, reason: str,
                      pnl_r: float, cfg) -> str:
        """离场登记：冷却/连损/拉黑/熔断。返回 "" 或熔断原因。"""
        risk = cfg["risk"]
        until = ts + risk["cooldown_min_bars"] * 60
        self.cooldowns[direction] = max(self.cooldowns.get(direction, 0), until)
        if reason in _STOP_REASONS:
            k = (zone_id, direction)
            self.stop_outs[k] = self.stop_outs.get(k, 0) + 1
        self.day_pnl_r += pnl_r
        self.consec_losses = self.consec_losses + 1 if pnl_r < 0 else 0
        halt = ""
        if self.day_pnl_r <= -abs(cfg["manage"]["kill"]["daily_loss_r"]):
            halt = "DAILY_LOSS"
        elif self.consec_losses >= cfg["manage"]["kill"]["consec_stops"]:
            halt = "CONSEC_STOPS"
        if halt:
            self.halt(halt)
            return halt
        self.state = "COOLDOWN"
        return ""

    def note_error_stop_fail(self, cfg) -> str:
        self.error_stop_fail += 1
        if self.error_stop_fail >= cfg["manage"]["kill"]["error_stop_fail"]:
            self.halt("ERROR_STOP_FAIL")
            return "ERROR_STOP_FAIL"
        return ""

    # ---------- 熔断 ----------
    def halt(self, reason: str) -> None:
        self.state = "HALTED_DAY"
        self.halt_reason = reason
