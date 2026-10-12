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

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.adapters.market.ccxt_okx import OKXAdapter
from app.core.config import settings
from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate, parse_rule
from app.models import db

SCAN_WINDOW = 600  # 评估窗口（≥WARMUP+余量；只取最新 bar 事件，指标种子暂态无关）
HISTORY_MIN_BARS = 250
CORE = settings.symbols  # 核心池 = 配置自选池（1m 仅此）；市值 TOP 扩展仅 1h/4h


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


def scan_tfs(symbol: str, tfs: tuple[str, ...]) -> tuple[str, ...]:
    return tfs if symbol in CORE else tuple(t for t in tfs if t != "1m")


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


def seed_rules(conn, pool: list[str], core: tuple[str, ...] = CORE) -> None:
    """4h 主信号 + 1h 组合（RSI 过滤 + chandelier 出场）覆盖全池；1m 仅核心池。"""
    cross = {"cross_up": ["ema20", "ema60"]}
    cross_dn = {"cross_down": ["ema20", "ema60"]}
    combo_entry = {"all": [cross, {"rsi14": {"<": 70}}]}
    combo_exit = {"any": [cross_dn, {"chandelier_exit": {"atr_mult": 3}}]}
    for sym in pool:
        for tf, entry, exit_, cd in (
                ("4h", cross, cross_dn, 2),
                ("1h", combo_entry, combo_exit, 6)):
            name = f"ema_2060_{'rsi_' if tf == '1h' else ''}{tf}_{sym}"
            db.upsert_rule(conn, name=name, symbol=sym, exchange="okx", contract_type="spot",
                           timeframe=tf, dsl=_dsl(name, sym, tf, entry, exit_, cd),
                           cooldown_bars=cd)
    for sym in core:
        name = f"ema_2060_1m_{sym}"
        db.upsert_rule(conn, name=name, symbol=sym, exchange="okx", contract_type="spot",
                       timeframe="1m", dsl=_dsl(name, sym, "1m", cross, cross_dn, 30),
                       cooldown_bars=30)


def load_rules(conn) -> list[dict]:
    rules = []
    for r in db.load_enabled_rules(conn):
        rules.append({**r, "rule": parse_rule(r["dsl_json"])})
    return rules


# ---------- 采集 + 评估 ----------

def scan(adapter: OKXAdapter, conn, rules: list[dict], pool: list[str],
         cts: tuple[str, ...], tfs: tuple[str, ...]) -> list[dict]:
    """各 tf：增量采集已收盘 bar；刚收盘 bar 为新 bar 时评估匹配规则并落库信号。"""
    now = adapter.server_time()
    fired: list[dict] = []
    for tf in tfs:
        tf_ms = adapter.ex.parse_timeframe(tf) * 1000
        trigger = last_closed_ms(now, tf_ms)
        for internal in pool:
            if tf not in scan_tfs(internal, tfs):
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
              tfs: tuple[str, ...]) -> None:
    now = adapter.server_time()
    stale = []
    for internal in pool:
        for ct in cts:
            for tf in scan_tfs(internal, tfs):
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

def _scan_with_conn(adapter: OKXAdapter, rules: list[dict], pool: list[str],
                    cts: tuple[str, ...], tf: str) -> list[dict]:
    """定时任务用短连接（避免长事务/断线粘连）。"""
    conn = db.connect(settings.pg_dsn)
    try:
        return scan(adapter, conn, rules, pool, cts, (tf,))
    finally:
        conn.close()


def main(once: bool = False, on_signal=None, on_weekly=None) -> None:
    adapter = OKXAdapter(settings.okx_proxy)
    conn = db.connect(settings.pg_dsn)
    pool = build_pool(adapter)
    cts, tfs = settings.contract_types, settings.timeframes
    print(f"自选池({len(pool)}): {pool} | ct={cts} | tf={tfs}")
    ensure_history(adapter, conn, pool, cts, tfs)
    seed_rules(conn, pool)
    rules = load_rules(conn)
    print(f"规则 {len(rules)} 条: {sorted({r['timeframe'] for r in rules})} 周期")

    def job(tf: str) -> None:
        t0 = time.time()
        fired = _scan_with_conn(adapter, rules, pool, cts, tf)
        print(f"[{datetime.now(timezone.utc):%m-%d %H:%M:%S}] {tf:3} 扫描 {time.time() - t0:.1f}s "
              f"新信号 {len(fired)}", flush=True)
        for s in fired:
            print(f"  ▶ {s['rule']} {s['direction']} @ {s['price']:.6g} "
                  f"区间[{s['price_zone_low']:.6g},{s['price_zone_high']:.6g}]", flush=True)
            if on_signal:
                on_signal(s)

    def hb() -> None:
        conn = db.connect(settings.pg_dsn)
        try:
            heartbeat(adapter, conn, pool, cts, tfs)
        finally:
            conn.close()

    if once:
        hb()
        for tf in tfs:
            job(tf)
        conn.close()
        return

    sched = BlockingScheduler(timezone="UTC")
    for tf in tfs:
        sched.add_job(lambda tf=tf: job(tf), trigger_for(tf),
                      id=f"scan_{tf}", max_instances=1, coalesce=True)
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
