"""MRS T03/T04/T05（规格书 §10）：rank 组合表 / 共振真值表 / zones fixture。

用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t03_05.py
"""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.decision import (build_int_materials, compute_rank, rebuild_zones,
                              resonance, struct_component)
from app.mrs.indicators import Swing
from app.mrs.types import Bar, Material

NOW = 1000


def _swings_seq(prices_sides):
    """[(side, price, ext_ts)] → Swing 列表（confirm_ts = ext_ts + 60，均在 NOW 前可见）。"""
    return [Swing(side, ts, price, ts + 60) for side, price, ts in prices_sides]


UP5 = _swings_seq([("H", 110, 0), ("L", 100, 60), ("H", 120, 120),
                   ("L", 105, 180), ("H", 130, 240)])
DN5 = _swings_seq([("H", 130, 0), ("L", 120, 60), ("H", 125, 120),
                   ("L", 110, 180), ("H", 115, 240)])


def t03() -> None:
    assert struct_component([], NOW) == 0
    assert struct_component(UP5[:4], NOW) == 0, "样本不足 → 0"
    non_alt = _swings_seq([("H", 110, 0), ("H", 120, 60), ("L", 100, 120),
                           ("H", 130, 180), ("L", 105, 240)])
    assert struct_component(non_alt, NOW) == 0, "不交替 → 0"
    assert struct_component(UP5, NOW) == 1
    assert struct_component(DN5, NOW) == -1
    assert struct_component(UP5, now_ts=200) == 0, "confirm_ts > now 不可见"

    cases = [  # (close, ma5, ma20, ma60, swings, 期望)
        (110, 105, 102, 100, [], 2),
        (110, 105, 102, 100, UP5, 2),       # 2+1=3 → clip 2
        (100, 97, 98, 96, [], 1),           # 仅松排列
        (100, 97, 98, 96, DN5, 0),          # 1-1=0
        (90, 95, 98, 100, [], -2),
        (90, 95, 98, 100, DN5, -2),         # -3 → clip -2
        (95, 105, 102, 100, UP5, 1),        # 排列分 0（close<MA60）+ struct +1
        (100, None, None, None, DN5, -1),   # MA 不足 → 仅 struct
    ]
    for close, ma5, ma20, ma60, sw, want in cases:
        got = compute_rank(close, ma5, ma20, ma60, sw, NOW)
        assert got == want, f"rank({close},{ma5},{ma20},{ma60})={got} != {want}"
    print("  [T03] struct 边界 + rank 8 例全组合通过 ✅")


def t04() -> None:
    table = [  # (rank30, rank15, rank5, 期望)
        (2, 2, 1, "LONG"),
        (2, 1, 0, "LONG"),
        (2, 1, -1, "LONG"),
        (2, -2, 1, "BOTH_OFF"),       # 多头 veto
        (1, 1, 0, "LONG"),
        (1, 1, -1, "BOTH_OFF"),
        (-2, 1, -1, "SHORT"),
        (-2, -2, -1, "SHORT"),        # 镜像强共振
        (-2, 2, -1, "BOTH_OFF"),      # 空头 veto
        (0, 0, 0, "BOTH_OFF"),
    ]
    for r30, r15, r5, want in table:
        got = resonance(r30, r15, r5)
        assert got == want, f"resonance({r30},{r15},{r5})={got} != {want}"
    print("  [T04] 共振真值表 10 行（含 veto/镜像/压线）通过 ✅")


TICK, ATR15, CLOSE = 0.1, 100.0, 1000.0


def t05() -> None:
    mats = [Material(850, "swing"), Material(858, "ma"),
            Material(780, "vwap"), Material(788, "swing"),
            Material(710, "swing"), Material(720, "fib"),
            Material(640, "prevday"), Material(650, "vwap"),
            Material(1050, "poc"), Material(1055, "ma"), Material(1060, "swing"),
            Material(1130, "swing")]
    kw = dict(close=CLOSE, atr15=ATR15, bars_1m_recent=[], now_ts=NOW, tick=TICK)
    zs = rebuild_zones(materials=mats, **kw)
    assert len(zs) == 5, f"12 原料位应聚成 5 区，实得 {[z.center for z in zs]}"
    assert [z.center for z in zs] == [710, 780, 850, 1055, 1130], [z.center for z in zs]
    assert [z.side for z in zs] == ["S", "S", "S", "R", "R"]
    assert all(abs(z.width - 35.0) < 1e-12 for z in zs), [z.width for z in zs]
    for z in zs:  # tick 对齐
        assert abs(z.center / TICK - round(z.center / TICK)) < 1e-9
        assert abs(z.width / TICK - round(z.width / TICK)) < 1e-9
    by_c = {z.center: z for z in zs}
    assert by_c[850].grade == 0.4 * math.log1p(1) + 0.2, by_c[850].grade
    assert by_c[1055].types == ("ma", "poc", "swing"), by_c[1055].types
    assert by_c[1130].grade == 0.4 * math.log1p(1), by_c[1130].grade
    assert 640 not in by_c, "低分 S 区（640）应被 top_n=3 截掉"
    # 确定性：打乱输入顺序 → 同 id/同 center 序列
    zs2 = rebuild_zones(materials=list(reversed(mats)), **kw)
    assert [(z.id, z.center) for z in zs2] == [(z.id, z.center) for z in zs], "确定性失败"

    # 整数阶梯（v1 口径 2）：间距 ≤ 0.5×ATR15 的档位剔除；正价格过滤
    ps = [m.price for m in build_int_materials(1000.0, 100.0, 0.1)]
    assert ps == sorted(ps) and all(p > 0 for p in ps)
    for p, want_in in [(1000.0, True), (900.0, True), (250.0, True), (750.0, True),
                       (10.0, False), (50.0, False)]:
        assert (p in ps) == want_in, f"int {p} 期望 {want_in}"
    print("  [T05] 12 原料位 → 5 区；center/width/tick/grade/id 确定性通过 ✅")

    # 触碰计数/后效（v1 口径 4）：区 [965,1035]；2 次在区内收盘 + 1 摆动成员 = 3 次；
    # 后效（支撑向上）= (0.3 + 0.2)/2 = 0.25（ATR15=100 归一）
    t0 = 100_000
    hist = []
    for i in range(71):
        if i == 0:
            c, h = 1010.0, 1015.0
        elif i == 4:
            c, h = 1020.0, 1030.0
        else:
            c, h = 960.0, 970.0
        if 5 <= i <= 10:
            h = 1040.0
        hist.append(Bar(t0 + i * 60, 960.0, h, 950.0, c, 1.0))
    zs3 = rebuild_zones(materials=[Material(1000, "swing")], close=1000.0, atr15=100.0,
                        bars_1m_recent=hist, now_ts=t0 + 80 * 60, tick=0.1)
    assert len(zs3) == 1 and zs3[0].touches == 3, zs3
    assert abs(zs3[0].rebound - 0.25) < 1e-12, zs3[0].rebound
    want_grade = 0.4 * math.log1p(3) + 0.4 * 0.25
    assert abs(zs3[0].grade - want_grade) < 1e-12, zs3[0].grade
    print("  [T05b] 触碰/后效/grade 手算通过 ✅")


if __name__ == "__main__":
    t03()
    t04()
    t05()
    print("T03/T04/T05 全部通过 ✅")
