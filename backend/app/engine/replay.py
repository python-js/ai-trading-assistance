"""信号历史回放（P1-1 提取，P1-5 加币复用）：对已入库 K 线用同一 evaluate 确定性回放。

实时态由扫描器逐 bar 写入；本模块把规则在历史 bar 上产生的信号一次性补登，
使台账/周报/看板能覆盖历史。重复运行安全（uq_signal_dedupe 去重，不覆盖已推送行）。
"""
from __future__ import annotations

import pandas as pd

from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate, parse_rule
from app.models import db


def replay_rule(conn, r: dict) -> tuple[int, int]:
    """对单条规则回放；返回 (事件数, 新增数)。样本不足跳过并返回 (0, 0)。"""
    df = db.load_ohlcv(conn, r["symbol"], r["exchange"], r["contract_type"], r["timeframe"])
    if len(df) < WARMUP_BARS + 2:
        print(f"  [跳过] {r['name']:28} 样本不足({len(df)})")
        return 0, 0
    ind = enrich(df)
    atr_by_ts = {pd.Timestamp(t): a for t, a in zip(ind["ts"], ind["atr14"])}
    events = evaluate(parse_rule(r["dsl_json"]), ind)
    new = 0
    for e in events:
        ts = pd.Timestamp(e.ts)
        a = float(atr_by_ts[ts])
        sid = db.insert_signal_if_new(
            conn, rule_id=r["id"], symbol=r["symbol"], exchange=r["exchange"],
            contract_type=r["contract_type"], timeframe=r["timeframe"],
            ts=ts.to_pydatetime(), direction=e.direction,
            price_zone_low=e.price - 0.5 * a, price_zone_high=e.price + 0.5 * a)
        new += sid is not None
    print(f"  [回放] {r['name']:28} 事件 {len(events):4}  新增 {new:4}  已存在 {len(events) - new:4}")
    return len(events), new


def backfill(conn, symbol: str | None = None) -> int:
    """全部启用规则（或指定标的）回放，返回新增信号总数。"""
    total_new = 0
    for r in db.load_enabled_rules(conn):
        if symbol and r["symbol"] != symbol:
            continue
        total_new += replay_rule(conn, r)[1]
    return total_new
