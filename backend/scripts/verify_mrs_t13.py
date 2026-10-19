"""MRS T13（规格书 §3.2-H3 / §11）：数据缺口 + M7 Runner 重启接线验证。

矩阵：
  A) 引擎缺口：跳 bar → NO_DATA 且 last_ts 不前进；补齐后恢复处理。
  B) contiguous_tail：取以 end_ts 收尾的最长连续段（缺口前截断、end 缺失 → 空）。
  C) 重启 IN_POS：Rig 落盘快照 → prepare/warm/restore → 持仓+PROTECTED 组重建，
     下一 bar 击穿止损 → STOP 离场（管理路径无缝续跑）。
  D) 重启 ARMED：挂单快照 + TRIGGER_SENT 组重建，下一 bar 触发成交 → IN_POS。
  E) 实时缺口：ingest 缺 1m bar 时不跳 bar（last_ts 停在缺口前），补齐后连续喂入。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t13.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))       # scripts/（mrs_rig / run_mrs_paper）
sys.path.insert(0, str(_HERE.parents[1]))   # backend/（app.*）

from app.mrs.config import CFG  # noqa: E402
from app.mrs.engine import Engine  # noqa: E402
from app.mrs.ledger import Ledger  # noqa: E402
from app.mrs.types import Bar  # noqa: E402
from mrs_rig import Rig, ts_of  # noqa: E402
from run_mrs_paper import PaperRunner, contiguous_tail  # noqa: E402

T0 = ts_of("13:00")


def flat(ts: int, px: float = 1700.0) -> Bar:
    return Bar(ts, px, px + 1, px - 1, px, 50.0)


def mk_fetch(rows: dict[int, Bar]):
    def fetch(since_s: int, end_s: int) -> list[Bar]:
        return [rows[t] for t in sorted(rows) if since_s <= t <= end_s]
    return fetch


def case_engine_gap() -> None:
    eng = Engine(ledger=Ledger())
    bars = [flat(T0 + i * 60) for i in range(3)]
    for b in bars:
        eng.on_1m_close(b)
    lab, _ = eng.on_1m_close(flat(T0 + 240))  # 缺 T0+180
    assert lab == "NO_DATA" and eng.last_ts == bars[-1].ts, (lab, eng.last_ts)
    lab, _ = eng.on_1m_close(flat(T0 + 300))  # last_ts 未前进，仍不连续
    assert lab == "NO_DATA", lab
    eng.on_1m_close(flat(T0 + 180))
    lab, _ = eng.on_1m_close(flat(T0 + 240))
    assert lab != "NO_DATA" and eng.last_ts == T0 + 240, (lab, eng.last_ts)
    print("  [A] 缺口 NO_DATA 不前进 / 补齐恢复 通过 ✅")


def case_tail() -> None:
    rows = [flat(T0), flat(T0 + 60), flat(T0 + 300), flat(T0 + 360)]
    assert [b.ts for b in contiguous_tail(rows, T0 + 360)] == [T0 + 300, T0 + 360]
    assert [b.ts for b in contiguous_tail(rows, T0 + 60)] == [T0, T0 + 60]
    assert contiguous_tail(rows, T0 + 120) == []
    print("  [B] contiguous_tail 缺口截断/缺失空 通过 ✅")


def _runner(tmp: str, rows: dict[int, Bar]) -> PaperRunner:
    return PaperRunner(tmp, mk_fetch(rows), warm_bars=30,
                       push_fn=lambda text: None)


def _warm_rows(end_ts: int) -> dict[int, Bar]:
    return {end_ts - k * 60: flat(end_ts - k * 60) for k in range(30)}


def case_resume_in_pos() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(ledger=Ledger(tmp))
        rig.prep()
        rig.inject()
        assert rig.signal() == ("OK", None)
        assert rig.fill() == ("BUSY", None)
        state_ts = ts_of("14:02")  # 快照 ts（fill 后 last_ts+60）
        runner = _runner(tmp, _warm_rows(state_ts - 60))
        eng, state, plan = runner.prepare()
        assert state["state"] == "IN_POS" and plan.position is not None
        runner.warm(eng, state_ts - 60)
        runner.restore(eng, state, plan)
        assert eng.pos is not None and eng.pos.sig_id == plan.position["sig_id"]
        g = eng.broker.get(eng.pos.sig_id)
        assert g is not None and g.stage == "PROTECTED", g and g.stage
        assert eng.acct.state == "IN_POS"
        exits = [t for t in eng.ledger.trades if t["event"] == "exit"]
        runner.feed(eng, Bar(state_ts, 1704.0, 1706.0, 1674.0, 1680.0, 50.0))
        assert eng.pos is None and eng.ledger.trades[-1]["event"] == "exit"
        assert eng.ledger.trades[-1]["reason"] == "STOP", eng.ledger.trades[-1]
        assert len([t for t in eng.ledger.trades
                    if t["event"] == "exit"]) == len(exits) + 1
        print("  [C] IN_POS 重启：组重建 + 止损续管 通过 ✅")


def case_resume_armed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(ledger=Ledger(tmp))
        rig.prep()
        rig.inject()
        assert rig.signal() == ("OK", None)
        state_ts = ts_of("14:01")
        runner = _runner(tmp, _warm_rows(state_ts - 60))
        eng, state, plan = runner.prepare()
        assert state["state"] == "ARMED" and plan.pending is not None
        runner.warm(eng, state_ts - 60)
        runner.restore(eng, state, plan)
        g = eng.broker.get(eng.pending_sig_id)
        assert g is not None and g.stage == "TRIGGER_SENT"
        runner.feed(eng, Bar(state_ts, 1707.0, 1710.0, 1699.0, 1700.5, 50.0))
        assert eng.pos is not None and eng.pending_sig_id is None
        assert eng.acct.state == "IN_POS"
        print("  [D] ARMED 重启：挂单组重建 + 触发成交 通过 ✅")


def case_live_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = {T0 - k * 60: flat(T0 - k * 60) for k in range(30)}
        runner = _runner(tmp, rows)
        eng, state, plan = runner.prepare()
        runner.warm(eng, T0)
        runner.restore(eng, state, plan)
        rows[T0 + 120] = flat(T0 + 120)  # 缺 T0+60
        assert runner.ingest(eng, T0 + 120) == 0 and eng.last_ts == T0
        rows[T0 + 60] = flat(T0 + 60)
        assert runner.ingest(eng, T0 + 120) == 2 and eng.last_ts == T0 + 120
        print("  [E] 实时缺口不跳 bar / 回补后连喂 通过 ✅")


def main() -> None:
    print("T13 数据缺口 + Runner 重启接线：")
    case_engine_gap()
    case_tail()
    case_resume_in_pos()
    case_resume_armed()
    case_live_gap()
    print("T13 全部通过 ✅")


if __name__ == "__main__":
    main()
