"""P1-1 信号历史回填：对已入库 K 线确定性回放（同一 evaluate 函数），补登历史信号。

实时态由扫描器逐 bar 写入；本脚本把规则在历史 bar 上产生的信号一次性补登，
使台账/周报能覆盖"过去两周"。重复运行安全（uq_signal_dedupe 去重，不覆盖已推送行）。

用法：.venv/Scripts/python.exe backend/scripts/backfill_signals.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd

from app.core.config import settings
from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate, parse_rule
from app.models import db


def backfill(conn) -> int:
    """返回新增信号总数。"""
    total_new = 0
    for r in db.load_enabled_rules(conn):
        df = db.load_ohlcv(conn, r["symbol"], r["exchange"], r["contract_type"], r["timeframe"])
        if len(df) < WARMUP_BARS + 2:
            print(f"  [跳过] {r['name']:28} 样本不足({len(df)})")
            continue
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
        total_new += new
        print(f"  [回填] {r['name']:28} 事件 {len(events):4}  新增 {new:4}  已存在 {len(events) - new:4}")
    return total_new


if __name__ == "__main__":
    conn = db.connect(settings.pg_dsn)
    n = backfill(conn)
    conn.close()
    print(f"合计新增信号: {n}")
