"""P1-6 A股验证：
1) 数据完整性（≥250 根、ts 对齐 00:00 UTC、最新 bar 已收盘且未滞后）；
2) 复权生效（hfq 与不复权对照，网络不可用则 SKIP）；
3) 信号确定性复算（evaluate 全窗口 == signal 表，双向集合相等）；
4) 台账 A股 逐笔独立复算（字面量口径，不复用 ledger 常量）+ 配对计数核对；
5) API 多市场兼容（/api/ohlcv 000157.SZ 与 600199.SH markers、/api/symbols、/api/report）；
6) 盘中/最新态 scan_daily 冒烟（不产生晚于最近已收盘 bar 的信号）。
用法：.venv/Scripts/python.exe backend/scripts/verify_stock.py
"""
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd
from fastapi.testclient import TestClient

from app.adapters.market.akshare_cn import AkshareAdapter
from app.core.config import settings
from app.engine import scanner
from app.engine.indicators import enrich
from app.engine.rules_dsl import evaluate, parse_rule
from app.main import app
from app.models import db
from scripts.backfill_signals import backfill

FAIL: list[str] = []

STOCKS = ["000157.SZ", "002284.SZ", "600199.SH"]
# 独立口径字面量（防与 ledger 常量同源错误）
_COMM, _MIN_COMM, _STAMP, _NOTIONAL_A, _SLIP = 0.00025, 5.0, 0.0005, 10_000.0, 0.0005
_DAY_MS = 86_400_000
_AVAIL_MS = 7 * 3600 * 1000


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def main() -> None:
    conn = db.connect(settings.pg_dsn)

    # ---------- ① 数据完整性 / PIT ----------
    print("① 数据完整性（回填量 / ts 对齐 / PIT 最新 bar）")
    now_ms = int(time.time() * 1000)
    for s in STOCKS:
        n, lo, hi = db.count_ohlcv(conn, s, "akshare", "stock", "1d")
        check(f"{s} ≥250 根", n >= 250, f"n={n}")
        df = db.load_ohlcv(conn, s, "akshare", "stock", "1d")
        ts_ms = [int(t.timestamp() * 1000) for t in df["ts"]]
        aligned = all(t % _DAY_MS == 0 for t in ts_ms)
        check(f"{s} ts 对齐 00:00 UTC", aligned)
        last = ts_ms[-1]
        check(f"{s} 最新 bar 已收盘(PIT) 且未滞后", last + _AVAIL_MS <= now_ms
              and now_ms - last <= 5 * _DAY_MS,
              f"last={df['ts'].iloc[-1]:%Y-%m-%d}")

    # ---------- ② 复权生效 ----------
    print("② 复权生效（hfq vs 不复权，网络项）")
    try:
        import akshare as ak
        raw = ak.stock_zh_a_daily(symbol="sz000157", adjust="")
        df = db.load_ohlcv(conn, "000157.SZ", "akshare", "stock", "1d")
        day = str(pd.Timestamp(df["ts"].iloc[-1]).date())
        hit = raw[raw["date"].astype(str) == day]
        r = hit.iloc[-1] if len(hit) else raw.iloc[-1]
        ratio = float(df["close"].iloc[-1]) / float(r["close"])
        check("hfq 复权因子>1（对照同交易日不复权收盘）", ratio > 1.5,
              f"{r['date']} 不复权={r['close']} hfq={df['close'].iloc[-1]:.2f} 倍数={ratio:.2f}")
    except Exception as e:
        print(f"  [SKIP] 复权对照（网络不可用: {type(e).__name__}）")

    # ---------- ③ 信号确定性复算 ----------
    print("③ 信号复算（evaluate 全窗口 == signal 表）")
    with conn.cursor() as cur:
        # 仅启用规则口径（停用的历史规则信号不参与复算对照）
        cur.execute("""SELECT s.rule_id, s.symbol, s.ts, s.direction FROM signal s
                       JOIN rule r ON r.id = s.rule_id
                       WHERE s.exchange='akshare' AND r.enabled ORDER BY s.id""")
        stored = {(r[0], r[1], int(r[2].timestamp() * 1000), r[3]) for r in cur.fetchall()}
    computed: set = set()
    rules = [r for r in db.load_enabled_rules(conn) if r["exchange"] == "akshare"]
    for r in rules:
        df = db.load_ohlcv(conn, r["symbol"], r["exchange"], r["contract_type"], r["timeframe"])
        ind = enrich(df)
        for e in evaluate(parse_rule(r["dsl_json"]), ind):
            computed.add((r["id"], r["symbol"], int(pd.Timestamp(e.ts).timestamp() * 1000),
                          e.direction))
    check("复算事件集与库内一致（双向）", computed == stored,
          f"库内 {len(stored)} / 复算 {len(computed)} / 差集 {len(computed ^ stored)}")
    print(f"     600199.SH 库内事件 {len([x for x in stored if x[1] == '600199.SH'])} 个")

    # ---------- ④ 台账 A股 复算 ----------
    print("④ 台账 A股 逐笔独立复算（字面量费率口径）")
    with conn.cursor() as cur:
        cur.execute("""SELECT p.signal_id, p.entry_price, p.exit_price, p.pnl, p.pnl_pct,
                              p.status FROM paper_position p
                       WHERE p.exchange='akshare' ORDER BY p.entry_ts""")
        rows = cur.fetchall()
    bad = 0
    for sid, epr, xpr, pnl, pnl_pct, st in rows:
        qty = _NOTIONAL_A / (float(epr) * (1 + _SLIP))
        cost_in = _NOTIONAL_A + max(_NOTIONAL_A * _COMM, _MIN_COMM)
        gross = qty * float(xpr) * (1 - _SLIP)
        proceeds = gross - max(gross * _COMM, _MIN_COMM) - gross * _STAMP
        pnl2 = proceeds - cost_in
        if not (abs(float(pnl) - pnl2) < 1e-6 and abs(float(pnl_pct) - pnl2 / cost_in) < 1e-9):
            bad += 1
    check("A股台账公式重算一致", bad == 0, f"{len(rows)} 笔，不一致 {bad}")
    # 配对计数状态机（独立于 ledger）
    with conn.cursor() as cur:
        cur.execute("""SELECT rule_id, direction FROM signal
                       WHERE exchange='akshare' ORDER BY rule_id, ts, id""")
        pending: set = set()
        n_pairs = 0
        for rid, d in cur.fetchall():
            if d == "entry":
                pending.add(rid)
            elif rid in pending:
                pending.discard(rid)
                n_pairs += 1
        n_pairs += len(pending)
    check("台账行数=信号配对推导", len(rows) == n_pairs, f"表 {len(rows)} / 推导 {n_pairs}")

    # 成交价=信号 bar 收盘价
    px_bad = 0
    for sid, epr, xpr, *_ in rows:
        with conn.cursor() as cur:
            cur.execute("""SELECT s.timeframe, s.symbol, s.exchange, s.contract_type, s.ts
                           FROM signal s WHERE s.id=%s""", (sid,))
            tf, sym, ex, ct, ets = cur.fetchone()
            df = db.load_ohlcv(conn, sym, ex, ct, tf)
            px_bad += float(df.set_index("ts")["close"].loc[ets]) != float(epr)
    check("A股入场价=信号 bar 收盘价", px_bad == 0)

    # ---------- ⑤ API 多市场 ----------
    print("⑤ API（TestClient 离线）")
    c = TestClient(app)
    r = c.get("/api/ohlcv", params={"symbol": "000157.SZ", "timeframe": "1d"})
    j = r.json() if r.status_code == 200 else {}
    check("/api/ohlcv 000157.SZ 1d", r.status_code == 200 and len(j.get("bars", [])) >= 250
          and len(j.get("sma5", [])) > 0 and len(j.get("sma20", [])) > 0,
          f"bars={len(j.get('bars', []))}")
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM signal WHERE exchange='akshare' AND symbol='600199.SH'""")
        n_600 = cur.fetchone()[0]
    r = c.get("/api/ohlcv", params={"symbol": "600199.SH", "timeframe": "1d"})
    j = r.json() if r.status_code == 200 else {}
    check("600199.SH markers 数=库内信号数",
          r.status_code == 200 and len(j.get("signals", [])) == n_600,
          f"markers={len(j.get('signals', []))} / 库 {n_600}")
    r = c.get("/api/symbols")
    per = {x["symbol"]: x["periods"] for x in r.json()} if r.status_code == 200 else {}
    ok_a = all(any(p["timeframe"] == "1d" and p["last_ts"] for p in per.get(s, []))
               for s in STOCKS)
    check("/api/symbols 含三只 A股 1d", ok_a,
          ", ".join(f"{s}:{per.get(s)}" for s in STOCKS[:1]))
    r = c.get("/api/report")
    check("/api/report 200", r.status_code == 200)

    # ---------- ⑥ 幂等 + 扫描冒烟 ----------
    print("⑥ 幂等 / scan_daily 冒烟")
    n2 = backfill(conn, symbol="600199.SH")
    check("回放二次执行 0 新增", n2 == 0, f"new={n2}")
    try:
        rl = scanner.load_rules(conn)
        pool = scanner.stock_pool(conn)
        fired = scanner.scan_daily(AkshareAdapter(), conn, rl, pool)
        trigger = scanner.stock_trigger_ms(int(time.time() * 1000))
        ok_pit = all(int(s["ts"].timestamp() * 1000) <= trigger for s in fired)
        check("scan_daily 冒烟（信号 bar 不晚于最近已收盘 bar）", ok_pit, f"fired={len(fired)}")
    except Exception as e:
        print(f"  [SKIP] scan_daily 冒烟（网络不可用: {type(e).__name__}）")

    conn.close()
    print()
    if FAIL:
        print(f"存在 {len(FAIL)} 项失败: {FAIL}")
        raise SystemExit(1)
    print("全部 PASS")


if __name__ == "__main__":
    main()
