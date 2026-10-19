"""DVR 信号引擎：15m 环境/关键区 → 5m 背离/反转/结构 → RSI/MACD 否决 → 成本闸门。

严格 PIT：
- 5m 分型 k=2，L2 在 confirm(ext+2) 后才可用；背离窗口只允许 i−confirm ∈ [0,2]。
- 15m 只用 close_ts ≤ 当前 5m 收盘时刻的最后一根已收盘 bar。
- 所有指标值来自已收盘 bar；禁止回填/重绘。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

import numpy as np

from .config import CFG, round_trip_bps
from .indicators import (Pivot, context_indicators, fib_prices, find_pivots,
                         poc_at, signal_indicators, split_pivots)


@dataclass(slots=True)
class Setup:
    side: str
    entry: float
    stop: float
    R: float
    r_bps: float
    l1: Pivot
    l2: Pivot
    reversal_idx: int
    zone_hits: int
    zone_kinds: tuple[str, ...]
    div_id: tuple


@dataclass(slots=True)
class SideResult:
    setup: Setup | None
    reason: str
    detail: dict = field(default_factory=dict)


class SignalEngine:
    def __init__(self, df5, df15, cfg: dict = CFG):
        self.cfg = cfg
        self.i5 = signal_indicators(df5, cfg)
        self.i15 = context_indicators(df15, cfg)
        self.ts5 = df5["ts"].to_numpy(dtype=np.int64)
        self.ts15 = df15["ts"].to_numpy(dtype=np.int64)
        piv5 = find_pivots(self.i5["high"], self.i5["low"], cfg["swing"]["k"])
        piv15 = find_pivots(self.i15["high"], self.i15["low"], cfg["swing"]["k"])
        self.h5, self.l5 = split_pivots(piv5)
        self.h15, self.l15 = split_pivots(piv15)
        self.piv15 = piv15
        self.ext_h5 = [p.ext for p in self.h5]
        self.ext_l5 = [p.ext for p in self.l5]
        self.conf_h5 = [p.confirm for p in self.h5]
        self.conf_l5 = [p.confirm for p in self.l5]
        self.ext_h15 = [p.ext for p in self.h15]
        self.ext_l15 = [p.ext for p in self.l15]
        self.conf_h15 = [p.confirm for p in self.h15]
        self.conf_l15 = [p.confirm for p in self.l15]
        close15_ts = self.ts15 + cfg["tf"]["context"]
        self.j15 = np.searchsorted(close15_ts, self.ts5 + cfg["tf"]["signal"],
                                   side="right") - 1
        self._poc_cache: dict[int, float | None] = {}

    # ---------------- 15m 关键区 ----------------
    def _poc(self, j: int) -> float | None:
        if j not in self._poc_cache:
            z = self.cfg["zone"]
            self._poc_cache[j] = poc_at(self.i15["high"], self.i15["low"],
                                        self.i15["close"], self.i15["volume"],
                                        j, z["poc_bars"], z["poc_bins"])
        return self._poc_cache[j]

    def _last_confirmed(self, pivs: list[Pivot], conf: list[int], now: int,
                        min_ext: int = -1) -> Pivot | None:
        k = bisect_right(conf, now) - 1
        while k >= 0:
            p = pivs[k]
            if p.ext >= min_ext:
                return p
            k -= 1
        return None

    def _key_zone(self, i: int, side: str, l2: Pivot) -> tuple[bool, int, tuple[str, ...]]:
        z = self.cfg["zone"]
        j = int(self.j15[i])
        if j < z["lookback"]:
            return False, 0, ()
        atr = self.i15["atr"][j]
        if not np.isfinite(atr) or atr <= 0:
            return False, 0, ()
        tol = z["tol_atr"] * atr
        hits: list[str] = []
        c = self.i5["close"][i]
        if side == "long":
            p = self._last_confirmed(self.l15, self.conf_l15, j, j - z["lookback"])
            if p is not None:
                if abs(l2.price - p.price) <= tol:
                    hits.append("prior_low")
                elif l2.price < p.price and c > p.price:
                    hits.append("prior_low_sweep")
            poc = self._poc(j)
            if poc is not None and abs(l2.price - poc) <= tol:
                hits.append("poc")
            fibs = fib_prices(self.piv15, j, "long", float(atr),
                              z["min_leg_atr"], z["fib_ratios"])
            if fibs and any(abs(l2.price - f) <= tol for f in fibs):
                hits.append("fib")
        else:
            p = self._last_confirmed(self.h15, self.conf_h15, j, j - z["lookback"])
            if p is not None:
                if abs(l2.price - p.price) <= tol:
                    hits.append("prior_high")
                elif l2.price > p.price and c < p.price:
                    hits.append("prior_high_sweep")
            poc = self._poc(j)
            if poc is not None and abs(l2.price - poc) <= tol:
                hits.append("poc")
            fibs = fib_prices(self.piv15, j, "short", float(atr),
                              z["min_leg_atr"], z["fib_ratios"])
            if fibs and any(abs(l2.price - f) <= tol for f in fibs):
                hits.append("fib")
        return bool(hits), len(hits), tuple(hits)

    # ---------------- 15m 环境 ----------------
    def _env(self, i: int, side: str) -> bool:
        e = self.cfg["env"]
        j = int(self.j15[i])
        look = e["adx_lookback"]
        if j < max(self.cfg["ind"]["sma_slow"], look):
            return False
        c = self.i15["close"][j]
        s20 = self.i15["sma_fast"][j]
        s60 = self.i15["sma_slow"][j]
        atr = self.i15["atr"][j]
        adx = self.i15["adx"][j]
        if not all(np.isfinite(x) for x in (c, s20, s60, atr, adx)):
            return False
        adx_win = self.i15["adx"][j - look + 1:j + 1]
        adx_max = float(np.nanmax(adx_win)) if np.isfinite(adx_win).any() else np.nan
        if not np.isfinite(adx_max):
            return False
        # 方案原文：ADX 从高位回落，或价格偏离 15m 均线过远（两者取或）。
        adx_falling = adx_max >= e["adx_min"] and adx < adx_max
        if side == "long":
            trend_ok = c > s60 or s20 > s60
            stretch_ok = (s20 - c) >= e["stretch_atr"] * atr
        else:
            trend_ok = c < s60 or s20 < s60
            stretch_ok = (c - s20) >= e["stretch_atr"] * atr
        return bool(trend_ok or adx_falling or stretch_ok)

    # ---------------- 5m 背离/反转/结构 ----------------
    def _active_div(self, i: int, side: str) -> tuple[Pivot, Pivot] | None:
        s = self.cfg["swing"]
        if side == "long":
            pivs, conf = self.l5, self.conf_l5
        else:
            pivs, conf = self.h5, self.conf_h5
        k = bisect_right(conf, i) - 1
        if k < 1:
            return None
        l2, l1 = pivs[k], pivs[k - 1]
        max_wait = self.cfg["reversal"]["max_after_confirm"] + 1
        if not (0 <= i - l2.confirm <= max_wait):
            return None
        if l2.ext < s["recent_bars"]:
            return None
        sep = l2.ext - l1.ext
        if not (s["min_sep"] <= sep <= s["max_sep"]):
            return None
        dif = self.i5["dif"]
        if side == "long":
            if not (l2.price < l1.price):
                return None
            if not (l2.price < float(self.i5["low"][l2.ext - s["recent_bars"]:l2.ext].min())):
                return None
            if not (np.isfinite(dif[l2.ext]) and np.isfinite(dif[l1.ext])
                    and dif[l2.ext] > dif[l1.ext]):
                return None
        else:
            if not (l2.price > l1.price):
                return None
            if not (l2.price > float(self.i5["high"][l2.ext - s["recent_bars"]:l2.ext].max())):
                return None
            if not (np.isfinite(dif[l2.ext]) and np.isfinite(dif[l1.ext])
                    and dif[l2.ext] < dif[l1.ext]):
                return None
        return l1, l2

    def _is_reversal(self, idx: int, side: str) -> bool:
        if idx < 1:
            return False
        o, h, l, c = (self.i5["open"], self.i5["high"],
                      self.i5["low"], self.i5["close"])
        rng = h[idx] - l[idx]
        if not np.isfinite(rng) or rng <= 0:
            return False
        body = abs(c[idx] - o[idx])
        prev_body = abs(c[idx - 1] - o[idx - 1])
        pos = (c[idx] - l[idx]) / rng
        cp = self.cfg["reversal"]
        if side == "long":
            engulf = (c[idx] > h[idx - 1] and body > 0
                      and body >= cp["engulf_body_mult"] * prev_body
                      and pos >= cp["close_pos"])
            lower = min(o[idx], c[idx]) - l[idx]
            pin = (lower >= cp["pin_shadow_mult"] * max(body, 1e-12)
                   and pos >= cp["close_pos"]
                   and l[idx] <= float(self.i5["low"][max(0, idx - 5):idx].min()))
            return bool(engulf or pin)
        engulf = (c[idx] < l[idx - 1] and body > 0
                  and body >= cp["engulf_body_mult"] * prev_body
                  and (1.0 - pos) >= cp["close_pos"])
        upper = h[idx] - max(o[idx], c[idx])
        pin = (upper >= cp["pin_shadow_mult"] * max(body, 1e-12)
               and (1.0 - pos) >= cp["close_pos"]
               and h[idx] >= float(self.i5["high"][max(0, idx - 5):idx].max()))
        return bool(engulf or pin)

    def _reversal(self, i: int, side: str, l2: Pivot) -> int | None:
        # 反转 K 线可出现在 L2 当根至 t0+1；因分型 k=2，早于 t0 的 K 线
        # 只能在 i≥t0 时才允许被使用（active_div 已保证 i≥t0），不会前视。
        cp = self.cfg["reversal"]
        found = None
        for cand in range(l2.ext, l2.confirm + cp["max_after_confirm"] + 1):
            if cand > i:
                break
            if self._is_reversal(cand, side):
                found = cand
        return found

    def _neckline_high(self, a: int, b: int) -> float | None:
        lo = bisect_right(self.ext_h5, a)
        hi = bisect_left(self.ext_h5, b)
        if hi > lo:
            return self.h5[hi - 1].price
        k = bisect_left(self.ext_h5, b) - 1
        return self.h5[k].price if k >= 0 else None

    def _neckline_low(self, a: int, b: int) -> float | None:
        lo = bisect_right(self.ext_l5, a)
        hi = bisect_left(self.ext_l5, b)
        if hi > lo:
            return self.l5[hi - 1].price
        k = bisect_left(self.ext_l5, b) - 1
        return self.l5[k].price if k >= 0 else None

    def _higher_low_break(self, i: int, l2: Pivot) -> bool:
        k = bisect_right(self.ext_l5, l2.ext)
        for p in reversed(self.l5[k:]):
            if p.confirm > i:
                continue
            if p.price <= l2.price:
                continue
            hi = bisect_right(self.ext_h5, l2.ext)
            lo = bisect_left(self.ext_h5, p.ext)
            if lo <= hi:
                continue
            hh = max(q.price for q in self.h5[hi:lo])
            return bool(self.i5["close"][i] > hh)
        return False

    def _lower_high_break(self, i: int, h2: Pivot) -> bool:
        k = bisect_right(self.ext_h5, h2.ext)
        for p in reversed(self.h5[k:]):
            if p.confirm > i:
                continue
            if p.price >= h2.price:
                continue
            hi = bisect_right(self.ext_l5, h2.ext)
            lo = bisect_left(self.ext_l5, p.ext)
            if lo <= hi:
                continue
            ll = min(q.price for q in self.l5[hi:lo])
            return bool(self.i5["close"][i] < ll)
        return False

    def _structure(self, i: int, side: str, l1: Pivot, l2: Pivot, r: int) -> bool:
        c = self.i5["close"][i]
        h, l = self.i5["high"], self.i5["low"]
        if side == "long":
            neck = self._neckline_high(l1.ext, l2.ext)
            if neck is not None and c > neck:
                return True
            if i == r + 1 and self._is_engulf(r, side) and c >= h[r - 1]:
                return True
            return self._higher_low_break(i, l2)
        neck = self._neckline_low(l1.ext, l2.ext)
        if neck is not None and c < neck:
            return True
        if i == r + 1 and self._is_engulf(r, side) and c <= l[r - 1]:
            return True
        return self._lower_high_break(i, l2)

    def _is_engulf(self, idx: int, side: str) -> bool:
        o, h, l, c = (self.i5["open"], self.i5["high"],
                      self.i5["low"], self.i5["close"])
        rng = h[idx] - l[idx]
        if not np.isfinite(rng) or rng <= 0 or idx < 1:
            return False
        body = abs(c[idx] - o[idx])
        prev_body = abs(c[idx - 1] - o[idx - 1])
        pos = (c[idx] - l[idx]) / rng
        cp = self.cfg["reversal"]
        if side == "long":
            return bool(c[idx] > h[idx - 1] and body > 0
                        and body >= cp["engulf_body_mult"] * prev_body
                        and pos >= cp["close_pos"])
        return bool(c[idx] < l[idx - 1] and body > 0
                    and body >= cp["engulf_body_mult"] * prev_body
                    and (1.0 - pos) >= cp["close_pos"])

    # ---------------- RSI / MACD / 成本 ----------------
    def _rsi_ok(self, i: int, side: str, l2: Pivot) -> bool:
        rsi = self.i5["rsi"]
        if not (np.isfinite(rsi[l2.ext]) and np.isfinite(rsi[i])
                and np.isfinite(rsi[i - 1])):
            return False
        if side == "long":
            return bool(rsi[l2.ext] <= self.cfg["rsi"]["long_max"]
                        and rsi[i] > rsi[i - 1])
        return bool(rsi[l2.ext] >= self.cfg["rsi"]["short_min"]
                    and rsi[i] < rsi[i - 1])

    def _macd_ok(self, i: int, side: str) -> bool:
        lb = self.cfg["macd"]["hist_lookback"]
        if i < lb:
            return False
        dif, dea, hist = self.i5["dif"], self.i5["dea"], self.i5["hist"]
        if not all(np.isfinite(x[i]) for x in (dif, dea, hist)):
            return False
        if side == "long":
            return bool(dif[i] >= dea[i] or
                        (dif[i] < dea[i] and hist[i] > hist[i - 1]
                         and hist[i] > hist[i - lb]))
        return bool(dif[i] <= dea[i] or
                    (dif[i] > dea[i] and hist[i] < hist[i - 1]
                     and hist[i] < hist[i - lb]))

    def _cost(self, i: int, side: str, l2: Pivot, r: int) -> tuple[float, float, float] | None:
        atr = self.i5["atr"][i]
        c = float(self.i5["close"][i])
        if not np.isfinite(atr) or atr <= 0 or not np.isfinite(c) or c <= 0:
            return None
        buf = max(2.0 * self.cfg["venue"]["tick_size"], 0.3 * atr)
        if side == "long":
            stop = min(float(self.i5["low"][l2.ext]), float(self.i5["low"][r])) - buf
            R = c - stop
        else:
            stop = max(float(self.i5["high"][l2.ext]), float(self.i5["high"][r])) + buf
            R = stop - c
        if not np.isfinite(R) or R <= 0:
            return None
        r_bps = R / c * 1e4
        if r_bps < self.cfg["risk"]["min_r_bps"]:
            return None
        if r_bps < self.cfg["risk"]["min_cost_r"] * round_trip_bps(self.cfg):
            return None
        cap = self.cfg["risk"]["r_max_atr15"]
        if cap is not None:
            j = int(self.j15[i])
            atr15 = self.i15["atr"][j]
            if not np.isfinite(atr15) or R > cap * atr15:
                return None
        return float(stop), float(R), float(r_bps)

    # ---------------- 单侧评估 ----------------
    def evaluate_side(self, i: int, side: str) -> SideResult:
        div = self._active_div(i, side)
        if div is None:
            return SideResult(None, "NO_DIV", {"active": False})
        l1, l2 = div
        detail = {"active": True, "l2_ts": int(self.ts5[l2.ext]),
                  "l2_confirm_ts": int(self.ts5[l2.confirm]) if l2.confirm < len(self.ts5) else None,
                  "div_id": (side, int(self.ts5[l2.ext]))}
        zone_ok, hits, kinds = self._key_zone(i, side, l2)
        detail.update({"zone_hits": hits, "zone_kinds": kinds})
        if not zone_ok:
            return SideResult(None, "ZONE", detail)
        if not self._env(i, side):
            return SideResult(None, "ENV", detail)
        r = self._reversal(i, side, l2)
        if r is None:
            return SideResult(None, "REVERSAL", detail)
        detail["reversal_ts"] = int(self.ts5[r])
        if not self._structure(i, side, l1, l2, r):
            return SideResult(None, "STRUCT", detail)
        if not self._rsi_ok(i, side, l2):
            return SideResult(None, "RSI", detail)
        if not self._macd_ok(i, side):
            return SideResult(None, "MACD", detail)
        cost = self._cost(i, side, l2, r)
        if cost is None:
            return SideResult(None, "COST", detail)
        stop, R, r_bps = cost
        setup = Setup(side=side, entry=float(self.i5["close"][i]), stop=stop,
                      R=R, r_bps=r_bps, l1=l1, l2=l2, reversal_idx=r,
                      zone_hits=hits, zone_kinds=kinds,
                      div_id=(side, int(self.ts5[l2.ext])))
        return SideResult(setup, "OK", detail)

    def evaluate_all(self, i: int) -> dict[str, SideResult]:
        return {side: self.evaluate_side(i, side) for side in ("long", "short")}
