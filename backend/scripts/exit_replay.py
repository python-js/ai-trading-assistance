"""R 化止损重放（只读，研究用）：与台账完全同源的入场/反向信号出场，叠加 k×ATR 止损对照。

口径（2026-10-13 定）：
- 入场：entry 信号 bar 收盘价 ×(1+滑点)（与 ledger._settle 一致，PIT）；名义 1000U、taker 单边 0.05%。
- 基线出场：反向(exit)信号 bar 收盘价结算（=现台账语义，无止损）。
- 变体：止损距离 = k × ATR%（ATR 取入场 bar 的 atr14/close），两种触发口径：
  touch = 任一 bar 最低价 <= 止损价 → 按止损价成交（计费+滑点）；
  close = 任一 bar 收盘价 <= 止损价 → 按该 bar 收盘价成交。
  同 bar 同时触及止损与反向信号收盘时按"止损先"（保守）。止损后同规则等下一次 entry（状态机同台账）。
  限知：跳空穿越止损按止损价成交（略乐观）；未建模盘中序列，仅有 OHLC。
- 自校验：无止损基线须与 paper_position 快照一致（交集逐笔核对；快照滞后于最新信号属正常，仅计数）。
- 周期：1h（主）+ 4h（附注）。okx spot only；A股/其余周期不涉。

用法：.venv/Scripts/python.exe backend/scripts/exit_replay.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from app.core.config import settings
from app.engine.indicators import enrich
from app.engine.ledger import FEES, SLIP_BPS, NOTIONAL
from app.models import db

TFS = ("1h", "4h")
KS = (1.0, 1.5, 2.0)
TRIGGERS = ("touch", "close")
SLIP = SLIP_BPS / 10_000


def _ns(ts) -> int:
    return pd.Timestamp(ts).value


def _settle(entry_eff: float, x_price: float) -> tuple[float, float]:
    qty = NOTIONAL / entry_eff
    cost_in = NOTIONAL * (1 + FEES)
    proceeds = qty * x_price * (1 - SLIP) * (1 - FEES)
    pnl = proceeds - cost_in
    return pnl, pnl / cost_in


def _stats(rows: list[dict]) -> str:
    pnls = np.array([r["pnl_pct"] for r in rows])
    if not len(pnls):
        return "n=0"
    w, l = pnls[pnls > 0], pnls[pnls < 0]
    pf = (w.sum() / -l.sum()) if len(l) else None
    pf_s = f"{pf:.2f}" if pf is not None else "—"
    total = sum(r["pnl"] for r in rows)
    return (f"已平 n={len(pnls):<4} 胜率={len(w)/len(pnls)*100:.1f}% PF={pf_s} "
            f"期望={pnls.mean()*100:+.4f}%/笔 合计={total:+.1f}U")


class _Bars:
    """按标的加载 bars：时间索引 + low/close/atr 数组。"""

    def __init__(self, conn, symbol: str, ct: str, tf: str):
        df = db.load_ohlcv(conn, symbol, "okx", ct, tf)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = enrich(df)
        self.ts = df["ts"].astype("int64").to_numpy()
        self.low = df["low"].to_numpy(float)
        self.close = df["close"].to_numpy(float)
        self.atr = df["atr14"].to_numpy(float)

    def idx(self, ts) -> int:
        t = _ns(ts)
        i = int(np.searchsorted(self.ts, t))
        if i >= len(self.ts) or self.ts[i] != t:
            raise KeyError(f"ts 不在 K 线中: {ts}")
        return i


def replay(conn, tf: str, k: float | None, trigger: str) -> list[dict]:
    """k=None 为基线；否则叠加 k×ATR 止损（trigger: touch/close）。返回逐笔结果。"""
    sigs = [s for s in db.load_all_signals(conn)
            if s["exchange"] == "okx" and s["timeframe"] == tf]
    bars_cache: dict[tuple, _Bars] = {}
    out: list[dict] = []
    pending: dict[tuple, dict] = {}
    for s in sigs:
        key = (s["symbol"], s["exchange"], s["contract_type"], tf, s["rule_id"])
        if s["direction"] == "entry":
            pending[key] = s
        elif key in pending:
            out.append(_one(conn, bars_cache, pending.pop(key), s, k, trigger))
    for key, entry in pending.items():
        out.append(_one(conn, bars_cache, entry, None, k, trigger))
    return out


def _stop_scan(b: _Bars, i_e: int, i_end: int, stop_px: float, trigger: str):
    """在 (i_e, i_end] 找止损触发；返回 (i_s, fill_px) 或 None。"""
    seg = (b.low if trigger == "touch" else b.close)[i_e + 1: i_end + 1]
    hits = np.nonzero(seg <= stop_px)[0]
    if not len(hits):
        return None
    i_s = i_e + 1 + int(hits[0])
    return i_s, (stop_px if trigger == "touch" else float(b.close[i_s]))


def _one(conn, cache: dict, entry: dict, exit_: dict | None, k: float | None,
         trigger: str) -> dict:
    key4 = (entry["symbol"], entry["contract_type"], entry["timeframe"])
    if key4 not in cache:
        cache[key4] = _Bars(conn, *key4)
    b = cache[key4]
    i_e = b.idx(entry["ts"])
    e_close = b.close[i_e]
    entry_eff = e_close * (1 + SLIP)
    row = {"signal_id": entry["id"], "symbol": entry["symbol"], "entry_ts": entry["ts"],
           "stop": False}
    stop_px = None
    if k is not None:
        a = b.atr[i_e]
        if not (np.isnan(a) or a <= 0 or np.isnan(e_close) or e_close <= 0):
            stop_px = entry_eff * (1 - k * (a / e_close))
    if exit_ is not None:
        i_x = b.idx(exit_["ts"])
        if stop_px is not None:
            hit = _stop_scan(b, i_e, i_x, stop_px, trigger)
            if hit:
                i_s, fill = hit
                row.update(stop=True, exit_ts=_ts_at(b, i_s), status="closed")
                pnl, pct = _settle(entry_eff, fill)
                row.update(pnl=pnl, pnl_pct=pct)
                return row
        row.update(status="closed", exit_ts=exit_["ts"])
        pnl, pct = _settle(entry_eff, b.close[i_x])
        row.update(pnl=pnl, pnl_pct=pct)
        return row
    if stop_px is not None:
        hit = _stop_scan(b, i_e, len(b.ts) - 1, stop_px, trigger)
        if hit:
            i_s, fill = hit
            row.update(stop=True, status="closed", exit_ts=_ts_at(b, i_s))
            pnl, pct = _settle(entry_eff, fill)
            row.update(pnl=pnl, pnl_pct=pct)
            return row
    row.update(status="open")
    return row


def _ts_at(b: _Bars, i: int):
    return pd.Timestamp(b.ts[i], tz="UTC").to_pydatetime()


def _self_check(conn, tf: str, base_rows: list[dict]) -> str:
    """快照比对：DB 有而重放无 → 错；均为已平 → 逐笔 pnl 核对；快照滞后（新增/新平）仅计数。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT p.signal_id, p.status, p.pnl
                       FROM paper_position p JOIN signal s ON s.id=p.signal_id
                       WHERE p.exchange='okx' AND s.timeframe=%s""", (tf,))
        db_rows = {r[0]: r for r in cur.fetchall()}
    mine = {r["signal_id"]: r for r in base_rows}
    lost = sorted(set(db_rows) - set(mine))
    if lost:
        return f"❌ 不一致：DB 有而重放缺失 {len(lost)} 笔（如 {lost[:5]}）"
    n_ok, n_lag_closed, worst, n_open_ok = 0, 0, 0.0, 0
    for sid, r in db_rows.items():
        m = mine[sid]
        if r[1] == "open":
            if m["status"] == "open":
                n_open_ok += 1
            else:
                n_lag_closed += 1
            continue
        if m["status"] != "closed":
            return f"❌ 不一致：signal_id={sid} 快照已平但重放到不了出场"
        n_ok += 1
        worst = max(worst, abs(float(r[2]) - m["pnl"]))
    new_only = len(set(mine) - set(db_rows))
    return (f"✅ 一致（快照 {len(db_rows)} 笔：已平 {n_ok} 逐笔最大差额 {worst:.2e}、"
            f"持仓 {n_open_ok}；快照后新增 {new_only} 笔、快照后新平 {n_lag_closed} 笔）")


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    for tf in TFS:
        base = replay(conn, tf, None, "touch")  # k=None 时 trigger 不影响结果
        check = _self_check(conn, tf, base)
        tag = "（主）" if tf == "1h" else "（附注，样本小）"
        print(f"=== {tf} {tag} ===")
        print(f"  基线自校验：{check}")
        closed = [r for r in base if r["status"] == "closed"]
        opens = len(base) - len(closed)
        print(f"  基线 无止损    ：{_stats(closed)}（持仓中 {opens}）")
        base_closed = {r["signal_id"]: r for r in closed}
        for trigger in TRIGGERS:
            tname = "touch：bar 最低价触发" if trigger == "touch" else "close：bar 收盘确认"
            print(f"  [{tname}]")
            for k in KS:
                rows = replay(conn, tf, k, trigger)
                cl = [r for r in rows if r["status"] == "closed"]
                op = len(rows) - len(cl)
                stopped = [r for r in cl if r["stop"]]
                stop_ids = {r["signal_id"] for r in stopped}
                diffs = [r for r in cl if r["signal_id"] not in stop_ids
                         and r["signal_id"] in base_closed]
                drift = max((abs(r["pnl"] - base_closed[r["signal_id"]]["pnl"]) for r in diffs),
                            default=0.0)
                print(f"  k={k:.2f}×ATR    ：{_stats(cl)}（持仓中 {op}）")
                share = len(stopped) / (len(cl) + op) * 100 if (len(cl) + op) else 0
                print(f"                    止损 {len(stopped)} 笔（占入场 {share:.1f}%）；"
                      f"非止损笔与基线最大差额 {drift:.2e}")
                if stopped:
                    b_ids = [r["signal_id"] for r in stopped if r["signal_id"] in base_closed]
                    if b_ids:
                        old = np.mean([base_closed[i]["pnl_pct"] for i in b_ids]) * 100
                        new = np.mean([r["pnl_pct"] for r in stopped
                                       if r["signal_id"] in base_closed]) * 100
                        print(f"                    被止损批次：基线均值 {old:+.3f}% → "
                              f"止损后 {new:+.3f}% （差 {new - old:+.3f} pp，正=变好）")
                    n_open_cut = len(stopped) - len(b_ids)
                    if n_open_cut:
                        print(f"                    （其中 {n_open_cut} 笔基线为持仓中，止损后已平）")
        print()
    conn.close()


if __name__ == "__main__":
    main()
