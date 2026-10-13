"""P2-1 资金面算子验证：
1) 采集冒烟：ensure_flow_history + collect_flow 真实执行（网络不可用则 SKIP）；全池 perp 非空 + 新鲜度；
2) 数据语义：funding 整点结算（min/sec=0、hour%4=0）、结算间隔整小时（周期可被交易所调整）、OI 严格递增；
3) asof 对齐独立复算：attach 结果 vs bisect 独立实现逐根一致（funding_rate / oi_change_24h）；
4) PIT 无未来函数：序列截断后的对齐 == 全量对齐的公共前缀（ts ≤ 截断点）；
5) DSL 方向性：恒假资金面条件 → 0 事件；恒真条件在数据可用区不改变事件集（双向一致）。
用法：.venv/Scripts/python.exe backend/scripts/verify_flow.py
"""
import bisect
import pathlib
import sys
import time
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd

from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.engine import flow, scanner
from app.engine.indicators import enrich
from app.engine.rules_dsl import evaluate, parse_rule
from app.models import db

FAIL: list[str] = []
SYM, EX = "BTC/USDT", "okx"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _asof(times: list, values: list, t):
    """独立 asof（bisect；不依赖 flow.attach 实现）。"""
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    pool = scanner.active_pool(conn, list(settings.symbols))

    # ---------- ① 采集冒烟（真实网络） ----------
    print("① 资金面采集（ensure + collect）")
    adapter = None
    try:
        adapter = OKXAdapter(settings.okx_proxy)
        t0 = time.time()
        scanner.ensure_flow_history(adapter, conn, pool)
        scanner.collect_flow(adapter, conn, pool)
        print(f"    采集耗时 {time.time() - t0:.1f}s")
        net_ok = True
    except Exception as e:
        net_ok = False
        print(f"    [SKIP] 网络不可用: {type(e).__name__}: {e}")

    # ---------- ② 数据语义 ----------
    print("② 数据语义（结算整点 / 周期恒定 / OI 递增）")
    if net_ok:
        perp_pool = [s for s in pool if adapter.to_ccxt_symbol(s, "perp") in adapter.ex.markets]
        now_ms = adapter.server_time()
    else:
        perp_pool = [s for s in pool if len(flow.load_funding(conn, s, EX))]
        now_ms = None
    check("perp 池非空", len(perp_pool) > 0, f"{perp_pool}")
    for s in perp_pool:
        f = flow.load_funding(conn, s, EX)
        o = flow.load_oi(conn, s, EX)
        check(f"{s} funding 非空", len(f) > 0, f"n={len(f)}")
        if len(f):
            align = bool((f["ts"].dt.minute == 0).all() and (f["ts"].dt.second == 0).all()
                         and (f["ts"].dt.hour % 4 == 0).all())
            check(f"{s} funding 整点结算", align)
            # 结算周期可被交易所调整（实测 ONDO 存在 4h/8h 混合段），只断言间隔为整小时
            gaps = f["ts"].diff().dropna().dt.total_seconds()
            check(f"{s} funding 结算间隔整小时",
                  bool((gaps % 3600 == 0).all() and (gaps >= 3600).all()),
                  f"范围={gaps.min():.0f}~{gaps.max():.0f}s")
        check(f"{s} OI 非空且严格递增", len(o) > 0 and bool(o["ts"].is_monotonic_increasing),
              f"n={len(o)}")
        if net_ok and len(f) and len(o):
            age_f = (now_ms - int(f["ts"].iloc[-1].timestamp() * 1000)) / 3600_000
            age_o = (now_ms - int(o["ts"].iloc[-1].timestamp() * 1000)) / 60_000
            check(f"{s} funding 新鲜(<12h)", age_f < 12, f"age={age_f:.1f}h")
            check(f"{s} OI 新鲜(<30min)", age_o < 30, f"age={age_o:.0f}min")

    # ---------- ③ asof 对齐独立复算 ----------
    print("③ asof 对齐独立复算（bisect 对照，BTC 1h 600 根）")
    f = flow.load_funding(conn, SYM, EX)
    o = flow.load_oi(conn, SYM, EX)
    df = db.load_ohlcv(conn, SYM, EX, "perp", "1h", limit=600)
    att = flow.attach(enrich(df), f, o)
    check("attach 注入两列", "funding_rate" in att.columns and "oi_change_24h" in att.columns)
    fts, frates = list(f["ts"]), list(f["rate"])
    ots, ovals = list(o["ts"]), list(o["oi"])
    bad_f = bad_o = 0
    for ts, fr, oc in zip(att["ts"], att["funding_rate"], att["oi_change_24h"]):
        t = pd.Timestamp(ts)
        ef = _asof(fts, frates, t)
        if (ef is None) != pd.isna(fr) or (ef is not None and pd.notna(fr)
                                           and abs(float(fr) - ef) > 1e-12):
            bad_f += 1
        v, v2 = _asof(ots, ovals, t), _asof(ots, ovals, t - timedelta(hours=24))
        eo = (v / v2 - 1.0) if (v is not None and v2 is not None) else None
        if (eo is None) != pd.isna(oc) or (eo is not None and pd.notna(oc)
                                           and abs(float(oc) - eo) > 1e-12):
            bad_o += 1
    check("funding_rate 逐根一致", bad_f == 0, f"mismatch={bad_f}/{len(att)}")
    check("oi_change_24h 逐根一致", bad_o == 0, f"mismatch={bad_o}/{len(att)}")

    # ---------- ④ PIT 无未来函数 ----------
    print("④ PIT 无未来函数（序列截断对照）")
    mid = f["ts"].iloc[len(f) // 2]
    att_tr = flow.attach(enrich(df), f[f["ts"] <= mid].reset_index(drop=True),
                         o[o["ts"] <= mid].reset_index(drop=True))
    m = att["ts"] <= mid
    same_f = bool((att.loc[m, "funding_rate"].fillna(-1).to_numpy()
                   == att_tr.loc[m, "funding_rate"].fillna(-1).to_numpy()).all())
    same_o = bool((att.loc[m, "oi_change_24h"].fillna(-1).to_numpy()
                   == att_tr.loc[m, "oi_change_24h"].fillna(-1).to_numpy()).all())
    check("截断序列公共前缀一致（funding）", same_f, f"n={int(m.sum())}")
    check("截断序列公共前缀一致（OI）", same_o, f"n={int(m.sum())}")

    # ---------- ⑤ DSL 方向性 ----------
    print("⑤ DSL 方向性（恒真/恒假资金面条件）")
    common = {"symbol": SYM, "timeframe": "1h", "exit": {"cross_down": ["sma5", "sma20"]},
              "cooldown_bars": 6}
    up = {"cross_up": ["sma5", "sma20"]}
    ev_base = evaluate(parse_rule({**common, "name": "t_base", "entry": up}), att)
    ev_false = evaluate(parse_rule({**common, "name": "t_false",
                                    "entry": {"all": [up, {"funding_rate": {"<": -1.0}}]}}), att)
    ev_true = evaluate(parse_rule({**common, "name": "t_true",
                                   "entry": {"all": [up, {"funding_rate": {"<": 1.0}}]}}), att)
    ev_oi = evaluate(parse_rule({**common, "name": "t_oi",
                                 "entry": {"all": [up, {"oi_change_24h": {">": -9.0}}]}}), att)
    check("恒假资金面条件 → 0 事件", len(ev_false) == 0, f"n={len(ev_false)}")
    for col, ev in (("funding_rate", ev_true), ("oi_change_24h", ev_oi)):
        fv = att.loc[att[col].notna(), "ts"].iloc[0]
        b_avail = [e.ts for e in ev_base if e.ts >= fv]
        t_avail = [e.ts for e in ev if e.ts >= fv]
        check(f"恒真 {col} 可用区事件双向一致", b_avail == t_avail,
              f"base={len(b_avail)} cond={len(t_avail)}")

    # ---------- ⑥ 签发回测注入冒烟 ----------
    print("⑥ 签发回测注入冒烟（含资金面条件规则）")
    from app.engine import backtest_event
    rule = {"name": "t_bt", "symbol": SYM, "exchange": EX, "contract_type": "perp",
            "timeframe": "1h",
            "dsl_json": {**common, "name": "t_bt",
                         "entry": {"all": [up, {"funding_rate": {"<": 1.0}}]}}}
    res = backtest_event.run_rule(conn, rule)
    check("run_rule 含资金面列不报错", isinstance(res["n_events"], int),
          f"events={res['n_events']} trades={res['metrics']['n_trades']}")

    conn.close()
    print(f"\n{'全部 PASS' if not FAIL else 'FAIL: ' + '; '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
