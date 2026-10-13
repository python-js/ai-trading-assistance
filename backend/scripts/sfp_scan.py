"""SFP 客观化扫描（只读，研究用）：Sweep-Fail-Pattern 多头版，独立策略度量 + 与现有台账的重合度。

口径（2026-10-13 定）：
- 事件定义（原始）：bar.low < 前 20 根最低 low（严格）且 bar.close > 该最低（收盘收回其上）。
- 量能变体：vol ≥ 1.5 × 前 20 根均量（不含当根）。
- 入场：事件 bar 收盘 ×(1+滑点)（PIT，与台账同源）；止损 = 事件 bar 低点 − 0.4×ATR14；
  目标 = 入场 + 2×(入场−止损)；时限 7 天（1h=168 根，15m/1m 按分钟等比）；
  同 bar 同时触及止损与目标按止损先（保守）；隐含假设：穿价即成交、无盘中序列。
- 去重：贪心，持仓期内新事件跳过（不重复入场）。
- 台账重合：以原始/放量事件集比对 entry 信号（信号 bar 前 3 根及当根窗内是否有事件），
  命中 vs 其余分组对比；台账组用 paper_position 已平 pnl。费用口径同台账
  （taker 单边 0.05%、滑点 5bps、名义 1000U）。
- 范围：okx spot only；1h 为主，15m/1m 附注（数据窗短）。

用法：.venv/Scripts/python.exe backend/scripts/sfp_scan.py
"""
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from app.core.config import settings
from app.engine.indicators import enrich
from app.engine.ledger import FEES, SLIP_BPS, NOTIONAL
from app.models import db

TFS = ("1h", "15m", "1m")
LOOKBACK = 20               # 扫针参照窗（不含当根）
VOL_MULT = 1.5              # 放量过滤倍数
STOP_ATR = 0.4              # 止损缓冲 = 0.4×ATR
TARGET_R = 2.0              # 目标 = 2R
HORIZON_MIN = 7 * 24 * 60   # 时限 7 天（按周期折算根数）
WINDOW = 3                  # 台账信号 bar 前 3 根（另含当根）
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
SLIP = SLIP_BPS / 10_000
TAGS = {"1h": "主", "15m": "附注，数据窗短/样本少", "1m": "附注"}


def _settle(entry_eff: float, x_price: float) -> tuple[float, float]:
    """与 ledger 同源的单笔结算（买入滑点已含在 entry_eff，卖出再计滑点+双边费）。"""
    qty = NOTIONAL / entry_eff
    cost_in = NOTIONAL * (1 + FEES)
    proceeds = qty * x_price * (1 - SLIP) * (1 - FEES)
    pnl = proceeds - cost_in
    return pnl, pnl / cost_in


def _stats(pcts: list[float], pnls: list[float] | None = None) -> str:
    ar = np.array(pcts, dtype=float)
    if not len(ar):
        return "无样本"
    w, l = ar[ar > 0], ar[ar < 0]
    pf = f"{w.sum() / -l.sum():.2f}" if len(l) else "—"
    s = (f"n={len(ar):<4} 胜率={len(w) / len(ar) * 100:.1f}% PF={pf} "
         f"期望={ar.mean() * 100:+.4f}%/笔")
    if len(ar) < 30:
        s += "（样本不足）"
    if pnls is not None:
        s += f" 合计={sum(pnls):+.1f}U"
    return s


def _kinds(trades: list[dict]) -> str:
    n = len(trades)
    if not n:
        return "（无入场）"
    c = Counter(t["kind"] for t in trades)
    return (f"stop {c.get('stop', 0) / n * 100:.1f}% / "
            f"target {c.get('target', 0) / n * 100:.1f}% / "
            f"time {c.get('time', 0) / n * 100:.1f}%")


class _Bars:
    """按标的加载 bars：时间索引 + high/low/close/vol/atr 数组。"""

    def __init__(self, conn, symbol: str, tf: str):
        df = db.load_ohlcv(conn, symbol, "okx", "spot", tf)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = enrich(df)
        self.ts = df["ts"].astype("int64").to_numpy()
        self.high = df["high"].to_numpy(float)
        self.low = df["low"].to_numpy(float)
        self.close = df["close"].to_numpy(float)
        self.vol = df["volume"].to_numpy(float)
        self.atr = df["atr14"].to_numpy(float)

    def idx(self, ts) -> int:
        t = pd.Timestamp(ts).value
        i = int(np.searchsorted(self.ts, t))
        if i >= len(self.ts) or self.ts[i] != t:
            raise KeyError(f"ts 不在 K 线中: {ts}")
        return i

    def events(self) -> tuple[np.ndarray, np.ndarray]:
        """返回 (原始事件索引, 其中放量事件索引)。"""
        prev_min = pd.Series(self.low).rolling(LOOKBACK).min().shift(1).to_numpy()
        ev = np.nonzero((self.low < prev_min) & (self.close > prev_min))[0]
        vavg = pd.Series(self.vol).rolling(LOOKBACK).mean().shift(1).to_numpy()
        return ev, ev[self.vol[ev] >= VOL_MULT * vavg[ev]]


def _ts_at(b: _Bars, i: int):
    return pd.Timestamp(b.ts[i], tz="UTC").to_pydatetime()


def _replay(b: _Bars, ev: np.ndarray, tf: str) -> list[dict]:
    """事件流贪心回放：持仓期内新事件跳过；返回逐笔结果。"""
    hz = HORIZON_MIN // TF_MIN[tf]
    n = len(b.ts)
    out: list[dict] = []
    last_exit = -1
    for i in ev:
        if i <= last_exit:
            continue
        a = b.atr[i]
        if not (np.isfinite(a) and a > 0):
            continue
        e_close = b.close[i]
        entry_eff = e_close * (1 + SLIP)
        stop_px = b.low[i] - STOP_ATR * a
        tgt_px = entry_eff + TARGET_R * (entry_eff - stop_px)
        i_end = min(i + hz, n - 1)
        row = {"ts": _ts_at(b, i), "kind": "time"}
        if i_end > i:
            hs = np.nonzero(b.low[i + 1: i_end + 1] <= stop_px)[0]
            ht = np.nonzero(b.high[i + 1: i_end + 1] >= tgt_px)[0]
            i_s = i + 1 + int(hs[0]) if len(hs) else None
            i_t = i + 1 + int(ht[0]) if len(ht) else None
            if i_s is not None and (i_t is None or i_s <= i_t):
                j, fill, row["kind"] = i_s, stop_px, "stop"
            elif i_t is not None:
                j, fill, row["kind"] = i_t, tgt_px, "target"
            else:
                j, fill = i_end, b.close[i_end]
        else:
            j, fill = i, e_close
        last_exit = j
        pnl, pct = _settle(entry_eff, fill)
        row.update(exit_ts=_ts_at(b, j), pnl=pnl, pnl_pct=pct)
        out.append(row)
    return out


def _scope(conn, tf: str):
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT symbol FROM ohlcv WHERE exchange='okx'
                       AND contract_type='spot' AND timeframe=%s ORDER BY symbol""", (tf,))
        syms = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT min(ts), max(ts) FROM ohlcv WHERE exchange='okx'
                       AND contract_type='spot' AND timeframe=%s""", (tf,))
        t0, t1 = cur.fetchone()
    return syms, t0, t1


def _overlap(conn, tf: str, bars: dict, is_raw: dict, is_vol: dict) -> None:
    """台账 entry 信号与 SFP 事件的重合度（信号 bar 前 3 根及当根窗内），分组对比。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT p.symbol, p.entry_ts, p.pnl, p.pnl_pct
                       FROM paper_position p JOIN signal s ON s.id = p.signal_id
                       WHERE p.status='closed' AND p.exchange='okx'
                         AND s.timeframe=%s AND p.entry_ts IS NOT NULL""", (tf,))
        rows = cur.fetchall()
    if not rows:
        print("  台账重合：无已平台账单")
        return
    g = {k: [] for k in ("r_hit", "r_rest", "v_hit", "v_rest")}
    on_bar = missed = 0
    for sym, ts, pnl, pct in rows:
        b, m_r, m_v = bars.get(sym), is_raw.get(sym), is_vol.get(sym)
        if b is None or m_r is None:
            missed += 1
            continue
        try:
            i_e = b.idx(ts)
        except KeyError:
            missed += 1
            continue
        lo = max(0, i_e - WINDOW)
        (g["r_hit"] if m_r[lo: i_e + 1].any() else g["r_rest"]).append((pnl, pct))
        (g["v_hit"] if m_v[lo: i_e + 1].any() else g["v_rest"]).append((pnl, pct))
        if m_r[i_e]:
            on_bar += 1
    note = f"（{missed} 笔无 K 线跳过）" if missed else ""
    print(f"  台账重合（信号 bar 前 {WINDOW} 根及当根内有 SFP 事件）：{note}")
    for name, hit, rest, extra in (("原始", "r_hit", "r_rest", f"恰在当根 {on_bar}"),
                                   ("放量", "v_hit", "v_rest", "—")):
        h, r = g[hit], g[rest]
        print(f"    {name}：命中 {len(h)}/{len(rows) - missed}（{extra}）｜ "
              f"命中组 {_stats([p for _, p in h], [u for u, _ in h])}")
        print(f"          其余 {len(r)} 笔 ｜ {_stats([p for _, p in r], [u for u, _ in r])}")


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    for tf in TFS:
        print(f"=== [{tf}]（{TAGS[tf]}） ===")
        syms, t0, t1 = _scope(conn, tf)
        span = (t1 - t0).total_seconds() / 86400
        print(f"  数据：{len(syms)} 标的，{t0:%Y-%m-%d}→{t1:%Y-%m-%d}（{span:.0f} 天）")
        bars: dict[str, _Bars] = {}
        is_raw: dict[str, np.ndarray] = {}
        is_vol: dict[str, np.ndarray] = {}
        trades_raw: list[dict] = []
        trades_vol: list[dict] = []
        n_raw = n_vol = 0
        for sym in syms:
            b = _Bars(conn, sym, tf)
            bars[sym] = b
            ev, ev_vol = b.events()
            n_raw += len(ev)
            n_vol += len(ev_vol)
            is_raw[sym] = np.zeros(len(b.ts), bool)
            is_raw[sym][ev] = True
            is_vol[sym] = np.zeros(len(b.ts), bool)
            is_vol[sym][ev_vol] = True
            trades_raw += _replay(b, ev, tf)
            trades_vol += _replay(b, ev_vol, tf)
        print(f"  原始定义：事件 {n_raw} 个（{n_raw / span:.1f}/天）→ 入场 {len(trades_raw)} 笔")
        print(f"             {_kinds(trades_raw)} ｜ "
              f"{_stats([r['pnl_pct'] for r in trades_raw], [r['pnl'] for r in trades_raw])}")
        print(f"  放量过滤：事件 {n_vol} 个（{n_vol / span:.1f}/天）→ 入场 {len(trades_vol)} 笔")
        print(f"             {_kinds(trades_vol)} ｜ "
              f"{_stats([r['pnl_pct'] for r in trades_vol], [r['pnl'] for r in trades_vol])}")
        _overlap(conn, tf, bars, is_raw, is_vol)
        print()
    conn.close()


if __name__ == "__main__":
    main()
