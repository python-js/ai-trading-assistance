"""M5/§9.4 一致性（v1 口径：dual-run）+ §9.0 同源静态检查。

- dual-run：同一冻结窗口、同一代码，两个子进程以不同 PYTHONHASHSEED（0 / 4242）运行
  backtest_mrs.py → Bundle 序列 JSONL 与结束态指纹逐字节一致（=0 diff）。
  窗口冻结（v1 教训）：必须显式 --start/--end（截止昨日 UTC）；harness 的 --days 锚定
  库内最新 ts，生产扫描器持续写入 → 两次子进程数据集漂移 → 假性指纹不一致。
  （v1 登记：harness vs 纸盘真日志对照待 M7 后补；§9.4 "一致率 100%" 的 v1 证据形式。）
- §9.0 静态：决策/执行路径不得出现 `if backtest:` 分支（grep backend/app + backend/scripts，
  排除本脚本自身——其探测字符串为运行时拼接，不会自命中）。
用法（项目根）：.venv/Scripts/python.exe backend/scripts/verify_mrs_consistency.py [--days 90]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

_HERE = pathlib.Path(__file__).resolve()
PROJ = _HERE.parents[2]  # project root
HARNESS = _HERE.parent / "backtest_mrs.py"
NEEDLE = "if " + "backtest:"


def static_check() -> None:
    hits: list[str] = []
    for sub in ("backend/app", "backend/scripts"):
        for p in (PROJ / sub).rglob("*.py"):
            if p.resolve() == _HERE:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if NEEDLE in line:
                    hits.append(f"{p.relative_to(PROJ)}:{i}")
    assert not hits, f"§9.0 违规（决策路径出现 `{NEEDLE}` 分支）：{hits}"
    print(f"  [静态] §9.0 同源：backend/app + backend/scripts 无 `{NEEDLE}` 分支 ✅")


def run_once(seed: int, start: str, end: str, out: pathlib.Path, tag: str) -> tuple:
    env = {**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONIOENCODING": "utf-8"}
    b = out / f"bundles_{tag}.jsonl"
    f = out / f"fp_{tag}.txt"
    print(f"  [dual-run] seed={seed} 运行中（窗口 {start} → {end}，预计 1-3 分钟）…", flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(HARNESS), "--start", start, "--end", end,
                        "--dump-bundles", str(b), "--dump-fingerprint", str(f)],
                       cwd=PROJ, env=env, capture_output=True, text=True,
                       encoding="utf-8", check=True)
    dt = time.time() - t0
    sig_line = next((ln for ln in r.stdout.splitlines() if ln.startswith("[信号]")), "")
    print(f"  [dual-run] seed={seed} 完成 {dt:.1f}s | {sig_line.strip()}")
    return b, f


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    print("[1/2] §9.0 同源静态检查")
    static_check()

    now = datetime.now(timezone.utc)
    end = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (now - timedelta(days=1 + args.days)).strftime("%Y-%m-%d")
    print(f"[2/2] §9.4 dual-run 一致性（冻结窗口 {start} → {end}，PYTHONHASHSEED 0 vs 4242）")
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td)
        b0, f0 = run_once(0, start, end, out, "seed0")
        b1, f1 = run_once(4242, start, end, out, "seed4242")
        raw0, raw1 = b0.read_bytes(), b1.read_bytes()
        n = raw0.count(b"\n")
        assert raw0 == raw1, (
            "Bundle 序列不一致（§9.4 违反）："
            f"diff 行数={sum(1 for x, y in zip(raw0.splitlines(), raw1.splitlines()) if x != y)}")
        print(f"  [一致性] Bundle 序列 {n} 行 逐字节一致（sha256 {hashlib.sha256(raw0).hexdigest()[:12]}）✅")
        fp0, fp1 = f0.read_text(encoding="utf-8"), f1.read_text(encoding="utf-8")
        assert fp0 == fp1, "结束态指纹不一致（决策路径存在非确定性）"
        print(f"  [一致性] 结束态全量指纹逐字节一致（{len(fp0)} 字符）✅")

    print("§9.4 一致性 v1（dual-run）与 §9.0 静态检查 全部通过 ✅")


if __name__ == "__main__":
    main()
