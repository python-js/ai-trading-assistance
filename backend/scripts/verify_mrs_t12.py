"""MRS T12（规格书 §10 / §6.3-B）：保护单失败路径（UNPROTECTED → 市价平 → HALTED_DAY）。

- A：保护单 3 连败 → UNPROTECTED → 当根市价 KILL + error_stop_fail=1（未熔断）。
- B：失败 2 次后第 3 次成功 → PROTECTED、error_stop_fail=0、持仓在管。
- C：两次构造性失败（第二次为自然再信号）→ error_stop_fail=2 → HALTED_DAY + on_halt 回调，
  次 bar funnel 记 HALT。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t12.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))   # scripts/（导入 mrs_rig）

from mrs_rig import Rig, ts_of  # noqa: E402


def near(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


def case_a() -> None:
    rig = Rig(fail_place_stop=3)
    rig.prep()
    rig.inject()
    assert rig.signal() == ("OK", None)
    assert rig.fill() == ("BUSY", None)  # 14:01 成交→保护三败→KILL
    sig = rig.led.signals[0]
    assert sig["type"] == "T2" and sig["suppressed"] == "T1"
    b = sig["bundle"]
    assert b["trigger"] == 1708.1 and b["R"] == 32.0 and b["qty"] == 5.4687
    assert near(b["stop"], 1676.1) and b["tp"] == 1767.0, b
    g = rig.eng.broker.get(sig["sig_id"])
    assert g.stage == "CLOSED" and g.protect_fills == 0
    assert sum(1 for r in g.order_log if r["state"] == "FAIL") == 3, "保护单 3 连败留痕"
    init, exit_ = rig.led.trades
    assert init["event"] == "init" and near(init["entry_px_real"], 1708.44162, 1e-6)
    assert exit_["event"] == "exit" and exit_["reason"] == "KILL"
    assert near(exit_["px"], 1700.1599, 1e-6), exit_["px"]
    assert abs(exit_["pnl_usdt"] + 54.61055) < 1e-4, exit_["pnl_usdt"]
    assert abs(exit_["pnl_R"] + 0.31206) < 1e-4, exit_["pnl_R"]
    assert exit_["bars_held"] == 0 and exit_["funding_paid"] == 0
    assert rig.eng.pos is None and rig.eng.acct.error_stop_fail == 1
    assert rig.eng.acct.state == "COOLDOWN"
    assert rig.eng.acct.cooldowns == {"LONG": ts_of("14:27")}
    assert abs(rig.eng.equity - (50_000.0 - 54.61055)) < 1e-4
    for _ in range(12):  # 14:02–14:13
        assert rig.flat() == ("COOLDOWN", None)
    assert rig.led.funnel_counts == {"REGIME": 15, "OK": 1, "BUSY": 1, "COOLDOWN": 12}
    print("  [A] 保护单三败 → KILL/计数/esf=1/冷却至 14:27 + 12 根 COOLDOWN 通过 ✅")


def case_b() -> None:
    rig = Rig(fail_place_stop=2)
    rig.prep()
    rig.inject()
    assert rig.signal() == ("OK", None)
    assert rig.fill() == ("BUSY", None)
    g = rig.eng.broker.get(rig.led.signals[0]["sig_id"])
    assert g.stage == "PROTECTED" and g.protect_fills == 1, "第 3 次尝试成功"
    assert sum(1 for r in g.order_log if r["state"] == "FAIL") == 2
    assert rig.eng.acct.error_stop_fail == 0 and rig.eng.pos is not None
    assert rig.eng.acct.state == "IN_POS"
    assert len(rig.led.trades) == 1 and rig.led.trades[0]["event"] == "init"
    assert rig.flat() == ("BUSY", None)  # 14:02 管理根（不触发任何动作）
    assert rig.eng.pos.bars_held == 1
    assert rig.led.funnel_counts == {"REGIME": 15, "OK": 1, "BUSY": 2}
    print("  [B] 两败后第三次成功 → PROTECTED/持仓在管/esf=0 通过 ✅")


def case_c() -> None:
    calls: list[str] = []
    rig = Rig(fail_place_stop=3, on_halt=calls.append)
    rig.prep()
    rig.inject()
    assert rig.signal() == ("OK", None)
    assert rig.fill() == ("BUSY", None)
    assert rig.eng.acct.error_stop_fail == 1
    for _ in range(12):  # 14:02–14:13
        assert rig.flat() == ("COOLDOWN", None)
    assert rig.flat() == ("REGIME", None)  # 14:14：5m/15m 同收 → adx 冲刷为 None（瞬时）
    rig.inject()                           # HTF 收盘后重注入
    for _ in range(8):  # 14:15–14:22
        assert rig.flat() == ("COOLDOWN", None)
    for _ in range(3):  # 14:23–14:25：铺垫根（close 1696 离 ZS 过远 → ZONE）
        assert rig.push() == ("ZONE", None)
    assert rig.eng.acct.cooldowns["LONG"] == ts_of("14:27")
    assert rig.signal() == ("OK", None)  # 14:26：now=14:27 冷却到期 → 第二信号
    sig2 = rig.led.signals[1]
    assert sig2["type"] == "T2" and sig2["bundle"]["trigger"] == 1708.1
    assert sig2["bundle"]["R"] == 32.0 and near(sig2["bundle"]["stop"], 1676.1)
    assert near(sig2["bundle"]["tp"], 1767.1), sig2["bundle"]["tp"]
    assert near(sig2["bundle"]["qty"], 5.4627), sig2["bundle"]["qty"]
    assert sig2["sig_id"] != rig.led.signals[0]["sig_id"]
    rig.eng.broker.fail_place_stop = 3  # 第二轮保护单再全败
    assert rig.fill() == ("BUSY", None)  # 14:27 成交→KILL→esf=2→HALTED
    assert rig.flat() == ("HALT", None)  # 14:28
    assert calls == ["ERROR_STOP_FAIL"], calls
    assert rig.eng.acct.state == "HALTED_DAY"
    assert rig.eng.acct.halt_reason == "ERROR_STOP_FAIL"
    assert rig.eng.acct.error_stop_fail == 2
    assert [t["reason"] for t in rig.led.trades if t["event"] == "exit"] == ["KILL", "KILL"]
    assert all(g.stage == "CLOSED" for g in rig.eng.broker.groups.values())
    assert rig.led.funnel_counts == {"REGIME": 16, "OK": 2, "BUSY": 2,
                                     "COOLDOWN": 20, "ZONE": 3, "HALT": 1}
    print("  [C] 二次自然失败 → esf=2 → HALTED_DAY + on_halt + HALT 漏斗通过 ✅")


if __name__ == "__main__":
    case_a()
    case_b()
    case_c()
    print("T12 全部通过 ✅")
