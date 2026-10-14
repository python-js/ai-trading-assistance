"""MRS 重启恢复（规格书 §8.5/§11；T11）：mrs_state.json（唯一依据）→ 恢复计划。

交叉校验 8.4 订单台账；三类行为：撤孤儿单 / 平孤儿仓 / 拒绝启动（state 损坏）。
v1 口径（登记，M7 Runner 对接点）：
  1) 校验失败（结构/类型/取值域/cfg_hash 不一致）→ RecoverError，调用方拒绝启动（§11）。
  2) 孤儿单 = 订单台账末态活动（TRIGGER SENT 未成交未撤 / 保护单在挂）且不被
     state.pending/position 引用的组 → Action(CANCEL_ORPHAN_ORDER)。
  3) 孤儿仓 = state.position 存在但保护单证据缺失（组缺失/无 STOP PLACED）或已离场证据 →
     Action(FLATTEN_ORPHAN_POSITION)（已离场证据时无动作、仅清位）；计划中 position 置 None。
  4) 账实不符（pending 但订单组缺失 / pending 已成交但无持仓记录）→ RecoverError
     （安全侧拒绝启动；可后续用 mrs_signal 重建，v1 不做）。
  5) 动作执行（撤单/市价平/register_exit 冷却）由 Runner 在恢复时刻以真实 ts 完成；
     本模块纯决策、无 IO。持仓完整字段随 8.5 持久化（engine.persist），to_position 重建。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import CFG, cfg_hash
from .manage import Position

_STATES = {"IDLE", "ARMED", "IN_POS", "COOLDOWN", "HALTED_DAY", "NO_DATA"}
_HALTS = {"", "ERROR_STOP_FAIL", "CONSEC_STOPS", "DAILY_LOSS"}
_REQUIRED = ("ts", "state", "day", "cooldowns", "trades_today", "consec_losses",
             "day_pnl_r", "error_stop_fail", "halt_reason", "equity", "cfg_hash",
             "pending", "position")
_POS_REQUIRED = ("sig_id", "dir", "open_ts", "entry", "qty0", "qty", "R", "stop", "tp",
                 "seg1_px", "be_px_target", "lev", "zone_id", "peak", "be_done",
                 "seg_done", "bars_held", "mfe_r", "mae_r", "funding_paid", "fees_paid",
                 "legs")


class RecoverError(Exception):
    """state 损坏 / 账实不符 → 拒绝启动（§11）。"""


@dataclass(frozen=True, slots=True)
class Action:
    kind: str            # CANCEL_ORPHAN_ORDER | FLATTEN_ORPHAN_POSITION
    sig_id: str
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class RestartPlan:
    fresh: bool
    resume_state: str
    day: int | None
    cooldowns: dict
    trades_today: int
    consec_losses: int
    day_pnl_r: float
    error_stop_fail: int
    halt_reason: str
    equity: float
    pending: str | None
    position: dict | None
    actions: list
    notes: list


def _is_num(x) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float))


def _is_int(x) -> bool:
    return not isinstance(x, bool) and isinstance(x, int)


def _validate_position(p: dict) -> None:
    if not isinstance(p, dict):
        raise RecoverError("STATE_CORRUPT: position 非对象")
    for k in _POS_REQUIRED:
        if k not in p:
            raise RecoverError(f"STATE_CORRUPT: position 缺键 {k}")
    if p["dir"] not in ("LONG", "SHORT"):
        raise RecoverError(f"STATE_CORRUPT: position.dir 非法 {p['dir']!r}")
    for k in ("entry", "qty0", "qty", "R", "stop", "tp", "seg1_px", "be_px_target",
              "peak", "mfe_r", "mae_r", "funding_paid", "fees_paid"):
        if not _is_num(p[k]):
            raise RecoverError(f"STATE_CORRUPT: position.{k} 非数值")
    for k in ("open_ts", "lev", "bars_held"):
        if not _is_int(p[k]):
            raise RecoverError(f"STATE_CORRUPT: position.{k} 非整数")
    for k in ("be_done", "seg_done"):
        if not isinstance(p[k], bool):
            raise RecoverError(f"STATE_CORRUPT: position.{k} 非布尔")
    if not isinstance(p["sig_id"], str) or not isinstance(p["zone_id"], str):
        raise RecoverError("STATE_CORRUPT: position.sig_id/zone_id 非字符串")
    if not isinstance(p["legs"], list):
        raise RecoverError("STATE_CORRUPT: position.legs 非列表")
    if p["qty"] <= 0 or p["R"] <= 0:
        raise RecoverError("STATE_CORRUPT: position.qty/R 非正")


def _validate(state: dict, cfg: dict) -> None:
    if not isinstance(state, dict):
        raise RecoverError("STATE_CORRUPT: 快照非对象")
    for k in _REQUIRED:
        if k not in state:
            raise RecoverError(f"STATE_CORRUPT: 缺键 {k}")
    if state["state"] not in _STATES:
        raise RecoverError(f"STATE_CORRUPT: 未知状态 {state['state']!r}")
    if state["cfg_hash"] != cfg_hash(cfg):
        raise RecoverError("STATE_CORRUPT: cfg_hash 不一致（配置已变更，禁止盲恢复）")
    cd = state["cooldowns"]
    if not isinstance(cd, dict) or any(
            not _is_int(v) or v < 0 for v in cd.values()):
        raise RecoverError("STATE_CORRUPT: cooldowns 非法")
    for k in ("trades_today", "consec_losses", "error_stop_fail"):
        if not _is_int(state[k]) or state[k] < 0 or state[k] > 10_000:
            raise RecoverError(f"STATE_CORRUPT: {k} 非法")
    if state["day"] is not None and not _is_int(state["day"]):
        raise RecoverError("STATE_CORRUPT: day 非法")
    if not _is_num(state["day_pnl_r"]) or not _is_num(state["equity"]) \
            or state["equity"] <= 0:
        raise RecoverError("STATE_CORRUPT: day_pnl_r/equity 非法")
    if state["halt_reason"] not in _HALTS:
        raise RecoverError(f"STATE_CORRUPT: halt_reason 非法 {state['halt_reason']!r}")
    if state["pending"] is not None and not isinstance(state["pending"], str):
        raise RecoverError("STATE_CORRUPT: pending 非字符串")
    if state["position"] is not None:
        _validate_position(state["position"])


def _order_view(orders: list[dict]) -> dict[str, dict]:
    """订单台账 → 每 sig_id 活动证据。"""
    v: dict[str, dict] = {}
    for r in orders:
        g = v.setdefault(r.get("sig_id"), {"trigger_sent": False, "trigger_filled": False,
                                           "protect_placed": False, "cancelled": False,
                                           "exited": False})
        typ, st = r.get("type"), r.get("state")
        if typ == "TRIGGER":
            g["trigger_sent"] |= st == "SENT"
            g["trigger_filled"] |= st == "FILLED"
        elif typ in ("STOP_MARKET", "STOP_MARKET_REDUCE"):
            if st in ("PLACED", "AMENDED"):
                g["protect_placed"] = True
            elif st == "FILLED":
                g["exited"] = True
        elif typ == "MARKET_REDUCE" and st == "FILLED":
            g["exited"] = True
        elif typ == "CANCEL" and st == "CANCELLED":
            g["cancelled"] = True
    return v


def plan_restart(state: dict | None, orders: list[dict], cfg: dict = CFG) -> RestartPlan:
    """快照 + 订单台账 → 恢复计划；损坏/账实不符 → RecoverError。"""
    if state is None:
        return RestartPlan(fresh=True, resume_state="IDLE", day=None, cooldowns={},
                           trades_today=0, consec_losses=0, day_pnl_r=0.0,
                           error_stop_fail=0, halt_reason="",
                           equity=float(cfg["account"]["init_equity"]),
                           pending=None, position=None, actions=[],
                           notes=["首次启动（无快照）"])
    _validate(state, cfg)
    views = _order_view(orders)
    pend, pos = state["pending"], state["position"]
    actions: list[Action] = []
    notes: list[str] = []

    # --- pending 交叉校验 ---
    if pend is not None:
        g = views.get(pend)
        if g is None:
            raise RecoverError(f"LEDGER_INCONSISTENT: 挂单 {pend} 在订单台账中缺失")
        if g["trigger_filled"]:
            if pos is None or pos["sig_id"] != pend:
                raise RecoverError(f"LEDGER_INCONSISTENT: 挂单 {pend} 已成交但无持仓记录")
            pend = None  # 成交与持仓记录齐备：pending 冗余清除
        elif g["cancelled"]:
            notes.append(f"挂单 {pend} 已撤（台账证据），pending 清除")
            pend = None
        elif not g["trigger_sent"]:
            raise RecoverError(f"LEDGER_INCONSISTENT: 挂单 {pend} 无在途证据")

    # --- 孤儿仓 / 持仓交叉校验 ---
    if pos is not None:
        g = views.get(pos["sig_id"])
        if g is None or not g["protect_placed"]:
            actions.append(Action("FLATTEN_ORPHAN_POSITION", pos["sig_id"],
                                  {"dir": pos["dir"], "qty": pos["qty"], "R": pos["R"]}))
            notes.append(f"孤儿仓 {pos['sig_id']}：无保护单证据 → 市价平")
            pos = None
        elif g["exited"]:
            notes.append(f"持仓 {pos['sig_id']} 已有离场证据（台账），按已平恢复")
            pos = None

    # --- 孤儿单（活动组且不被引用）---
    keep = {s for s in (pend, (pos or {}).get("sig_id")) if s}
    for sid in sorted(views):
        g = views[sid]
        active = (g["trigger_sent"] and not g["trigger_filled"] and not g["cancelled"]) \
            or (g["protect_placed"] and not g["exited"])
        if active and sid not in keep:
            actions.append(Action("CANCEL_ORPHAN_ORDER", sid))
            notes.append(f"孤儿单 {sid}：活动组无 state 引用 → 撤单")

    # --- 恢复态重算 ---
    if state["state"] in ("HALTED_DAY", "NO_DATA"):
        resume = state["state"]
    elif pos is not None:
        resume = "IN_POS"
    elif pend is not None:
        resume = "ARMED"
    else:
        resume = state["state"] if state["state"] == "COOLDOWN" else "IDLE"

    return RestartPlan(fresh=False, resume_state=resume, day=state["day"],
                       cooldowns=dict(state["cooldowns"]),
                       trades_today=state["trades_today"],
                       consec_losses=state["consec_losses"],
                       day_pnl_r=state["day_pnl_r"],
                       error_stop_fail=state["error_stop_fail"],
                       halt_reason=state["halt_reason"], equity=state["equity"],
                       pending=pend, position=pos, actions=actions, notes=notes)


def to_position(d: dict) -> Position:
    """8.5 持仓字段 → Position 对象（Runner 恢复装配用）。"""
    return Position(sig_id=d["sig_id"], dir=d["dir"], open_ts=d["open_ts"], entry=d["entry"],
                    qty0=d["qty0"], qty=d["qty"], R=d["R"], stop=d["stop"], tp=d["tp"],
                    seg1_px=d["seg1_px"], be_px_target=d["be_px_target"], zone_id=d["zone_id"],
                    lev=d["lev"], peak=d["peak"], be_done=d["be_done"], seg_done=d["seg_done"],
                    bars_held=d["bars_held"], mfe_r=d["mfe_r"], mae_r=d["mae_r"],
                    funding_paid=d["funding_paid"], fees_paid=d["fees_paid"],
                    legs=[tuple(x) for x in d["legs"]])
