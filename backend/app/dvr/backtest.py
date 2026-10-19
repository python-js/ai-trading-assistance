"""DVR 回测 Harness：5m 信号 + 1m 成交路径 + 费用/funding/分批出场/风控。

时间轴：5m bar 收盘评估信号 → 下一根 1m 开盘入场；持仓期间逐根 1m 检查
止损/TP1/BE/TP2/时间止损。同一 1m bar 同时触及止损与目标按止损先（保守）。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.core.config import settings
from app.models import db

from .config import CFG, be_bps, round_trip_bps
from .strategy import Setup, SignalEngine


@dataclass(slots=True)
class Position:
    setup: Setup
    entry_idx: int
    entry_ts: int
    entry: float
    stop: float
    init_stop: float
    R: float
    qty: float
    qty_open: float
    tp1: float
    tp2: float
    tp1_hit: bool = False
    mfe_r: float = 0.0
    mae_r: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    cash_pnl: float = 0.0
    next_idx: int = 0
    equity_at_entry: float = 0.0


@dataclass
class Result:
    trades: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    funnel: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    event_funnel: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    raw_div: dict[str, int] = field(default_factory=lambda: Counter())
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    funding_imputed: int = 0
    missing_entry: int = 0
    cfg: dict = field(default_factory=lambda: CFG)


def _to_s(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ts"] = (pd.to_datetime(out["ts"], utc=True).astype("int64") // 10**9).astype("int64")
    return out.sort_values("ts", ignore_index=True).drop_duplicates("ts")


def resample(df1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    x = df1.copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    g = x.set_index("dt").resample(f"{minutes}min", label="left", closed="left")
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), n=("open", "count"))
    out = out[out["n"] == minutes].copy()
    out["ts"] = (out.index.astype("int64") // 10**9).astype("int64")
    return out.reset_index(drop=True)[["ts", "open", "high", "low", "close", "volume"]]


def load_data(conn, start_dt: datetime, end_dt: datetime,
              cfg: dict = CFG) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    sym, ex_, ct = (cfg["symbol"]["internal"], cfg["symbol"]["exchange"],
                    cfg["symbol"]["contract_type"])
    df1 = _to_s(db.load_ohlcv(conn, sym, ex_, ct, "1m", start=start_dt, end=end_dt))
    if df1.empty:
        raise ValueError(f"无 1m 数据：{start_dt} → {end_dt}")
    with conn.cursor() as cur:
        cur.execute("""SELECT ts, rate FROM funding_rate
                       WHERE symbol=%s AND exchange=%s AND rate IS NOT NULL
                         AND ts >= %s AND ts <= %s""",
                    (sym, ex_, start_dt - timedelta(hours=8), end_dt + timedelta(hours=8)))
        fmap = {int(t.timestamp()): float(r) for t, r in cur.fetchall()}
    return df1, resample(df1, 5), resample(df1, 15), fmap


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "win": 0.0, "pf": 0.0, "exp_r": 0.0, "exp_bps": 0.0,
                "fees": 0.0, "funding": 0.0}
    rs = np.array([t["pnl_R"] for t in trades], dtype=float)
    bps = np.array([t["pnl_bps"] for t in trades], dtype=float)
    gw = float(rs[rs > 0].sum())
    gl = float(-rs[rs < 0].sum())
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return {"n": len(trades), "win": float((rs > 0).mean() * 100),
            "pf": float(pf), "exp_r": float(rs.mean()),
            "exp_bps": float(bps.mean()),
            "fees": float(sum(t["fees"] for t in trades)),
            "funding": float(sum(t["funding"] for t in trades))}


def run_backtest(df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame,
                 fmap: dict, cfg: dict = CFG) -> Result:
    if len(df5) < 100 or len(df15) < 80:
        raise ValueError("5m/15m 数据不足")
    eng = SignalEngine(df5, df15, cfg)
    ts1 = df1["ts"].to_numpy(dtype=np.int64)
    o1 = df1["open"].to_numpy(dtype=float)
    h1 = df1["high"].to_numpy(dtype=float)
    l1 = df1["low"].to_numpy(dtype=float)
    c1 = df1["close"].to_numpy(dtype=float)
    ts5 = df5["ts"].to_numpy(dtype=np.int64)
    start_i = np.searchsorted(ts1, ts5, side="left")
    end_i = np.searchsorted(ts1, ts5 + cfg["tf"]["signal"], side="left")
    res = Result(cfg=cfg)
    fee_rate = cfg["venue"]["taker_bps"] / 1e4
    slip = cfg["venue"]["slip_bps"] / 1e4
    lot = cfg["venue"].get("lot_step", 0.0001)
    be = be_bps(cfg)
    acct = cfg["account"]
    mg = cfg["manage"]
    equity = float(acct["init_equity"])
    res.equity_curve.append((int(ts1[0]), equity))
    pos: Position | None = None
    cooldown_side: str | None = None
    cooldown_until = -1
    day = int(ts1[0]) // 86_400
    week = int(ts1[0]) // (7 * 86_400)
    day_start = equity
    week_start = equity
    day_block = week_block = False
    seen_div: set[tuple] = set()
    event_best: dict[tuple, int] = {}
    rank = {"ZONE": 1, "ENV": 2, "REVERSAL": 3, "STRUCT": 4,
            "RSI": 5, "MACD": 6, "COST": 7, "OK": 8}

    def _record(pos_: Position, ts: int, reason: str) -> None:
        nonlocal equity, cooldown_side, cooldown_until
        risk_amt = pos_.qty * pos_.R
        net = pos_.cash_pnl - pos_.fees - pos_.funding
        equity += net
        pnl_r = net / risk_amt if risk_amt > 0 else 0.0
        pnl_bps = net / (pos_.qty * pos_.entry) * 1e4 if pos_.qty > 0 else 0.0
        rec = {
            "side": pos_.setup.side, "entry_ts": pos_.entry_ts, "exit_ts": int(ts),
            "entry": pos_.entry, "stop": pos_.init_stop, "R": pos_.R,
            "qty": pos_.qty, "pnl": net, "pnl_R": pnl_r, "pnl_bps": pnl_bps,
            "fees": pos_.fees, "funding": pos_.funding, "reason": reason,
            "tp1_hit": pos_.tp1_hit, "mfe_R": pos_.mfe_r, "mae_R": pos_.mae_r,
            "bars": int((ts - pos_.entry_ts) // cfg["tf"]["signal"]),
            "zone_hits": pos_.setup.zone_hits,
            "zone_kinds": ",".join(pos_.setup.zone_kinds),
            "equity_at_entry": pos_.equity_at_entry,
        }
        res.trades.append(rec)
        res.equity_curve.append((int(ts), equity))
        if reason in ("stop", "be") or net < 0:
            cooldown_side = pos_.setup.side
            cooldown_until = int(ts) + mg["cooldown_bars"] * cfg["tf"]["signal"]

    def _fill_exit(pos_: Position, price: float) -> float:
        return price - slip * price if pos_.setup.side == "long" else price + slip * price

    def _close(pos_: Position, price: float, ts: int, reason: str) -> None:
        qty = pos_.qty_open
        if qty <= 0:
            return
        px = _fill_exit(pos_, price)
        pos_.cash_pnl += qty * (px - pos_.entry) * (1 if pos_.setup.side == "long" else -1)
        pos_.fees += qty * px * fee_rate
        pos_.qty_open = 0.0
        _record(pos_, ts, reason)

    def _partial(pos_: Position, price: float, ts: int) -> None:
        qty = round(pos_.qty * mg["tp1_frac"] / lot) * lot
        qty = min(qty, pos_.qty_open)
        if qty < lot:
            qty = pos_.qty_open
        px = _fill_exit(pos_, price)
        pos_.cash_pnl += qty * (px - pos_.entry) * (1 if pos_.setup.side == "long" else -1)
        pos_.fees += qty * px * fee_rate
        pos_.qty_open -= qty
        if pos_.qty_open < lot:
            pos_.qty_open = 0.0
            _record(pos_, ts, "tp1")
            return
        pos_.tp1_hit = True
        pos_.stop = (pos_.entry * (1 + be / 1e4) if pos_.setup.side == "long"
                     else pos_.entry * (1 - be / 1e4))

    def _process(pos_: Position, a: int, b: int) -> None:
        nonlocal res
        idx = max(a, pos_.next_idx)
        while idx < b and pos_.qty_open > 0:
            ts = int(ts1[idx])
            if ts % 28_800 == 0:
                rate = fmap.get(ts)
                if rate is None:
                    res.funding_imputed += 1
                    rate = 0.0
                notional = pos_.qty_open * float(o1[idx])
                pos_.funding += notional * rate * (1 if pos_.setup.side == "long" else -1)
            hi, lo, op = float(h1[idx]), float(l1[idx]), float(o1[idx])
            long = pos_.setup.side == "long"
            pos_.mfe_r = max(pos_.mfe_r, (hi - pos_.entry) / pos_.R if long
                             else (pos_.entry - lo) / pos_.R)
            pos_.mae_r = min(pos_.mae_r, (lo - pos_.entry) / pos_.R if long
                             else (pos_.entry - hi) / pos_.R)
            # 1) 初始/移动止损（跳空按开盘）
            if long and (op <= pos_.stop or lo <= pos_.stop):
                _close(pos_, op if op <= pos_.stop else pos_.stop, ts, "stop")
                break
            if not long and (op >= pos_.stop or hi >= pos_.stop):
                _close(pos_, op if op >= pos_.stop else pos_.stop, ts, "stop")
                break
            # 2) TP1（半仓）
            if not pos_.tp1_hit:
                if (long and hi >= pos_.tp1) or (not long and lo <= pos_.tp1):
                    _partial(pos_, pos_.tp1, ts)
                    if pos_.qty_open <= 0:
                        break
            # 3) TP1 后 BE（同 bar 保守先于 TP2）
            if pos_.tp1_hit:
                if long and lo <= pos_.stop:
                    _close(pos_, pos_.stop, ts, "be")
                    break
                if not long and hi >= pos_.stop:
                    _close(pos_, pos_.stop, ts, "be")
                    break
            # 4) TP2（剩余全平）
            if (long and hi >= pos_.tp2) or (not long and lo <= pos_.tp2):
                _close(pos_, pos_.tp2, ts, "tp2")
                break
            pos_.next_idx = idx + 1
            idx += 1
        pos_.next_idx = max(pos_.next_idx, b)

    n = len(df5)
    for i in range(n):
        ts = int(ts5[i])
        d, w = ts // 86_400, ts // (7 * 86_400)
        if d != day:
            day, day_start, day_block = d, equity, False
        if w != week:
            week, week_start, week_block = w, equity, False
        a, b = int(start_i[i]), int(end_i[i])
        if a >= len(ts1):
            break
        if pos is not None:
            _process(pos, a, b)
            if pos.qty_open <= 0:
                pos = None
        if pos is not None and b > a:
            held = (ts + cfg["tf"]["signal"] - pos.entry_ts) // cfg["tf"]["signal"]
            if held >= mg["max_hold_bars"]:
                _close(pos, float(c1[b - 1]), int(ts1[b - 1]) + 60, "max_hold")
                pos = None
            elif (held >= mg["time_stop_bars"] and not pos.tp1_hit
                  and pos.mfe_r < mg["time_stop_mfe_r"]):
                _close(pos, float(c1[b - 1]), int(ts1[b - 1]) + 60, "time")
                pos = None
        # 风控闸门
        if equity <= day_start * (1 - acct["daily_loss_pct"] / 100):
            day_block = True
        if equity <= week_start * (1 - acct["weekly_loss_pct"] / 100):
            week_block = True
        results = eng.evaluate_all(i)
        for side, r in results.items():
            if not r.detail.get("active"):
                continue
            did = r.detail.get("div_id")
            if did not in seen_div:
                seen_div.add(did)
                res.raw_div[side] += 1
                event_best[did] = 0
            res.funnel[side][r.reason] += 1
            event_best[did] = max(event_best.get(did, 0), rank.get(r.reason, 0))
        ok = [r.setup for r in results.values() if r.setup is not None]
        if not ok:
            continue
        setup = sorted(ok, key=lambda s: (-s.zone_hits, 0 if s.side == "long" else 1))[0]
        sig = {"ts": ts, "side": setup.side, "entry": setup.entry, "stop": setup.stop,
               "R": setup.R, "r_bps": setup.r_bps, "zone_hits": setup.zone_hits,
               "zone_kinds": ",".join(setup.zone_kinds),
               "div_l2_ts": setup.div_id[1], "taken": False, "blocked": ""}
        res.signals.append(sig)
        if pos is not None:
            res.funnel[setup.side]["BLOCKED_POS"] += 1
            sig["blocked"] = "BUSY"
            continue
        if day_block or week_block:
            res.funnel[setup.side]["BLOCKED_RISK"] += 1
            sig["blocked"] = "RISK"
            continue
        if cooldown_side == setup.side and ts < cooldown_until:
            res.funnel[setup.side]["BLOCKED_COOLDOWN"] += 1
            sig["blocked"] = "COOLDOWN"
            continue
        if b >= len(ts1):
            res.missing_entry += 1
            sig["blocked"] = "NO_ENTRY_BAR"
            continue
        raw = float(o1[b])
        entry = raw * (1 + slip) if setup.side == "long" else raw * (1 - slip)
        stop = setup.stop
        R = (entry - stop) if setup.side == "long" else (stop - entry)
        if R <= 0:
            continue
        qty = equity * acct["risk_pct"] / 100 / R
        qty = min(qty, equity * acct["notional_max_pct"] / 100 / entry)
        qty = round(qty / lot) * lot
        if qty < lot:
            continue
        tp1 = entry + mg["tp1_r"] * R if setup.side == "long" else entry - mg["tp1_r"] * R
        tp2 = entry + mg["tp2_r"] * R if setup.side == "long" else entry - mg["tp2_r"] * R
        pos = Position(setup=setup, entry_idx=b, entry_ts=int(ts1[b]), entry=entry,
                       stop=stop, init_stop=stop, R=R, qty=qty, qty_open=qty,
                       tp1=tp1, tp2=tp2, next_idx=b, equity_at_entry=equity)
        pos.fees += qty * entry * fee_rate
        pos.next_idx = b
        sig.update({"taken": True, "entry": entry, "stop": stop, "R": R, "qty": qty})
    if pos is not None and pos.qty_open > 0:
        _process(pos, pos.next_idx, len(ts1))
        if pos.qty_open > 0:
            _close(pos, float(c1[-1]), int(ts1[-1]) + 60, "eod")
    rev = {v: k for k, v in rank.items()}
    for (side, _), rk in event_best.items():
        res.event_funnel[side][rev.get(rk, "NO_DIV")] += 1
    return res


def summarize(res: Result) -> dict:
    long_t = [t for t in res.trades if t["side"] == "long"]
    short_t = [t for t in res.trades if t["side"] == "short"]
    curve = res.equity_curve
    peak = float(res.cfg["account"]["init_equity"])
    max_dd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100 if peak > 0 else 0.0)
    monthly: dict[str, dict] = {}
    for t in res.trades:
        m = datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc).strftime("%Y-%m")
        row = monthly.setdefault(m, {"n": 0, "pnl": 0.0, "R": 0.0})
        row["n"] += 1
        row["pnl"] += t["pnl"]
        row["R"] += t["pnl_R"]
    allm = _metrics(res.trades)
    allm["max_dd_pct"] = max_dd
    return {
        "all": allm,
        "long": _metrics(long_t),
        "short": _metrics(short_t),
        "funnel": {k: dict(v) for k, v in res.funnel.items()},
        "event_funnel": {k: dict(v) for k, v in res.event_funnel.items()},
        "raw_div": dict(res.raw_div),
        "monthly": monthly,
        "funding_imputed": res.funding_imputed,
        "missing_entry": res.missing_entry,
        "equity_end": curve[-1][1] if curve else 0.0,
        "round_trip_bps": round_trip_bps(res.cfg),
    }
