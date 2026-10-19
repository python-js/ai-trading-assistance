"""DVR-1.0 回测入口（只读 DB，不产生任何订单）。

用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/backtest_dvr.py --days 730
  ... --start 2025-01-01 --end 2025-06-01 --out-dir ../reports/dvr_v1_20261019
  ... --set venue.slip_bps=4          # 滑点翻倍压力场景
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))

from app.core.config import settings  # noqa: E402
from app.dvr.backtest import load_data, run_backtest, summarize  # noqa: E402
from app.dvr.config import CFG, cfg_hash, validate_cfg  # noqa: E402
from app.models import db  # noqa: E402


def _apply_sets(cfg: dict, sets: list[str]) -> None:
    for item in sets:
        path, sep, raw = item.partition("=")
        keys = path.strip().split(".")
        node = cfg
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                sys.exit(f"--set 路径不存在: {path}")
            node = node[k]
        if not sep or keys[-1] not in node:
            sys.exit(f"--set 键不存在: {path}")
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        old = node[keys[-1]]
        node[keys[-1]] = val
        print(f"[覆盖] {path}: {old!r} → {val!r}")


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _fmt(ts_s: int) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _print(summary: dict) -> None:
    a, lo, sh = summary["all"], summary["long"], summary["short"]
    print(f"[权益] {CFG['account']['init_equity']:.2f} → {summary['equity_end']:.2f}")
    for name, m in (("全部", a), ("多头", lo), ("空头", sh)):
        print(f"[{name}] n={m['n']} 胜率={m['win']:.1f}% PF={m['pf']:.2f} "
              f"期望={m['exp_bps']:+.2f}bp/笔 ({m['exp_r']:+.3f}R) "
              f"费用={m['fees']:.2f} funding={m['funding']:.2f}")
    print(f"[回撤] {a.get('max_dd_pct', 0):.2f}% | 往返成本 {summary['round_trip_bps']:.1f}bp "
          f"| funding_imputed={summary['funding_imputed']} | 缺入场bar={summary['missing_entry']}")
    print(f"[原始背离] {summary['raw_div']}")
    for side in ("long", "short"):
        f = summary["funnel"].get(side, {})
        if f:
            print(f"[漏斗-{side}] " + " ".join(f"{k}={v}" for k, v in
                                               sorted(f.items(), key=lambda x: -x[1])))
        e = summary["event_funnel"].get(side, {})
        if e:
            print(f"[事件漏斗-{side}] " + " ".join(f"{k}={v}" for k, v in
                                                   sorted(e.items(), key=lambda x: -x[1])))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="DVR-1.0 回测")
    ap.add_argument("--start", help="UTC 日期 YYYY-MM-DD")
    ap.add_argument("--end", help="UTC 日期 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--out-dir", default=str(_HERE.parents[2] / "reports" / "dvr_v1_20261019"))
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    cfg = copy.deepcopy(CFG)
    _apply_sets(cfg, args.set)
    validate_cfg(cfg)
    sym, ex_, ct = (cfg["symbol"]["internal"], cfg["symbol"]["exchange"],
                    cfg["symbol"]["contract_type"])
    conn = db.connect(settings.pg_dsn)
    try:
        if args.start and args.end:
            start_dt, end_dt = _dt(args.start), _dt(args.end)
        else:
            hi = db.max_ohlcv_ts(conn, sym, ex_, ct, "1m")
            if hi is None:
                sys.exit("库内无 1m 数据")
            end_dt = hi + timedelta(seconds=60)
            start_dt = end_dt - timedelta(days=args.days)
        t0 = time.time()
        df1, df5, df15, fmap = load_data(conn, start_dt, end_dt, cfg)
    finally:
        conn.close()
    print(f"[窗口] {_fmt(int(df5.ts.iloc[0]))} → {_fmt(int(df5.ts.iloc[-1]))} UTC | "
          f"1m={len(df1)} 5m={len(df5)} 15m={len(df15)} | funding点={len(fmap)} | "
          f"加载 {time.time() - t0:.1f}s")
    print(f"[配置] hash={cfg_hash(cfg)} | taker/slip={cfg['venue']['taker_bps']}/"
          f"{cfg['venue']['slip_bps']}bp")
    t0 = time.time()
    res = run_backtest(df1, df5, df15, fmap, cfg)
    print(f"[耗时] {time.time() - t0:.1f}s | 交易 {len(res.trades)} | 信号 {len(res.signals)}")
    summary = summarize(res)
    _print(summary)

    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    (out / "funnel.json").write_text(json.dumps(summary["funnel"], ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    if res.trades:
        import csv
        with open(out / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(res.trades[0].keys()))
            w.writeheader()
            w.writerows(res.trades)
    if res.signals:
        import csv
        with open(out / "signals.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(res.signals[0].keys()))
            w.writeheader()
            w.writerows(res.signals)
    print(f"[落盘] {out}")


if __name__ == "__main__":
    main()
