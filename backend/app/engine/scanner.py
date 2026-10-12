"""P0-6 扫描调度（APScheduler）：各周期 bar 收盘 +3s 触发增量采集与规则评估。

- 增量：since = 库内 max(ts)（含最后一根重取，修正形成中 bar 的部分数据）；
  只入库已收盘 bar（<= 刚收盘 bar），ohlcv upsert 为 DO UPDATE（幂等自愈）。
- 触发：仅当"刚收盘 bar"确为新 bar 时评估；停机跨多根只评估最后一根（避免补推洪峰）；
  signal 表唯一索引去重（重启安全，不重复推送）。
- 自选池：SYMBOLS + 市值 TOP N（fetch_tickers 按 quoteVolume，启动时计算）；
  1m 数据与规则仅核心池（= 配置的 SYMBOLS；市值 TOP 扩展仅 1h/4h），避免高请求量与推送噪音。
- 监控：心跳日志（数据新鲜度 / 停更告警）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.adapters.market.akshare_cn import AkshareAdapter
from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.engine import replay
from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate, parse_rule
from app.models import db

SCAN_WINDOW = 600  # 评估窗口（≥WARMUP+余量；只取最新 bar 事件，指标种子暂态无关）
HISTORY_MIN_BARS = 250
CORE = settings.symbols  # 核心池 = 配置自选池（1m 仅此）；市值 TOP 扩展仅 1h/4h
STOCK_TF = "1d"
STOCK_TF_MS = 86_400_000
STOCK_AVAIL_OFFSET_MS = 7 * 3600 * 1000  # A股 15:00 CST 收盘 = 07:00 UTC（无夏令时）


def last_closed_ms(now_ms: int, tf_ms: int, buffer_ms: int = 3000) -> int:
    """最近一根已收盘 bar 的开盘时间（收盘 + buffer 后才视为可用，PIT）。"""
    return ((now_ms - buffer_ms) // tf_ms) * tf_ms - tf_ms


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def trigger_for(tf: str) -> CronTrigger:
    """按周期对齐到收盘 +3s（1d 用 +5s）。"""
    return {
        "1m": CronTrigger(second=3),
        "1h": CronTrigger(minute=0, second=3),
        "4h": CronTrigger(hour="*/4", minute=0, second=3),
        "1d": CronTrigger(hour=0, minute=0, second=5),
    }[tf]


def scan_tfs(symbol: str, tfs: tuple[str, ...], core=None) -> tuple[str, ...]:
    core = CORE if core is None else core
    return tfs if symbol in core else tuple(t for t in tfs if t != "1m")


def core_from_rules(rules: list[dict]) -> set[str]:
    """1m 采集资格 = 拥有 1m 规则的标的（seed 覆盖配置核心池；自助加币选 1m 自动获得）。"""
    return {r["symbol"] for r in rules if r["timeframe"] == "1m"} or set(CORE)


# ---------- 自选池与历史准备 ----------

def build_pool(adapter: OKXAdapter) -> list[str]:
    pool = list(settings.symbols)
    if settings.pool_top_n > 0:
        try:
            top = adapter.top_symbols(settings.pool_top_n)
            print(f"市值TOP{settings.pool_top_n}(quoteVolume): {top}")
        except Exception as e:  # 外部接口边界：失败降级为仅配置自选池
            top, _ = [], print(f"[警告] 市值TOP获取失败，仅用配置自选池: {e}")
        pool += [s for s in top if s not in pool]
    return pool


def active_pool(conn, startup_pool: list[str]) -> list[str]:
    """启动池（配置+市值TOP）∪ symbol 表自助加币（去重保序，每 tick 调用）。"""
    extra = [r["symbol"] for r in db.load_active_symbols(conn)
             if r["exchange"] == "okx" and r["contract_type"] == "spot"]
    return list(dict.fromkeys(startup_pool + extra))


def ensure_history(adapter: OKXAdapter, conn, pool: list[str], cts: tuple[str, ...],
                   tfs: tuple[str, ...]) -> None:
    """不足 HISTORY_MIN_BARS 的 (symbol, ct, tf) 回填所需最短天数（按周期换算，避免 1m 拉 30 天）。"""
    for internal in pool:
        for ct in cts:
            sym = adapter.to_ccxt_symbol(internal, ct)
            if sym not in adapter.ex.markets:
                continue
            for tf in scan_tfs(internal, tfs):
                n, _, _ = db.count_ohlcv(conn, internal, adapter.exchange, ct, tf)
                if n >= HISTORY_MIN_BARS:
                    continue
                tf_ms = adapter.ex.parse_timeframe(tf) * 1000
                need_days = max(1, (HISTORY_MIN_BARS + 20) * tf_ms // 86_400_000 + 1)
                since = adapter.server_time() - need_days * 86400 * 1000
                rows = adapter.fetch_ohlcv(sym, tf, since=since, paginate=True)
                if rows:
                    db.upsert_ohlcv(conn, rows, {"symbol": internal, "exchange": adapter.exchange,
                                                 "contract_type": ct, "timeframe": tf})
                print(f"  [回填] {sym:16} {tf:3} {len(rows):6} 根（{need_days}天）")


# ---------- 规则种子（P0 规则集） ----------

def _dsl(name: str, symbol: str, tf: str, entry: dict, exit_: dict, cooldown: int) -> dict:
    return {"name": name, "symbol": symbol, "timeframe": tf, "entry": entry,
            "exit": exit_, "cooldown_bars": cooldown}


def rule_name(sym: str, tf: str) -> str:
    return f"ema_2060_{'rsi_' if tf == '1h' else ''}{tf}_{sym}"


def rule_templates(tfs: tuple[str, ...]) -> list[tuple[str, dict, dict, int]]:
    """(tf, entry, exit, cooldown) 模板：4h 裸交叉 / 1h RSI 过滤+chandelier / 1m 裸交叉 / 1d 裸交叉。"""
    cross = {"cross_up": ["ema20", "ema60"]}
    cross_dn = {"cross_down": ["ema20", "ema60"]}
    tpl = {"4h": (cross, cross_dn, 2),
           "1h": ({"all": [cross, {"rsi14": {"<": 70}}]},
                  {"any": [cross_dn, {"chandelier_exit": {"atr_mult": 3}}]}, 6),
           "1m": (cross, cross_dn, 30),
           "1d": (cross, cross_dn, 2)}
    return [(tf, *tpl[tf]) for tf in tfs]


def ensure_rules(conn, sym: str, tfs: tuple[str, ...], *,
                 exchange: str = "okx", ct: str = "spot") -> None:
    """按模板为标的 upsert 规则（幂等；seed 与自助加币共用）。"""
    for tf, entry, exit_, cd in rule_templates(tfs):
        name = rule_name(sym, tf)
        db.upsert_rule(conn, name=name, symbol=sym, exchange=exchange, contract_type=ct,
                       timeframe=tf, dsl=_dsl(name, sym, tf, entry, exit_, cd),
                       cooldown_bars=cd)


def seed_rules(conn, pool: list[str], core: tuple[str, ...] = CORE) -> None:
    """4h 主信号 + 1h 组合（RSI 过滤 + chandelier 出场）覆盖全池；1m 仅核心池。"""
    for sym in pool:
        ensure_rules(conn, sym, ("4h", "1h"))
    for sym in core:
        ensure_rules(conn, sym, ("1m",))


def load_rules(conn) -> list[dict]:
    rules = []
    for r in db.load_enabled_rules(conn):
        rules.append({**r, "rule": parse_rule(r["dsl_json"])})
    return rules


# ---------- A股（P1-6：akshare 日线，07:00 UTC 收盘评估） ----------

def stock_pool(conn) -> list[str]:
    """A股池 = 配置 STOCK_SYMBOLS ∪ symbol 表（exchange=akshare）。"""
    extra = [r["symbol"] for r in db.load_active_symbols(conn)
             if r["exchange"] == "akshare" and r["contract_type"] == "stock"]
    return list(dict.fromkeys(list(settings.stock_symbols) + extra))


def seed_stock(conn, pool: list[str]) -> None:
    """A股标的登记 + 1d 规则种子（幂等）。symbol "000157.SZ" ↔ akshare 代码 "000157"。"""
    for sym in pool:
        code = sym.split(".")[0]
        db.upsert_symbol(conn, symbol=sym, exchange="akshare", asset_class="stock",
                         contract_type="stock", base=code, quote="CNY", exchange_symbol=code)
        ensure_rules(conn, sym, (STOCK_TF,), exchange="akshare", ct="stock")


def ensure_stock_history(adapter: AkshareAdapter, conn, pool: list[str]) -> None:
    """不足 HISTORY_MIN_BARS 的标的回填（500 自然日 ≈ 340 交易日）并立即 replay 补登信号，
    避免首次评估把历史 bar 当新信号推送。"""
    for internal in pool:
        n, _, _ = db.count_ohlcv(conn, internal, adapter.exchange, "stock", STOCK_TF)
        if n >= HISTORY_MIN_BARS:
            continue
        since = adapter.server_time() - 500 * 86_400_000
        rows = adapter.fetch_ohlcv(adapter.to_ccxt_symbol(internal, "stock"),
                                   STOCK_TF, since=since)
        if rows:
            db.upsert_ohlcv(conn, rows, {"symbol": internal, "exchange": adapter.exchange,
                                         "contract_type": "stock", "timeframe": STOCK_TF})
        print(f"  [回填/A股] {internal} {len(rows)} 根")
        replay.backfill(conn, symbol=internal)


def stock_trigger_ms(now_ms: int) -> int:
    """最近一个已收盘交易日的 bar ts（00:00 UTC）；当日 bar 07:00 UTC 前不可用。"""
    return ((now_ms - STOCK_AVAIL_OFFSET_MS) // STOCK_TF_MS) * STOCK_TF_MS


def _evaluate_daily(conn, adapter: AkshareAdapter, rules: list[dict], internal: str,
                    new_ts: set) -> list[dict]:
    """对指定 bar（new_ts）评估该标的 1d 规则；价格区间用事件 bar 自身 ATR。"""
    matching = [r for r in rules if r["symbol"] == internal and r["timeframe"] == STOCK_TF]
    if not matching:
        return []
    df = db.load_ohlcv(conn, internal, adapter.exchange, "stock", STOCK_TF, limit=SCAN_WINDOW)
    if len(df) < WARMUP_BARS + 2:
        print(f"  [警告] {internal} {STOCK_TF} 样本不足({len(df)})，跳过评估")
        return []
    ind = enrich(df)
    atr_by_ts = {pd.Timestamp(t): float(a) for t, a in zip(ind["ts"], ind["atr14"])}
    out = []
    for r in matching:
        for e in evaluate(r["rule"], ind):
            if int(pd.Timestamp(e.ts).timestamp() * 1000) not in new_ts:
                continue
            a = atr_by_ts[pd.Timestamp(e.ts)]
            sid = db.insert_signal_if_new(
                conn, rule_id=r["id"], symbol=internal, exchange=adapter.exchange,
                contract_type="stock", timeframe=STOCK_TF, ts=e.ts, direction=e.direction,
                price_zone_low=e.price - 0.5 * a, price_zone_high=e.price + 0.5 * a)
            if sid:
                out.append({"signal_id": sid, "rule": r["name"], "symbol": internal,
                            "exchange": adapter.exchange, "contract_type": "stock",
                            "timeframe": STOCK_TF, "ts": e.ts, "direction": e.direction,
                            "price": e.price, "price_zone_low": e.price - 0.5 * a,
                            "price_zone_high": e.price + 0.5 * a, "atr": a})
    return out


def scan_daily(adapter: AkshareAdapter, conn, rules: list[dict], pool: list[str]) -> list[dict]:
    """A股日线扫描：拉新 bar 全部评估（数据源延迟时次日自动补评；去重不重推）。"""
    now = adapter.server_time()
    trigger = stock_trigger_ms(now)
    fired: list[dict] = []
    for internal in pool:
        prev = db.max_ohlcv_ts(conn, internal, adapter.exchange, "stock", STOCK_TF)
        prev_ms = int(prev.timestamp() * 1000) if prev else None
        rows = adapter.fetch_ohlcv(adapter.to_ccxt_symbol(internal, "stock"), STOCK_TF,
                                   since=prev_ms or (now - 500 * 86_400_000))
        rows = [r for r in rows if r[0] <= trigger]
        if not rows:
            continue
        db.upsert_ohlcv(conn, rows, {"symbol": internal, "exchange": adapter.exchange,
                                     "contract_type": "stock", "timeframe": STOCK_TF})
        new_ts = {r[0] for r in rows if prev_ms is None or r[0] > prev_ms}
        if new_ts:
            fired += _evaluate_daily(conn, adapter, rules, internal, new_ts)
    return fired


def heartbeat_stock(adapter: AkshareAdapter, conn, pool: list[str]) -> None:
    now = adapter.server_time()
    stale = []
    for internal in pool:
        hi = db.max_ohlcv_ts(conn, internal, adapter.exchange, "stock", STOCK_TF)
        if hi is None:
            stale.append(f"{internal}:无数据")
            continue
        age_days = (now - int(hi.timestamp() * 1000)) / 86_400_000
        if age_days > 5:  # 覆盖周末与小长假
            stale.append(f"{internal}:{age_days:.1f}天无新bar")
    print(f"[心跳/A股] {_utc(now):%m-%d %H:%M:%S} 池{len(pool)}标的 | 停更: {stale or '无'}", flush=True)


# ---------- 采集 + 评估 ----------

def scan(adapter: OKXAdapter, conn, rules: list[dict], pool: list[str],
         cts: tuple[str, ...], tfs: tuple[str, ...]) -> list[dict]:
    """各 tf：增量采集已收盘 bar；刚收盘 bar 为新 bar 时评估匹配规则并落库信号。"""
    now = adapter.server_time()
    core = core_from_rules(rules)
    fired: list[dict] = []
    for tf in tfs:
        tf_ms = adapter.ex.parse_timeframe(tf) * 1000
        trigger = last_closed_ms(now, tf_ms)
        for internal in pool:
            if tf not in scan_tfs(internal, tfs, core):
                continue
            for ct in cts:
                sym = adapter.to_ccxt_symbol(internal, ct)
                if sym not in adapter.ex.markets:
                    continue
                prev = db.max_ohlcv_ts(conn, internal, adapter.exchange, ct, tf)
                prev_ms = int(prev.timestamp() * 1000) if prev else None
                # 分页追平：停机跨多根时一次补齐（正常情况 1 请求即返回，代价不变）
                rows = adapter.fetch_ohlcv(sym, tf, since=prev_ms or trigger, limit=10000, paginate=True)
                rows = [r for r in rows if r[0] <= trigger]
                if not rows:
                    continue
                db.upsert_ohlcv(conn, rows, {"symbol": internal, "exchange": adapter.exchange,
                                             "contract_type": ct, "timeframe": tf})
                if trigger in {r[0] for r in rows} and (prev_ms is None or trigger > prev_ms):
                    fired += _evaluate_trigger(conn, adapter, rules, internal, ct, tf, trigger)
    return fired


def _evaluate_trigger(conn, adapter: OKXAdapter, rules: list[dict], internal: str,
                      ct: str, tf: str, trigger_ms: int) -> list[dict]:
    matching = [r for r in rules if r["symbol"] == internal
                and r["contract_type"] == ct and r["timeframe"] == tf]
    if not matching:
        return []
    df = db.load_ohlcv(conn, internal, adapter.exchange, ct, tf, limit=SCAN_WINDOW)
    if len(df) < WARMUP_BARS + 2:
        print(f"  [警告] {internal} {ct} {tf} 样本不足({len(df)})，跳过评估")
        return []
    ind = enrich(df)
    trigger = _utc(trigger_ms)
    atr = float(ind["atr14"].iloc[-1])
    out = []
    for r in matching:
        for e in evaluate(r["rule"], ind):
            if e.ts.to_pydatetime() != trigger:
                continue
            sid = db.insert_signal_if_new(
                conn, rule_id=r["id"], symbol=internal, exchange=adapter.exchange,
                contract_type=ct, timeframe=tf, ts=trigger, direction=e.direction,
                price_zone_low=e.price - 0.5 * atr, price_zone_high=e.price + 0.5 * atr)
            if sid:
                out.append({"signal_id": sid, "rule": r["name"], "symbol": internal,
                            "exchange": adapter.exchange,
                            "contract_type": ct, "timeframe": tf, "ts": trigger,
                            "direction": e.direction, "price": e.price,
                            "price_zone_low": e.price - 0.5 * atr,
                            "price_zone_high": e.price + 0.5 * atr, "atr": atr})
    return out


# ---------- 心跳 ----------

def heartbeat(adapter: OKXAdapter, conn, pool: list[str], cts: tuple[str, ...],
              tfs: tuple[str, ...], core=None) -> None:
    now = adapter.server_time()
    stale = []
    for internal in pool:
        for ct in cts:
            for tf in scan_tfs(internal, tfs, core):
                hi = db.max_ohlcv_ts(conn, internal, adapter.exchange, ct, tf)
                if hi is None:
                    stale.append(f"{internal}/{ct}/{tf}:无数据")
                    continue
                tf_ms = adapter.ex.parse_timeframe(tf) * 1000
                age = now - int(hi.timestamp() * 1000) - tf_ms
                if age > 2 * tf_ms:
                    stale.append(f"{internal}/{ct}/{tf}:滞后{age // 60000}min")
    print(f"[心跳] {_utc(now):%m-%d %H:%M:%S} 池{len(pool)}标的 | 停更: {stale or '无'}", flush=True)


# ---------- 入口 ----------

def _scan_with_conn(adapter: OKXAdapter, startup_pool: list[str],
                    cts: tuple[str, ...], tf: str) -> list[dict]:
    """定时任务用短连接（避免长事务/断线粘连）；每 tick 重读规则与池（自助加币 ≤1 周期生效）。"""
    conn = db.connect(settings.pg_dsn)
    try:
        rules = load_rules(conn)
        pool = active_pool(conn, startup_pool)
        return scan(adapter, conn, rules, pool, cts, (tf,))
    finally:
        conn.close()


def _scan_stock_with_conn(adapter: AkshareAdapter) -> list[dict]:
    conn = db.connect(settings.pg_dsn)
    try:
        return scan_daily(adapter, conn, load_rules(conn), stock_pool(conn))
    finally:
        conn.close()


def main(once: bool = False, on_signal=None, on_weekly=None) -> None:
    adapter = OKXAdapter(settings.okx_proxy)
    stock_adapter = AkshareAdapter()
    conn = db.connect(settings.pg_dsn)
    pool = active_pool(conn, build_pool(adapter))  # 并入 symbol 表自助加币（重启自愈回填）
    spool = stock_pool(conn)
    cts, tfs = settings.contract_types, settings.timeframes
    print(f"自选池({len(pool)}): {pool} | ct={cts} | tf={tfs}")
    print(f"A股池({len(spool)}): {spool} | tf={STOCK_TF}")
    ensure_history(adapter, conn, pool, cts, tfs)
    seed_rules(conn, pool)
    seed_stock(conn, spool)
    ensure_stock_history(stock_adapter, conn, spool)
    rules = load_rules(conn)
    print(f"规则 {len(rules)} 条: {sorted({r['timeframe'] for r in rules})} 周期")

    def job(tf: str) -> None:
        t0 = time.time()
        fired = _scan_with_conn(adapter, pool, cts, tf)
        print(f"[{datetime.now(timezone.utc):%m-%d %H:%M:%S}] {tf:3} 扫描 {time.time() - t0:.1f}s "
              f"新信号 {len(fired)}", flush=True)
        for s in fired:
            print(f"  ▶ {s['rule']} {s['direction']} @ {s['price']:.6g} "
                  f"区间[{s['price_zone_low']:.6g},{s['price_zone_high']:.6g}]", flush=True)
            if on_signal:
                on_signal(s)

    def job_stock() -> None:
        if not spool:
            return
        t0 = time.time()
        fired = _scan_stock_with_conn(stock_adapter)
        print(f"[{datetime.now(timezone.utc):%m-%d %H:%M:%S}] {STOCK_TF:3} 扫描A股 "
              f"{time.time() - t0:.1f}s 新信号 {len(fired)}", flush=True)
        for s in fired:
            print(f"  ▶ {s['rule']} {s['direction']} @ {s['price']:.6g} "
                  f"区间[{s['price_zone_low']:.6g},{s['price_zone_high']:.6g}]", flush=True)
            if on_signal:
                on_signal(s)

    def hb() -> None:
        conn = db.connect(settings.pg_dsn)
        try:
            heartbeat(adapter, conn, active_pool(conn, pool), cts, tfs,
                      core_from_rules(load_rules(conn)))
            if spool:
                heartbeat_stock(stock_adapter, conn, stock_pool(conn))
        finally:
            conn.close()

    if once:
        hb()
        for tf in tfs:
            job(tf)
        job_stock()
        conn.close()
        return

    sched = BlockingScheduler(timezone="UTC")
    for tf in tfs:
        sched.add_job(lambda tf=tf: job(tf), trigger_for(tf),
                      id=f"scan_{tf}", max_instances=1, coalesce=True)
    if spool:
        sched.add_job(job_stock, CronTrigger(hour=7, minute=5, second=5),
                      id="scan_stock_1d", max_instances=1, coalesce=True)
    sched.add_job(hb, IntervalTrigger(seconds=60), id="heartbeat", max_instances=1)
    if on_weekly:
        sched.add_job(on_weekly, CronTrigger(day_of_week="mon", hour=1, minute=0),
                      id="weekly_report", max_instances=1, coalesce=True)
    print("扫描器启动（Ctrl+C 退出）")
    conn.close()
    try:
        hb()
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
