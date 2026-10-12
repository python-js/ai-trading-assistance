"""P0-2 历史 K 线回填入口。验证：入库行数、时间范围、缺口检测。
用法（项目根）：.venv/Scripts/python.exe backend/scripts/backfill.py [天数] [周期列表] [合约类型列表]
示例：backend/scripts/backfill.py 365 1h,1d spot
不传参数时用 .env 配置；也可用环境变量临时覆盖。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.models import db


def count_gaps(rows: list, tf_ms: int) -> int:
    return sum(1 for a, b in zip(rows, rows[1:]) if b[0] - a[0] != tf_ms)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else settings.backfill_days
    tfs = sys.argv[2].split(",") if len(sys.argv) > 2 else settings.timeframes
    cts = sys.argv[3].split(",") if len(sys.argv) > 3 else settings.contract_types

    adapter = OKXAdapter(settings.okx_proxy)
    conn = db.connect(settings.pg_dsn)
    tf_ms = {tf: adapter.ex.parse_timeframe(tf) * 1000 for tf in tfs}
    now = adapter.server_time()
    since = now - days * 86400 * 1000
    print(f"回填 {days} 天 | symbols={settings.symbols} | ct={cts} | tf={tfs}")

    for internal in settings.symbols:
        for ct in cts:
            sym = adapter.to_ccxt_symbol(internal, ct)
            if sym not in adapter.ex.markets:
                print(f"  [跳过] {sym} 市场不存在")
                continue
            for tf in tfs:
                rows = adapter.fetch_ohlcv(sym, tf, since=since, paginate=True)
                if not rows:
                    print(f"  [空] {sym} {tf}")
                    continue
                meta = {"symbol": internal, "exchange": adapter.exchange, "contract_type": ct, "timeframe": tf}
                db.upsert_ohlcv(conn, rows, meta)
                gaps = count_gaps(rows, tf_ms[tf])
                n, lo, hi = db.count_ohlcv(conn, internal, adapter.exchange, ct, tf)
                flag = "OK" if gaps == 0 else f"缺口{gaps}"
                print(f"  [{flag}] {sym:16} {tf:3} 拉取{len(rows):6} 库内{n:6} | {lo} ~ {hi}")

    # 资金面数据（仅永续）：funding(8h) + OI(1h 分页)。symbol 用内部规范串（funding/OI 天然属永续，无歧义）。
    if "perp" in cts:
        print("--- 资金面历史（perp）---")
        for internal in settings.symbols:
            sym = adapter.to_ccxt_symbol(internal, "perp")
            if sym not in adapter.ex.markets:
                print(f"  [跳过] {sym} 市场不存在")
                continue
            fh = adapter.fetch_funding_rate_history(sym, since=since, limit=1000)
            db.upsert_funding_history(conn, internal, adapter.exchange, fh)
            oi = adapter.fetch_open_interest_history(sym, "1h", since=since, paginate=True)
            db.upsert_oi_history(conn, internal, adapter.exchange, oi)
            print(f"  [OK] {sym:16} funding={len(fh):4} oi_1h={len(oi):4}")

    conn.close()
    print("回填完成")


if __name__ == "__main__":
    main()
