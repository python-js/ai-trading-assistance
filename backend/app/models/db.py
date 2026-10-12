"""数据库写入层：psycopg3 连接 + upsert（P0-2 / P0-6）。"""
from datetime import datetime, timezone

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb


def connect(dsn: str):
    return psycopg.connect(dsn)


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


_OHLCV_UPSERT = """
INSERT INTO ohlcv (ts, symbol, exchange, contract_type, timeframe,
                   open, high, low, close, volume, quote_volume)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (symbol, exchange, contract_type, timeframe, ts)
DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
              close = EXCLUDED.close, volume = EXCLUDED.volume,
              quote_volume = EXCLUDED.quote_volume
"""

_FUNDING_UPSERT = """
INSERT INTO funding_rate (ts, symbol, exchange, rate, next_funding_ts)
VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (symbol, exchange, ts) DO NOTHING
"""

_OI_UPSERT = """
INSERT INTO open_interest (ts, symbol, exchange, oi, oi_value)
VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (symbol, exchange, ts) DO NOTHING
"""


def upsert_ohlcv(conn, rows: list, meta: dict) -> None:
    """rows: ccxt 原始 [[ms,o,h,l,c,v],...]; meta: symbol/exchange/contract_type/timeframe。"""
    params = [
        (_ts(r[0]), meta["symbol"], meta["exchange"], meta["contract_type"],
         meta["timeframe"], r[1], r[2], r[3], r[4], r[5], None)
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(_OHLCV_UPSERT, params)
    conn.commit()


def upsert_funding(conn, symbol: str, exchange: str, ts_ms: int, rate: float, next_ms: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(_FUNDING_UPSERT, (_ts(ts_ms), symbol, exchange, rate, _ts(next_ms) if next_ms else None))
    conn.commit()


def upsert_open_interest(conn, symbol: str, exchange: str, ts_ms: int, oi: float, oi_value: float | None) -> None:
    with conn.cursor() as cur:
        cur.execute(_OI_UPSERT, (_ts(ts_ms), symbol, exchange, oi, oi_value))
    conn.commit()


def upsert_funding_history(conn, symbol: str, exchange: str, items: list) -> None:
    """items: ccxt fetch_funding_rate_history 结果；历史点无 next_funding_ts。"""
    params = [(_ts(x["timestamp"]), symbol, exchange, x.get("fundingRate"), None) for x in items]
    with conn.cursor() as cur:
        cur.executemany(_FUNDING_UPSERT, params)
    conn.commit()


def upsert_oi_history(conn, symbol: str, exchange: str, items: list) -> None:
    """items: ccxt fetch_open_interest_history 结果。"""
    params = [(_ts(x["timestamp"]), symbol, exchange,
               x.get("openInterestAmount"), x.get("openInterestValue")) for x in items]
    with conn.cursor() as cur:
        cur.executemany(_OI_UPSERT, params)
    conn.commit()


def count_ohlcv(conn, symbol: str, exchange: str, contract_type: str, timeframe: str) -> tuple:
    q = """SELECT count(*), min(ts), max(ts) FROM ohlcv
           WHERE symbol=%s AND exchange=%s AND contract_type=%s AND timeframe=%s"""
    with conn.cursor() as cur:
        cur.execute(q, (symbol, exchange, contract_type, timeframe))
        n, lo, hi = cur.fetchone()
    return n, lo, hi


def load_ohlcv(conn, symbol: str, exchange: str, contract_type: str, timeframe: str,
               start=None, end=None, limit: int | None = None) -> pd.DataFrame:
    """读取 ohlcv 为按 ts 升序的 DataFrame（列：ts, open, high, low, close, volume）。
    limit: 只取最近 N 根（扫描窗口用，先倒序截断再升序返回）。"""
    q = """SELECT ts, open, high, low, close, volume FROM ohlcv
           WHERE symbol=%s AND exchange=%s AND contract_type=%s AND timeframe=%s"""
    params: list = [symbol, exchange, contract_type, timeframe]
    if start is not None:
        q += " AND ts >= %s"
        params.append(start)
    if end is not None:
        q += " AND ts <= %s"
        params.append(end)
    if limit is not None:
        q += " ORDER BY ts DESC LIMIT %s"
        params.append(limit)
    else:
        q += " ORDER BY ts"
    with conn.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values("ts", ignore_index=True) if limit is not None else df


def max_ohlcv_ts(conn, symbol: str, exchange: str, contract_type: str, timeframe: str):
    q = """SELECT max(ts) FROM ohlcv
           WHERE symbol=%s AND exchange=%s AND contract_type=%s AND timeframe=%s"""
    with conn.cursor() as cur:
        cur.execute(q, (symbol, exchange, contract_type, timeframe))
        return cur.fetchone()[0]


# ---------- 规则（P0-6） ----------

_RULE_UPSERT = """
INSERT INTO rule (name, symbol, exchange, contract_type, timeframe, dsl_json, cooldown_bars, enabled, version)
VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
ON CONFLICT (name, version) DO UPDATE SET dsl_json = EXCLUDED.dsl_json, enabled = TRUE
RETURNING id
"""


def upsert_rule(conn, *, name: str, symbol: str, exchange: str, contract_type: str,
                timeframe: str, dsl: dict, cooldown_bars: int = 0, version: int = 1) -> int:
    with conn.cursor() as cur:
        cur.execute(_RULE_UPSERT, (name, symbol, exchange, contract_type, timeframe,
                                   Jsonb(dsl), cooldown_bars, version))
        rid = cur.fetchone()[0]
    conn.commit()
    return rid


def load_enabled_rules(conn) -> list[dict]:
    q = """SELECT id, name, symbol, exchange, contract_type, timeframe, dsl_json, cooldown_bars
           FROM rule WHERE enabled ORDER BY id"""
    with conn.cursor() as cur:
        cur.execute(q)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- 信号（P0-6，唯一索引去重：重启安全） ----------

_SIGNAL_INSERT = """
INSERT INTO signal (rule_id, symbol, exchange, contract_type, timeframe, ts, direction,
                    price_zone_low, price_zone_high, passed)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
ON CONFLICT (rule_id, symbol, contract_type, timeframe, ts, direction) DO NOTHING
RETURNING id
"""


def insert_signal_if_new(conn, *, rule_id: int, symbol: str, exchange: str,
                         contract_type: str, timeframe: str, ts, direction: str,
                         price_zone_low: float | None = None,
                         price_zone_high: float | None = None) -> int | None:
    """插入成功返回 id；已存在（同规则同 bar 同方向）返回 None。"""
    with conn.cursor() as cur:
        cur.execute(_SIGNAL_INSERT, (rule_id, symbol, exchange, contract_type, timeframe,
                                     ts, direction, price_zone_low, price_zone_high))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


# ---------- 推送日志（P0-8） ----------

_PUSH_LOG_INSERT = """
INSERT INTO push_log (signal_id, channel, status, message_id, error)
VALUES (%s,%s,%s,%s,%s)
"""


def insert_push_log(conn, *, signal_id: int, channel: str, status: str,
                    message_id: str | None = None, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(_PUSH_LOG_INSERT, (signal_id, channel, status, message_id, error))
    conn.commit()
