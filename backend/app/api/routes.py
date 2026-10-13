"""P1-4 看板 REST API：台账报表 / 持仓 / 信号历史 / K线 / 自选池 / 标注回流。
P1-5：标的搜索与自助加币（ccxt load_markets 实时搜索，入池+绑规则+回填+回放+台账重建）。

只读为主（标注/加币除外）；连接为每请求短连接；K线含 sma5/sma10/sma20（MA5/10/20）与研究同源买卖点标记。
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.adapters.llm import gateway
from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.engine import backtest_event, ledger, rag, report, replay, rule_gen
from app.engine.indicators import enrich
from app.engine.scanner import HISTORY_MIN_BARS, ensure_rules, last_closed_ms
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


def _with_live_bar(df, symbol: str, timeframe: str, ct: str):
    """展示层增强：并入 OKX 当前未收盘 bar（不入库，仅本次响应）；任何失败静默回落已收盘数据。"""
    try:
        ad = _adapter()
        rows = ad.fetch_ohlcv(ad.to_ccxt_symbol(symbol, ct), timeframe, limit=2)
        if not rows:
            return df
        ts_ms, o, h, l, c, v = rows[-1]
        if ts_ms <= int(df["ts"].iloc[-1].timestamp() * 1000):
            return df
        live = pd.DataFrame({"ts": [pd.Timestamp(ts_ms, unit="ms", tz="UTC")],
                             "open": [o], "high": [h], "low": [l], "close": [c],
                             "volume": [v]})
        return pd.concat([df, live], ignore_index=True)
    except Exception:  # 盘中 bar 仅展示层：外呼失败不得影响已收盘数据返回
        return df


@router.get("/ohlcv")
def ohlcv(symbol: str, timeframe: str = "1h", limit: int = Query(500, ge=50, le=2000),
          live: bool = False, conn=Depends(get_conn)):
    meta = _rows(conn, """SELECT exchange, contract_type FROM ohlcv
                          WHERE symbol=%s AND timeframe=%s LIMIT 1""", [symbol, timeframe])
    if not meta:
        raise HTTPException(404, f"无数据: {symbol} {timeframe}")
    ex, ct = meta[0]["exchange"], meta[0]["contract_type"]
    df = db.load_ohlcv(conn, symbol, ex, ct, timeframe, limit=limit)
    if df.empty:
        raise HTTPException(404, f"无数据: {symbol} {timeframe}")
    if live and ex == "okx":
        df = _with_live_bar(df, symbol, timeframe, ct)
    ind = enrich(df)
    ts = list(ind["ts"])
    bars = [{"time": int(t.timestamp()), "open": float(o), "high": float(h),
             "low": float(l), "close": float(c), "volume": float(v)}
            for t, o, h, l, c, v in zip(ts, ind["open"], ind["high"], ind["low"],
                                        ind["close"], ind["volume"])]
    ma = {k: [{"time": int(t.timestamp()), "value": float(v)}
              for t, v in zip(ts, ind[k]) if math.isfinite(float(v))]
          for k in ("sma5", "sma10", "sma20")}
    sigs = _rows(conn, """SELECT id, ts, direction, price_zone_low, price_zone_high
                          FROM signal
                          WHERE symbol=%s AND timeframe=%s AND exchange=%s
                            AND contract_type=%s AND ts >= %s AND ts <= %s
                          ORDER BY ts""",
                 [symbol, timeframe, ex, ct, ts[0].to_pydatetime(), ts[-1].to_pydatetime()])
    markers = [{"time": int(s["ts"].timestamp()), "direction": s["direction"],
                "price": (s["price_zone_low"] + s["price_zone_high"]) / 2,
                "signal_id": s["id"]} for s in sigs]
    return {"bars": bars, "sma5": ma["sma5"], "sma10": ma["sma10"],
            "sma20": ma["sma20"], "signals": markers}


@router.get("/symbols")
def get_symbols(conn=Depends(get_conn)):
    rows = _rows(conn, """
        SELECT r.symbol, r.timeframe, count(*) AS n_rules,
               (SELECT max(o.ts) FROM ohlcv o
                WHERE o.symbol = r.symbol AND o.timeframe = r.timeframe
                  AND o.exchange = r.exchange AND o.contract_type = r.contract_type) AS last_ts
        FROM rule r WHERE r.enabled
        GROUP BY r.symbol, r.timeframe, r.exchange, r.contract_type
        ORDER BY r.symbol, r.timeframe""", [])
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
        now_ms = ad.server_time()
        since = now_ms - need_days * 86400 * 1000
        rows = ad.fetch_ohlcv(sym, tf, since=since, paginate=True)
        rows = [r for r in rows if r[0] <= last_closed_ms(now_ms, tf_ms)]
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
        cur.execute("""SELECT s.symbol, s.timeframe, s.direction, r.name
                       FROM signal s LEFT JOIN rule r ON r.id = s.rule_id
                       WHERE s.id = %s""", (body.signal_id,))
        sig = cur.fetchone()
        if not sig:
            raise HTTPException(404, f"signal 不存在: {body.signal_id}")
        cur.execute("""INSERT INTO annotation (signal_id, user_action, note)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (body.signal_id, body.user_action, body.note))
        aid = cur.fetchone()[0]
    conn.commit()
    # A5：笔记非空 → 向量化入知识库（失败只告警，不阻塞标注）
    if body.note and body.note.strip():
        try:
            text = (f"[{sig[0]} {sig[1]} {'买入' if sig[2] == 'entry' else '卖出'}] "
                    f"标注「{body.user_action}」：{body.note.strip()}")
            rag.add_chunk(conn, text, source_type="note",
                          meta={"signal_id": body.signal_id, "action": body.user_action})
        except Exception as e:
            print(f"[知识] 标注笔记入库跳过（{type(e).__name__}: {str(e)[:60]}）", flush=True)
    return {"id": aid}


@router.get("/annotations")
def get_annotations(signal_id: int, conn=Depends(get_conn)):
    return _rows(conn, """SELECT id, signal_id, user_action, note, created_ts
                          FROM annotation WHERE signal_id = %s ORDER BY id DESC""",
                 [signal_id])


# ---------- A5 个人研判知识库（P3） ----------

class KnowledgeIn(BaseModel):
    text: str


@router.post("/knowledge", status_code=201)
def add_knowledge(body: KnowledgeIn, conn=Depends(get_conn)):
    """手动知识条目 → 向量化入库。"""
    text = body.text.strip()
    if not text or len(text) > 2000:
        raise HTTPException(422, "text 非空且 ≤2000 字")
    try:
        kid = rag.add_chunk(conn, text, source_type="manual")
    except gateway.LlmError as e:
        raise HTTPException(503, f"向量化失败（LLM 不可用）：{e}")
    return {"id": kid}


@router.get("/knowledge")
def get_knowledge(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                  conn=Depends(get_conn)):
    return db.list_knowledge(conn, limit=limit, offset=offset)


@router.delete("/knowledge/{kid}")
def remove_knowledge(kid: int, conn=Depends(get_conn)):
    if not db.delete_knowledge(conn, kid):
        raise HTTPException(404, f"知识不存在: {kid}")
    return {"ok": True}


# ---------- A3 自然语言→规则草稿 / 规则管理 / 沙箱回测（P3） ----------

class RuleDraftIn(BaseModel):
    text: str
    symbol: str
    timeframe: str


@router.post("/rules/draft", status_code=201)
def create_rule_draft(body: RuleDraftIn, conn=Depends(get_conn)):
    """中文想法 → LLM 生成 DSL（校验+回喂重试）→ 草稿入库（enabled=false，人审=开关）。"""
    text = body.text.strip()
    if not text or len(text) > 500:
        raise HTTPException(422, "text 非空且 ≤500 字")
    meta = _rows(conn, """SELECT exchange, contract_type FROM ohlcv
                          WHERE symbol=%s AND timeframe=%s LIMIT 1""",
                 [body.symbol, body.timeframe])
    if not meta:
        raise HTTPException(404, f"无数据: {body.symbol} {body.timeframe}")
    try:
        gen = rule_gen.generate(text, body.symbol, body.timeframe)
    except rule_gen.RuleGenError as e:
        raise HTTPException(422, f"生成未通过校验（已重试一次）：{e}")
    except gateway.LlmError as e:
        raise HTTPException(503, f"LLM 不可用：{e}")
    name = f"gen_{body.symbol}_{body.timeframe}_{int(time.time() * 1000)}"
    dsl = {"name": name, "symbol": body.symbol, "timeframe": body.timeframe, **gen}
    rid = db.insert_draft_rule(conn, name=name, symbol=body.symbol,
                               exchange=meta[0]["exchange"],
                               contract_type=meta[0]["contract_type"],
                               timeframe=body.timeframe, dsl=dsl,
                               cooldown_bars=gen["cooldown_bars"])
    return {"id": rid, "name": name, "dsl": dsl}


@router.get("/rules")
def get_rules(limit: int = Query(200, ge=1, le=500), conn=Depends(get_conn)):
    return db.list_rules(conn, limit=limit)


class RuleToggleIn(BaseModel):
    enabled: bool


@router.post("/rules/{rid}/toggle")
def toggle_rule(rid: int, body: RuleToggleIn, conn=Depends(get_conn)):
    if not db.set_rule_enabled(conn, rid, body.enabled):
        raise HTTPException(404, f"规则不存在: {rid}")
    return {"id": rid, "enabled": body.enabled}


@router.post("/rules/{rid}/backtest")
def backtest_rule(rid: int, days: int = Query(120, ge=5, le=730), conn=Depends(get_conn)):
    """沙箱回测（同步：台账口径事件驱动，默认最近 120 天；只算不写）。"""
    r = db.get_rule(conn, rid)
    if not r:
        raise HTTPException(404, f"规则不存在: {rid}")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        res = backtest_event.run_rule(conn, r, start=start, end=end)
    except ValueError as e:
        raise HTTPException(422, f"回测不可执行：{e}")
    trades = [{**t, "entry_ts": t["entry_ts"].isoformat(),
               "exit_ts": t["exit_ts"].isoformat()} for t in res["trades"]]
    open_pos = ({**res["open_pos"],
                 "entry_ts": res["open_pos"]["entry_ts"].isoformat()}
                if res["open_pos"] else None)
    return _json_safe({"id": rid, "name": r["name"], "symbol": r["symbol"],
                       "timeframe": r["timeframe"], "days": days,
                       "metrics": res["metrics"], "n_events": res["n_events"],
                       "trades": trades, "open_pos": open_pos})
