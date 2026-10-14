"""MRS T07（规格书 §10 / §6.6）：make_plan 数值算例 + 边界 + 对照组。

- 主算例：§6.6 全 13 步可观测项（净值 50k、risk 0.35%、tick 0.1、T2 多、
  trigger 65030 / invalid 64920 / ATR5 95 / ATR15 210 / 上方 R 区下边缘 65380）。
  算例口径 lot_step=0.001（§6.6 写死 qty=1.041）；另以真实 venue lot=0.0001 复核 qty=1.0416。
- 对照组：taker 5bp + 2bp 滑点 → rt=91.042 → 4rt=364.168 > R=168 → REJECT("COST")。
- 边界：R0=1.6×ATR15±1tick；tp_dist=1.2R±1e-3（浮点下“精确相等”落在 1e-11 噪声内，
  取 ±1e-3 双侧证明阈值位置）；zone_edge 截断 / 2.2R 优先两例。
- 补充：BAD_R0 / QTY_MIN / LIQ 迭代 / HALF 减半 / 空头镜像（v1 口径 1：一律不利方向）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t07.py
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.config import CFG
from app.mrs.plan import Bundle, Reject, make_plan
from app.mrs.types import Signal, Zone

CFG_LOT = copy.deepcopy(CFG)
CFG_LOT["venue"]["lot_step"] = 0.001  # §6.6 算例口径


def sig(dir="LONG", trigger=65030.0, invalid=64920.0, zone="z1", typ="T2") -> Signal:
    return Signal(id="s1", now_ts=1_000_000, type=typ, dir=dir, zone_id=zone,
                  trigger_px=trigger, invalid_px=invalid, close=trigger)


def zone_by_edge(side: str, edge: float, zid="z1", w: float = 35.0) -> Zone:
    center = edge + w / 2 if side == "R" else edge - w / 2
    return Zone(id=zid, center=center, width=w, side=side,
                grade=1.0, touches=0, rebound=0.0, types=("swing",))


def ctx(atr15=210.0, atr5=95.0, equity=50_000.0, zones=None, regime="FULL", fees=(1.0, 2.0)) -> dict:
    return {"atr1": atr5, "atr5": atr5, "atr15": atr15, "equity": equity,
            "zones": zones if zones is not None else [zone_by_edge("R", 65380.0)],
            "regime": regime, "fees": fees}


def near(a, b, tol=1e-6) -> bool:
    return abs(a - b) < tol


def main() -> None:
    # ---- 1. §6.6 主算例 13 步（可观测项逐行断言） ----
    b = make_plan(sig(), ctx(), cfg=CFG_LOT)
    assert isinstance(b, Bundle), f"主算例应出 Bundle: {b}"
    assert near(b.stop, 64862.0), b.stop                                    # 行3
    assert near(b.R, 168.0, 1e-9), b.R                                      # 行3：R0=124.3<168 → 夹紧到 0.8×210
    assert abs(b.stop - 64905.7) > 1.0, "行1-2 结构止损 64905.7 应被夹紧覆盖"
    rt = 2 * 65030.0 * (1.0 + 2.0) / 1e4
    assert near(rt, 39.018, 1e-9) and b.R >= 4 * rt, "行4/5：rt=39.018，168≥156.072"  # 行4/5
    assert near(b.qty, 1.041), b.qty                                        # 行6
    assert near(b.qty * b.trigger, 67696.23) and b.qty * b.trigger <= 125_000  # 行7
    assert near(b.tp, 65365.7), b.tp                                        # 行8/10：335.75<369.6 → 区前档截断
    assert near(b.tp - b.trigger, 335.7), "行8：截断后距离=335.7（floor 到 tick）"
    assert near(b.seg1_px, 65231.6), b.seg1_px                              # 行11：+1.2R
    assert near(b.be_px_target, 65108.0), b.be_px_target                    # 行12：entry+max(2rt,0.05R)
    assert b.leverage == 3, "行13：3x 强平距离 21676≫3R=504"                 # 行13
    assert b.trigger == 65030.0 and b.dir == "LONG" and b.e_sig == "s1" and b.zone_id == "z1"
    print("  [主算例] §6.6 13 步可观测项全对齐 ✅")

    # ---- 2. 真实 venue lot=0.0001 复核（§11 实测） ----
    b2 = make_plan(sig(), ctx(), cfg=CFG)
    assert isinstance(b2, Bundle) and near(b2.qty, 1.0416), b2
    print("  [真实 lot] floor(1.041666…/0.0001)→1.0416 ✅")

    # ---- 3. 对照组：成本闸门拒绝 ----
    r = make_plan(sig(), ctx(fees=(5.0, 2.0)), cfg=CFG_LOT)
    assert isinstance(r, Reject) and r.reason == "COST", r
    rt2 = 2 * 65030.0 * 7.0 / 1e4
    assert near(rt2, 91.042, 1e-9) and 4 * rt2 > 168, "4×91.042=364.168 > 168"
    print("  [对照组] taker 5bp+2bp → rt=91.042 → REJECT(COST) ✅")

    # ---- 4. 边界：R0 = 1.6×ATR15 ± 1 tick（=336.0，invalid 构造 raw_stop=64694.0） ----
    z_far = [zone_by_edge("R", 65580.0)]  # dist=550 ≥ 1.5R=504 → ROOM 通过
    b_at = make_plan(sig(invalid=64708.25), ctx(zones=z_far), cfg=CFG_LOT)
    assert isinstance(b_at, Bundle) and near(b_at.R, 336.0, 1e-9), b_at       # 恰好=不多不少
    assert near(b_at.stop, 64694.0) and near(b_at.tp, 65565.7) and near(b_at.qty, 0.520)
    b_lo = make_plan(sig(invalid=64708.35), ctx(zones=z_far), cfg=CFG_LOT)    # R0=335.9
    assert isinstance(b_lo, Bundle) and near(b_lo.R, 335.9, 1e-9), b_lo
    r_hi = make_plan(sig(invalid=64708.15), ctx(zones=z_far), cfg=CFG_LOT)    # R0=336.1
    assert isinstance(r_hi, Reject) and r_hi.reason == "STOP_FAR", r_hi
    print("  [R0 边界] 336.0 通过 / +1tick STOP_FAR / -1tick 通过 ✅")

    # ---- 5. 边界：tp_dist = 1.2R ± 1e-3（atr5=400 → 区前档=edge−60） ----
    tp_lim = 1.2 * 168.0
    for dlt, is_gap in ((+0.001, False), (-0.001, True)):
        edge = 65030.0 + (tp_lim + dlt) + 60.0
        res = make_plan(sig(invalid=64965.7), ctx(atr5=400.0, zones=[zone_by_edge("R", edge)]),
                        cfg=CFG_LOT)  # raw_stop=64905.7 → r0=124.3 → R=168
        if is_gap:
            assert isinstance(res, Reject) and res.reason == "TP_GAP", res
        else:
            assert isinstance(res, Bundle) and near(res.tp, 65231.6), res
    print("  [tp_dist 边界] 1.2R+1e-3 通过 / -1e-3 TP_GAP ✅")

    # ---- 6. zone_edge 截断 / 2.2R 优先 ----
    b_far_zone = make_plan(sig(), ctx(zones=[zone_by_edge("R", 65430.0)]), cfg=CFG_LOT)
    assert isinstance(b_far_zone, Bundle) and near(b_far_zone.tp, 65399.6), b_far_zone  # 2.2R=369.6 < 385.75
    print("  [止盈两候选] 区前档近→截断 / 2.2R 近→2.2R ✅")

    # ---- 7. 补充：BAD_R0 / QTY_MIN / LIQ / HALF / 空头镜像 ----
    r_bad = make_plan(sig(invalid=65050.0), ctx(), cfg=CFG_LOT)  # raw_stop=65035.7 > e
    assert isinstance(r_bad, Reject) and r_bad.reason == "BAD_R0", r_bad
    r_qty = make_plan(sig(), ctx(equity=30.0), cfg=CFG_LOT)  # 0.000625 → floor 0.0
    assert isinstance(r_qty, Reject) and r_qty.reason == "QTY_MIN", r_qty
    # LIQ：R=12000 ∈ (e/6, 0.5e)，qty 0.014 经 7 次 ×0.8 恰剩 1 张 → 第 9 次迭代 REJECT
    r_liq = make_plan(sig(invalid=53044.25), ctx(atr15=7500.0, zones=[zone_by_edge("R", 83030.0)]),
                      cfg=CFG_LOT)
    assert isinstance(r_liq, Reject) and r_liq.reason == "LIQ", r_liq
    b_half = make_plan(sig(), ctx(regime="HALF"), cfg=CFG_LOT)
    assert isinstance(b_half, Bundle) and near(b_half.qty, 0.520), b_half  # 1.0416×0.5
    # 空头镜像：trigger 65030 / invalid 65140 / 下方 S 区上边缘 64680（全 ceil 不利方向）
    bs = make_plan(sig(dir="SHORT", invalid=65140.0, zone="z2"),
                   ctx(zones=[zone_by_edge("S", 64680.0, zid="z2")]), cfg=CFG_LOT)
    assert isinstance(bs, Bundle), bs
    assert near(bs.stop, 65198.0) and near(bs.tp, 64694.3), (bs.stop, bs.tp)
    assert near(bs.seg1_px, 64828.4) and near(bs.be_px_target, 64952.0), bs
    assert near(bs.R, 168.0, 1e-9) and near(bs.qty, 1.041) and bs.leverage == 3
    print("  [补充] BAD_R0 / QTY_MIN / LIQ / HALF / 空头镜像 通过 ✅")

    print("T07 全部通过 ✅")


if __name__ == "__main__":
    main()
