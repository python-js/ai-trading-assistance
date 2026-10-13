"""P1-1 台账验证：①回填/重建幂等 ②精确复算（对原始 K 线） ③与 vectorbt 对照（执行位移）。
用法：.venv/Scripts/python.exe backend/scripts/verify_ledger.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd

from app.core.config import settings
from app.engine import backtest_vectorbt as bt
from app.engine import ledger
from app.engine.indicators import enrich
from app.engine.rules_dsl import parse_rule
from app.models import db
from scripts.backfill_signals import backfill

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


_RULE_13 = "sma_520_1m_BTC/USDT"


def main() -> None:
    conn = db.connect(settings.pg_dsn)

    # ---------- ① 幂等 ----------
    print("① 幂等性（重复执行）")
    n2 = backfill(conn)
    check("回填二次执行 0 新增", n2 == 0, f"new={n2}")
    st1, st2 = ledger.rebuild(conn), ledger.rebuild(conn)
    check("重建两次计数一致", st1 == st2, str(st1))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(*) FILTER (WHERE status='closed') FROM paper_position")
        total, closed = cur.fetchone()
    check("表行数与回算一致", (total, closed) == (st1["total"], st1["closed"]),
          f"表 {total}/{closed}")

    # ---------- ② 精确复算 ----------
    print("② 精确复算（公式重算 + 原始 K 线核对）")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.signal_id, p.entry_ts, p.entry_price, p.qty, p.status, p.exit_ts,
                   p.exit_price, p.pnl, p.pnl_pct, s.rule_id, s.symbol, s.exchange,
                   s.contract_type, s.timeframe
            FROM paper_position p JOIN signal s ON s.id = p.signal_id
            ORDER BY s.rule_id, p.entry_ts""")
        rows = cur.fetchall()
    bad = 0
    for (sid, ets, epr, qty, st, xts, xpr, pnl, pnl_pct, rid, sym, ex, ct, tf) in rows:
        slip = ledger.SLIP_BPS / 10_000
        if st == "closed":
            if ex == "akshare":  # A股口径：佣金万2.5(最低5元/笔)+印花税千0.5卖出+滑点5bps
                qty2 = ledger.NOTIONAL_A / (epr * (1 + slip))
                cost_in = ledger.NOTIONAL_A + max(ledger.NOTIONAL_A * ledger.COMM_A,
                                                  ledger.MIN_COMM_A)
                gross = qty2 * xpr * (1 - slip)
                proceeds = gross - max(gross * ledger.COMM_A, ledger.MIN_COMM_A) \
                    - gross * ledger.STAMP_A
                pnl2, base = proceeds - cost_in, cost_in
            else:
                qty2 = ledger.NOTIONAL / (epr * (1 + slip))
                pnl2 = qty2 * xpr * (1 - slip) * (1 - ledger.FEES) - ledger.NOTIONAL * (1 + ledger.FEES)
                base = ledger.NOTIONAL * (1 + ledger.FEES)
            ok = (abs(qty - qty2) < 1e-9 and abs(pnl - pnl2) < 1e-9
                  and abs(pnl_pct - pnl2 / base) < 1e-12
                  and xts is not None)
        else:
            ok = (pnl is None and pnl_pct is None and xts is None and xpr is None)
        bad += not ok
    check("公式重算逐笔一致", bad == 0, f"{len(rows)} 笔，不一致 {bad}")

    # 原始 K 线核对：entry_price 必须等于该 bar 的 close（同一行按规则缓存）
    px_cache: dict[tuple, pd.DataFrame] = {}
    bad_bar = 0
    for (sid, ets, epr, qty, st, xts, xpr, pnl, pnl_pct, rid, sym, ex, ct, tf) in rows:
        key = (sym, ex, ct, tf)
        if key not in px_cache:
            df = db.load_ohlcv(conn, sym, ex, ct, tf)
            px_cache[key] = df.set_index("ts")["close"]
        closes = px_cache[key]
        if float(closes.loc[ets]) != epr:
            bad_bar += 1
        if st == "closed" and float(closes.loc[xts]) != xpr:
            bad_bar += 1
    check("成交价=信号 bar 收盘价（全量逐笔）", bad_bar == 0, f"不等 {bad_bar} 处")

    # 配对不重叠（同规则内 上一笔出场 <= 下一笔入场）
    overlap = 0
    prev: dict[int, pd.Timestamp] = {}
    for (sid, ets, epr, qty, st, xts, xpr, pnl, pnl_pct, rid, *_rest) in rows:
        if rid in prev and st == "closed" and ets < prev[rid]:
            overlap += 1
        if st == "closed":
            prev[rid] = xts
    check("同规则配对无重叠", overlap == 0, f"重叠 {overlap}")

    # ---------- ③ vectorbt 对照 ----------
    print("③ vectorbt 对照（同数据同成本，差异应为执行位移/冷却语义）")
    with conn.cursor() as cur:
        cur.execute("SELECT id, dsl_json FROM rule WHERE name=%s", (_RULE_13,))
        rid13, dsl = cur.fetchone()
        cur.execute("""SELECT entry_ts FROM paper_position p JOIN signal s ON s.id = p.signal_id
                       WHERE s.rule_id=%s ORDER BY entry_ts""", (rid13,))
        led_e = [r[0] for r in cur.fetchall()]
    df = db.load_ohlcv(conn, "BTC/USDT", "okx", "spot", "1m")
    df = enrich(df)
    rule = parse_rule(dsl)
    e, x = bt.signals_from_rule(rule, df)
    close, e, x = bt._with_ts(df, df["close"], e, x)
    pf = bt.run_portfolio(close, e.astype(bool), x.astype(bool),
                          fees=ledger.FEES, slippage_bps=ledger.SLIP_BPS, freq="1m")
    tr = pf.trades.records_readable
    vbt_e = set(pd.to_datetime(tr["Entry Timestamp"]).dt.tz_convert("UTC"))
    print(f"     规则 {_RULE_13}: 台账入场 {len(led_e)} / vectorbt 入场 {len(vbt_e)}")
    unmatched = [l for l in led_e if (l + pd.Timedelta(minutes=1)) not in vbt_e]
    check("台账入场均在 vectorbt 中有次 bar 对应", not unmatched, f"缺 {len(unmatched)}")
    led_set = set(led_e)
    missing = [v for v in vbt_e if (v - pd.Timedelta(minutes=1)) not in led_set]
    with conn.cursor() as cur:
        cur.execute("""SELECT exit_ts FROM paper_position p JOIN signal s ON s.id = p.signal_id
                       WHERE s.rule_id=%s AND exit_ts IS NOT NULL ORDER BY exit_ts""", (rid13,))
        exits = [r[0] for r in cur.fetchall()]
    explained = 0
    for v in missing:
        t = v - pd.Timedelta(minutes=1)
        prev_exit = max((x for x in exits if x < t), default=None)
        if prev_exit is not None and (t - prev_exit) <= pd.Timedelta(minutes=30):
            explained += 1
    check("vectorbt 多出的入场均由 cooldown(30) 解释", explained == len(missing),
          f"多出 {len(missing)}，可解释 {explained}")
    led_pcts = [float(r[8]) for r in rows if r[9] == rid13 and r[4] == "closed"]
    vbt_pnl = tr["PnL"].dropna()
    print("     台账已平 %d 笔，胜率 %.1f%% | vectorbt 已平 %d 笔，胜率 %.1f%%（信息展示）"
          % (len(led_pcts), 100 * sum(p > 0 for p in led_pcts) / max(len(led_pcts), 1),
             len(vbt_pnl), 100 * float((vbt_pnl > 0).mean()) if len(vbt_pnl) else 0.0))

    conn.close()
    print()
    if FAIL:
        print(f"存在 {len(FAIL)} 项失败: {FAIL}")
        raise SystemExit(1)
    print("全部 PASS")


if __name__ == "__main__":
    main()
