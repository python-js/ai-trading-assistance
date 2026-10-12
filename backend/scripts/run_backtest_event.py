"""P1-3 签发回测 CLI：按规则名跑事件驱动回测；--compare 输出与研究层对照；
--out DIR 落盘 trades.csv / equity.csv / metrics.json（签发凭证，可复现）。

用法：.venv/Scripts/python.exe backend/scripts/run_backtest_event.py \
        --rule ema_2060_1m_BTC/USDT [--compare] [--out data/backtest_reports/event_xxx]
"""
import argparse
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.core.config import settings
from app.engine import backtest_event
from app.models import db

_LOAD = """SELECT name, symbol, exchange, contract_type, timeframe, dsl_json, cooldown_bars
           FROM rule WHERE name=%s AND enabled ORDER BY version DESC LIMIT 1"""


def _w(x, fmt: str) -> str:
    return "-" if x is None else fmt.format(x)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", required=True, help="规则名（rule.name）")
    ap.add_argument("--compare", action="store_true", help="与研究层 vectorbt 对照")
    ap.add_argument("--out", default="", help="输出目录（缺省不落盘）")
    args = ap.parse_args()

    conn = db.connect(settings.pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(_LOAD, (args.rule,))
            row = cur.fetchone()
        if not row:
            print(f"规则不存在或未启用: {args.rule}")
            return
        cols = ["name", "symbol", "exchange", "contract_type", "timeframe",
                "dsl_json", "cooldown_bars"]
        rule = dict(zip(cols, row))

        ev = backtest_event.run_rule(conn, rule)
        m = ev["metrics"]
        print(f"规则 {rule['name']} | {rule['symbol']} {rule['contract_type']} {rule['timeframe']}"
              f" | 事件 {ev['n_events']}（入场 {ev['n_entries']}）")
        print(f"  交易 {m['n_trades']} 笔 · 胜率 {_w(m['win_rate'], '{:.1%}')} · "
              f"盈亏比 {_w(m['pf'], '{:.2f}')} · 均收益 {_w(m['avg_pnl_pct'], '{:+.2%}')} · "
              f"累计 {m['total_pnl']:+.1f} USDT（初始资金口径 {m['return_on_init[%]']:+.2f}%）")
        print(f"  净值最大回撤 {m['max_drawdown[%]']:.2f}% · 成本 {m['fees_total']:.1f} USDT · "
              f"平均持仓 {_w(m['avg_hold_bars'], '{:.1f}')} 根 · 仓位时间 {m['exposure[%]']:.1f}%")
        if ev["open_pos"]:
            p = ev["open_pos"]
            print(f"  持仓中：{p['entry_ts']:%m-%d %H:%M} 入场 @ {p['entry_price']:.6g}，"
                  f"浮盈 {p['unrealized']:+.1f} USDT")

        if args.compare:
            print(backtest_event.render_compare(rule["name"], backtest_event.compare(conn, rule)))

        if args.out:
            out = pathlib.Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(ev["trades"][0].keys()) if ev["trades"] else
                                   ["entry_ts", "exit_ts", "entry_price", "exit_price", "qty",
                                    "pnl", "pnl_pct", "fees", "hold_bars", "mae"])
                w.writeheader()
                w.writerows(ev["trades"])
            with (out / "equity.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts", "equity"])
                w.writerows(ev["equity"])
            (out / "metrics.json").write_text(json.dumps(
                {"rule": rule["name"], "symbol": rule["symbol"], "timeframe": rule["timeframe"],
                 "dsl_json": rule["dsl_json"], "cooldown_bars": rule["cooldown_bars"],
                 "n_events": ev["n_events"], "n_entries": ev["n_entries"],
                 "metrics": m, "open_pos": ev["open_pos"]},
                ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"  已落盘: {out}/ (trades.csv, equity.csv, metrics.json)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
