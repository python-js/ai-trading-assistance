"""MRS T01（规格书 §10）：SMA/RMA/ATR/ADX 递推验证。

- 手算固定样本逐值断言（≤1e-12）；
- 增量实现 vs §4.2/4.3 公式直译参考实现（确定性数据全序列 ≤1e-9）；
- None 传播：样本不足 → None（不得默认值填充）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t01.py
"""
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.indicators import ADX, ATR, RMA, SMA


def ref_rma(xs: list[float], n: int) -> list:
    out, seed, v = [], [], None
    for x in xs:
        if v is None:
            seed.append(x)
            if len(seed) == n:
                v = sum(seed) / n
            out.append(v)
        else:
            v += (x - v) / n
            out.append(v)
    return out


def ref_atr(bars: list[tuple], n: int) -> list:
    trs, pc = [], None
    for h, l, c in bars:
        trs.append(h - l if pc is None else max(h - l, abs(h - pc), abs(l - pc)))
        pc = c
    return ref_rma(trs, n)


def ref_adx(bars: list[tuple], n: int) -> list:
    """§4.3 公式直译（全量重算式参考实现）。"""
    trs, sdp, sdn = [], [], []
    ph = pl = pc = None
    for h, l, c in bars:
        if pc is None:
            tr, up, down = h - l, 0.0, 0.0
        else:
            tr = max(h - l, abs(h - pc), abs(l - pc))
            up, down = h - ph, pl - l
        trs.append(tr)
        sdp.append(up if (up > down and up > 0) else 0.0)
        sdn.append(down if (down > up and down > 0) else 0.0)
        ph, pl, pc = h, l, c
    rtr, rp, rn = ref_rma(trs, n), ref_rma(sdp, n), ref_rma(sdn, n)
    out, seed, adx = [None] * len(bars), [], None
    for i in range(len(bars)):
        if rtr[i] is None or rtr[i] == 0 or rp[i] is None or rn[i] is None:
            continue
        dip, din = 100.0 * rp[i] / rtr[i], 100.0 * rn[i] / rtr[i]
        if dip + din == 0:
            continue
        dx = 100.0 * abs(dip - din) / (dip + din)
        if adx is None:
            seed.append(dx)
            if len(seed) == n:
                adx = sum(seed) / n
                out[i] = adx
        else:
            adx += (dx - adx) / n
            out[i] = adx
    return out


def _eq(a, b, tol=1e-12) -> bool:
    if (a is None) != (b is None):
        return False
    return a is None or abs(a - b) <= tol


def main() -> None:
    # ---- 1. 手算样本 ----
    sma = SMA(3)
    got = [sma.update(c) for c in (1.0, 2.0, 3.0, 4.0, 5.0)]
    assert got == [None, None, 2.0, 3.0, 4.0], got

    rma = RMA(3)
    got = [rma.update(x) for x in (2.0, 3.0, 1.0, 4.0, 2.0)]
    want = [None, None, 2.0, 2.6666666666666665, 2.4444444444444446]
    assert all(_eq(a, b) for a, b in zip(got, want)), got

    bars = [(10, 8, 9), (12, 9, 11), (11, 10, 10), (14, 11, 13), (14, 12, 12.5)]
    atr = ATR(3)
    got = [atr.update(h, l, c) for h, l, c in bars]
    want = [None, None, 2.0, 2.6666666666666665, 2.4444444444444446]
    assert all(_eq(a, b) for a, b in zip(got, want)), got
    print("  [手算] SMA/RMA/ATR 固定样本逐值断言通过")

    # ---- 2. 公式直译参考实现交叉验证 ----
    rng = random.Random(42)
    px, seq = 100.0, []
    for _ in range(400):
        px *= 1 + rng.uniform(-0.004, 0.004)
        o = px * (1 + rng.uniform(-0.001, 0.001))
        h = px * (1 + rng.uniform(0, 0.003))
        l = px * (1 - rng.uniform(0, 0.003))
        seq.append((max(h, o), min(l, o), px))
    for n in (3, 14):
        got = []
        a = ATR(n)
        for h, l, c in seq:
            got.append(a.update(h, l, c))
        want = ref_atr(seq, n)
        dmax = max((abs(a_ - b_) for a_, b_ in zip(got, want) if a_ is not None), default=0)
        assert dmax <= 1e-9, f"ATR(n={n}) 最大差 {dmax}"
        got = []
        d = ADX(n)
        for h, l, c in seq:
            got.append(d.update(h, l, c))
        want = ref_adx(seq, n)
        dmax = max((abs(a_ - b_) for a_, b_ in zip(got, want) if a_ is not None), default=0)
        n_got = sum(x is not None for x in got)
        assert n_got > 300, f"ADX(n={n}) 有效值过少: {n_got}"
        assert (got[-1] is None) == (want[-1] is None)
        assert dmax <= 1e-9, f"ADX(n={n}) 最大差 {dmax}"
    print("  [交叉] ATR/ADX 增量实现 vs 公式直译（400 根×n=3/14）全序列 ≤1e-9")

    # ---- 3. None 传播 ----
    a = ATR(14)
    for k in range(13):
        assert a.update(10 + k, 8 + k, 9 + k) is None, f"第 {k} 根应 None"
    assert a.update(23, 21, 22) is not None, "第 14 根应出种子值"

    d = ADX(14)
    vals = [d.update(10 + (k % 5), 8 + (k % 3), 9) for k in range(20)]
    assert all(v is None for v in vals), "ADX 种子建立前应全 None"
    print("  [None] 样本不足 → None；ATR 第 n 根出种子值")

    print("T01 全部通过 ✅")


if __name__ == "__main__":
    main()
