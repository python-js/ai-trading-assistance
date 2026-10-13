"""P0-3 指标验证：ta 库结果 vs 手写标准公式（SMA=rolling mean；EMA=ewm；
RSI/ATR=Wilder RMA，即 TradingView 口径）。生产口径为 sma5/10/20（对齐 OKX MA），
EMA 经研究层参数 emas 覆盖。量化差异，判断是否需要对齐看盘软件口径。
用法：.venv/Scripts/python.exe backend/scripts/verify_indicators.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

import pandas as pd

from app.core.config import settings
from app.engine.indicators import enrich
from app.models import db


def sma_std(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n).mean()


def ema_std(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / n, adjust=False).mean()
    al = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr_wilder(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def cmp(name: str, a: pd.Series, b: pd.Series) -> None:
    d = (a - b).abs()
    dd = d.dropna()
    print(f"{name:8} ta末值={a.iloc[-1]:10.4f}  标准末值={b.iloc[-1]:10.4f}  "
          f"全程最大差={dd.max():9.4f}(第{dd.idxmax()}根) 平均={dd.mean():.4f}")


def decay(name: str, a: pd.Series, b: pd.Series, bins=(20, 50, 100, 200, 400)) -> None:
    """分段报告 ta vs 标准 的最大绝对差，观察种子差异随 bar 衰减是否收敛。"""
    d = (a - b).abs()
    edges = (0,) + bins + (len(d),)
    parts = []
    for lo, hi in zip(edges, edges[1:]):
        seg = d.iloc[lo:hi].dropna()
        parts.append(f"[{lo}-{hi})={seg.max():.4f}" if len(seg) else f"[{lo}-{hi})=NA")
    print(f"{name:8} 分段最大差 " + "  ".join(parts))


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    df = db.load_ohlcv(conn, "BTC/USDT", "okx", "spot", "1h")
    conn.close()
    print(f"样本: BTC/USDT 1h 共 {len(df)} 根 | {df.ts.min()} ~ {df.ts.max()}\n")

    ind = enrich(df)  # 生产口径：含 sma5/sma10/sma20/rsi14/atr14（ta 库）
    close, high, low = df["close"], df["high"], df["low"]

    cmp("SMA5", ind["sma5"], sma_std(close, 5))
    cmp("SMA10", ind["sma10"], sma_std(close, 10))
    cmp("SMA20", ind["sma20"], sma_std(close, 20))
    cmp("RSI14", ind["rsi14"], rsi_wilder(close, 14))
    cmp("ATR14", ind["atr14"], atr_wilder(high, low, close, 14))
    ind_e = enrich(df, emas=(20, 60))  # 研究层可选 EMA
    cmp("EMA20*", ind_e["ema20"], ema_std(close, 20))
    cmp("EMA60*", ind_e["ema60"], ema_std(close, 60))
    print("（* 研究层 emas 参数口径，非生产默认）")
    print()
    decay("RSI14", ind["rsi14"], rsi_wilder(close, 14))
    decay("ATR14", ind["atr14"], atr_wilder(high, low, close, 14))

    print("\n最近 3 根（ta 口径）:")
    print(ind[["ts", "close", "sma5", "sma20", "rsi14", "atr14"]].tail(3).to_string(index=False))


if __name__ == "__main__":
    main()
