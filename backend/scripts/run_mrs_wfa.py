"""M6/§9.6 Walk-forward 协议运行器（v1 口径）。

- 参数固定为规格书默认/ex-ante 指定（本策略无拟合过程）→ 折内"样本内 6 月 + 样本外 2 月"
  是稳定性检验：两段期望符号相反 = 不过发布门（规格书 §9.6）。
- v1 折方案：8 月块 × N 折非重叠（步长=块长；默认 3 折 = 24 个月数据）；每折独立 harness
  子进程（独立引擎状态、自带指标预热）。更密折可减小步长——成本线性。
- v1 每折无"新增拒绝规则"（参数 ex ante 固定），版本变更日志为空。
- --set 透传给 harness（如 venue.taker_bps=2 的 maker 场景）；统计经 --dump-stats 回收。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/run_mrs_wfa.py --start 2024-10-14 --end 2026-10-14 \
      --set venue.taker_bps=2 --folds 3 --work-dir OUT --out wfa.md
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

_HERE = pathlib.Path(__file__).resolve()
PROJ = _HERE.parents[2]
HARNESS = _HERE.parent / "backtest_mrs.py"


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MRS §9.6 WFA 运行器")
    ap.add_argument("--start", required=True, help="窗口起（UTC YYYY-MM-DD）")
    ap.add_argument("--end", required=True, help="窗口止")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    ap.add_argument("--work-dir", required=True, help="每折 stats JSON 目录")
    ap.add_argument("--out", required=True, help="markdown 报表落盘")
    return ap.parse_args(argv)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _run_fold(i: int, start: datetime, end: datetime, in_end: datetime,
              sets: list[str], work: pathlib.Path) -> dict:
    stats_path = work / f"stats_fold{i}.json"
    cmd = [sys.executable, str(HARNESS), "--start", start.strftime("%Y-%m-%d"),
           "--end", end.strftime("%Y-%m-%d"), "--dump-stats", str(stats_path)]
    for s in sets:
        cmd += ["--set", s]
    print(f"  [fold{i}] {start:%Y-%m-%d} → {end:%Y-%m-%d}（内样本止 {in_end:%Y-%m-%d}）运行中…",
          flush=True)
    r = subprocess.run(cmd, cwd=PROJ, capture_output=True, text=True,
                       encoding="utf-8", check=True,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    sig = next((ln for ln in r.stdout.splitlines() if ln.startswith("[信号]")), "")
    print(f"  [fold{i}] 完成 | {sig.strip()}")
    return json.loads(stats_path.read_text(encoding="utf-8"))


def _seg(rs: list[float]) -> str:
    if not rs:
        return "n=0（无样本）"
    m = sum(rs) / len(rs)
    return f"n={len(rs)} 期望 {m:+.3f}R 胜率 {sum(x > 0 for x in rs) / len(rs) * 100:.0f}%"


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    start, end = _dt(args.start), _dt(args.end)
    total_days = (end - start).days
    block = total_days / args.folds
    in_days = block * 6 / 8
    work = pathlib.Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    L = ["# MRS §9.6 Walk-forward 报表（v1：8 月块非重叠 / 内 6 月 + 外 2 月）", "",
         f"- 窗口 {args.start} → {args.end} UTC（{total_days} 天，{args.folds} 折）"
         f" | 覆盖参数：{' '.join(args.set) if args.set else '默认（VIP0 taker）'}", ""]
    L += ["| 折 | 区间 | 内样本(前 6 月) | 外推(后 2 月) | 符号一致性 |",
          "|---|---|---|---|---|"]
    rows = []
    for i in range(args.folds):
        f0 = start + timedelta(days=block * i)
        f1 = start + timedelta(days=block * (i + 1))
        f_in_end = f0 + timedelta(days=in_days)
        st = _run_fold(i, f0, f1, f_in_end, args.set, work)
        exits = [t for t in st["trades"] if t["event"] == "exit"]
        in_rs = [t["pnl_R"] for t in exits
                 if datetime.fromtimestamp(t["ts"], tz=timezone.utc) < f_in_end]
        out_rs = [t["pnl_R"] for t in exits
                  if datetime.fromtimestamp(t["ts"], tz=timezone.utc) >= f_in_end]
        sin = None if not in_rs else (1 if sum(in_rs) >= 0 else -1)
        sout = None if not out_rs else (1 if sum(out_rs) >= 0 else -1)
        if sin is None or sout is None:
            verdict = "样本不足（未判定）"
        elif sin == sout:
            verdict = "一致 ✅"
        else:
            verdict = "**相反 ❌（不过发布门）**"
        rows.append((i, f0, f1, in_rs, out_rs, verdict))
        L.append(f"| {i + 1} | {f0:%Y-%m-%d}→{f1:%Y-%m-%d} | {_seg(in_rs)} | "
                 f"{_seg(out_rs)} | {verdict} |")
    L += ["", "## 结论", ""]
    bad = [r for r in rows if "❌" in r[5]]
    und = [r for r in rows if "未判定" in r[5]]
    if bad:
        L += [f"- {len(bad)} 折样本内外符号相反 → **不通过发布门（§9.6）**"]
    elif und:
        L += [f"- 无符号相反折；{len(und)}/{len(rows)} 折样本不足（成交稀疏，符号检验低效力，"
              f"见各折 n）。", "- 样本充分折全部同号。" if len(und) < len(rows) else ""]
    else:
        L += ["- 全部折样本内外期望同号 ✅"]
    L += ["", "- v1：参数 ex-ante 固定（无拟合过程）→ 无新增拒绝规则入版本日志；两段统计直接按"
          " exit ts 切分同一折运行。", ""]
    pathlib.Path(args.out).write_text("\n".join(L), encoding="utf-8")
    print(f"[WFA] {args.out}")


if __name__ == "__main__":
    main()
