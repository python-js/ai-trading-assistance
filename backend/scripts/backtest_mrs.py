"""M5/§9 回测 Harness（同源约束 §9.0：仅替换 DataHub=DB 读盘，决策路径 = Engine 唯一实例）。

- 数据：PostgreSQL ohlcv 1m + funding_rate（§9.2；结算点缺数据按 0 计提并 imputed 计数）。
- 成本：PaperBroker 悲观内插（§9.1）+ 双边 taker 费 + 滑点（CFG venue 默认档）。
- 窗口：--start/--end（UTC 日期）或 --days N（默认 30，锚定库内最新 ts）。
- --out-dir PATH：§8 台账落盘（signal/trade/orders/funnel/state；M6 归档与 COST 拒绝聚合用）。
- --set PATH=VALUE：配置覆盖（点路径，可重复；如 --set venue.taker_bps=2 得 maker 费率场景、
  §9.5 灵敏度扰动）。值按 JSON 解析（数/布尔/列表），失败回退字符串；覆盖后 validate_cfg 强校验。
- --dump-stats PATH：§9.5 报表数据源 JSON（窗口/cfg/cfg_hash/费用/漏斗计数/signals/trades 全量）。
- --dump-bundles PATH：Bundle 序列 JSONL（sig_id/stop/tp/qty…，§9.4 一致性 diff 输入）。
- --dump-fingerprint PATH：结束态全量指纹（mrs_snap.fingerprint，dual-run 对照用）。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/backtest_mrs.py --days 30
  ... --start 2025-09-01 --end 2025-10-01 --dump-bundles out.jsonl
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))       # scripts/（mrs_snap）
sys.path.insert(0, str(_HERE.parents[1]))   # backend/（app.*）

from app.core.config import settings  # noqa: E402
from app.mrs.config import CFG, cfg_hash, validate_cfg  # noqa: E402
from app.mrs.engine import Engine  # noqa: E402
from app.mrs.ledger import Ledger  # noqa: E402
from app.mrs.types import Bar  # noqa: E402
from app.models import db  # noqa: E402
from mrs_snap import fingerprint  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MRS 回测 Harness（§9 同源）")
    ap.add_argument("--start", help="窗口起（UTC，YYYY-MM-DD）")
    ap.add_argument("--end", help="窗口止（UTC，YYYY-MM-DD）")
    ap.add_argument("--days", type=int, default=30, help="无 --start/--end 时：锚定最新 ts 回溯天数")
    ap.add_argument("--out-dir", help="台账落盘目录（§8 全量：signal/trade/orders/funnel/state）")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                    help="配置覆盖（点路径，可重复），如 venue.taker_bps=2")
    ap.add_argument("--dump-stats", help="§9.5 报表数据源 JSON 落盘路径")
    ap.add_argument("--dump-bundles", help="Bundle 序列 JSONL 落盘路径")
    ap.add_argument("--dump-fingerprint", help="结束态指纹落盘路径")
    return ap.parse_args(argv)


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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
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
        df = db.load_ohlcv(conn, sym, ex_, ct, "1m", start=start_dt, end=end_dt)
        with conn.cursor() as cur:
            cur.execute("""SELECT ts, rate FROM funding_rate
                           WHERE symbol=%s AND exchange=%s AND rate IS NOT NULL
                             AND ts >= %s AND ts <= %s""",
                        (sym, ex_, start_dt - timedelta(hours=8), end_dt + timedelta(hours=8)))
            fmap = {int(t.timestamp()): float(r) for t, r in cur.fetchall()}
    finally:
        conn.close()
    if df.empty:
        sys.exit(f"窗口无数据：{start_dt} → {end_dt}")
    load_s = time.time() - t0

    ts_s = (df["ts"].astype("int64") // 1_000_000_000).tolist()
    bars = [Bar(int(t), float(o), float(h), float(l), float(c), float(v))
            for t, o, h, l, c, v in zip(ts_s, df["open"], df["high"], df["low"],
                                        df["close"], df["volume"])]
    print(f"[窗口] {_fmt(ts_s[0])} → {_fmt(ts_s[-1])} UTC | {len(bars)} 根 1m | "
          f"加载 {load_s:.1f}s | funding 点 {len(fmap)}")
    print(f"[配置] cfg_hash={cfg_hash(cfg)} | fees taker/slip = {cfg['venue']['taker_bps']}/"
          f"{cfg['venue']['slip_base_bps']} bps")

    eng = Engine(cfg=cfg, ledger=Ledger(out_dir=args.out_dir), funding_fn=fmap.get)
    t0 = time.time()
    for b in bars:
        eng.on_1m_close(b)
    run_s = time.time() - t0

    led = eng.ledger
    inits = [t for t in led.trades if t["event"] == "init"]
    exits = [t for t in led.trades if t["event"] == "exit"]
    reasons = Counter(t["reason"] for t in exits)
    rs = [t["pnl_R"] for t in exits]
    gw = sum(x for x in rs if x > 0)
    gl = -sum(x for x in rs if x < 0)
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
    print("[漏斗] " + " ".join(f"{k}={v}" for k, v in
                               led.funnel_counts.most_common()))
    print(f"[信号] {len(led.signals)} | 成交 init={len(inits)} exit={len(exits)}"
          f"（在管 {len(inits) - len(exits)}）")
    if exits:
        print("[离场] " + " ".join(f"{k}={v}" for k, v in reasons.most_common())
              + f" | 胜率 {sum(x > 0 for x in rs) / len(rs) * 100:.1f}%"
              + f" | ΣR={sum(rs):+.2f} | PF={pf:.2f}")
    print(f"[权益] {cfg['account']['init_equity']:.2f} → {eng.equity:.2f}"
          f"（Δ{eng.equity - cfg['account']['init_equity']:+.2f}）| 末态 {eng.acct.state}"
          f" | funding_imputed={eng.funding_imputed}")
    print(f"[耗时] 决策 {run_s:.1f}s（{len(bars) / max(run_s, 1e-9):.0f} bar/s）")

    if args.dump_bundles:
        with open(args.dump_bundles, "w", encoding="utf-8") as f:
            for r in led.signals:
                b = r["bundle"]
                f.write(json.dumps({"sig_id": r["sig_id"], "ts": r["ts"], "dir": r["dir"],
                                    "type": r["type"], "zone_id": r["zone_id"],
                                    "trigger": b["trigger"], "stop": b["stop"], "tp": b["tp"],
                                    "R": b["R"], "qty": b["qty"]},
                                   ensure_ascii=False) + "\n")
        print(f"[落盘] bundles → {args.dump_bundles}（{len(led.signals)} 行）")
    if args.dump_fingerprint:
        pathlib.Path(args.dump_fingerprint).write_text(fingerprint(eng), encoding="utf-8")
        print(f"[落盘] fingerprint → {args.dump_fingerprint}")
    if args.dump_stats:
        stats = {
            "window": {"start": _fmt(ts_s[0]), "end": _fmt(ts_s[-1]),
                       "n_bars": len(bars), "days": len(bars) / 1440},
            "cfg": cfg, "cfg_hash": cfg_hash(cfg),
            "fees_bps": {"fee": eng.fees[0], "slip": eng.fees[1]},
            "equity_init": cfg["account"]["init_equity"], "equity_final": eng.equity,
            "state_final": eng.acct.state, "funding_imputed": eng.funding_imputed,
            "funding_settlements": eng.funding_settlements,
            "funnel_counts": dict(led.funnel_counts.most_common()),
            "plan_reject_counts": dict(led.plan_reject_counts.most_common()),
            "signals": led.signals, "trades": led.trades,
            "orders": led.orders,
        }
        pathlib.Path(args.dump_stats).write_text(
            json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[落盘] stats → {args.dump_stats}（signals={len(led.signals)}）")


if __name__ == "__main__":
    main()
