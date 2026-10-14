"""MRS T06（规格书 §3.4/§10）：未来函数审计（截断不变性 + t+1 注入反例）。

- 正例：完整数据流跑到 t 的引擎指纹 == 仅喂到 t 的截断运行的指纹（多个检查点逐一
  bitwise 比对；指纹覆盖指标/分型/区/日快照/量分位/账户/持仓/订单组/台账全部中间变量）。
  数据用库内真实 1m bar（最近 N 根）。
- 反例：monkeypatch 决策函数 in_session 读"bar k+1 注入值"（模拟未来函数）→ 同一审计
  必须捕获不一致（全量运行有 t+1 数据、截断运行没有 → 指纹不等）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t06.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))      # scripts/

import app.mrs.decision as decision  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.mrs.config import CFG  # noqa: E402
from app.mrs.engine import Engine  # noqa: E402
from app.mrs.types import Bar  # noqa: E402
from app.models import db  # noqa: E402
from mrs_snap import fingerprint  # noqa: E402

N_BARS = 3200
_STEP = 160  # 检查点步长（≈19 个截断比对点）
_ENTRY_PHASE = {"SESSION", "EVENT", "REGIME", "RESON", "ZONE", "COOLDOWN", "SPREAD",
                "VOL", "NO_TRIGGER", "PLAN", "OK"}


def load_bars() -> list[Bar]:
    conn = db.connect(settings.pg_dsn)
    try:
        df = db.load_ohlcv(conn, CFG["symbol"]["internal"], CFG["symbol"]["exchange"],
                           CFG["symbol"]["contract_type"], "1m", limit=N_BARS)
    finally:
        conn.close()
    assert len(df) == N_BARS, f"库内 1m 不足 {N_BARS} 根（实际 {len(df)}）"
    return [Bar(int(r.ts.timestamp()), float(r.open), float(r.high), float(r.low),
                float(r.close), float(r.volume)) for r in df.itertuples()]


def main() -> None:
    bars = load_bars()
    windows = CFG["manage"]["session_windows_utc"]

    # 1) 全量运行：逐根喂入，记录检查点指纹与每根漏斗标签
    full = Engine()
    labs: list[str] = []
    cps = list(range(_STEP - 1, len(bars), _STEP))
    fps: dict[int, str] = {}
    for j, b in enumerate(bars):
        lab, _ = full.on_1m_close(b)
        labs.append(lab)
        if j in cps:
            fps[j] = fingerprint(full)

    # 2) 正例：截断运行逐一 bitwise 比对
    for k in cps:
        eng = Engine()
        for b in bars[:k + 1]:
            eng.on_1m_close(b)
        assert fingerprint(eng) == fps[k], \
            f"截断不变性破坏 @bar {k}（存在未来函数/路径污染）"
    print(f"  [正例] {len(cps)} 个检查点：全量 vs 截断指纹 bitwise 一致 ✅")

    # 3) 反例：注入 bar k+1 数据影响 bar k 决策（in_session 读注入值）
    cands = [j for j in range(len(bars) // 2, len(bars) - 1)
             if labs[j] in _ENTRY_PHASE and decision.in_session(bars[j].ts + 60, windows)]
    assert cands, "无可用反例检查点（后半窗口无会话内 entry-phase bar）"
    real_in_session = decision.in_session
    leak = {"on": False}

    def leaky(now_ts: int, win: list[str]) -> bool:
        return False if leak["on"] else real_in_session(now_ts, win)

    decision.in_session = leaky
    try:
        caught = 0
        for k in cands[:: max(1, len(cands) // 2)][:2]:  # 取 ≤2 个分散检查点
            a = Engine()
            for j, b in enumerate(bars[:k + 1]):
                leak["on"] = (j == k)  # 全量：bar k 的决策读到了 k+1 的数据
                a.on_1m_close(b)
            fp_leak = fingerprint(a)
            b_eng = Engine()
            leak["on"] = False         # 截断运行：不存在 k+1 数据 → 无法注入
            for b in bars[:k + 1]:
                b_eng.on_1m_close(b)
            assert fp_leak != fingerprint(b_eng), f"反例未被捕获 @bar {k}（审计失效）"
            caught += 1
    finally:
        decision.in_session = real_in_session
    print(f"  [反例] t+1 注入 {caught} 处均被审计捕获（指纹不一致）✅")


if __name__ == "__main__":
    main()
    print("T06 全部通过 ✅")
