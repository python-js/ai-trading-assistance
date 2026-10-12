"""P1-4 看板 REST API：台账报表 / 持仓 / 信号历史 / K线 / 自选池 / 标注回流。
P1-5：标的搜索与自助加币（ccxt load_markets 实时搜索，入池+绑规则+回填+回放+台账重建）。

只读为主（标注/加币除外）；连接为每请求短连接；K线含 ema20/ema60 与研究同源买卖点标记。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.engine import ledger, report, replay
from app.engine.indicators import enrich
from app.engine.scanner import HISTORY_MIN_BARS, ensure_rules
from app.models import db

router = APIRouter(prefix="/api")

ACTIONS = ("采纳", "忽略", "看错")


def _json_safe(x):
    """inf/NaN → None（盈亏比全胜组为 inf，JSON 不容）。"""
    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_json_safe(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def get_conn():
    conn = db.connect(settings.pg_dsn)
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn, q: str, params: list) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(q, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/health")
def health(conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    return {"ok": True}


@router.get("/report")
def get_report(days: int = Query(14, ge=1, le=365), conn=Depends(get_conn)):
    end = datetime.now(timezone.utc)
    rep = report.build_report(conn, end - timedelta(days=days), end)
    rep["markdown"] = report.render_markdown(rep)
    return _json_safe(rep)


@router.get("/positions")
def positions(status: str = "all", limit: int = Query(200, ge=1, le=1000),
              conn=Depends(get_conn)):
    if status not in ("all", "closed", "open"):
        raise HTTPException(422, "status ∈ all/closed/open")
    q = """SELECT p.id, p.signal_id, p.symbol, r.timeframe, p.status, p.entry_ts,
                  p.entry_price, p.qty, p.exit_ts, p.exit_price, p.pnl, p.pnl_pct,
                  p.max_adverse_excursion
           FROM paper_position p
           JOIN signal s ON s.id = p.signal_id
           JOIN rule r   ON r.id = s.rule_id
           {where}
           ORDER BY COALESCE(p.exit_ts, p.entry_ts) DESC LIMIT %s"""
    where, params = ("", []) if status == "all" else ("WHERE p.status = %s", [status])
    return _rows(conn, q.format(where=where), params + [limit])


@router.get("/signals")
def signals(symbol: str | None = None, timeframe: str | None = None,
            limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
            conn=Depends(get_conn)):
    conds, params = [], []
    if symbol:
        conds.append("g.symbol = %s")
        params.append(symbol)
    if timeframe:
        conds.append("g.timeframe = %s")
        params.append(timeframe)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    q = f"""SELECT g.id, r.name AS rule, g.symbol, g.timeframe, g.contract_type, g.ts,
                   g.direction, g.price_zone_low, g.price_zone_high,
                   (SELECT count(*) FROM push_log pl
                    WHERE pl.signal_id = g.id AND pl.status = 'sent') AS pushed,
                   (SELECT a.user_action FROM annotation a WHERE a.signal_id = g.id
                    ORDER BY a.id DESC LIMIT 1) AS annotation
            FROM signal g JOIN rule r ON r.id = g.rule_id
            {where} ORDER BY g.ts DESC LIMIT %s OFFSET %s"""
    return _rows(conn, q, params + [limit, offset])


@router.get("/ohlcv")
def ohlcv(symbol: str, timeframe: str = "1h", limit: int = Query(500, ge=50, le=2000),
          conn=Depends(get_conn)):
    df = db.load_ohlcv(conn, symbol, "okx", "spot", timeframe, limit=limit)
    if df.empty:
        raise HTTPException(404, f"无数据: {symbol} {timeframe}")
    ind = enrich(df)
    ts = list(ind["ts"])
    bars = [{"time": int(t.timestamp()), "open": float(o), "high": float(h),
             "low": float(l), "close": float(c), "volume": float(v)}
            for t, o, h, l, c, v in zip(ts, ind["open"], ind["high"], ind["low"],
                                        ind["close"], ind["volume"])]
    ema = {k: [{"time": int(t.timestamp()), "value": float(v)}
               for t, v in zip(ts, ind[k]) if math.isfinite(float(v))]
           for k in ("ema20", "ema60")}
    sigs = _rows(conn, """SELECT id, ts, direction, price_zone_low, price_zone_high
                          FROM signal
                          WHERE symbol=%s AND timeframe=%s AND exchange='okx'
                            AND contract_type='spot' AND ts >= %s AND ts <= %s
                          ORDER BY ts""",
                 [symbol, timeframe, ts[0].to_pydatetime(), ts[-1].to_pydatetime()])
    markers = [{"time": int(s["ts"].timestamp()), "direction": s["direction"],
                "price": (s["price_zone_low"] + s["price_zone_high"]) / 2,
                "signal_id": s["id"]} for s in sigs]
    return {"bars": bars, "ema20": ema["ema20"], "ema60": ema["ema60"], "signals": markers}


@router.get("/symbols")
def get_symbols(conn=Depends(get_conn)):
    rows = _rows(conn, """
        SELECT r.symbol, r.timeframe, count(*) AS n_rules,
               (SELECT max(o.ts) FROM ohlcv o
                WHERE o.symbol = r.symbol AND o.timeframe = r.timeframe
                  AND o.exchange = 'okx' AND o.contract_type = 'spot') AS last_ts
        FROM rule r WHERE r.enabled
        GROUP BY r.symbol, r.timeframe ORDER BY r.symbol, r.timeframe""", [])
    pool: dict[str, list] = {}
    for r in rows:
        pool.setdefault(r["symbol"], []).append(
            {"timeframe": r["timeframe"], "n_rules": r["n_rules"], "last_ts": r["last_ts"]})
    return [{"symbol": k, "periods": v} for k, v in pool.items()]


# ---------- 自助加币（P1-5） ----------

ALLOWED_TFS = ("1m", "1h", "4h")

_adapter_lock = threading.Lock()
_adapter_cache: dict = {}


def _adapter() -> OKXAdapter:
    """OKXAdapter 懒加载单例（load_markets 昂贵，搜索/加币复用）。"""
    with _adapter_lock:
        if "okx" not in _adapter_cache:
            _adapter_cache["okx"] = OKXAdapter(settings.okx_proxy)
        return _adapter_cache["okx"]


@router.get("/symbols/search")
def search_symbols(q: str = Query(..., min_length=1, max_length=20), conn=Depends(get_conn)):
    ad = _adapter()
    ql = q.strip().upper()
    hits = [s for s, m in ad.ex.markets.items()
            if m.get("spot") and m.get("quote") == "USDT" and ql in s.upper()]
    hits.sort(key=lambda s: (not s.startswith(ql + "/"), s))
    in_pool = set(settings.symbols) | {r["symbol"] for r in db.load_active_symbols(conn)}
    return [{"symbol": s, "in_pool": s in in_pool} for s in hits[:20]]


class SymbolIn(BaseModel):
    symbol: str
    timeframes: list[str] = ["1h", "4h"]


@router.post("/symbols", status_code=201)
def add_symbol(body: SymbolIn, conn=Depends(get_conn)):
    """入 symbol 表 → 绑默认规则 → 回填 K 线 → 历史信号回放 → 台账重建（全部幂等）。

    扫描器每 tick 重读规则与池，≤1 个周期自动纳入（无需重启）。
    """
    sym = body.symbol.strip().upper()
    tfs = tuple(dict.fromkeys(t for t in body.timeframes if t in ALLOWED_TFS))
    if not tfs:
        raise HTTPException(422, "timeframes 非空且 ∈ 1m/1h/4h")
    ad = _adapter()
    m = ad.ex.markets.get(sym)
    if not (m and m.get("spot")):
        raise HTTPException(404, f"OKX 现货市场不存在: {sym}")
    db.upsert_symbol(conn, symbol=sym, exchange="okx", asset_class="crypto",
                     contract_type="spot", base=m["base"], quote=m["quote"],
                     exchange_symbol=sym)
    ensure_rules(conn, sym, tfs)
    bars = 0
    for tf in tfs:
        n, _, _ = db.count_ohlcv(conn, sym, "okx", "spot", tf)
        if n >= HISTORY_MIN_BARS:
            continue
        tf_ms = ad.ex.parse_timeframe(tf) * 1000
        need_days = max(1, (HISTORY_MIN_BARS + 20) * tf_ms // 86_400_000 + 1)
        since = ad.server_time() - need_days * 86400 * 1000
        rows = ad.fetch_ohlcv(sym, tf, since=since, paginate=True)
        if rows:
            db.upsert_ohlcv(conn, rows, {"symbol": sym, "exchange": "okx",
                                         "contract_type": "spot", "timeframe": tf})
            bars += len(rows)
    signals = replay.backfill(conn, symbol=sym)
    stats = ledger.rebuild(conn)
    return {"symbol": sym, "timeframes": list(tfs), "bars": bars, "signals": signals,
            "ledger": {"total": stats["total"], "closed": stats["closed"]}}


class AnnotationIn(BaseModel):
    signal_id: int
    user_action: str
    note: str | None = None


@router.post("/annotations", status_code=201)
def add_annotation(body: AnnotationIn, conn=Depends(get_conn)):
    if body.user_action not in ACTIONS:
        raise HTTPException(422, f"user_action ∈ {'/'.join(ACTIONS)}")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM signal WHERE id = %s", (body.signal_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"signal 不存在: {body.signal_id}")
        cur.execute("""INSERT INTO annotation (signal_id, user_action, note)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (body.signal_id, body.user_action, body.note))
        aid = cur.fetchone()[0]
    conn.commit()
    return {"id": aid}


@router.get("/annotations")
def get_annotations(signal_id: int, conn=Depends(get_conn)):
    return _rows(conn, """SELECT id, signal_id, user_action, note, created_ts
                          FROM annotation WHERE signal_id = %s ORDER BY id DESC""",
                 [signal_id])
