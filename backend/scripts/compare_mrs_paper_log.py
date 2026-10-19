"""M7/§9.4 真日志对照：纸面台账 ↔ harness 同窗口重放（逐 sig_id / 订单 / funnel 比对）。

v1 口径：
  1) 对照窗口 [start, end) UTC；harness 从 start-PRE_ROLL 起跑（冷启动指标需预热，
     截取窗口内记录比对，避免预热差异误报）。
  2) 信号：按 sig_id（uuid5 确定性）集合比对；命中者逐字段比对 dir/type/zone_id 与
     bundle(trigger/stop/tp/R/qty)。
  3) 成交：按 (sig_id, event) 集合比对；init/exit 全字段（含 pnl_R）逐字节比对。
  4) 订单：按 sig_id 分组，比对 (type,state,px,qty) 序列。
  5) funnel：窗口内 stop_at / plan_reject 计数与逐行 (ts, stop_at, plan_reject) 比对。
  6) 一致率 = 全部分组逐条相等的占比；任一分组不一致 → 非零退出。
  7) 中期窗口（台账已有历史成交）对照须传
     `--set account.init_equity=<窗口起点权益>`；否则两 run 权益不同 → 风险预算不同 →
     qty/订单/成交金额必然不一致（并非引擎非确定性）。窗口起点权益 = 之前各笔 exit
     pnl_usdt 累加；首笔之前的历史窗口无需对齐（均为 50,000）。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/compare_mrs_paper_log.py \\
      --paper-dir <paper_out_dir> --start 2026-06-01 --end 2026-06-16 \\
      --set venue.taker_bps=2 [--pre-roll 6] [--out report.md]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

_HERE = pathlib.Path(__file__).resolve()
PROJ = _HERE.parents[2]
HARNESS = _HERE.parent / "backtest_mrs.py"


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _ts(s: str) -> int:
    return int(_dt(s).timestamp())


def _jsonl(path: pathlib.Path, lo: int, hi: int) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if lo <= r.get("ts", 0) < hi:
                out.append(r)
    return out


def _sig_view(s: dict) -> dict:
    b = s.get("bundle") or {}
    return {"dir": s.get("dir"), "type": s.get("type"), "zone_id": s.get("zone_id"),
            "bundle": {k: b.get(k) for k in ("trigger", "stop", "tp", "R", "qty")}}


def _orders_by_sig(rows: list[dict]) -> dict[str, list]:
    g: dict[str, list] = defaultdict(list)
    for r in rows:
        g[r.get("sig_id")].append((r.get("type"), r.get("state"), r.get("px"), r.get("qty")))
    return {k: sorted(v) for k, v in g.items()}  # 同 ts 多单落盘序不保证 → 按内容归一


def _trade_view(t: dict) -> tuple:
    keys = ("event", "reason", "px", "qty", "pnl_usdt", "pnl_R", "bars_held",
            "mfe_R", "mae_R", "funding_paid", "be_done", "seg_done")
    return tuple(t.get(k) for k in keys)


def run_harness(start: str, end: str, pre_roll: int, sets: list[str]) -> pathlib.Path:
    out = pathlib.Path(tempfile.mkdtemp(prefix="mrs_cmp_"))
    cmd = [sys.executable, str(HARNESS),
           "--start", (_dt(start) - timedelta(days=pre_roll)).strftime("%Y-%m-%d"),
           "--end", end, "--out-dir", str(out)]
    for s in sets:
        cmd += ["--set", s]
    print(f"[harness] 运行 {' '.join(cmd[1:])}", flush=True)
    subprocess.run(cmd, cwd=PROJ, check=True,
                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return out


def compare(paper: pathlib.Path, harn: pathlib.Path, lo: int, hi: int) -> tuple[list[str], list[str]]:
    ps = {s["sig_id"]: s for s in _jsonl(paper / "mrs_signal.jsonl", lo, hi)}
    hs = {s["sig_id"]: s for s in _jsonl(harn / "mrs_signal.jsonl", lo, hi)}
    pt = _jsonl(paper / "mrs_trade.jsonl", lo, hi)
    ht = _jsonl(harn / "mrs_trade.jsonl", lo, hi)
    po = _orders_by_sig(_jsonl(paper / "mrs_orders.jsonl", lo, hi))
    ho = _orders_by_sig(_jsonl(harn / "mrs_orders.jsonl", lo, hi))
    pf = _jsonl(paper / "mrs_funnel.jsonl", lo, hi)
    hf = _jsonl(harn / "mrs_funnel.jsonl", lo, hi)

    checks: list[str] = []
    bad: list[str] = []

    def chk(name: str, a, b) -> None:
        ok = a == b
        checks.append(name)
        if not ok:
            bad.append(name)

    chk("信号集合", sorted(ps), sorted(hs))
    for sid in sorted(set(ps) & set(hs)):
        chk(f"信号字段 {sid[:8]}", _sig_view(ps[sid]), _sig_view(hs[sid]))
    ptu = {(t["sig_id"], t["event"]): _trade_view(t) for t in pt}
    htu = {(t["sig_id"], t["event"]): _trade_view(t) for t in ht}
    chk("成交集合", sorted(ptu), sorted(htu))
    for k in sorted(set(ptu) & set(htu)):
        chk(f"成交字段 {k[0][:8]}:{k[1]}", ptu[k], htu[k])
    chk("订单组集合", sorted(po), sorted(ho))
    for sid in sorted(set(po) & set(ho)):
        chk(f"订单序列 {sid[:8]}", po[sid], ho[sid])
    chk("funnel 计数", Counter(r["stop_at"] for r in pf),
        Counter(r["stop_at"] for r in hf))
    chk("PLAN 拒绝计数", Counter(r.get("plan_reject") for r in pf if r.get("plan_reject")),
        Counter(r.get("plan_reject") for r in hf if r.get("plan_reject")))
    chk("funnel 逐行", [(r["ts"], r["stop_at"], r.get("plan_reject")) for r in pf],
        [(r["ts"], r["stop_at"], r.get("plan_reject")) for r in hf])
    return checks, bad


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="MRS 纸面台账 ↔ harness 同源对照")
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--start", required=True, help="对照窗口起（含，UTC 日期）")
    ap.add_argument("--end", required=True, help="对照窗口止（不含，UTC 日期）")
    ap.add_argument("--pre-roll", type=int, default=6, help="harness 预热天数")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--out", help="markdown 报告路径")
    args = ap.parse_args(argv)

    lo, hi = _ts(args.start), _ts(args.end)
    paper = pathlib.Path(args.paper_dir)
    harn = run_harness(args.start, args.end, args.pre_roll, args.set)
    try:
        checks, bad = compare(paper, harn, lo, hi)
    finally:
        shutil.rmtree(harn, ignore_errors=True)
    rate = (len(checks) - len(bad)) / len(checks) * 100 if checks else 0.0
    lines = [f"# MRS §9.4 真日志对照 — {paper.name}", "",
             f"- 窗口 {args.start} → {args.end}（UTC）| harness 预热 {args.pre_roll} 天"
             f" | 对照项 {len(checks)}",
             f"- **一致率 {rate:.2f}%**（{len(checks) - len(bad)}/{len(checks)}）", ""]
    if bad:
        lines += ["## 不一致项", ""] + [f"- {b}" for b in bad]
    else:
        lines += ["- 信号/成交/订单组/funnel 逐条一致 ✅"]
    md = "\n".join(lines) + "\n"
    if args.out:
        pathlib.Path(args.out).write_text(md, encoding="utf-8")
        print(f"[对照] {args.out}")
    print(md)
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
