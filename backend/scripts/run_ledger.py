"""P1-1 台账入口：重建 paper_position 并输出概览。

用法：.venv/Scripts/python.exe backend/scripts/run_ledger.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.engine import ledger
from app.models import db

_RECENT = """
SELECT p.symbol, r.timeframe, p.status, p.entry_ts, p.entry_price,
       p.exit_ts, p.exit_price, p.pnl_pct
FROM paper_position p
JOIN signal s ON s.id = p.signal_id
JOIN rule r   ON r.id = s.rule_id
ORDER BY p.entry_ts DESC LIMIT 8
"""


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    stats = ledger.rebuild(conn)
    print(f"台账重建：共 {stats['total']} 笔（已平 {stats['closed']} / 持仓中 {stats['open']}）")
    with conn.cursor() as cur:
        cur.execute(_RECENT)
        rows = cur.fetchall()
    print("最近 8 笔：")
    for sym, tf, st, ets, epr, xts, xpr, pnl in rows:
        tail = (f" → {xpr:.6g} @ {xts:%m-%d %H:%M}  pnl {pnl:+.2%}"
                if st == "closed" else "  （持仓中）")
        print(f"  {sym:10} {tf:3} {'已平' if st == 'closed' else '持仓'}"
              f"  {epr:.6g} @ {ets:%m-%d %H:%M}{tail}")
    conn.close()


if __name__ == "__main__":
    main()
