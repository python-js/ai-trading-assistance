"""M0：回补 MRS 依赖数据（OKX BTC-USDT-SWAP）。

- 1m：目标 24 个月（§9.6 WFA 6 月拟合/2 月外推 ×≥3 折）；从库内最早 ts 反向分页拉，
  断点续传（重复运行从当前最早继续向过去补）、幂等（upsert）。
- funding：OKX 端点仅保留近 3 个月，能拉多少拉多少（§9.2 更早段落回测按 imputed=0 标记）。
- --check：仅输出统计（行数/边界/>1min 缺口数），不联网回补。

用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/backfill_mrs.py          # 回补 + 统计
  .venv/Scripts/python.exe backend/scripts/backfill_mrs.py --check  # 仅统计
"""
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.mrs.config import CFG, cfg_hash, validate_cfg, verify_venue
from app.models import db

TARGET_DAYS = 730  # 24 个月
PASS = 100         # OKX history-candles 单请求上限（实测）
FLUSH = 50         # 每 50 批（5000 根）落地一次
STOP_EMPTY = 3     # 连续空返回停止（探测更早段，保守）


def _dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def backfill_1m(adapter: OKXAdapter, conn) -> None:
    meta = {"symbol": CFG["symbol"]["internal"], "exchange": CFG["symbol"]["exchange"],
            "contract_type": CFG["symbol"]["contract_type"], "timeframe": "1m"}
    ccxt_sym = CFG["symbol"]["ccxt"]
    now_ms = adapter.server_time()
    floor_ms = now_ms - TARGET_DAYS * 86_400_000
    n0, lo, hi = db.count_ohlcv(conn, meta["symbol"], meta["exchange"], meta["contract_type"], "1m")
    print(f"[回补/1m] 现有 {n0} 根 | {lo} → {hi}")
    cursor = int(lo.timestamp() * 1000) if lo else now_ms
    if cursor <= floor_ms:
        print("[回补/1m] 已覆盖目标窗口，跳过")
        return

    buf: list = []
    done, empty = 0, 0
    t0 = time.time()
    while cursor > floor_ms and empty < STOP_EMPTY:
        since = cursor - PASS * 60_000
        rows = adapter.fetch_ohlcv(ccxt_sym, "1m", since=since, limit=PASS)
        rows = [r for r in rows if r[0] < cursor]
        if not rows:
            empty += 1
            cursor = since
            continue
        empty = 0
        buf += rows
        done += len(rows)
        cursor = rows[0][0]
        if len(buf) >= FLUSH * PASS:
            db.upsert_ohlcv(conn, buf, meta)
            buf = []
            speed = done / max(time.time() - t0, 1e-6)
            eta_min = (cursor - floor_ms) / 60_000 / speed / 60 if speed else 0
            print(f"  +{done} 根 | 游标 {_dt(cursor)} | {speed:.0f} 根/s | ETA {eta_min:.0f}min",
                  flush=True)
    if buf:
        db.upsert_ohlcv(conn, buf, meta)
    print(f"[回补/1m] 完成：新增/刷新 {done} 根，用时 {(time.time() - t0) / 60:.1f} 分钟")


def backfill_funding(adapter: OKXAdapter, conn) -> None:
    now_ms = adapter.server_time()
    fh = adapter.fetch_funding_rate_history(CFG["symbol"]["ccxt"],
                                            since=now_ms - 100 * 86_400_000, limit=1000)
    if fh:
        db.upsert_funding_history(conn, CFG["symbol"]["internal"],
                                  CFG["symbol"]["exchange"], fh)
    print(f"[回补/funding] {len(fh)} 条（OKX 端点仅保留近 3 个月，更早段回测按 imputed 标记）")


def report(conn) -> None:
    sym, ex_, ct = CFG["symbol"]["internal"], CFG["symbol"]["exchange"], CFG["symbol"]["contract_type"]
    n, lo, hi = db.count_ohlcv(conn, sym, ex_, ct, "1m")
    print(f"[统计/1m] {n} 根 | {lo} → {hi}")
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM (
                         SELECT ts - lag(ts) OVER (ORDER BY ts) AS gap FROM ohlcv
                         WHERE symbol=%s AND exchange=%s AND contract_type=%s AND timeframe='1m'
                       ) t WHERE gap > interval '1 minute'""", (sym, ex_, ct))
        gaps = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM funding_rate WHERE symbol=%s AND exchange=%s""",
                    (sym, ex_))
        nf = cur.fetchone()[0]
    print(f"[统计/缺口] 1m 中 >1min 间隔数: {gaps} | funding 行数: {nf}")


def main() -> None:
    check_only = "--check" in sys.argv
    validate_cfg()
    conn = db.connect(settings.pg_dsn)
    try:
        if not check_only:
            adapter = OKXAdapter(settings.okx_proxy)
            verify_venue(adapter)
            print(f"[校验] venue tick/lot 与交易所一致 | cfg_hash={cfg_hash()}")
            backfill_1m(adapter, conn)
            backfill_funding(adapter, conn)
        report(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
