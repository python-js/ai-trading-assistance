"""MRS T08（规格书 §10）：触发器 T1/T2 正负例 + §7.2 决策序列闸门。

用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t08.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.decision import eval_entry, eval_t1, eval_t2
from app.mrs.types import Bar, Zone

TICK, ATR1, ATR5, ATR15 = 0.1, 10.0, 20.0, 100.0
T0 = 43_200  # 12:00 UTC（避开 funding±30min）

ZS = Zone(id="zs", center=1000.0, width=40.0, side="S", grade=0.5, touches=1,
          rebound=0.0, types=("swing",))
ZR = Zone(id="zr", center=1000.0, width=40.0, side="R", grade=0.5, touches=1,
          rebound=0.0, types=("swing",))


def bars(*rows):
    return [Bar(T0 + i * 60, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)]


def t2_cases() -> None:
    # 正例 1（多）：穿透 3 根后第 4 根收回
    b_pos = bars((984, 985, 978, 979), (979, 981, 977, 978),
                 (978, 980, 975, 978), (978, 990, 976, 986))
    got = eval_t2(bars=b_pos, zone=ZS, direction="LONG", atr1=ATR1, tick=TICK)
    assert got == (975.0, 990.1), got
    # 正例 2（空）：镜像
    b_short = bars((1016, 1022, 1015, 1021), (1021, 1023, 1017, 1022),
                   (1022, 1025, 1019, 1022), (1022, 1024, 1010, 1014))
    got = eval_t2(bars=b_short, zone=ZR, direction="SHORT", atr1=ATR1, tick=TICK)
    assert got == (1025.0, 1009.9), got
    # 负例 1：穿透发生在 4 根前（窗口外）→ 无穿透
    b_late = bars((970, 972, 968, 970), (990, 995, 985, 992), (992, 996, 988, 994),
                  (994, 997, 990, 995), (995, 998, 992, 996))
    assert eval_t2(bars=b_late, zone=ZS, direction="LONG", atr1=ATR1, tick=TICK) is None
    # 负例 2：收回 K 收盘位置不足（close 986→982 < low+0.6*range）
    b_weak = bars((984, 985, 978, 979), (979, 981, 977, 978),
                  (978, 980, 975, 978), (978, 990, 976, 982))
    assert eval_t2(bars=b_weak, zone=ZS, direction="LONG", atr1=ATR1, tick=TICK) is None
    # 负例 3：非首次收回（前根已收在线上）
    b_dup = bars((979, 981, 976, 979), (979, 984, 975, 981),
                 (981, 985, 978, 983), (983, 990, 980, 987))
    assert eval_t2(bars=b_dup, zone=ZS, direction="LONG", atr1=ATR1, tick=TICK) is None
    print("  [T2] 2 正例（多/空）+ 3 负例（窗口外穿透/收盘位不足/非首次收回）通过 ✅")


def t1_cases() -> None:
    zid = ZS.id
    # 正例（多）：贴 S 上沿 + 长下影 + 收高 + 不破前低
    b_pos = bars((1002, 1008, 990, 1004), (1004, 1006, 980, 1002),
                 (995, 1008, 983, 1005))
    got = eval_t1(bars=b_pos, zone=ZS, direction="LONG", atr1=ATR1, atr5=ATR5,
                  atr15=ATR15, tick=TICK, t1_used_today=set())
    assert got == (982.0, 1008.1), got  # invalid = low − 0.05×ATR5 = 983−1
    # 正例（空）：镜像
    b_short = bars((1000, 1006, 996, 1000), (992, 1004, 986, 994))
    got = eval_t1(bars=b_short, zone=ZR, direction="SHORT", atr1=ATR1, atr5=ATR5,
                  atr15=ATR15, tick=TICK, t1_used_today=set())
    assert got == (1005.0, 985.9), got  # invalid = high + 0.05×ATR5 = 1004+1
    # 负例 1：下影不足
    b_noshadow = bars((1004, 1006, 980, 1002), (990, 992, 985, 991))
    assert eval_t1(bars=b_noshadow, zone=ZS, direction="LONG", atr1=ATR1, atr5=ATR5,
                   atr15=ATR15, tick=TICK, t1_used_today=set()) is None
    # 负例 2：未收高于前根（位置/影线均过，卡在 close_n ≤ close_{n-1}）
    b_flat = bars((1004, 1006, 980, 1002), (985, 990, 975, 986))
    assert eval_t1(bars=b_flat, zone=ZS, direction="LONG", atr1=ATR1, atr5=ATR5,
                   atr15=ATR15, tick=TICK, t1_used_today=set()) is None
    # 负例 3：离区太远（position 不成立）
    b_far = bars((1004, 1006, 980, 1002), (955, 965, 940, 950))
    assert eval_t1(bars=b_far, zone=ZS, direction="LONG", atr1=ATR1, atr5=ATR5,
                   atr15=ATR15, tick=TICK, t1_used_today=set()) is None
    # 负例 4：日内已产生过 T1（去重）
    assert eval_t1(bars=b_pos, zone=ZS, direction="LONG", atr1=ATR1, atr5=ATR5,
                   atr15=ATR15, tick=TICK, t1_used_today={("T1", zid)}) is None
    print("  [T1] 2 正例（多/空）+ 4 负例（影线/收高/位置/日内去重）通过 ✅")


def entry_sequence() -> None:
    common = dict(now_ts=T0 + 180, rank30=2, rank15=1, rank5=0, adx15=30.0,
                  atr1=ATR1, atr5=ATR5, atr15=ATR15, vol_pctile=0.8, spread_bps=None,
                  zones=[ZS, ZR], zone_touch_today={}, zone_blacklist=set(),
                  t1_used_today=set(), tick=TICK, session_windows=["13:00-17:00"],
                  funding_hours=[0, 8, 16], blackout_min=30)
    b_sup = bars((984, 985, 978, 979), (979, 981, 977, 978),
                 (978, 980, 975, 978), (985, 992, 975, 988))  # T2 与 T1 同根满足
    sig, reason = eval_entry(bars=b_sup, **common)
    assert reason == "SESSION", reason  # 12:00 不在 13:00-17:00
    common.update(session_windows=[])
    sig, reason = eval_entry(bars=b_sup, **common)
    assert reason == "OK" and sig.type == "T2" and sig.dir == "LONG", (reason, sig)
    assert sig.suppressed == "T1", "T1 同根满足应被抑制留痕"
    assert sig.trigger_px == 992.1 and sig.invalid_px == 975.0
    assert sig.gates_passed == ["SESSION", "EVENT", "REGIME", "RESON", "ZONE",
                                "SPREAD-NA", "VOL", "T2"], sig.gates_passed

    # 量分位不足
    sig2, reason = eval_entry(bars=b_sup, **{**common, "vol_pctile": 0.5})
    assert sig2 is None and reason == "VOL", (sig2, reason)
    # 点差超限
    sig2, reason = eval_entry(bars=b_sup, **{**common, "spread_bps": 7.0})
    assert sig2 is None and reason == "SPREAD", (sig2, reason)
    # 共振关闭
    sig2, reason = eval_entry(bars=b_sup, **{**common, "rank15": -2})
    assert sig2 is None and reason == "RESON", (sig2, reason)
    # regime 弱趋势
    sig2, reason = eval_entry(bars=b_sup, **{**common, "adx15": 19.0})
    assert sig2 is None and reason == "REGIME", (sig2, reason)
    # 活动区命中外但已拉黑（§6.5 按 zone+方向）/ 达当日触达上限
    sig2, reason = eval_entry(bars=b_sup, **{**common, "zone_blacklist": {(ZS.id, "LONG")}})
    assert sig2 is None and reason == "ZONE", (sig2, reason)
    sig2, reason = eval_entry(bars=b_sup, **{**common, "zone_touch_today": {ZS.id: 2}})
    assert sig2 is None and reason == "ZONE", (sig2, reason)
    # §6.5 冷却：同向禁开、异向放行、到期放行（per-dir 分存）
    sig2, reason = eval_entry(bars=b_sup, **{**common, "cooldowns": {"LONG": T0 + 999}})
    assert sig2 is None and reason == "COOLDOWN", (sig2, reason)
    sig2, reason = eval_entry(bars=b_sup, **{**common, "cooldowns": {"SHORT": T0 + 999}})
    assert reason == "OK", reason
    sig2, reason = eval_entry(bars=b_sup, **{**common, "cooldowns": {"LONG": T0 - 1}})
    assert reason == "OK", reason
    # T2 不满足、T1 满足 → T1 信号
    b_t1 = bars((1000, 1005, 995, 1002), (1002, 1008, 990, 1004),
                (1004, 1006, 980, 1002), (995, 1008, 983, 1005))
    sig3, reason = eval_entry(bars=b_t1, **{**common, "t1_used_today": set()})
    assert reason == "OK" and sig3.type == "T1" and sig3.suppressed is None, (reason, sig3)
    # 同 bar 重复评估幂等：sig_id 确定性一致
    sig4, _ = eval_entry(bars=b_t1, **{**common, "t1_used_today": set()})
    assert sig4.id == sig3.id, "sig_id 应确定性（uuid5）"
    # 无触发器
    b_none = bars((1000, 1005, 995, 1002), (1002, 1008, 990, 1004),
                  (1004, 1006, 980, 1002), (1005, 1006, 1001, 1003))
    sig5, reason = eval_entry(bars=b_none, **common)
    assert sig5 is None and reason == "NO_TRIGGER", (sig5, reason)
    print("  [序列] §7.2 闸门顺序/漏斗标签/T2 优先抑制/T1 路径/确定性 sig_id 通过 ✅")


if __name__ == "__main__":
    t2_cases()
    t1_cases()
    entry_sequence()
    print("T08 全部通过 ✅")
