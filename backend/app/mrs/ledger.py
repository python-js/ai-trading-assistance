"""MRS 日志层（规格书 §8）：mrs_signal / mrs_funnel / mrs_trade / mrs_orders.jsonl + mrs_state.json。

- 内存记录始终保留（回测/报表用）；out_dir 给出时同步追加落盘，signal/trade/orders 每条 fsync（§8）。
- funnel 逐 1m 收盘一行、是拒单计数唯一来源（§8.2）；stop_at 取值集合在本文件常量登记。
- mrs_state.json 原子写（tmp + os.replace，§8.5）；replace 对 Windows 瞬态文件锁
  （读句柄/杀软占用 → WinError 5）做有界重试（10×100ms），超限抛错。
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

# §8.2 枚举 + v1 扩展（登记）：BUSY=持仓/挂单中未评估、OK=当 bar 通过全闸门已挂单、
# NO_FILL=TTL 撤单、HALT/NO_DATA=横切态、TRADES_CAP=日笔数上限。
STOP_AT = {"SESSION", "EVENT", "REGIME", "RESON", "ZONE", "COOLDOWN", "SPREAD", "VOL",
           "NO_TRIGGER", "PLAN", "NO_FILL", "BUSY", "OK", "HALT", "NO_DATA", "TRADES_CAP"}

_MRS_VER = "1.0"


class Ledger:
    def __init__(self, out_dir: str | Path | None = None):
        self.out_dir = Path(out_dir) if out_dir else None
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        self.funnel_counts: Counter = Counter()
        self.plan_reject_counts: Counter = Counter()  # §9.5 PLAN 拒绝原因分布（COST 等）
        self.signals: list[dict] = []
        self.trades: list[dict] = []   # init/exit 两类行，trade_id 配对
        self.orders: list[dict] = []

    # ---------- 落盘 ----------
    def _append(self, name: str, rec: dict, fsync: bool) -> None:
        if not self.out_dir:
            return
        with open(self.out_dir / f"{name}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if fsync:
                os.fsync(f.fileno())

    def signal(self, rec: dict) -> None:
        rec.setdefault("mrs_ver", _MRS_VER)
        self.signals.append(rec)
        self._append("mrs_signal", rec, fsync=True)

    def funnel(self, ts: int, stop_at: str, plan_reject: str | None = None,
               detail: dict | None = None) -> None:
        assert stop_at in STOP_AT, f"未登记 funnel 标签: {stop_at}"
        self.funnel_counts[stop_at] += 1
        if plan_reject:
            self.plan_reject_counts[plan_reject] += 1
        if self.out_dir:
            self._append("mrs_funnel", {"ts": ts, "stop_at": stop_at,
                                        "plan_reject": plan_reject,
                                        "detail": detail or {}}, fsync=False)

    def trade(self, rec: dict) -> None:
        self.trades.append(rec)
        self._append("mrs_trade", rec, fsync=True)

    def order(self, rec: dict) -> None:
        self.orders.append(rec)
        self._append("mrs_orders", rec, fsync=True)

    def dump_state(self, obj: dict) -> None:
        if not self.out_dir:
            return
        tmp = self.out_dir / "mrs_state.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, self.out_dir / "mrs_state.json")
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
