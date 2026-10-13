"""P0-7 图表验证：BTC/USDT 1h 尾部窗口渲染 MA5/10/20 三线 + 买卖点标注。
验证点：事件 ts 与 K 线 bar 对齐（无静默丢标）、标记方向/位置正确（可视复核）。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.chart.mplfinance_chart import render
from app.core.config import settings
from app.engine.indicators import enrich
from app.engine.rules_dsl import evaluate, parse_rule
from app.models import db

RAW = {"name": "sma_cross", "symbol": "BTC/USDT", "timeframe": "1h",
       "entry": {"cross_up": ["sma5", "sma20"]},
       "exit": {"cross_down": ["sma5", "sma20"]}, "cooldown_bars": 0}
BARS = 240
OUT = "data/charts/verify_btc_1h.png"


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    df = db.load_ohlcv(conn, "BTC/USDT", "okx", "spot", "1h", limit=600)
    conn.close()
    ind = enrich(df)
    ev = evaluate(parse_rule(RAW), ind)

    win = ind.tail(BARS).reset_index(drop=True)
    t0, t1 = win["ts"].iloc[0], win["ts"].iloc[-1]
    in_win = [e for e in ev if t0 <= e.ts <= t1]
    print(f"数据 {len(ind)} 根 | 全程事件 {len(ev)} | 窗口 [{t0} ~ {t1}] 内事件 {len(in_win)}")

    sig = []
    for e in in_win:
        atr = float(ind.loc[ind["ts"] == e.ts, "atr14"].iloc[0])
        sig.append({"ts": e.ts, "direction": e.direction, "price": e.price,
                    "price_zone_low": e.price - 0.5 * atr, "price_zone_high": e.price + 0.5 * atr})
        print(f"  {e.ts:%m-%d %H:%M} {e.direction:5} @ {e.price:.6g}")

    out = render(ind, sig, OUT, bars=BARS, title="BTC/USDT 1h  MA5/10/20 买卖点（P0-7 验证）")
    # 对齐断言：窗口内每个事件都能在窗口中按 ts 找到对应 bar（tz 对齐无静默丢标）
    missed = [s["ts"] for s in sig if win.index[win["ts"] == s["ts"]].empty]
    assert not missed, f"标丢失: {missed}"
    print(f"对齐断言 PASS（{len(sig)} 个事件全部命中 bar）| 已出图: {out}")


if __name__ == "__main__":
    main()
