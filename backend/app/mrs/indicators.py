"""MRS 指标层（规格书 §4）：已收盘 bar 驱动的增量式纯计算。

- §4.9 递推实现：SMA 环形缓冲、RMA/ATR/ADX 增量更新；回测与实盘喂入同一状态对象（§9.0 同源）。
- §4.1 None 语义：样本不足 → None（使所属闸门不通过，不得填充默认值）。
- §0.2 时间单位秒（UTC）；§3.4：调用方只允许喂已收盘 bar。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


class SMA:
    """§4.1：SMA_n(t) = mean(close[t−n+1..t])；样本不足 n → None。"""

    __slots__ = ("n", "buf", "sum")

    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.sum = 0.0

    def update(self, close: float) -> float | None:
        if len(self.buf) == self.n:  # 先摘旧值再加新值（deque 满时 append 会自动挤出最左）
            self.sum -= self.buf[0]
        self.buf.append(close)
        self.sum += close
        return self.value

    @property
    def value(self) -> float | None:
        return self.sum / self.n if len(self.buf) == self.n else None


class RMA:
    """§4.2 Wilder RMA：种子 = 前 n 个样本均值，之后 v += (x − v) / n。"""

    __slots__ = ("n", "seed", "v")

    def __init__(self, n: int):
        self.n = n
        self.seed: list[float] = []
        self.v: float | None = None

    def update(self, x: float) -> float | None:
        if self.v is None:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self.v = sum(self.seed) / self.n
            return self.v
        self.v += (x - self.v) / self.n
        return self.v


class ATR:
    """§4.2：TR = max(h−l, |h−prev_c|, |l−prev_c|) → RMA_n。数据不足 → None。"""

    __slots__ = ("rma", "prev_close", "v")

    def __init__(self, n: int):
        self.rma = RMA(n)
        self.prev_close: float | None = None
        self.v: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is None:
            tr = high - low  # 首根无 prev_close（约定：仅用自身振幅）
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        self.v = self.rma.update(tr)
        return self.v


class ADX:
    """§4.3：s_DM± → RMA(DM±)/RMA(TR) → ±DI → DX → ADX=RMA(DX)。

    种子在累计 n 个有效 DX 后建立；DX 为 None（分母为 0）的 bar 跳过计数。
    数据不足 → ADX=None → regime 闸门 NO_TRADE（安全侧缺省）。
    """

    __slots__ = ("n", "rtr", "rdmp", "rdmn", "prev_high", "prev_low", "prev_close",
                 "dx_seed", "adx", "v")

    def __init__(self, n: int):
        self.n = n
        self.rtr = RMA(n)
        self.rdmp = RMA(n)
        self.rdmn = RMA(n)
        self.prev_high: float | None = None
        self.prev_low: float | None = None
        self.prev_close: float | None = None
        self.dx_seed: list[float] = []
        self.adx: float | None = None
        self.v: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is None:
            tr, up, down = high - low, 0.0, 0.0
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            up = high - self.prev_high
            down = self.prev_low - low
        self.prev_high, self.prev_low, self.prev_close = high, low, close
        rtr = self.rtr.update(tr)
        sdm_p = up if (up > down and up > 0) else 0.0
        sdm_n = down if (down > up and down > 0) else 0.0
        rp = self.rdmp.update(sdm_p)
        rn = self.rdmn.update(sdm_n)
        self.v = None
        if rtr is None or rtr == 0 or rp is None or rn is None:
            return None
        dip = 100.0 * rp / rtr
        din = 100.0 * rn / rtr
        denom = dip + din
        if denom == 0:
            return None  # DX=None → 跳过计数
        dx = 100.0 * abs(dip - din) / denom
        if self.adx is None:
            self.dx_seed.append(dx)
            if len(self.dx_seed) == self.n:
                self.adx = sum(self.dx_seed) / self.n
                self.v = self.adx
        else:
            self.adx += (dx - self.adx) / self.n
            self.v = self.adx
        return self.v


@dataclass(frozen=True, slots=True)
class Swing:
    side: str      # "H" | "L"（§3.3 swings 的 side）
    ext_ts: int    # 极值 bar 开盘 ts（§0.2：秒）
    price: float   # 极值价（HIGH 用 high[j]，LOW 用 low[j]）
    confirm_ts: int  # 确认时刻 = 极值 bar 收盘 + k×period（§4.4-S2）


class SwingTracker:
    """§4.4：k 确认分型（严格不等）→ 确认点 append-only，永不修改删除。

    update() 在每根 bar 收盘时调用一次；每次只需判定窗口中心（i−k）一根。
    §3.4：查询接口只返回 confirm_ts ≤ now 的点，未确认点任何调用方不可见。
    """

    __slots__ = ("period", "k", "win", "confirmed")

    def __init__(self, period: int, k: int):
        self.period = period
        self.k = k
        self.win: deque[tuple] = deque(maxlen=2 * k + 1)  # (ts, high, low)
        self.confirmed: list[Swing] = []

    def update(self, ts: int, high: float, low: float) -> None:
        """喂入一根已收盘 bar（ts=开盘时间；调用时刻即 bar 收盘时刻）。"""
        self.win.append((ts, high, low))
        if len(self.win) < 2 * self.k + 1:
            return
        j, k = self.k, self.k
        ct, ch, cl = self.win[j]
        is_h = all(ch > self.win[j - m][1] for m in range(1, k + 1)) and \
               all(ch > self.win[j + m][1] for m in range(1, k + 1))
        is_l = all(cl < self.win[j - m][2] for m in range(1, k + 1)) and \
               all(cl < self.win[j + m][2] for m in range(1, k + 1))
        confirm_ts = ct + (k + 1) * self.period  # = 极值 bar 收盘 + k×period = bar[j+k] 收盘时刻
        if is_h:
            self.confirmed.append(Swing("H", ct, ch, confirm_ts))
        if is_l:
            self.confirmed.append(Swing("L", ct, cl, confirm_ts))

    def confirmed_swings(self, side: str, now_ts: int, count: int) -> list[Swing]:
        """S-3：confirm_ts ≤ now 的同侧点，取时间最近 count 个（同 ext_ts 去重保留后写），
        返回按极值价格排序（H 从高到低、L 从低到高）。"""
        dedup: dict[int, Swing] = {}
        for s in self.confirmed:
            if s.side == side and s.confirm_ts <= now_ts:
                dedup[s.ext_ts] = s
        pts = sorted(dedup.values(), key=lambda s: s.ext_ts, reverse=True)[:count]
        pts.sort(key=lambda s: s.price, reverse=(side == "H"))
        return pts

    def recent_mixed(self, now_ts: int, count: int) -> list[Swing]:
        """最近 count 个已确认点（混合两侧、时间升序；§5.1 struct 用）。"""
        pts = [s for s in self.confirmed if s.confirm_ts <= now_ts]
        return pts[-count:]


class VolWindow:
    """§4.6：1m 量分位。pctile = #{x ∈ 前 n 根 : x ≤ cur} / n（分母恒为 n=vol_pctile_n，
    窗口未满亦同），严格不含当前 bar；样本 < min_n（刚重启等）→ None → 闸门不通过。"""

    __slots__ = ("n", "buf", "min_n")

    def __init__(self, n: int = 60, min_n: int = 40):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.min_n = min_n

    def update(self, vol: float) -> float | None:
        res = None
        if len(self.buf) >= self.min_n:
            res = sum(1 for x in self.buf if x <= vol) / self.n
        self.buf.append(vol)
        return res


class DayStats:
    """§4.5 当日 VWAP（UTC 重置）+ §4.7 昨日快照（H/L/C/VWAP/POC）。

    - 日内累积 (typical×vol, typical) 明细，日切时按昨日最终 [low,high] 均分 bins 桶算 POC；
    - §3.4：快照只在 UTC 今日已存在至少 1 个 1m bar 后可用（日切即发生在新日首根）；
      数值冻结自昨日最后一根；昨日无数据（新上市）→ snapshot.poc = None。
    """

    __slots__ = ("bins", "day", "cur", "acc", "snapshot")

    def __init__(self, bins: int = 240):
        self.bins = bins
        self.day: int | None = None
        self.cur: dict | None = None
        self.acc: list[tuple] = []       # (成交额, typical)
        self.snapshot: dict | None = None

    def update(self, ts: int, high: float, low: float, close: float, vol: float) -> None:
        day = ts // 86_400
        if self.day is None:
            self.day = day
        elif day != self.day:
            self._freeze()
            self.day, self.cur, self.acc = day, None, []
        typical = (high + low + close) / 3.0
        if self.cur is None:
            self.cur = {"high": high, "low": low, "close": close, "pv": 0.0, "vol": 0.0}
        else:
            self.cur["high"] = max(self.cur["high"], high)
            self.cur["low"] = min(self.cur["low"], low)
            self.cur["close"] = close
        self.cur["pv"] += typical * vol
        self.cur["vol"] += vol
        if vol > 0:
            self.acc.append((typical * vol, typical))

    def _freeze(self) -> None:
        if self.cur is None:
            self.snapshot = None  # 昨日无数据 → None
            return
        lo, hi = self.cur["low"], self.cur["high"]
        poc = None
        if hi > lo and self.acc:
            w = (hi - lo) / self.bins
            buckets = [0.0] * self.bins
            for amt, tp in self.acc:
                k = min(int((tp - lo) / w), self.bins - 1)
                buckets[k] += amt
            if sum(buckets) > 0:
                bi = max(range(self.bins), key=buckets.__getitem__)
                poc = lo + (bi + 0.5) * w
        self.snapshot = {"high": hi, "low": lo, "close": self.cur["close"],
                         "vwap": self.vwap, "poc": poc}

    @property
    def vwap(self) -> float | None:
        """当日 VWAP（首根之前 = None）。"""
        if self.cur is None or self.cur["vol"] <= 0:
            return None
        return self.cur["pv"] / self.cur["vol"]

    @property
    def yesterday(self) -> dict | None:
        return self.snapshot


def fib_levels(swings: list[Swing], now_ts: int, period: int) -> list[float] | None:
    """§4.8（v1 简化）：最近一对方向交替的已确认摆动 → 完成波段 → 0.382/0.5/0.618 位。

    涨波段（LOW→HIGH）自顶向下取回撤支撑；跌波段（HIGH→LOW）自底向上取反弹阻力。
    极值点收盘距今 < 12 根 period → None（波段太新鲜）。
    """
    pts = [s for s in swings if s.confirm_ts <= now_ts]
    for i in range(len(pts) - 1, 0, -1):
        a, b = pts[i - 1], pts[i]
        if a.side == b.side:
            continue
        if now_ts - (b.ext_ts + period) < 12 * period:
            return None
        if a.side == "L":
            lo, hi = a.price, b.price
            return [hi - (hi - lo) * r for r in (0.382, 0.5, 0.618)]
        lo, hi = b.price, a.price
        return [lo + (hi - lo) * r for r in (0.382, 0.5, 0.618)]
    return None
