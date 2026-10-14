"""MRS T02（规格书 §10）：分型确认延迟验证 + §4.5/4.6/4.7/4.8 自检。

- k=2：极值 bar 后第 2 根收盘（confirm_ts）前任何接口不可见，之后可见；
- 同侧查询/排序/count、VWAP/昨 POC、量分位（严格不含当前 bar、min_n）、FIB 位。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t02.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.indicators import DayStats, Swing, SwingTracker, VolWindow, fib_levels

PERIOD, K = 60, 2


def main() -> None:
    # ---- 1. HIGH 分型确认延迟（k=2） ----
    t = SwingTracker(PERIOD, K)
    highs = [5, 6, 9, 6, 5, 4, 3]
    lows = [1, 2, 3, 4, 5, 6, 7]
    for i, (h, l) in enumerate(zip(highs, lows)):
        t.update(i * PERIOD, h, l)
        if i < 4:
            assert t.confirmed == [], f"第 {i} 根后不应有确认点: {t.confirmed}"
    # b2（ts=120）为高点：confirm_ts = 120 + 3×60 = 300 = bar4 收盘时刻
    assert len(t.confirmed) == 1 and t.confirmed[0].side == "H"
    s = t.confirmed[0]
    assert (s.ext_ts, s.price, s.confirm_ts) == (120, 9.0, 300), s
    assert t.confirmed_swings("H", now_ts=299, count=5) == [], "confirm 前不可见"
    assert len(t.confirmed_swings("H", now_ts=300, count=5)) == 1, "confirm 时刻可见"
    assert t.confirmed_swings("L", now_ts=300, count=5) == [], "无 LOW 分型"
    print("  [确认延迟] k=2：极值后第 2 根收盘前不可见，之后可见 ✅")

    # ---- 2. LOW 分型 + 价格排序 + count ----
    t2 = SwingTracker(PERIOD, K)
    lows2 = [5, 4, 2, 4, 3, 1.5, 3, 4]
    for i, low in enumerate(lows2):
        t2.update(i * PERIOD, 6.0, low)  # high 全等 → 不可能形成 HIGH 分型
    got = t2.confirmed_swings("L", now_ts=10_000, count=5)
    assert [s.price for s in got] == [1.5, 2.0], f"应价格升序: {got}"
    assert [s.ext_ts for s in got] == [300, 120]
    got1 = t2.confirmed_swings("L", now_ts=10_000, count=1)
    assert len(got1) == 1 and got1[0].price == 1.5
    # 未确认点不可见：b5（ts=300）confirm_ts = 300+180 = 480
    assert len(t2.confirmed_swings("L", now_ts=479, count=5)) == 1, "b5 在 confirm 前不可见"
    assert len(t2.confirmed_swings("L", now_ts=480, count=5)) == 2, "b5 在 confirm 后可见"
    print("  [LOW/排序/count/可见性] 通过 ✅")

    # ---- 3. VWAP（UTC 重置）+ 昨日快照 ----
    ds = DayStats(bins=4)
    day0 = [(0, 10, 8, 9, 1.0), (60, 11, 9, 10, 1.0), (120, 12, 10, 11, 2.0)]
    for ts, h, l, c, v in day0:
        ds.update(ts, h, l, c, v)
    assert abs(ds.vwap - 10.25) < 1e-12, ds.vwap  # (9+10+22)/4
    ds.update(86_400 + 0, 20, 19, 19.5, 1.0)  # 新 UTC 日首根 → 日切
    snap = ds.yesterday
    assert snap and (snap["high"], snap["low"], snap["close"]) == (12.0, 8.0, 11.0), snap
    assert abs(snap["vwap"] - 10.25) < 1e-12, snap
    # POC：tp=9→桶1(额9)、tp=10→桶2(额10)、tp=11→桶3(额22)；最大桶3 → 中点 8+3.5=11.5
    assert abs(snap["poc"] - 11.5) < 1e-12, snap
    assert abs(ds.vwap - 19.5) < 1e-12, "新日 VWAP 应重置"
    print("  [VWAP/POC] 日切快照 + 重置通过 ✅")

    # ---- 4. 量分位（§4.6：分母恒为 n；严格不含当前 bar；样本<40 → None） ----
    vw = VolWindow(60, min_n=40)
    for i in range(40):
        assert vw.update(float(i + 1)) is None, "样本<40 → None"
    p = vw.update(41.0)  # 样本=[1..40] 全 ≤41，分母恒 60 → 40/60
    assert abs(p - 40 / 60) < 1e-12, p
    p = vw.update(1.0)  # 样本=[1..40,41]：仅 1 个 ≤1.0 → 1/60（含当前 bar 则计数错）
    assert abs(p - 1 / 60) < 1e-12, p
    print("  [量分位] 样本<40 → None / 严格不含当前 bar / 分母恒 60 通过 ✅")

    # ---- 5. FIB（§4.8） ----
    lo = Swing("L", 0, 100.0, 300)
    hi = Swing("H", 120, 200.0, 480)
    f = fib_levels([lo, hi], now_ts=1000, period=900)
    assert f is None, "极值收盘距今 < 12 根 → None"
    # 边界：now −(120+900) = 10800 = 12 根 → 恰好可见
    f = fib_levels([lo, hi], now_ts=120 + 900 * 13, period=900)
    assert f and abs(f[0] - 161.8) < 1e-9 and abs(f[1] - 150) < 1e-9 and abs(f[2] - 138.2) < 1e-9, f
    fd = fib_levels([hi, lo], now_ts=20_000, period=900)
    assert fd and abs(fd[0] - 138.2) < 1e-9 and abs(fd[2] - 161.8) < 1e-9, fd
    print("  [FIB] 涨/跌波段回撤位 + 12 根新鲜度 通过 ✅")

    print("T02 全部通过 ✅")


if __name__ == "__main__":
    main()
