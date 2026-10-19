"""M6/§9.6 灵敏度分析运行器：6 关键参数 ±20% 单参数扰动（全窗口）。

v1 口径登记：
  1) 参数表（±20%，req_score/cooldown 取整）：resonance.req_score 3→2/4、
     entry.room_rr_min 1.5→1.2/1.8、manage.tp_r_base 2.2→1.76/2.64、
     manage.be_trigger_r 1.5→1.2/1.8、risk.cooldown_min_bars 25→20/30、
     entry.vol_pctile_min 0.60→0.48/0.72。共 12 变体，每变体 = 生产 harness 独立子进程
     （--set 覆盖 + --dump-stats），与基线同窗口同场景（调用方经 --set 固定，如 maker）。
  2) 判定（趋势/平台/孤峰，三点 lo-base-hi 或两点 lo-hi）：
        - 符号翻转：任一变体期望符号 ≠ 基线（mean×base<0）→ 不稳（红线）；
        - 平台：极差 < 0.1R 且无翻转 → 局部稳健；
        - 单调：lo≤base≤hi 或反向且极差 ≥ 0.1R → 趋势敏感（方向性风险）；
        - 其余 → 非单调（孤峰/谷）。
  3) 本分析仅测参数邻域局部稳定性，与 §9.6 WFA（分段外推）互为补充，不互相替代。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/run_mrs_sensitivity.py \\
      --start 2024-10-14 --end 2026-10-14 --set venue.taker_bps=2 \\
      --base-stats <maker_full>.json --work-dir <work> --out sensitivity.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

_HERE = pathlib.Path(__file__).resolve()
_BACKEND = _HERE.parents[1]
HARNESS = _HERE.parent / "backtest_mrs.py"

# (点路径, 参数名, 基线值, [变体低, 变体高])；值写死 = v1 口径 1（±20% 取整结果）
PARAMS = [
    ("resonance.req_score", "req_score", 3, [2, 4]),
    ("entry.room_rr_min", "room_rr_min", 1.5, [1.2, 1.8]),
    ("manage.tp_r_base", "tp_r_base", 2.2, [1.76, 2.64]),
    ("manage.be_trigger_r", "be_trigger_r", 1.5, [1.2, 1.8]),
    ("risk.cooldown_min_bars", "cooldown_min_bars", 25, [20, 30]),
    ("entry.vol_pctile_min", "vol_pctile_min", 0.60, [0.48, 0.72]),
]


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MRS §9.6 灵敏度（±20% 单参数扰动）")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                    help="固定场景集（转发给每个变体，如 venue.taker_bps=2）")
    ap.add_argument("--base-stats", help="基线 stats JSON（maker 全量）用于三点判定")
    ap.add_argument("--work-dir", required=True, help="变体 stats/日志目录")
    ap.add_argument("--out", required=True, help="markdown 报表")
    ap.add_argument("--out-stats", help="变体汇总 JSON")
    ap.add_argument("--par", type=int, default=3, help="并行变体数")
    return ap.parse_args(argv)


def _summ(stats: dict) -> dict:
    exs = [t for t in stats["trades"] if t["event"] == "exit"]
    rs = [t["pnl_R"] for t in exs]
    n = len(rs)
    m = sum(rs) / n if n else 0.0
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    ci = 1.96 * sd / math.sqrt(n) if n else 0.0
    gw = sum(x for x in rs if x > 0)
    gl = -sum(x for x in rs if x < 0)
    return {"n_sig": len(stats["signals"]), "n_exit": n, "mean": m, "ci95": ci,
            "sum_r": sum(rs), "pf": (gw / gl if gl > 0 else
                                     (float("inf") if gw > 0 else 0.0)),
            "win": (sum(x > 0 for x in rs) / n * 100 if n else 0.0)}


def _verdict(points: list[tuple[str, float]]) -> str:
    """points = [(标签, 期望R)]，按 lo→(base)→hi 序；v1 口径 2。"""
    means = [p[1] for p in points]
    if any(means[i] * means[j] < 0 for i in range(len(means)) for j in range(i + 1, len(means))):
        return "符号翻转（红线）"
    rng = max(means) - min(means)
    if rng < 0.1:
        return "平台（局部稳健）"
    if len(points) == 3 and (means[0] <= means[1] <= means[2] or means[0] >= means[1] >= means[2]):
        return "单调敏感（趋势）"
    return "非单调（孤峰/谷）"


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    work = pathlib.Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    py = sys.executable

    base = None
    if args.base_stats:
        st = json.loads(pathlib.Path(args.base_stats).read_text(encoding="utf-8"))
        base = _summ(st)
        print(f"[基线] n={base['n_exit']} 期望 {base['mean']:+.3f}R PF {base['pf']:.2f}")

    jobs = []  # (key, label, path, value)
    for path, name, _bv, vals in PARAMS:
        for v in vals:
            jobs.append((f"{name}={v}", f"{name}", path, v))
    print(f"[变体] {len(jobs)} 个 × 窗口 {args.start}→{args.end} | 并行 {args.par}")

    def _run(job):
        key, _name, path, v = job
        out = work / f"stats_{key.replace('=', '_')}.json"
        log = work / f"log_{key.replace('=', '_')}.log"
        cmd = [py, str(HARNESS), "--start", args.start, "--end", args.end,
               "--dump-stats", str(out)]
        for s in args.set:
            cmd += ["--set", s]
        cmd += ["--set", f"{path}={v}"]
        t0 = time.time()
        with open(log, "w", encoding="utf-8") as f:
            rc = subprocess.run(cmd, cwd=str(_BACKEND), stdout=f,
                                stderr=subprocess.STDOUT, env=env).returncode
        return key, rc, time.time() - t0, out

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.par) as pool:
        for key, rc, el, out in pool.map(_run, jobs):
            ok = rc == 0 and out.exists()
            print(f"[变体] {key}: {'✅' if ok else f'❌ rc={rc}'} {el:.0f}s", flush=True)
            if ok:
                results[key] = _summ(json.loads(out.read_text(encoding="utf-8")))

    L = ["# MRS §9.6 灵敏度分析（±20% 单参数扰动）", "",
         f"- 窗口 {args.start} → {args.end}（UTC）| 场景 --set {' '.join(args.set) or '（默认）'}",
         f"- 判定口径：符号翻转 / 平台(<0.1R) / 单调 / 孤峰；每变体 = 生产 harness 独立运行", "",
         "## 总表", "",
         "| 参数 | 变体 | n信号 | n成交 | 期望R | 95%CI | ΣR | PF | 胜率 |",
         "|---|---|---|---|---|---|---|---|---|"]
    if base:
        L.append(f"| （基线） | {args.base_stats.split('/')[-1]} | {base['n_sig']} | {base['n_exit']} "
                 f"| {base['mean']:+.3f} | ±{base['ci95']:.3f} | {base['sum_r']:+.2f} "
                 f"| {base['pf']:.2f} | {base['win']:.1f}% |")
    for _path, name, _bv, vals in PARAMS:
        for v in vals:
            key = f"{name}={v}"
            r = results.get(key)
            if r is None:
                L.append(f"| {name} | {v} | ❌ | | | | | |")
            else:
                L.append(f"| {name} | {v} | {r['n_sig']} | {r['n_exit']} | {r['mean']:+.3f} "
                         f"| ±{r['ci95']:.3f} | {r['sum_r']:+.2f} | {r['pf']:.2f} "
                         f"| {r['win']:.1f}% |")
    L += ["", "## 参数级判定", ""]
    for _path, name, _bv, vals in PARAMS:
        pts = []
        for v in vals:
            r = results.get(f"{name}={v}")
            if r is not None:
                pts.append((str(v), r["mean"]))
        if base is not None and len(pts) == 2:
            pts = [pts[0], ("base", base["mean"]), pts[1]]
        if len(pts) < 2:
            L.append(f"- **{name}**：样本不足（变体失败），无法判定")
            continue
        L.append(f"- **{name}**：{_verdict(pts)} | 点值 " +
                 " / ".join(f"{lab}={m:+.3f}R" for lab, m in pts))
    pathlib.Path(args.out).write_text("\n".join(L), encoding="utf-8")
    if args.out_stats:
        pathlib.Path(args.out_stats).write_text(
            json.dumps({"window": [args.start, args.end], "sets": args.set,
                        "base": base, "variants": results},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[报表] {args.out}（变体 {len(results)}/{len(jobs)} 完成）")


if __name__ == "__main__":
    main()
