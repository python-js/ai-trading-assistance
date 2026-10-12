"""P1-3 签发回测验证：
1) 台账交叉核对（核心）：事件驱动回放交易 == paper_position 实盘台账（同语义铁证，
   取 end=now-2min 避开正在形成/尚未复审的最后一根 bar；只比对 exit_ts<=end 的已平仓）；
2) 确定性：同规则跑两次逐笔一致；
3) 双层对照：1m 规则（无 chandelier）vbt 入场 > 事件入场（cooldown 生效方向性验证）；
   1h chandelier 规则研究层明确不可对照（vbt_error）；
4) 边界：恒假规则 0 事件 0 交易不崩、净值恒等于初始资金。
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

import pandas as pd

from app.core.config import settings
from app.engine import backtest_event, ledger
from app.models import db

FAIL: list[str] = []

_LEDGER_TRADES = """
SELECT p.entry_ts, p.exit_ts, p.entry_price, p.exit_price, p.pnl, p.pnl_pct, p.qty,
       p.max_adverse_excursion
FROM paper_position p
JOIN signal s ON s.id = p.signal_id
JOIN rule r   ON r.id = s.rule_id
WHERE r.name = %s AND p.status = 'closed' AND p.exit_ts <= %s
ORDER BY p.entry_ts
"""


def _eq(name: str, got, exp, tol: float = 1e-9) -> None:
    if got is None or exp is None:
        if got != exp:
            FAIL.append(f"{name}: got={got!r} exp={exp!r}")
        return
    if abs(float(got) - float(exp)) > max(tol, tol * abs(float(exp))):
        FAIL.append(f"{name}: got={got!r} exp={exp!r}")


def load_rule(conn, name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("""SELECT name, symbol, exchange, contract_type, timeframe, dsl_json
                       FROM rule WHERE name=%s ORDER BY version DESC LIMIT 1""", (name,))
        row = cur.fetchone()
    cols = ["name", "symbol", "exchange", "contract_type", "timeframe", "dsl_json"]
    return dict(zip(cols, row))


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    end = datetime.now(timezone.utc) - timedelta(seconds=120)  # 稳定边界

    # 1) 台账交叉核对
    rule = load_rule(conn, "ema_2060_1m_BTC/USDT")
    ev = backtest_event.run_rule(conn, rule, end=end)
    ledger.rebuild(conn)
    with conn.cursor() as cur:
        cur.execute(_LEDGER_TRADES, (rule["name"], end))
        rows = cur.fetchall()
    ev_closed = [t for t in ev["trades"] if t["exit_ts"] <= end]
    if len(rows) != len(ev_closed):
        FAIL.append(f"交叉核对笔数: ledger={len(rows)} ev={len(ev_closed)}"
                    f"（ev 全量 {len(ev['trades'])}，尾部差异={len(ev['trades']) - len(ev_closed)}）")
    for i, (r, t) in enumerate(zip(rows, ev_closed)):
        ets, xts, epr, xpr, pnl, pct, qty, mae = r
        if not (pd.Timestamp(t["entry_ts"]) == pd.Timestamp(ets)
                and pd.Timestamp(t["exit_ts"]) == pd.Timestamp(xts)):
            FAIL.append(f"[{i}].ts: ev=({t['entry_ts']},{t['exit_ts']}) "
                        f"ledger=({ets},{xts})")
        for k, v in (("entry_price", epr), ("exit_price", xpr), ("pnl", pnl),
                     ("pnl_pct", pct), ("qty", qty), ("mae", mae)):
            _eq(f"[{i}].{k}", t[k], float(v))
    print(f"1) 台账交叉核对：{len(ev_closed)} 笔已平仓逐字段一致"
          f"（引擎全量 {len(ev['trades'])} 笔，end={end:%m-%d %H:%M}）"
          f" {'PASS' if not FAIL else 'FAIL'}")

    # 2) 确定性
    ev2 = backtest_event.run_rule(conn, rule, end=end)
    same = (len(ev2["trades"]) == len(ev["trades"])
            and all(a == b for a, b in zip(ev2["trades"], ev["trades"]))
            and ev2["equity"][-1] == ev["equity"][-1])
    if not same:
        FAIL.append("确定性: 两次回放不一致")
    print(f"2) 确定性：两次运行逐笔+净值一致 {'PASS' if same else 'FAIL'}")

    # 3) 双层对照
    cmp1 = backtest_event.compare(conn, rule)
    if not (cmp1["vbt"] and cmp1["vbt_entries"] is not None
            and cmp1["vbt_entries"] > ev["n_entries"] and cmp1["vbt_error"] is None):
        FAIL.append(f"对照1m: vbt={bool(cmp1['vbt'])} entries={cmp1['vbt_entries']}"
                    f" ≤ event_entries={ev['n_entries']} err={cmp1['vbt_error']}")
    v = cmp1["vbt"] or {}
    print(f"3a) 对照1m：事件入场 {ev['n_entries']} < vbt 入场 {cmp1['vbt_entries']}"
          f"（cooldown 30 生效）；事件 {ev['metrics']['n_trades']} 笔"
          f" vs vbt {v.get('交易笔数')} 笔 {'PASS' if not FAIL else 'FAIL'}")

    rule_1h = load_rule(conn, "ema_2060_rsi_1h_BTC/USDT")
    cmp2 = backtest_event.compare(conn, rule_1h)
    if not (cmp2["vbt"] is None and cmp2["vbt_error"] and "chandelier" in cmp2["vbt_error"]):
        FAIL.append(f"对照1h: vbt={bool(cmp2['vbt'])} err={cmp2['vbt_error']}")
    print(f"3b) 对照1h(chandelier)：研究层不可对照 → {cmp2['vbt_error']}"
          f" {'PASS' if not FAIL else 'FAIL'}")

    # 4) 边界：恒假规则
    dead = {"name": "impossible", "symbol": "BTC/USDT", "exchange": "okx",
            "contract_type": "spot", "timeframe": "1h",
            "dsl_json": {"name": "impossible", "symbol": "BTC/USDT", "timeframe": "1h",
                         "entry": {"ema20": {"<": 0}}, "exit": {"ema20": {"<": 0}},
                         "cooldown_bars": 6}}
    ev0 = backtest_event.run_rule(conn, dead, end=end)
    m0 = ev0["metrics"]
    flat = {v for _, v in ev0["equity"]} == {backtest_event.INIT_CASH}
    if not (ev0["n_events"] == 0 and m0["n_trades"] == 0 and m0["max_drawdown[%]"] == 0.0
            and flat and ev0["open_pos"] is None):
        FAIL.append(f"边界: n_events={ev0['n_events']} n_trades={m0['n_trades']} "
                    f"dd={m0['max_drawdown[%]']} flat={flat}")
    print(f"4) 边界：恒假规则 0 事件 0 交易，净值恒 {backtest_event.INIT_CASH:.0f}"
          f" {'PASS' if not FAIL else 'FAIL'}")

    conn.close()
    if FAIL:
        print("\n".join(f"FAIL {f}" for f in FAIL))
        sys.exit(1)
    print("P1-3 签发回测验证全部 PASS")
    print(backtest_event.render_compare(rule["name"], cmp1))


if __name__ == "__main__":
    main()
