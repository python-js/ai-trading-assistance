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


# ---------- 标的池（P1-5 自助加币） ----------

_SYMBOL_UPSERT = """
INSERT INTO symbol (symbol, exchange, asset_class, contract_type, base, quote,
                    exchange_symbol, is_active)
VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)
ON CONFLICT (exchange, symbol, contract_type)
DO UPDATE SET is_active = TRUE, exchange_symbol = EXCLUDED.exchange_symbol
"""


def upsert_symbol(conn, *, symbol: str, exchange: str, asset_class: str,
                  contract_type: str, base: str, quote: str, exchange_symbol: str) -> None:
    with conn.cursor() as cur:
        cur.execute(_SYMBOL_UPSERT, (symbol, exchange, asset_class, contract_type,
                                     base, quote, exchange_symbol))
    conn.commit()


def load_active_symbols(conn) -> list[dict]:
    q = """SELECT symbol, exchange, contract_type FROM symbol
           WHERE is_active ORDER BY id"""
    with conn.cursor() as cur:
        cur.execute(q)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


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


def load_all_signals(conn) -> list[dict]:
    """全量信号（P1-1 台账配对/报表用），按规则 + bar 时间升序。"""
    q = """SELECT id, rule_id, symbol, exchange, contract_type, timeframe, ts, direction
           FROM signal ORDER BY rule_id, symbol, exchange, contract_type, timeframe, ts, id"""
    with conn.cursor() as cur:
        cur.execute(q)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- 推送日志（P0-8） ----------

_PUSH_LOG_INSERT = """
INSERT INTO push_log (signal_id, channel, status, message_id, error)
VALUES (%s,%s,%s,%s,%s)
"""


def insert_push_log(conn, *, signal_id: int | None, channel: str, status: str,
                    message_id: str | None = None, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(_PUSH_LOG_INSERT, (signal_id, channel, status, message_id, error))
    conn.commit()


# ---------- 资讯（P2-3 A1 信息挖掘） ----------

_NEWS_RAW_INSERT = """
INSERT INTO news_raw (source, url, title, content, published_ts)
VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (url) DO NOTHING
RETURNING id
"""

_NEWS_EVENT_INSERT = """
INSERT INTO news_event (news_id, symbols, event_type, sentiment, impact_dir,
                        magnitude, extracted_by_model, confidence)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
"""


def insert_news_raw_if_new(conn, *, source: str, url: str, title: str,
                           content: str, published_ts) -> int | None:
    """按 url 去重；新条目返回 id，已存在返回 None。"""
    with conn.cursor() as cur:
        cur.execute(_NEWS_RAW_INSERT, (source, url, title, content, published_ts))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def load_unextracted_news(conn, limit: int, sources: tuple[str, ...] | None = None) -> list[dict]:
    """待结构化条目（PIT：published_ts 非空且 ≤ now），新→旧；sources 可限定来源（验证隔离）。"""
    q = """SELECT id, source, title, content, url, published_ts FROM news_raw
           WHERE processed_ts IS NULL AND published_ts IS NOT NULL AND published_ts <= now()"""
    params: list = []
    if sources:
        q += " AND source = ANY(%s)"
        params.append(list(sources))
    q += " ORDER BY published_ts DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(q, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_news_processed(conn, news_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE news_raw SET processed_ts = now() WHERE id = %s", (news_id,))
    conn.commit()


def insert_news_events(conn, news_id: int, events: list[dict], model: str) -> None:
    if not events:
        return
    params = [(news_id, e["symbols"], e["event_type"], e["sentiment"], e["impact_dir"],
               e["magnitude"], model, e["confidence"]) for e in events]
    with conn.cursor() as cur:
        cur.executemany(_NEWS_EVENT_INSERT, params)
    conn.commit()


# ---------- 三票合议（P2-4 S7） ----------

def update_signal_scores(conn, signal_id: int, *, tech, flow, info, total) -> None:
    with conn.cursor() as cur:
        cur.execute("""UPDATE signal SET tech_score=%s, flow_score=%s, info_score=%s,
                       total_score=%s WHERE id=%s""",
                    (tech, flow, info, total, signal_id))
    conn.commit()


def insert_signal_vote(conn, *, signal_id: int, vote_type: str,
                       strength: float | None, evidence: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO signal_vote (signal_id, vote_type, strength, evidence_json)
                       VALUES (%s,%s,%s,%s)""",
                    (signal_id, vote_type, strength, Jsonb(evidence)))
    conn.commit()


# ---------- A5 知识库 RAG（P3） ----------

def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


def insert_knowledge(conn, *, source_type: str, text: str,
                     embedding: list[float], meta: dict) -> int:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO knowledge_chunk (source_type, text, embedding, meta_json)
                       VALUES (%s, %s, %s::vector, %s) RETURNING id""",
                    (source_type, text, _vec(embedding), Jsonb(meta)))
        kid = cur.fetchone()[0]
    conn.commit()
    return kid


def list_knowledge(conn, limit: int = 100, offset: int = 0) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT id, source_type, text, meta_json, created_ts
                       FROM knowledge_chunk ORDER BY id DESC LIMIT %s OFFSET %s""",
                    (limit, offset))
        rows = cur.fetchall()
    return [{"id": r[0], "source_type": r[1], "text": r[2], "meta": r[3],
             "created_ts": r[4].isoformat()} for r in rows]


def delete_knowledge(conn, kid: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge_chunk WHERE id = %s", (kid,))
        n = cur.rowcount
    conn.commit()
    return n > 0


def search_knowledge(conn, embedding: list[float], limit: int = 3) -> list[dict]:
    """余弦距离 Top-N 召回（embedding 列无维度约束，pgvector 顺序扫描）。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT id, source_type, text, meta_json, embedding <=> %s::vector AS dist
                       FROM knowledge_chunk WHERE embedding IS NOT NULL
                       ORDER BY dist LIMIT %s""",
                    (_vec(embedding), limit))
        rows = cur.fetchall()
    return [{"id": r[0], "source_type": r[1], "text": r[2], "meta": r[3],
             "dist": float(r[4])} for r in rows]
