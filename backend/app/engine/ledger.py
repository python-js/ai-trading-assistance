"""P1-1 模拟台账（S6）：信号配对结算 → paper_position。

口径（2026-10-12 确认）：成交价 = 信号 bar 收盘价（=推送价，PIT）；每笔名义本金：
币圈 1000 USDT（taker 单边 0.05% + 滑点单边 5bps，与研究层 run_backtest.py 一致），
A股 10000 CNY（佣金万 2.5 单边、最低 5 元/笔 + 印花税千 0.5 仅卖出 + 滑点单边 5bps）。
幂等：以入场 signal_id 为唯一键 upsert，rebuild() 可反复执行、结果一致。
"""
from __future__ import annotations

import pandas as pd

from app.models import db

FEES = 0.0005      # taker 单边（币圈）
SLIP_BPS = 5.0     # 滑点单边 bps
NOTIONAL = 1000.0  # 每笔名义本金 USDT（币圈）

COMM_A = 0.00025     # A股佣金万 2.5 单边
MIN_COMM_A = 5.0     # A股佣金最低 5 元/笔
STAMP_A = 0.0005     # A股印花税千 0.5 仅卖出
NOTIONAL_A = 10_000.0  # A股每笔名义本金 CNY


class _Px:
    """价格视图：TS 索引 + numpy 数组（避免逐笔 pandas 切片——rebuild 分钟级瓶颈即此）。"""

    __slots__ = ("index", "low", "close")

    def __init__(self, df: pd.DataFrame):
        self.index = df.index
        self.low = df["low"].to_numpy()
        self.close = df["close"].to_numpy()

    def close_at(self, ts) -> float:
        pos = self.index.searchsorted(ts)
        if pos >= len(self.index) or self.index[pos] != ts:
            raise KeyError(f"ts 不在 K 线中: {ts}")
        return float(self.close[pos])

    def low_min(self, start, end) -> float:
        a = self.index.searchsorted(start, side="left")
        b = self.index.searchsorted(end, side="right")
        return float(self.low[a:b].min())


def _load_px(conn, symbol: str, exchange: str, contract_type: str, timeframe: str) -> _Px:
    df = db.load_ohlcv(conn, symbol, exchange, contract_type, timeframe)
    return _Px(df.set_index("ts")[["low", "close"]])


def _get_px(conn, cache: dict, key: tuple) -> _Px:
    """按标的口径惰性加载并缓存（OHLCV 只依赖 key 前 4 元，同标的/周期多规则共享）。"""
    k4 = key[:4]
    if k4 not in cache:
        cache[k4] = _load_px(conn, *k4)
    return cache[k4]


def _settle(entry: dict, exit_: dict | None, px: _Px, *,
            fees: float, slip_bps: float, notional: float, stock: bool = False) -> dict:
    slip = slip_bps / 10_000
    e_close = px.close_at(entry["ts"])
    entry_eff = e_close * (1 + slip)
    if stock:
        notional = NOTIONAL_A
    qty = notional / entry_eff
    end_ts = exit_["ts"] if exit_ else px.index[-1]
    row = {
        "signal_id": entry["id"], "symbol": entry["symbol"], "exchange": entry["exchange"],
        "contract_type": entry["contract_type"], "direction": "long",
        "entry_ts": entry["ts"], "entry_price": e_close, "qty": qty,
        "max_adverse_excursion": px.low_min(entry["ts"], end_ts) / entry_eff - 1,
    }
    if exit_ is None:
        row.update(status="open", exit_ts=None, exit_price=None, pnl=None, pnl_pct=None)
        return row
    x_close = px.close_at(exit_["ts"])
    if stock:
        cost_in = notional + max(notional * COMM_A, MIN_COMM_A)
        gross = qty * x_close * (1 - slip)
        proceeds = gross - max(gross * COMM_A, MIN_COMM_A) - gross * STAMP_A
    else:
        cost_in = notional * (1 + fees)
        proceeds = qty * x_close * (1 - slip) * (1 - fees)
    row.update(status="closed", exit_ts=exit_["ts"], exit_price=x_close,
               pnl=proceeds - cost_in, pnl_pct=(proceeds - cost_in) / cost_in)
    return row


def build_trades(conn, *, fees: float = FEES, slip_bps: float = SLIP_BPS,
                 notional: float = NOTIONAL) -> list[dict]:
    """从 signal 表重算全部配对（状态机语义：持仓中不重复入场、空仓不重复出场）。"""
    out: list[dict] = []
    px_cache: dict[tuple, _Px] = {}
    pending: dict[tuple, dict] = {}
    for s in db.load_all_signals(conn):
        key = (s["symbol"], s["exchange"], s["contract_type"], s["timeframe"], s["rule_id"])
        if s["direction"] == "entry":
            pending[key] = s
        elif key in pending:
            px = _get_px(conn, px_cache, key)
            out.append(_settle(pending.pop(key), s, px, fees=fees, slip_bps=slip_bps,
                               notional=notional, stock=s["exchange"] == "akshare"))
    for key, entry in pending.items():
        px = _get_px(conn, px_cache, key)
        out.append(_settle(entry, None, px, fees=fees, slip_bps=slip_bps,
                           notional=notional, stock=entry["exchange"] == "akshare"))
    out.sort(key=lambda r: r["entry_ts"])
    return out


_UPSERT = """
INSERT INTO paper_position (signal_id, symbol, exchange, contract_type, direction,
       entry_ts, entry_price, qty, status, exit_ts, exit_price, pnl, pnl_pct,
       max_adverse_excursion)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (signal_id) DO UPDATE SET
       symbol=EXCLUDED.symbol, exchange=EXCLUDED.exchange,
       contract_type=EXCLUDED.contract_type, direction=EXCLUDED.direction,
       entry_ts=EXCLUDED.entry_ts, entry_price=EXCLUDED.entry_price, qty=EXCLUDED.qty,
       status=EXCLUDED.status, exit_ts=EXCLUDED.exit_ts, exit_price=EXCLUDED.exit_price,
       pnl=EXCLUDED.pnl, pnl_pct=EXCLUDED.pnl_pct,
       max_adverse_excursion=EXCLUDED.max_adverse_excursion
"""


def rebuild(conn, *, fees: float = FEES, slip_bps: float = SLIP_BPS,
            notional: float = NOTIONAL) -> dict:
    """全量重算并 upsert（幂等）；清理已不在回算集合中的陈旧行（含孤儿）。"""
    trades = build_trades(conn, fees=fees, slip_bps=slip_bps, notional=notional)
    with conn.cursor() as cur:
        cur.executemany(_UPSERT, [
            (t["signal_id"], t["symbol"], t["exchange"], t["contract_type"],
             t["direction"], t["entry_ts"], t["entry_price"], t["qty"], t["status"],
             t["exit_ts"], t["exit_price"], t["pnl"], t["pnl_pct"],
             t["max_adverse_excursion"]) for t in trades])
        cur.execute("""DELETE FROM paper_position
                       WHERE signal_id IS NULL OR signal_id <> ALL(%s)""",
                    ([t["signal_id"] for t in trades],))
    conn.commit()
    closed = sum(t["status"] == "closed" for t in trades)
    return {"total": len(trades), "closed": closed, "open": len(trades) - closed}
