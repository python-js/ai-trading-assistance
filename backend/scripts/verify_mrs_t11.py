"""MRS T11（规格书 §8.5/§11）：重启恢复（recover.plan_restart）验证。

矩阵（离线 fixture + 一组真实落盘 E2E）：
  A) 干净恢复：fresh / IDLE / ARMED / IN_POS / COOLDOWN / HALTED_DAY —— 恢复态正确、零动作。
  B) 孤儿单：活动组无 state 引用 → 恰 CANCEL_ORPHAN_ORDER（逐一且不误伤在管组）。
  C) 孤儿仓：保护证据缺失/组缺失 → 恰 FLATTEN_ORPHAN_POSITION 且 position=None；
     已有离场证据（stop 成交 / 市价平）→ 无动作仅清位。
  D) 损坏快照：5 变体 × 5 基态 → RecoverError（拒绝启动，§11 安全侧）。
  E) 账实不符：pending 缺组 / pending 已成交无持仓 → RecoverError；
     pending 已撤 → 清除+IDLE；成交且持仓齐备 → pending 冗余清除+IN_POS。
  F) E2E：Rig 真实落盘（mrs_state.json + mrs_orders.jsonl）→ plan_restart → resume IN_POS、
     零动作、to_position(snapshot) 与引擎内存 Position 全等（snap 对账）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t11.py
"""
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))       # scripts/（mrs_snap / mrs_rig）
sys.path.insert(0, str(_HERE.parents[1]))   # backend/（app.*）

from app.mrs.config import CFG, cfg_hash  # noqa: E402
from app.mrs.ledger import Ledger  # noqa: E402
from app.mrs.recover import RecoverError, plan_restart, to_position  # noqa: E402
from mrs_rig import Rig, ts_of  # noqa: E402
from mrs_snap import snap  # noqa: E402

CH = cfg_hash(CFG)
SIG = "s1"
TRIG_SENT = ("TRIGGER", "SENT")
TRIG_FILLED = ("TRIGGER", "FILLED")
STOP_PLACED = ("STOP_MARKET_REDUCE", "PLACED")
STOP_FILLED = ("STOP_MARKET_REDUCE", "FILLED")
MKT_FILLED = ("MARKET_REDUCE", "FILLED")
CANCELED = ("CANCEL", "CANCELLED")


def mk_pos(sig: str = SIG, **kw) -> dict:
    d = dict(sig_id=sig, dir="LONG", open_ts=ts_of("14:01"), entry=1708.44162,
             qty0=5.4687, qty=5.4687, R=32.0, stop=1676.1, tp=1767.0, seg1_px=1746.5,
             be_px_target=1712.8, zone_id="zs", lev=3, peak=1708.44162, be_done=False,
             seg_done=False, bars_held=3, mfe_r=0.2, mae_r=-0.1, funding_paid=0.0,
             fees_paid=1.5, legs=[[1708.44162, 5.4687, "ENTRY"]])
    d.update(kw)
    return d


def mk_state(base: str, *, pos=None, pending=None, **kw) -> dict:
    d = dict(ts=ts_of("14:05"), state=base, day=20000, cooldowns={}, trades_today=0,
             consec_losses=0, day_pnl_r=0.0, error_stop_fail=0, halt_reason="",
             equity=50_000.0, cfg_hash=CH, pending=pending, position=pos)
    d.update(kw)
    return d


def rows(*specs) -> list:
    """订单台账行生成：spec = (sig_id,) + (type, state)。"""
    return [{"ts": ts_of("14:00"), "sig_id": sig, "type": typ, "state": st}
            for sig, typ, st in specs]


def mk_base(name: str) -> tuple:
    if name == "IDLE":
        return mk_state("IDLE"), []
    if name == "ARMED":
        return mk_state("ARMED", pending=SIG), rows((SIG,) + TRIG_SENT)
    if name == "IN_POS":
        return (mk_state("IN_POS", pos=mk_pos()),
                rows((SIG,) + TRIG_FILLED, (SIG,) + STOP_PLACED))
    if name == "COOLDOWN":
        return mk_state("COOLDOWN", cooldowns={"LONG": ts_of("14:27")}), []
    if name == "HALTED_DAY":
        return mk_state("HALTED_DAY", halt_reason="CONSEC_STOPS", consec_losses=4), []
    raise AssertionError(name)


def acts(r) -> list:
    return [(a.kind, a.sig_id) for a in r.actions]


def raises(fn) -> bool:
    try:
        fn()
    except RecoverError:
        return True
    return False


def case_fresh() -> None:
    r = plan_restart(None, [])
    assert r.fresh and r.resume_state == "IDLE" and r.pending is None
    assert r.position is None and r.actions == []
    assert r.equity == float(CFG["account"]["init_equity"])
    assert r.cooldowns == {} and r.trades_today == 0 and r.halt_reason == ""
    assert r.notes == ["首次启动（无快照）"]
    print("  [A0] 无快照 → fresh/IDLE/初始权益 通过 ✅")


def case_clean(bases: tuple) -> None:
    for name in bases:
        state, orders = mk_base(name)
        r = plan_restart(state, orders)
        assert not r.fresh and r.resume_state == name, (name, r.resume_state)
        assert r.actions == [], (name, acts(r))
        assert r.day == 20000 and r.equity == 50_000.0
        if name == "ARMED":
            assert r.pending == SIG
        if name == "IN_POS":
            assert r.pending is None and r.position["sig_id"] == SIG
            p = to_position(r.position)  # round-trip：dict → Position
            assert p.sig_id == SIG and p.dir == "LONG" and p.entry == 1708.44162
            assert p.legs == [(1708.44162, 5.4687, "ENTRY")], "legs 还原为元组"
            assert p.qty == 5.4687 and p.R == 32.0 and p.bars_held == 3
        if name == "COOLDOWN":
            assert r.cooldowns == {"LONG": ts_of("14:27")}
        if name == "HALTED_DAY":
            assert r.halt_reason == "CONSEC_STOPS" and r.consec_losses == 4
    print(f"  [A] 干净恢复 ×{len(bases)} 基态 → 恢复态/零动作/字段保持 通过 ✅")


def case_orphan_orders() -> None:
    state = mk_state("IN_POS", pos=mk_pos())
    orders = rows((SIG,) + TRIG_FILLED, (SIG,) + STOP_PLACED,
                  ("orph1",) + TRIG_SENT,
                  ("orph2",) + TRIG_FILLED, ("orph2",) + STOP_PLACED)
    r = plan_restart(state, orders)
    assert acts(r) == [("CANCEL_ORPHAN_ORDER", "orph1"),
                       ("CANCEL_ORPHAN_ORDER", "orph2")], acts(r)
    assert r.resume_state == "IN_POS" and r.position["sig_id"] == SIG, "在管组不被误伤"
    assert sum("孤儿单" in n for n in r.notes) == 2
    print("  [B] 孤儿单 ×2（未成交挂单 / 在挂保护）→ 恰撤两单、在管组保留 通过 ✅")


def case_orphan_positions() -> None:
    # C1 有成交无保护单证据 → 平
    r = plan_restart(mk_state("IN_POS", pos=mk_pos()), rows((SIG,) + TRIG_FILLED))
    assert acts(r) == [("FLATTEN_ORPHAN_POSITION", SIG)], acts(r)
    assert r.position is None and r.resume_state == "IDLE"
    # C2 订单组缺失 → 平
    r = plan_restart(mk_state("IN_POS", pos=mk_pos()), [])
    assert acts(r) == [("FLATTEN_ORPHAN_POSITION", SIG)] and r.position is None
    # C3 已有止损成交离场证据 → 无动作仅清位
    r = plan_restart(mk_state("IN_POS", pos=mk_pos()),
                     rows((SIG,) + TRIG_FILLED, (SIG,) + STOP_PLACED, (SIG,) + STOP_FILLED))
    assert r.actions == [] and r.position is None and r.resume_state == "IDLE"
    assert any("离场证据" in n for n in r.notes)
    # C4 已有市价平离场证据 → 同 C3
    r = plan_restart(mk_state("IN_POS", pos=mk_pos()),
                     rows((SIG,) + TRIG_FILLED, (SIG,) + STOP_PLACED, (SIG,) + MKT_FILLED))
    assert r.actions == [] and r.position is None
    print("  [C] 孤儿仓（无保护/组缺失 → 平；已离场证据 → 仅清位）通过 ✅")


def case_corrupt() -> None:
    variants = {
        "trades_today 类型": lambda s: s.__setitem__("trades_today", "12"),
        "cfg_hash 不符": lambda s: s.__setitem__("cfg_hash", "deadbeef"),
        "缺键 cooldowns": lambda s: s.pop("cooldowns"),
        "未知状态名": lambda s: s.__setitem__("state", "ZZZ"),
        "仓位字段非数值": lambda s: s.__setitem__("position", mk_pos(entry="x")),
    }
    n = 0
    for vname, mut in variants.items():
        for base in ("IDLE", "ARMED", "IN_POS", "COOLDOWN", "HALTED_DAY"):
            state, orders = mk_base(base)
            mut(state)
            assert raises(lambda: plan_restart(state, orders)), (vname, base)
            n += 1
    assert raises(lambda: plan_restart([], [])), "根非对象"
    print(f"  [D] 损坏快照 5 变体 × 5 基态（+根非对象）→ RecoverError 拒绝启动 ×{n + 1} 通过 ✅")


def case_ledger_mismatch() -> None:
    # E1 pending 但订单组缺失
    assert raises(lambda: plan_restart(mk_state("ARMED", pending=SIG), []))
    # E2 pending 已成交但无持仓记录
    assert raises(lambda: plan_restart(mk_state("ARMED", pending=SIG),
                                       rows((SIG,) + TRIG_FILLED)))
    # E3 pending 已撤 → 清除 + IDLE
    r = plan_restart(mk_state("ARMED", pending=SIG),
                     rows((SIG,) + TRIG_SENT, (SIG,) + CANCELED))
    assert r.pending is None and r.resume_state == "IDLE" and r.actions == []
    assert any("已撤" in n for n in r.notes)
    # E4 成交且持仓齐备 → pending 冗余清除 + IN_POS
    r = plan_restart(mk_state("IN_POS", pending=SIG, pos=mk_pos()),
                     rows((SIG,) + TRIG_FILLED, (SIG,) + STOP_PLACED))
    assert r.pending is None and r.resume_state == "IN_POS" and r.actions == []
    print("  [E] 账实不符拒绝 ×2 / 已撤清除 / 成交冗余清除 通过 ✅")


def case_e2e() -> None:
    with tempfile.TemporaryDirectory() as td:
        rig = Rig(ledger=Ledger(out_dir=td))
        rig.prep()
        rig.inject()
        assert rig.signal() == ("OK", None)
        assert rig.fill() == ("BUSY", None)  # 14:01 成交 + 保护单
        state = json.loads((pathlib.Path(td) / "mrs_state.json").read_text(encoding="utf-8"))
        lines = (pathlib.Path(td) / "mrs_orders.jsonl").read_text(encoding="utf-8").splitlines()
        orders = [json.loads(x) for x in lines]
        assert len(orders) == 4, "SENT/FILLED/tp PLACED/stop PLACED"
        assert state["state"] == "IN_POS" and state["pending"] is None
        assert state["cfg_hash"] == CH and state["cooldowns"] == {}
        r = plan_restart(state, orders)
        assert not r.fresh and r.resume_state == "IN_POS" and r.actions == [], acts(r)
        assert r.pending is None and r.day == rig.eng.acct.day
        assert r.equity == rig.eng.equity
        assert snap(to_position(r.position)) == snap(rig.eng.pos), "快照 ⇄ 内存持仓全等"
        assert snap(to_position(r.position)) == snap(to_position(state["position"]))
        print("  [F] E2E：磁盘快照+真实订单台账 → IN_POS 零动作、持仓全等 通过 ✅")


if __name__ == "__main__":
    case_fresh()
    case_clean(("IDLE", "ARMED", "IN_POS", "COOLDOWN", "HALTED_DAY"))
    case_orphan_orders()
    case_orphan_positions()
    case_corrupt()
    case_ledger_mismatch()
    case_e2e()
    print("T11 全部通过 ✅")
