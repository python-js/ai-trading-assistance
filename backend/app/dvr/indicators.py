"""DVR 指标层：数组化计算 + 分型/POC/斐波那契。

- 5m 信号指标：MACD(12,26,9)、RSI14、ATR14、SMA20/60。
- 15m 环境指标：SMA20/60、ATR14、ADX14。
- 分型 k=2：极值 bar 之后 k 根才确认；调用方只允许使用 confirm_idx ≤ 当前 5m bar 的点。
- POC：滚动 96 根 15m 的成交量加权价格分布（240 桶）；按需计算并缓存。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange


@dataclass(frozen=True, slots=True)
class Pivot:
    side: str      # "H" | "L"
    ext: int       # 极值 bar 在对应周期序列中的下标
    price: float
    confirm: int   # 确认 bar 下标 = ext + k


def _arr(df: pd.DataFrame, col: str) -> np.ndarray:
    return df[col].to_numpy(dtype=float)


def signal_indicators(df5: pd.DataFrame, cfg: dict) -> dict:
    c = _arr(df5, "close")
    ic = cfg["ind"]
    macd = MACD(pd.Series(c), window_slow=ic["macd_slow"],
                window_fast=ic["macd_fast"],
                window_sign=ic["macd_signal"], fillna=False)
    dif = macd.macd().to_numpy(dtype=float)
    dea = macd.macd_signal().to_numpy(dtype=float)
    out = {
        "open": _arr(df5, "open"), "high": _arr(df5, "high"),
        "low": _arr(df5, "low"), "close": c, "volume": _arr(df5, "volume"),
        "dif": dif, "dea": dea, "hist": 2.0 * (dif - dea),
        "rsi": RSIIndicator(pd.Series(c), window=ic["rsi"], fillna=False).rsi().to_numpy(dtype=float),
        "atr": AverageTrueRange(pd.Series(_arr(df5, "high")), pd.Series(_arr(df5, "low")),
                                pd.Series(c), window=ic["atr"], fillna=False)
        .average_true_range().to_numpy(dtype=float),
        "sma_fast": SMAIndicator(pd.Series(c), window=ic["sma_fast"], fillna=False)
        .sma_indicator().to_numpy(dtype=float),
        "sma_slow": SMAIndicator(pd.Series(c), window=ic["sma_slow"], fillna=False)
        .sma_indicator().to_numpy(dtype=float),
    }
    return out


def context_indicators(df15: pd.DataFrame, cfg: dict) -> dict:
    h, l, c = _arr(df15, "high"), _arr(df15, "low"), _arr(df15, "close")
    ic = cfg["ind"]
    return {
        "open": _arr(df15, "open"), "high": h, "low": l, "close": c,
        "volume": _arr(df15, "volume"),
        "sma_fast": SMAIndicator(pd.Series(c), window=ic["sma_fast"], fillna=False)
        .sma_indicator().to_numpy(dtype=float),
        "sma_slow": SMAIndicator(pd.Series(c), window=ic["sma_slow"], fillna=False)
        .sma_indicator().to_numpy(dtype=float),
        "atr": AverageTrueRange(pd.Series(h), pd.Series(l), pd.Series(c),
                                window=ic["atr"], fillna=False)
        .average_true_range().to_numpy(dtype=float),
        "adx": ADXIndicator(pd.Series(h), pd.Series(l), pd.Series(c),
                            window=ic["adx"], fillna=False).adx().to_numpy(dtype=float),
    }


def find_pivots(high: np.ndarray, low: np.ndarray, k: int) -> list[Pivot]:
    out: list[Pivot] = []
    n = len(high)
    for j in range(k, n - k):
        h = high[j]
        l = low[j]
        is_h = all(h > high[j - m] for m in range(1, k + 1)) and \
               all(h > high[j + m] for m in range(1, k + 1))
        is_l = all(l < low[j - m] for m in range(1, k + 1)) and \
               all(l < low[j + m] for m in range(1, k + 1))
        if is_h:
            out.append(Pivot("H", j, float(h), j + k))
        if is_l:
            out.append(Pivot("L", j, float(l), j + k))
    out.sort(key=lambda p: p.ext)
    return out


def split_pivots(pivots: list[Pivot]) -> tuple[list[Pivot], list[Pivot]]:
    highs = [p for p in pivots if p.side == "H"]
    lows = [p for p in pivots if p.side == "L"]
    return highs, lows


def poc_at(h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray,
           j: int, bars: int, bins: int) -> float | None:
    lo_idx = j + 1 - bars
    if lo_idx < 0:
        return None
    hi_p = float(h[lo_idx:j + 1].max())
    lo_p = float(l[lo_idx:j + 1].min())
    if hi_p <= lo_p:
        return None
    tp = (h[lo_idx:j + 1] + l[lo_idx:j + 1] + c[lo_idx:j + 1]) / 3.0
    w = v[lo_idx:j + 1]
    if float(w.sum()) <= 0:
        return None
    hist, _ = np.histogram(tp, bins=bins, range=(lo_p, hi_p), weights=w)
    if float(hist.sum()) <= 0:
        return None
    k = int(hist.argmax())
    return lo_p + (k + 0.5) * (hi_p - lo_p) / bins


def fib_prices(pivots: list[Pivot], j: int, side: str, atr15: float,
               min_leg_atr: float, ratios: list[float]) -> list[float] | None:
    if not np.isfinite(atr15) or atr15 <= 0:
        return None
    pts = [p for p in pivots if p.confirm <= j and p.ext <= j]
    for i in range(len(pts) - 1, 0, -1):
        b, a = pts[i], pts[i - 1]
        if a.side == b.side:
            continue
        if side == "long":
            if not (a.side == "L" and b.side == "H"):
                return None
            lo, hi = a.price, b.price
            levels = [hi - (hi - lo) * r for r in ratios]
        else:
            if not (a.side == "H" and b.side == "L"):
                return None
            lo, hi = b.price, a.price
            levels = [lo + (hi - lo) * r for r in ratios]
        if hi - lo < min_leg_atr * atr15:
            return None
        return levels
    return None
