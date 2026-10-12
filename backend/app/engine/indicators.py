"""指标计算（ta 库封装）。
输入：含 open/high/low/close 列、按时间升序的 ohlcv DataFrame。
输出：原地/副本追加指标列（ema{n}/rsi{n}/atr{n}），供规则 DSL 与回测使用。
选型说明：pandas-ta 仅剩 beta 且要求 Python≥3.12，改用成熟稳定的 ta(bukosabino) 库，见 docs/P0-阶段1-设计.md §6。
"""
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange


def add_ema(df: pd.DataFrame, window: int) -> pd.DataFrame:
    df[f"ema{window}"] = EMAIndicator(df["close"], window=window).ema_indicator()
    return df


def add_rsi(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    df[f"rsi{window}"] = RSIIndicator(df["close"], window=window).rsi()
    return df


def add_atr(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    df[f"atr{window}"] = AverageTrueRange(df["high"], df["low"], df["close"], window=window).average_true_range()
    return df


def enrich(df: pd.DataFrame, emas=(20, 60), rsi=14, atr=14) -> pd.DataFrame:
    """按 ts 升序排列后一次性追加常用指标列（返回副本，不改原 df）。"""
    out = df.sort_values("ts").reset_index(drop=True).copy()
    for w in emas:
        add_ema(out, w)
    if rsi:
        add_rsi(out, rsi)
    if atr:
        add_atr(out, atr)
    return out
