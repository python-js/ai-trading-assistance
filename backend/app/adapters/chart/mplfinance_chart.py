"""P0-7 K 线截图（mplfinance）：蜡烛 + MA 叠加 + 买卖点标注。

约定：df 须先经 engine.indicators.enrich（含 sma5/sma10/sma20）；events 为
[{ts, direction(entry/exit), price, price_zone_low?, price_zone_high?}]。
只画尾部 bars 根；价格区间虚线仅画最后一个带区间的事件。
时间轴显示北京时间（UTC+8）；事件 ts 匹配仍按库内 UTC。
标题附第二行线色说明（橙=MA5/紫=MA10/蓝=MA20）；价格轴小数位按 exchange 自适应
（虚拟币≥4 位、A 股 2 位，见 core.fmt）。
"""
import pathlib

import mplfinance as mpf
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.ticker import FuncFormatter

from app.core.fmt import price_decimals
from app.core.tz import CST

# mplfinance 的 style 会重置字体，中文须经 make_mpf_style(rc=...) 注入
_STYLE = mpf.make_mpf_style(base_mpf_style="yahoo", rc={
    "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
    "axes.unicode_minus": False,
})

_UP, _DN = "#27ae60", "#c0392b"


def render(df: pd.DataFrame, events: list[dict], out_path: str, *,
           bars: int = 120, title: str = "", exchange: str = "") -> str:
    win = df.tail(bars).reset_index(drop=True)
    idx = pd.DatetimeIndex(win["ts"]).tz_convert(CST)
    ohlc = pd.DataFrame({"Open": win["open"].values, "High": win["high"].values,
                         "Low": win["low"].values, "Close": win["close"].values,
                         "Volume": win["volume"].values}, index=idx)
    apds = [mpf.make_addplot(pd.Series(win["sma20"].values, index=idx), color="#2f80ed", width=0.9),
            mpf.make_addplot(pd.Series(win["sma10"].values, index=idx), color="#eb2f96", width=0.9),
            mpf.make_addplot(pd.Series(win["sma5"].values, index=idx), color="#f5a623", width=0.9)]

    pad = float(win["high"].max() - win["low"].min()) * 0.03
    ent = pd.Series(float("nan"), index=idx, dtype=float)
    ext = pd.Series(float("nan"), index=idx, dtype=float)
    last_zone, zone_i = None, -1
    for e in events:
        t = pd.Timestamp(e["ts"])
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        hit = win.index[win["ts"] == t]
        if hit.empty:
            continue
        i = int(hit[0])
        if e["direction"] == "entry":
            ent.iloc[i] = float(win["low"].iloc[i]) - 0.25 * pad
        else:
            ext.iloc[i] = float(win["high"].iloc[i]) + 0.25 * pad
        if e.get("price_zone_low") is not None:
            last_zone, zone_i = e, i
    if ent.notna().any():
        apds.append(mpf.make_addplot(ent, type="scatter", marker="^", markersize=110, color=_UP))
    if ext.notna().any():
        apds.append(mpf.make_addplot(ext, type="scatter", marker="v", markersize=110, color=_DN))
    if last_zone is not None:
        # 区间虚线从信号 bar 向右延伸（可操作区间，纵向不做长横线误读为支撑位）
        for y, c in ((last_zone["price_zone_low"], _UP), (last_zone["price_zone_high"], _DN)):
            z = pd.Series(float("nan"), index=idx, dtype=float)
            z.iloc[zone_i:] = y
            apds.append(mpf.make_addplot(z, color=c, width=0.7, linestyle="--"))

    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    dec = price_decimals(float(win["close"].iloc[-1]), exchange)
    fig, _ = mpf.plot(ohlc, type="candle", style=_STYLE, addplot=apds, volume=True, figsize=(12, 7),
                      title=f"{title}\n橙线=MA5 · 紫线=MA10 · 蓝线=MA20", returnfig=True)
    for ax in fig.axes:  # 价格轴按币种小数位（twinx 轴 ylabel 为空，仅命中主价格轴）
        if ax.get_ylabel().startswith("Price"):
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.{dec}f}"))
    fig.savefig(out_path, dpi=80, bbox_inches="tight")
    plt.close(fig)
    return out_path
