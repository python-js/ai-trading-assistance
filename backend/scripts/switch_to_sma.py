"""SMA 迁移（一次性，幂等可重跑）：生产均线口径 EMA20/60 → SMA5/10/20（对齐 OKX MA5/10/20）。

1) 镜像现有启用 ema_2060_* 规则的覆盖面（symbol×tf）建立 sma_520_* 规则（复用 scanner.ensure_rules 模板）；
2) 停用全部 ema_2060_* 旧规则（保留行与历史信号，仅 enabled=FALSE，供审计）；
3) 对新启用规则确定性回放补登历史信号（replay.backfill，uq_signal_dedupe 去重）；
4) 重建台账（ledger.rebuild，幂等）。

须在扫描器停机窗口内执行（旧进程 enrich 不产出 sma 列，停机避免评估缺列报错）；
扫描器下次启动 seed_rules 会按当前池幂等补齐，本脚本无需感知池外新标的。
用法：.venv/Scripts/python.exe backend/scripts/switch_to_sma.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.core.config import settings
from app.engine import ledger, replay
from app.engine.scanner import ensure_rules
from app.models import db


def migrate(conn) -> dict:
    # ① 覆盖面镜像：旧规则按 (symbol, exchange, contract_type) 分组 → 建 sma 规则
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT symbol, exchange, contract_type, timeframe
                       FROM rule WHERE enabled AND name LIKE 'ema_2060%'""")
        rows = cur.fetchall()
    by_key: dict[tuple, list[str]] = {}
    for sym, ex, ct, tf in rows:
        by_key.setdefault((sym, ex, ct), []).append(tf)
    for (sym, ex, ct), tfs in by_key.items():
        ensure_rules(conn, sym, tuple(sorted(tfs)), exchange=ex, ct=ct)
    # ② 停用旧规则（不删）——先建后停，回填时仅新规则在场
    with conn.cursor() as cur:
        cur.execute("UPDATE rule SET enabled=FALSE WHERE name LIKE 'ema_2060%' AND enabled")
        off = cur.rowcount
        cur.execute("SELECT count(*) FROM rule WHERE name LIKE 'sma_520%' AND enabled")
        on = cur.fetchone()[0]
    conn.commit()
    # ③ 历史信号确定性回填（仅启用规则）+ ④ 台账重建
    n_new = replay.backfill(conn)
    stats = ledger.rebuild(conn)
    return {"groups": len(by_key), "legacy_off": off, "sma_on": on,
            "signals_new": n_new, **stats}


if __name__ == "__main__":
    conn = db.connect(settings.pg_dsn)
    r = migrate(conn)
    conn.close()
    print(f"迁移完成：覆盖面 {r['groups']} 组 | 旧规则停用 {r['legacy_off']} 条 | "
          f"新规则启用 {r['sma_on']} 条 | 回填信号 +{r['signals_new']} | "
          f"台账 {r['total']} 笔（已平 {r['closed']} / 持仓 {r['open']}）")
