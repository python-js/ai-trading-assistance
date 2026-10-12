"""P1-3 签发层事件驱动回测（S5）：rules_dsl.evaluate 逐 bar 事件 + 台账口径成本。

口径（与 P1-1 台账完全同语义）：信号 bar 收盘价成交（PIT 无未来函数）、cooldown/
状态机保真、taker 单边 0.05% + 滑点单边 5bps、每笔名义本金 1000 USDT（固定名义不复利）；
与研究层（vectorbt：次 bar 成交、无 cooldown、全仓复利）的差异见 compare()。
净值 = 初始资金 + 已实现盈亏 + 持仓按收盘价的可平仓浮盈（含双边成本），逐 bar 标记。
"""
from __future__ import annotations

import pandas as pd

from app.engine import flow, ledger
from app.engine.indicators import enrich
from app.engine.rules_dsl import evaluate, parse_rule
from app.models import db

INIT_CASH = 10_000.0


def run_rule(conn, rule: dict, *, start=None, end=None, fees: float = ledger.FEES,
             slip_bps: float = ledger.SLIP_BPS, notional: float = ledger.NOTIONAL,
             init_cash: float = INIT_CASH) -> dict:
    """rule: 含 symbol/exchange/contract_type/timeframe/dsl_json 的 dict。
    返回 {trades, equity(ts,value), metrics, open_pos, n_events}；确定性可复现。"""
    df = db.load_ohlcv(conn, rule["symbol"], rule["exchange"], rule["contract_type"],
                       rule["timeframe"], start=start, end=end)
    ind = enrich(df)
    ind = flow.attach_if_needed(conn, ind, [rule], rule["symbol"], rule["exchange"])
    events = evaluate(parse_rule(rule["dsl_json"]), ind)
    slip = slip_bps / 10_000

    trades: list[dict] = []
    equity: list[tuple] = []
    realized = 0.0
    exposure = 0
    entries = 0
    pos = None
    ev_i = 0
    rows = list(ind.itertuples(index=False))
    for i, r in enumerate(rows):
        ts, close, low = r.ts, float(r.close), float(r.low)
        while ev_i < len(events) and events[ev_i].ts <= ts:
            e = events[ev_i]
            ev_i += 1
            if e.direction == "entry" and pos is None:
                entry_eff = e.price * (1 + slip)
                qty = notional / entry_eff
                entries += 1
                pos = {"ix": i, "entry_ts": ts, "entry_price": e.price, "qty": qty,
                       "cost_in": notional * (1 + fees), "entry_eff": entry_eff,
                       "low_min": low}
            elif e.direction == "exit" and pos is not None:
                pos["low_min"] = min(pos["low_min"], low)  # 与台账同口径：含出场 bar
                proceeds = pos["qty"] * e.price * (1 - slip) * (1 - fees)
                pnl = proceeds - pos["cost_in"]
                trades.append({
                    "entry_ts": pos["entry_ts"], "exit_ts": ts,
                    "entry_price": pos["entry_price"], "exit_price": e.price,
                    "qty": pos["qty"], "pnl": pnl, "pnl_pct": pnl / pos["cost_in"],
                    "fees": notional * fees + pos["qty"] * e.price * fees,
                    "hold_bars": i - pos["ix"],
                    "mae": pos["low_min"] / pos["entry_eff"] - 1})
                realized += pnl
                pos = None
        if pos is not None:
            exposure += 1
            pos["low_min"] = min(pos["low_min"], low)
            unreal = pos["qty"] * close * (1 - slip) * (1 - fees) - pos["cost_in"]
        else:
            unreal = 0.0
        equity.append((ts, init_cash + realized + unreal))

    open_pos = None
    if pos is not None:
        open_pos = {"entry_ts": pos["entry_ts"], "entry_price": pos["entry_price"],
                    "unrealized": equity[-1][1] - init_cash - realized}
    return {"trades": trades, "equity": equity, "open_pos": open_pos,
            "n_events": len(events), "n_entries": entries,
            "metrics": _metrics(trades, equity, init_cash, exposure, len(rows))}


def _metrics(trades: list[dict], equity: list[tuple], init_cash: float,
             exposure: int, n_bars: int) -> dict:
    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    if not n:
        pf = None
    elif not losses:
        pf = float("inf")
    else:
        pf = sum(wins) / -sum(losses)
    eq = pd.Series([v for _, v in equity])
    dd = float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0
    return {"n_trades": n,
            "win_rate": (len(wins) / n) if n else None,
            "pf": pf,
            "avg_pnl_pct": (sum(t["pnl_pct"] for t in trades) / n) if n else None,
            "total_pnl": sum(pnls),
            "return_on_init[%]": sum(pnls) / init_cash * 100,
            "max_drawdown[%]": dd * 100,
            "fees_total": sum(t["fees"] for t in trades),
            "avg_hold_bars": (sum(t["hold_bars"] for t in trades) / n) if n else None,
            "exposure[%]": (exposure / n_bars * 100) if n_bars else 0.0}


def compare(conn, rule: dict, *, fees: float = ledger.FEES,
            slip_bps: float = ledger.SLIP_BPS, seed: int = 42) -> dict:
    """签发层 vs 研究层（vectorbt）同数据对照。研究层不支持 chandelier 规则时
    返回 vbt_error；差异主因：状态机/cooldown、成交位移（同 bar 收盘 vs 次 bar）。"""
    ev = run_rule(conn, rule, fees=fees, slip_bps=slip_bps)
    out = {"event": ev["metrics"], "vbt": None, "vbt_error": None,
           "vbt_entries": None, "n_events": ev["n_events"]}
    try:  # 研究层导入较重，仅在对照时加载
        from app.engine.backtest_vectorbt import (metrics as vbt_metrics,
                                                  run_portfolio, signals_from_rule)

        df = db.load_ohlcv(conn, rule["symbol"], rule["exchange"], rule["contract_type"],
                           rule["timeframe"])
        ind = enrich(df)
        ind = flow.attach_if_needed(conn, ind, [rule], rule["symbol"], rule["exchange"])
        e, x = signals_from_rule(parse_rule(rule["dsl_json"]), ind)
        idx = pd.DatetimeIndex(ind["ts"])
        close = pd.Series(ind["close"].to_numpy(), index=idx)
        e = pd.Series(e.to_numpy(), index=idx)
        x = pd.Series(x.to_numpy(), index=idx)
        pf = run_portfolio(close, e, x, fees=fees, slippage_bps=slip_bps,
                           freq=rule["timeframe"], seed=seed)
        out["vbt"] = vbt_metrics(pf, close, rule["timeframe"])
        out["vbt_entries"] = int(e.sum())
    except Exception as e:  # 研究层无法表达该规则（如 chandelier）
        out["vbt_error"] = f"{type(e).__name__}: {e}"
    return out


def render_compare(rule_name: str, cmp: dict) -> str:
    ev = cmp["event"]
    lines = [f"### 签发回测对照 {rule_name}",
             f"- 签发层（事件驱动·台账口径）：{ev['n_trades']} 笔 · 胜率 "
             f"{ev['win_rate']:.1%} · 盈亏比 {_pf(ev['pf'])} · 累计 {ev['total_pnl']:+.1f} USDT · "
             f"最大回撤 {ev['max_drawdown[%]']:.2f}% · 事件 {cmp['n_events']}"
             if ev["n_trades"] else
             f"- 签发层：0 笔（事件 {cmp['n_events']}）"]
    if cmp["vbt"]:
        v = cmp["vbt"]
        tr = v["交易笔数"]
        lines.append(f"- 研究层（vectorbt·次bar成交）：{tr} 笔 · 胜率 "
                     f"{(v['胜率[%]'] or 0):.1f}% · 盈亏比 {v['盈亏比']} · "
                     f"策略收益 {v['策略收益[%]']}%")
        lines.append(f"> 差异主因：研究层无 cooldown/状态机（入场更多）、次 bar 成交、"
                     f"全仓复利；签发层为实盘同语义（cooldown + 同 bar 收盘 + 固定名义）。"
                     f"研究层入场信号 {cmp['vbt_entries']} 个")
    elif cmp["vbt_error"]:
        lines.append(f"- 研究层：不可对照（{cmp['vbt_error']}）")
    return "\n".join(lines)


def _pf(pf) -> str:
    if pf is None:
        return "-"
    return "∞" if pf == float("inf") else f"{pf:.2f}"
