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
from app.engine import backtest_event, ledger, rag, report, replay, rule_gen, sim
from app.engine.indicators import enrich
from app.engine.scanner import (HISTORY_MIN_BARS, ensure_rules, last_closed_ms,
                                rule_templates)
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
    for v in pool.values():  # 周期顺序跟随报表口径（1m→5m→15m→1h→4h→1d）
        v.sort(key=lambda p: report.TF_ORDER.get(p["timeframe"], len(report.TF_ORDER)))
    return [{"symbol": k, "periods": v} for k, v in pool.items()]


# ---------- 自助加币（P1-5） ----------

ALLOWED_TFS = ("1m", "5m", "15m", "1h", "4h")

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
        raise HTTPException(422, "timeframes 非空且 ∈ 1m/5m/15m/1h/4h")
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


# ---------- 模拟盘（A6：共享资金池 · 固定比例复利 · 止损线/到期冻结） ----------

class SimUnitIn(BaseModel):
    symbol: str
    timeframe: str
    rule_id: int | None = None  # 选填：绑定现有规则（登记时快照 DSL）；缺省用 sma_520 模板


class SimIn(BaseModel):
    name: str
    init_cash: float
    size_pct: float = 30.0       # 每笔用当前权益的 %（复利）
    stop_loss_pct: float = 50.0  # 权益回撤止损线 %；0=关闭
    start_days_ago: int = 0      # 0=从创建时刻起跟随；>0=过去起点（立即回放出完整结果）
    duration_days: int | None = None  # 从创建时刻起 N 天后到期；None=不限
    units: list[SimUnitIn]


def _default_dsl(tf: str, sym: str):
    tpl = {t: (e, x, cd) for t, e, x, cd in rule_templates((tf,))}
    if tf not in tpl:
        raise HTTPException(422, f"周期 {tf} 无默认策略模板")
    e, x, cd = tpl[tf]
    name = f"sma_520_{tf}"
    return name, {"name": name, "symbol": sym, "timeframe": tf,
                  "entry": e, "exit": x, "cooldown_bars": cd}


def _pick_strategy(conn, sym: str, tf: str, rule_id: int | None):
    """策略快照：rule_id → 规则库存量 DSL；缺省 → sma_520 模板。返回 (name, dsl)。"""
    if rule_id is None:
        return _default_dsl(tf, sym)
    r = db.get_rule(conn, rule_id)
    if not r:
        raise HTTPException(404, f"规则不存在: {rule_id}")
    if r["symbol"] != sym or r["timeframe"] != tf:
        raise HTTPException(422, f"规则 {r['name']} 属 {r['symbol']} {r['timeframe']}，与单元 {sym} {tf} 不符")
    return r["name"], r["dsl_json"]


def _sim_acct(conn, aid: int) -> dict:
    """组装重放入参；不存在抛 404。"""
    a = db.get_sim_account(conn, aid)
    if not a:
        raise HTTPException(404, f"模拟盘不存在: {aid}")
    units = a.pop("units_json")
    by_unit: dict[int, list] = {}
    for c in db.load_sim_changes(conn, aid):
        by_unit.setdefault(c["unit_idx"], []).append(c)
    for i, u in enumerate(units):
        u["changes"] = by_unit.get(i, [])
    return {**a, "units": units}


@router.post("/sim", status_code=201)
def create_sim(body: SimIn, conn=Depends(get_conn)):
    """创建一个模拟盘：登记标的/周期与初始策略快照（同一市场、共享一份资金池）。"""
    name = body.name.strip()
    if not name or len(name) > 50:
        raise HTTPException(422, "name 非空且 ≤50 字")
    if body.init_cash <= 0:
        raise HTTPException(422, "init_cash 须 > 0")
    if not 0 < body.size_pct <= 100:
        raise HTTPException(422, "size_pct ∈ (0,100]")
    if not 0 <= body.stop_loss_pct < 100:
        raise HTTPException(422, "stop_loss_pct ∈ [0,100)：0=关闭止损线")
    if not body.units:
        raise HTTPException(422, "units 非空（至少 1 个标的）")
    if not 0 <= body.start_days_ago <= 730:
        raise HTTPException(422, "start_days_ago ∈ [0,730]")
    if body.duration_days is not None and not 0 < body.duration_days <= 730:
        raise HTTPException(422, "duration_days ∈ (0,730]")
    units, markets, seen = [], set(), set()
    for u in body.units:
        sym, tf = u.symbol.strip(), u.timeframe.strip()
        if (sym, tf) in seen:
            raise HTTPException(422, f"标的重复: {sym} {tf}")
        seen.add((sym, tf))
        meta = _rows(conn, """SELECT exchange, contract_type FROM ohlcv
                              WHERE symbol=%s AND timeframe=%s LIMIT 1""", [sym, tf])
        if not meta:
            raise HTTPException(404, f"无数据: {sym} {tf}")
        ex, ct = meta[0]["exchange"], meta[0]["contract_type"]
        markets.add("cn" if ex == "akshare" else "crypto")
        sname, dsl = _pick_strategy(conn, sym, tf, u.rule_id)
        units.append({"symbol": sym, "exchange": ex, "contract_type": ct,
                      "timeframe": tf, "_name": sname, "_dsl": dsl})
    if len(markets) > 1:
        raise HTTPException(422, "同一模拟盘的标的须属同一市场（币圈 USDT 与 A股 CNY 不可混）")
    market = markets.pop()
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=body.start_days_ago)
    end_ts = now + timedelta(days=body.duration_days) if body.duration_days else None
    aid = db.insert_sim_account(
        conn, name=name, market=market, init_cash=body.init_cash,
        size_pct=body.size_pct, stop_loss_pct=body.stop_loss_pct,
        start_ts=start, end_ts=end_ts,
        units=[{k: v for k, v in u.items() if not k.startswith("_")} for u in units])
    for i, u in enumerate(units):
        db.insert_sim_change(conn, account_id=aid, unit_idx=i, effective_from=start,
                             name=u["_name"], dsl=u["_dsl"])
    return {"id": aid, "name": name, "market": market,
            "start_ts": start.isoformat(),
            "end_ts": end_ts.isoformat() if end_ts else None,
            "units": [{"symbol": u["symbol"], "timeframe": u["timeframe"],
                       "strategy": u["_name"]} for u in units]}


@router.get("/sim")
def list_sim(conn=Depends(get_conn)):
    out = []
    for r in db.list_sim_accounts(conn):
        units = r.pop("units_json")
        out.append({**r, "units": units})
    return _json_safe(out)


@router.get("/sim/{aid}")
def get_sim(aid: int, conn=Depends(get_conn)):
    """按需重放（确定性）：打开页面即从起点重算到 min(现在, 到期)，无常驻任务。"""
    acct = _sim_acct(conn, aid)
    res = sim.run_account(conn, acct)
    return _json_safe({
        "id": aid, "name": acct["name"], "market": acct["market"],
        "init_cash": acct["init_cash"], "size_pct": acct["size_pct"],
        "stop_loss_pct": acct["stop_loss_pct"], "start_ts": acct["start_ts"],
        "end_ts": acct["end_ts"], "db_status": acct["status"],
        "units": [{"symbol": u["symbol"], "timeframe": u["timeframe"],
                   "changes": [{"effective_from": c["effective_from"],
                                "name": c["name"]} for c in u["changes"]]}
                  for u in acct["units"]],
        "result": res})


class SimChangeIn(BaseModel):
    unit_idx: int
    rule_id: int | None = None
    text: str | None = None  # 中文想法（与策略页同链路 LLM 生成）


@router.post("/sim/{aid}/change")
def change_sim_strategy(aid: int, body: SimChangeIn, conn=Depends(get_conn)):
    """调整策略：快照留痕，从下一根 bar 起生效（重放按时间线分段）。"""
    a = db.get_sim_account(conn, aid)
    if not a:
        raise HTTPException(404, f"模拟盘不存在: {aid}")
    if a["status"] == "finished":
        raise HTTPException(422, "已结束的模拟盘不能再调整策略")
    units = a["units_json"]
    if not 0 <= body.unit_idx < len(units):
        raise HTTPException(422, f"unit_idx ∈ [0,{len(units) - 1}]")
    text = (body.text or "").strip()
    if (body.rule_id is None) == (not text):
        raise HTTPException(422, "rule_id 与 text 二选一")
    u = units[body.unit_idx]
    if body.rule_id is not None:
        sname, dsl = _pick_strategy(conn, u["symbol"], u["timeframe"], body.rule_id)
    else:
        if len(text) > 500:
            raise HTTPException(422, "text ≤500 字")
        try:
            gen = rule_gen.generate(text, u["symbol"], u["timeframe"])
        except rule_gen.RuleGenError as e:
            raise HTTPException(422, f"策略生成未通过校验（已重试一次）：{e}")
        except gateway.LlmError as e:
            raise HTTPException(503, f"LLM 不可用：{e}")
        sname = f"gen_{int(time.time() * 1000)}"
        dsl = {"name": sname, "symbol": u["symbol"], "timeframe": u["timeframe"], **gen}
    db.insert_sim_change(conn, account_id=aid, unit_idx=body.unit_idx,
                         effective_from=datetime.now(timezone.utc), name=sname, dsl=dsl)
    return {"ok": True, "unit_idx": body.unit_idx, "name": sname}


@router.post("/sim/{aid}/finish")
def finish_sim(aid: int, conn=Depends(get_conn)):
    if not db.finish_sim_account(conn, aid, datetime.now(timezone.utc)):
        raise HTTPException(404, f"模拟盘不存在: {aid}")
    return {"ok": True}


@router.delete("/sim/{aid}")
def remove_sim(aid: int, conn=Depends(get_conn)):
    if not db.delete_sim_account(conn, aid):
        raise HTTPException(404, f"模拟盘不存在: {aid}")
    return {"ok": True}


@router.post("/sim/{aid}/review")
def review_sim(aid: int, llm: bool = True, conn=Depends(get_conn)):
    """复盘报告：确定性统计 + 归因要点（+ LLM 改进建议，可关；LLM 失败自动降级）。"""
    acct = _sim_acct(conn, aid)
    res = sim.run_account(conn, acct)
    rev = sim.review_stats(res, acct)
    text, err = sim.llm_review(rev, acct, res) if llm else (None, "已按请求关闭")
    return _json_safe({"status": res["status"], "end_reason": res["end_reason"],
                       "stats": rev["overall"], "by_unit": rev["by_unit"],
                       "by_segment": rev["by_segment"], "notes": rev["notes"],
                       "llm": {"text": text, "ok": text is not None, "error": err}})
