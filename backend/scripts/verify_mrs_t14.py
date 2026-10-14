"""MRS T14（规格书 §10）：模型检测（不变量）与端到端冒烟。

三部分：
1) 受限 BFS（状态等价类合并 + 时间跳跃抽象——§10 T14 "状态全遍历" 的可行化，
   全量时间×PnL 网格 ~10^7 不可行）：
   - 驱动真实 AccountState 接口；模型独立维护"口径冷却剩余"（gt），每态断言
     gt ≡ 账户内部冷却（捕获 §6.5 单对覆盖类缺陷）；
   - 抽象化取舍（登记）：时间跳跃 WAIT 取"跳到最近冷却到期"（唯一时间依赖；无冷却时
     无时间依赖、不生成边）；canon 中冷却取零/非零位（到期边界语义由第 2 部分单测覆盖）、
     day_pnl_r 不入 canon（仅影响熔断阈值、不改变控制骨架；熔断可达性由覆盖断言保证）；
   - 探索沿真实状态机演进（发现 violation 必为真；完备性限于该抽象空间）；
   - INV1 无保护持仓（POS_NP）为瞬态：唯一出口 = 错误通道市价平（同 bar 原子闭合，
     无 WAIT 边）；该原子性由 T12 对真实引擎实证；
   - INV2 can_enter 放行 ⟹ 口径冷却已到期（"无冷却的连续同向开仓"不可达）；
   - 熔断三源（ERROR_STOP_FAIL×2 / 连损×3 / 日亏≤-3R）均可达且 HALTED 拒绝开仓。
2) 单元场景：日切清除 HALTED/计数、冷却跨日不重置、per-dir 覆盖缺陷回放（S1）、
   TTL 双向 5 根、max 只延不缩。
3) 冒烟（复用 mrs_rig）：自然止损全链路数值 + 冷却期内反向离场不得解锁同向
   （缺陷回放 E2E：旧实现该处会放行）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t14.py
"""
import pathlib
import sys
from copy import deepcopy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))      # scripts/

from app.mrs.config import CFG
from app.mrs.state import MAX_TRADES_PER_DAY, AccountState
from mrs_rig import Rig, ts_of

# ---- 抽象动作的收益档（§6.5 熔断可覆盖性：-0.9×3 触发连损但不触日亏）----
_WIN, _LOSS, _BIG, _KILL = 1.5, -0.9, -3.5, -0.3
_DAY = 86_400
_CAP = 1500  # 25 根冷却（秒）


def canon(S):
    """等价类归一（t 相对化；冷却取零/非零位、day_pnl_r/halt_reason 不入类——
    见模块 docstring 抽象取舍）。"""
    st, t, a, gtL, gtS = S
    return (st, a.state, min(a.trades_today, MAX_TRADES_PER_DAY),
            min(a.consec_losses, 3), min(a.error_stop_fail, 2),
            int(gtL > 0), int(gtS > 0))


def expand(S, flags):
    st, t, a, gtL, gtS = S
    gts = {"LONG": gtL, "SHORT": gtS}
    for d in ("LONG", "SHORT"):  # 核心不变量：口径冷却 ≡ 账户内部冷却（per-dir）
        i = max(0, a.cooldowns.get(d, 0) - t)
        assert gts[d] == i, f"冷却口径不一致 {d}: 模型 {gts[d]}s vs 账户 {i}s（§6.5）"

    if st.startswith("POS_NP"):  # INV1：唯一出口 = 错误通道市价平（无 WAIT 边）
        d = st[7:]
        a2 = deepcopy(a)
        a2.register_exit(ts=t, direction=d, zone_id="z", reason="KILL", pnl_r=_KILL, cfg=CFG)
        a2.note_error_stop_fail(CFG)
        gg = max(gts[d], _CAP)
        flags.add("KILL")
        return [("IDLE", t, a2, gg if d == "LONG" else gtL,
                 gg if d == "SHORT" else gtS)]

    # WAIT = 跳到最近冷却到期（时间维唯一依赖；无正冷却时无时间依赖、不生成边）
    dt = min((g for g in (gtL, gtS) if g > 0), default=0)
    out = []
    if dt:
        a_w = deepcopy(a)
        a_w.maybe_resume(t + dt)
        out.append((st, t + dt, a_w, max(0, gtL - dt), max(0, gtS - dt)))

    if st == "IDLE":
        for d in ("LONG", "SHORT"):
            ok, why = a.can_enter(t, d)
            if ok:
                assert gts[d] == 0, f"can_enter 放行但冷却未到（{d}，INV2）"
                a2 = deepcopy(a)
                a2.register_entry("z", t)
                flags.add("ENTRY")
                out.append((f"PEND_{d}", t, a2, gtL, gtS))
            elif gts[d] > 0 and a.state == "COOLDOWN":
                assert why == "COOLDOWN", f"冷却应拒为 COOLDOWN，实为 {why}"
            elif why == "TRADES_CAP":
                flags.add("CAP10")
        if a.state == "HALTED_DAY":
            ko, kw = a.can_enter(t, "LONG")
            assert not ko and kw == "HALTED_DAY", (ko, kw)
            flags.add("HALT_REJECT")
    elif st.startswith("PEND"):
        d = st[5:]
        out.append((f"POS_{d}", t, deepcopy(a), gtL, gtS))       # 保护单就位
        out.append((f"POS_NP_{d}", t, deepcopy(a), gtL, gtS))    # 保护单失败（瞬态）
        a2 = deepcopy(a)
        a2.register_ttl_abort(t, CFG)
        out.append(("IDLE", t, a2, max(gtL, 300), max(gtS, 300)))
        flags.add("FILL_OK")
        flags.add("FILL_FAIL")
        flags.add("TTL")
    else:  # POS_LONG / POS_SHORT（有保护单）
        d = st[4:]
        for pnl_r, tag in ((_WIN, "WIN"), (_LOSS, "LOSS"), (_BIG, "BIGLOSS")):
            a2 = deepcopy(a)
            a2.register_exit(ts=t, direction=d, zone_id="z", reason="STOP",
                             pnl_r=pnl_r, cfg=CFG)
            gg = max(gts[d], _CAP)
            out.append(("IDLE", t, a2, gg if d == "LONG" else gtL,
                        gg if d == "SHORT" else gtS))
            flags.add(tag)

    for _, _, a2, *_ in out:
        if a2.halt_reason == "ERROR_STOP_FAIL":
            flags.add("HALT_ERR")
        elif a2.halt_reason == "CONSEC_STOPS":
            flags.add("HALT_CONSEC")
        elif a2.halt_reason == "DAILY_LOSS":
            flags.add("HALT_DAILY")
    return out


def bfs() -> None:
    flags: set[str] = set()
    a0 = AccountState()
    a0.roll_day(0)
    frontier = [("IDLE", 1_000, a0, 0, 0)]
    seen: set = set()
    while frontier:
        nxt = []
        for S in frontier:
            k = canon(S)
            if k in seen:
                continue
            seen.add(k)
            for S2 in expand(S, flags):
                if S2[1] < _DAY:  # 单日窗口（日切行为见单元场景）
                    nxt.append(S2)
        frontier = nxt
        assert len(seen) < 30_000, "BFS 状态数超限（检查 canon 归一化）"
    need = ("ENTRY", "FILL_OK", "FILL_FAIL", "KILL", "TTL", "WIN", "LOSS", "BIGLOSS",
            "HALT_ERR", "HALT_CONSEC", "HALT_DAILY", "CAP10", "HALT_REJECT")
    missing = [f for f in need if f not in flags]
    assert not missing, f"BFS 未覆盖场景: {missing}"
    print(f"  [BFS] 可达等价类 {len(seen)}：INV1 瞬态唯一出口 / INV2 放行⟹冷却到期 / "
          f"熔断三源+笔数上限全部可达通过 ✅")


def unit_scenarios() -> None:
    # S1【缺陷回放】反向离场/撤单不得覆盖同向冷却（单对实现的覆盖缺陷）
    a = AccountState()
    a.roll_day(0)
    t = 100_000
    a.register_entry("z", t)
    a.register_exit(ts=t + 60, direction="LONG", zone_id="z", reason="STOP",
                    pnl_r=_LOSS, cfg=CFG)                      # LONG 冷却至 t+1560
    assert a.can_enter(t + 700, "LONG") == (False, "COOLDOWN")
    assert a.can_enter(t + 700, "SHORT") == (True, "")        # 反向不受同向冷却约束
    a.register_entry("z", t + 700)
    a.register_exit(ts=t + 760, direction="SHORT", zone_id="z", reason="STOP",
                    pnl_r=_LOSS, cfg=CFG)                      # SHORT 冷却至 t+2260
    assert a.can_enter(t + 1200, "LONG") == (False, "COOLDOWN"), "LONG 冷却被覆盖（per-dir 缺陷）"
    assert a.can_enter(t + 1560, "LONG") == (True, "")        # LONG 独立到期
    assert a.can_enter(t + 2200, "SHORT") == (False, "COOLDOWN")
    assert a.can_enter(t + 2260, "SHORT") == (True, "")
    # TTL 双向 5 根 + max 只延不缩（既有 LONG 20 根剩余 → TTL 不得缩短）
    a.register_entry("z", t + 2400)
    a.register_exit(ts=t + 2400, direction="LONG", zone_id="z", reason="STOP",
                    pnl_r=_LOSS, cfg=CFG)                      # LONG 至 t+3900
    a.register_ttl_abort(t + 2500, CFG)                        # 双向至 t+2800
    assert a.can_enter(t + 2900, "SHORT") == (True, "")        # SHORT 按 TTL 到期
    assert a.can_enter(t + 3700, "LONG") == (False, "COOLDOWN"), "TTL 不得缩短既有 LONG 窗口"
    assert a.can_enter(t + 3900, "LONG") == (True, "")
    # maybe_resume：全部窗口到期才回 IDLE
    a.maybe_resume(t + 3899)
    assert a.state == "COOLDOWN"
    a.maybe_resume(t + 3900)
    assert a.state == "IDLE"
    print("  [S1] per-dir 覆盖缺陷回放 / TTL 双向 / max 只延不缩 / maybe_resume 通过 ✅")

    # S2 日切：HALTED_DAY 与计数清除、冷却按时间跨日保持
    a2 = AccountState()
    a2.roll_day(1)
    a2.note_error_stop_fail(CFG)
    r2 = a2.note_error_stop_fail(CFG)
    assert r2 == "ERROR_STOP_FAIL" and a2.state == "HALTED_DAY"
    a2.roll_day(2)
    assert a2.state == "IDLE" and a2.halt_reason == "" and a2.error_stop_fail == 0
    t0 = 5 * _DAY + 1_000
    a2.register_entry("z", t0)
    a2.register_exit(ts=t0, direction="LONG", zone_id="z", reason="STOP",
                     pnl_r=_LOSS, cfg=CFG)
    a2.roll_day(6)
    assert a2.can_enter(t0 + _CAP - 1, "LONG") == (False, "COOLDOWN")
    assert a2.can_enter(t0 + _CAP, "LONG") == (True, "")
    # S3 熔断：连损×3（-0.9）与日亏（单笔 -3.5R）；KILL 不计入止损拉黑
    a3 = AccountState()
    a3.roll_day(3)
    for i in range(3):
        h = a3.register_exit(ts=1000 + i * 100, direction="LONG", zone_id="z",
                             reason="STOP", pnl_r=_LOSS, cfg=CFG)
    assert h == "CONSEC_STOPS" and a3.state == "HALTED_DAY"
    a3.roll_day(4)
    h = a3.register_exit(ts=2000, direction="LONG", zone_id="z", reason="STOP",
                         pnl_r=_BIG, cfg=CFG)
    assert h == "DAILY_LOSS"
    a3.roll_day(5)
    a3.register_exit(ts=3000, direction="LONG", zone_id="z", reason="KILL",
                     pnl_r=_KILL, cfg=CFG)
    assert not a3.zone_blacklisted("z", "LONG"), "KILL 不计入止损拉黑（§6.5 口径 3）"
    a3.register_exit(ts=3100, direction="LONG", zone_id="z", reason="STOP",
                     pnl_r=_KILL, cfg=CFG)
    a3.register_exit(ts=3200, direction="LONG", zone_id="z", reason="BE",
                     pnl_r=_KILL, cfg=CFG)
    assert a3.zone_blacklisted("z", "LONG") and not a3.zone_blacklisted("z", "SHORT")
    # S4 态门：NO_DATA/ARMED/IN_POS 拒开
    a4 = AccountState()
    a4.roll_day(0)
    for st in ("NO_DATA", "ARMED", "IN_POS"):
        a4.state = st
        assert a4.can_enter(100_000, "LONG") == (False, st)
    a4.state = "IDLE"
    assert a4.can_enter(100_000, "LONG") == (True, "")
    print("  [S2-S4] 日切清除/冷却跨日/熔断三源/拉黑口径/态门通过 ✅")


def smoke() -> None:
    rig = Rig()  # 保护单正常
    rig.prep()
    rig.inject()
    assert rig.signal() == ("OK", None)
    assert rig.fill() == ("BUSY", None)
    g = rig.eng.broker.get(rig.led.signals[0]["sig_id"])
    assert g.stage == "PROTECTED"
    assert rig.flat() == ("BUSY", None)  # 14:02 管理根
    assert rig.dump() == ("BUSY", None)  # 14:03 止损命中
    init, exit_ = rig.led.trades
    assert exit_["reason"] == "STOP"
    assert near(exit_["px"], 1675.76478), exit_["px"]
    assert near(exit_["pnl_usdt"], -187.95344, 1e-3), exit_["pnl_usdt"]
    assert near(exit_["pnl_R"], -1.074029, 1e-4), exit_["pnl_R"]
    assert exit_["bars_held"] == 1 and not exit_["be_done"] and not exit_["seg_done"]
    assert rig.eng.acct.cooldowns == {"LONG": ts_of("14:29")}
    assert rig.eng.acct.state == "COOLDOWN" and rig.eng.pos is None
    # 【缺陷回放 E2E】冷却期内反向 SHORT 离场：LONG 窗口不得被覆盖/解锁
    rig.eng.acct.register_exit(ts=ts_of("14:05"), direction="SHORT", zone_id="z",
                               reason="STOP", pnl_r=-1.0, cfg=rig.eng.cfg)
    assert rig.eng.acct.cooldowns["LONG"] == ts_of("14:29"), "LONG 冷却被 SHORT 离场覆盖"
    assert rig.eng.acct.cooldowns["SHORT"] == ts_of("14:30")
    assert rig.eng.acct.state != "HALTED_DAY"
    for _ in range(3):  # 14:04–14:06
        assert rig.flat() == ("COOLDOWN", None)
    rig.push(3)          # 14:07–14:09 铺垫
    assert rig.signal() == ("COOLDOWN", None), "冷却期同向信号须被拦截（旧实现会放行）"
    assert len(rig.led.signals) == 1 and rig.eng.pending_sig_id is None
    print("  [冒烟] 自然止损全链路数值 + 反向离场不解锁同向冷却（缺陷回放 E2E）通过 ✅")


def near(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) < tol


if __name__ == "__main__":
    bfs()
    unit_scenarios()
    smoke()
    print("T14 全部通过 ✅")
