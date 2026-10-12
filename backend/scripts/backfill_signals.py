"""P1-1 信号历史回填 CLI：对已入库 K 线确定性回放（逻辑见 app/engine/replay.py）。

实时态由扫描器逐 bar 写入；本脚本把规则在历史 bar 上产生的信号一次性补登，
使台账/周报能覆盖"过去两周"。重复运行安全（uq_signal_dedupe 去重，不覆盖已推送行）。

用法：.venv/Scripts/python.exe backend/scripts/backfill_signals.py [SYMBOL]
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.engine.replay import backfill
from app.models import db

if __name__ == "__main__":
    conn = db.connect(settings.pg_dsn)
    n = backfill(conn, symbol=sys.argv[1] if len(sys.argv) > 1 else None)
    conn.close()
    print(f"合计新增信号: {n}")
