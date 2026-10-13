"""模拟盘引擎（A6）：多标的共享资金池 → 固定比例复利 → 止损线/到期冻结，确定性重放。

口径与台账/签发回测同源（PIT：信号 bar 收盘价成交、无未来函数）：
- 币圈：taker 单边 0.05% + 滑点单边 5bps；A股：佣金万 2.5（最低 5 元/笔）+
  印花税千 0.5（仅卖出）+ 滑点单边 5bps（与签发层 ledger 一致）。
- 每笔名义 = min(当前权益 × size_pct%, 可用现金)；多标的共享一份资金池（同一市场）。
- 策略时间线：每单元按变更分段评估（同一 evaluate 入口、同一预热数据，段窗口过滤事件）；
  换策略后旧仓位由新策略的出场条件管理（持仓状态跨段延续）。
- 冻结：每根 bar 收盘先核权益 ≤ 初始资金 × (1 - stop_loss_pct%) → 按最后收盘价全部强平
  （stop_loss，当根信号不再执行）；时间到期 → 清算（expired）；手动结束（status=finished）→ manual。
- 纯函数按需重放：同一数据必得同一结果；数据截止 = min(now, end_ts)，不含未来。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pandas as pd

from app.adapters.llm import gateway
from app.engine import flow, ledger
from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate, parse_rule
from app.models import db

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
          "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
PRE_BARS = WARMUP_BARS + 50  # 起点前预热根数（指标/cooldown 收敛）


def tf_ms(tf: str) -> int:
    return _TF_MS[tf]


def _ts(v) -> pd.Timestamp:
    """归一为 tz-aware UTC 的 pandas Timestamp（DB 读回与内部计算统一）。"""
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _slip() -> float:
    return ledger.SLIP_BPS / 10_000


def _is_stock(exchange: str) -> bool:
    return exchange == "akshare"


# ---------- 成本模型（与 ledger._settle 同口径） ----------

def entry_cost(notional: float, stock: bool) -> float:
    """入场现金支出（名义 + 费用）。"""
    if stock:
        return notional + max(notional * ledger.COMM_A, ledger.MIN_COMM_A)
    return notional * (1 + ledger.FEES)


def exit_proceeds(qty: float, price: float, stock: bool) -> float:
    """出场可平仓净额（扣滑点与费用）。"""
    gross = qty * price * (1 - _slip())
    if stock:
        return gross - max(gross * ledger.COMM_A, ledger.MIN_COMM_A) - gross * ledger.STAMP_A
    return gross * (1 - ledger.FEES)


def exit_fees(qty: float, price: float, stock: bool) -> float:
    if stock:
        gross = qty * price * (1 - _slip())
        return max(gross * ledger.COMM_A, ledger.MIN_COMM_A) + gross * ledger.STAMP_A
    return qty * price * ledger.FEES


# ---------- 重放 ----------

def _load_unit(conn, u: dict, start: pd.Timestamp, cutoff: pd.Timestamp):
    """加载单元数据（起点前 PRE_BARS 根预热）→ enrich → 资金面注入 → 分段事件。

    返回 dict（ts/close/low/events/stock/symbol/timeframe）或 None（数据不足）。"""
    lo = start - pd.Timedelta(milliseconds=tf_ms(u["timeframe"]) * PRE_BARS)
    df = db.load_ohlcv(conn, u["symbol"], u["exchange"], u["contract_type"],
                       u["timeframe"], start=lo.to_pydatetime())
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[df["ts"] <= cutoff].reset_index(drop=True)
    if len(df) < WARMUP_BARS + 2:
        return None
    ind = enrich(df)
    ind = flow.attach_if_needed(conn, ind, [{"dsl_json": c["dsl_json"]}
                                            for c in u["changes"]],
                                u["symbol"], u["exchange"])
    changes = sorted(u["changes"], key=lambda c: _ts(c["effective_from"]))
    events: list[tuple] = []
    for i, c in enumerate(changes):
        seg_lo = max(_ts(c["effective_from"]), start)
        seg_hi = _ts(changes[i + 1]["effective_from"]) if i + 1 < len(changes) else None
        if seg_hi is not None and seg_hi <= seg_lo:
            continue
        for e in evaluate(parse_rule(c["dsl_json"]), ind):
            t = _ts(e.ts)
            if t < seg_lo or t > cutoff:
                continue
            if seg_hi is not None and t >= seg_hi:
                continue
            events.append((t, e.direction, float(e.price), c["name"]))
    events.sort(key=lambda x: x[0])
    return {"ts": pd.DatetimeIndex(ind["ts"]),
            "close": ind["close"].tolist(), "low": ind["low"].tolist(),
            "events": events, "stock": _is_stock(u["exchange"]),
            "symbol": u["symbol"], "timeframe": u["timeframe"]}


def _metrics(trades: list[dict], equity: list[tuple], init_cash: float,
             exposure: int, n_marks: int) -> dict:
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
    dd = 0.0
    if equity:
        peak = equity[0][1]
        for _, v in equity:
            if v > peak:
                peak = v
            dd = min(dd, v / peak - 1 if peak else 0.0)
    return {"n_trades": n,
            "win_rate": (len(wins) / n) if n else None,
            "pf": pf,
            "avg_pnl_pct": (sum(t["pnl_pct"] for t in trades) / n) if n else None,
            "total_pnl": sum(pnls),
            "return_on_init[%]": sum(pnls) / init_cash * 100,
            "max_drawdown[%]": dd * 100,
            "fees_total": sum(t["fees"] for t in trades),
            "avg_hold_hours": (sum(t["hold_hours"] for t in trades) / n) if n else None,
            "exposure[%]": (exposure / n_marks * 100) if n_marks else 0.0}


def _sample(pts: list[tuple], max_n: int) -> list[tuple]:
    if len(pts) <= max_n:
        return pts
    idxs = sorted({int(i * (len(pts) - 1) / (max_n - 1)) for i in range(max_n)})
    return [pts[i] for i in idxs]


def run_account(conn, acct: dict, *, now: datetime | None = None,
                max_points: int = 1500) -> dict:
    """确定性重放一个模拟盘。acct 由 API 层组装：

    {init_cash, size_pct, stop_loss_pct, start_ts, end_ts, status,
     units: [{symbol, exchange, contract_type, timeframe,
              changes: [{effective_from, name, dsl_json}]}]}
    """
    now = now or datetime.now(timezone.utc)
    start, end = _ts(acct["start_ts"]), (_ts(acct["end_ts"]) if acct.get("end_ts") else None)
    cutoff = min(_ts(now), end) if end else _ts(now)
    size_frac = float(acct["size_pct"]) / 100.0
    init_cash = float(acct["init_cash"])
    stop = float(acct.get("stop_loss_pct") or 0)
    floor = init_cash * (1 - stop / 100.0) if stop > 0 else None

    udata = [_load_unit(conn, u, start, cutoff) for u in acct["units"]]
    marks = None
    for d in udata:
        if d is None:
            continue
        idx = d["ts"]
        idx = idx[(idx >= start) & (idx <= cutoff)]
        marks = idx if marks is None else marks.union(idx)

    cash, realized = init_cash, 0.0
    positions: dict[int, dict] = {}
    trades: list[dict] = []
    equity: list[tuple] = []
    exposure = n_events = 0
    reason = None
    ptrs = [0] * len(udata)
    ev_ptrs = [0] * len(udata)
    last_close: list[float | None] = [None] * len(udata)
    n_marks = len(marks) if marks is not None else 0

    def _equity() -> float:
        return cash + sum(exit_proceeds(p["qty"], last_close[ui], udata[ui]["stock"])
                          for ui, p in positions.items())

    def _close(ui: int, price: float, ts_exit, why: str) -> float:
        nonlocal cash, realized
        d = udata[ui]
        pos = positions.pop(ui)
        proceeds = exit_proceeds(pos["qty"], price, d["stock"])
        pnl = proceeds - pos["cost_in"]
        cash += proceeds
        realized += pnl
        trades.append({
            "unit_idx": ui, "symbol": d["symbol"], "timeframe": d["timeframe"],
            "strategy": pos["strategy"], "entry_ts": pos["entry_ts"], "exit_ts": ts_exit,
            "entry_price": pos["entry_price"], "exit_price": price,
            "qty": pos["qty"], "notional": pos["notional"], "pnl": pnl,
            "pnl_pct": pnl / pos["cost_in"],
            "fees": (pos["cost_in"] - pos["notional"]) + exit_fees(pos["qty"], price, d["stock"]),
            "hold_hours": (ts_exit - pos["entry_ts"]).total_seconds() / 3600,
            "mae": pos["low_min"] / pos["entry_eff"] - 1,
            "exit_reason": why})
        return pnl

    if marks is not None:
        for t in marks:
            for ui, d in enumerate(udata):  # 推进各单元指针：最新收盘价 + 持仓最低价
                if d is None:
                    continue
                arr, p = d["ts"], ptrs[ui]
                while p < len(arr) and arr[p] <= t:
                    last_close[ui] = d["close"][p]
                    if ui in positions:
                        positions[ui]["low_min"] = min(positions[ui]["low_min"], d["low"][p])
                    p += 1
                ptrs[ui] = p
            value = _equity()
            if floor is not None and value <= floor:  # 止损线优先：强平冻结，当根信号不再执行
                reason = "stop_loss"
                for ui in list(positions):
                    _close(ui, last_close[ui], t, "stop_loss")
                equity.append((t, value))
                break
            for ui, d in enumerate(udata):  # 执行该时刻事件（entry/exit）
                if d is None:
                    continue
                ev = d["events"]
                while ev_ptrs[ui] < len(ev) and ev[ev_ptrs[ui]][0] == t:
                    _, direction, price, name = ev[ev_ptrs[ui]]
                    ev_ptrs[ui] += 1
                    n_events += 1
                    if direction == "entry" and ui not in positions and last_close[ui] is not None:
                        desired = min(_equity() * size_frac, cash)
                        cost = entry_cost(desired, d["stock"])
                        if cost > cash and cost > 0:
                            desired *= cash / cost
                            cost = entry_cost(desired, d["stock"])
                        if desired > 0 and cost <= cash:
                            entry_eff = price * (1 + _slip())
                            cash -= cost
                            positions[ui] = {
                                "qty": desired / entry_eff, "entry_price": price,
                                "entry_eff": entry_eff, "cost_in": cost,
                                "notional": desired, "strategy": name,
                                "entry_ts": t, "low_min": d["low"][ptrs[ui] - 1]}
                    elif direction == "exit" and ui in positions:
                        _close(ui, price, t, "signal")
            value = _equity()
            equity.append((t, value))
            if positions:
                exposure += 1

    if reason is None and end is not None and end <= _ts(now):  # 时间走完：到期/手动结束
        reason = "manual" if acct.get("status") == "finished" else "expired"
        if positions:
            t_end = marks[-1] if n_marks else _ts(now)
            for ui in list(positions):
                _close(ui, last_close[ui], t_end, reason)

    open_pos = [{"unit_idx": ui, "symbol": udata[ui]["symbol"],
                 "timeframe": udata[ui]["timeframe"], "strategy": p["strategy"],
                 "entry_ts": p["entry_ts"].isoformat(), "entry_price": p["entry_price"],
                 "qty": p["qty"], "notional": p["notional"],
                 "unrealized": exit_proceeds(p["qty"], last_close[ui],
                                             udata[ui]["stock"]) - p["cost_in"]}
                for ui, p in positions.items()]
    return {"status": "finished" if reason else "active", "end_reason": reason,
            "end_ts": equity[-1][0].isoformat() if equity else None,
            "cash": cash, "realized": realized,
            "equity": [{"time": int(t.timestamp()), "value": v}
                       for t, v in _sample(equity, max_points)],
            "trades": [{**t, "entry_ts": t["entry_ts"].isoformat(),
                        "exit_ts": t["exit_ts"].isoformat()} for t in trades],
            "open_pos": open_pos,
            "metrics": _metrics(trades, equity, init_cash, exposure, n_marks),
            "n_marks": n_marks, "n_events": n_events}


# ---------- 复盘（确定性统计 + LLM 建议，可关） ----------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_BANNED = re.compile(r"保证|必涨|必跌|稳赚|包赚|无风险|一定会|100%|百分百|必定|梭哈|重仓")

_REVIEW_SYS = ("你是交易复盘助手。基于模拟盘统计写一段 ≤300 字的中文复盘："
               "先归因（对照各标的、策略段、费用与回撤数据说明盈亏来源），"
               "再给 2~3 条具体可执行的改进建议（如仓位、周期、策略参数方向）。"
               "不确定处用'偏/可能'等措辞；禁止确定性价格承诺、收益保证与诱导表述"
               "（如'保证/必赚/稳赚/梭哈'）；仅输出 JSON：{\"review\":\"…\"}，无其他文字。")


def _agg(rows: list[dict]) -> dict:
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {"n": len(rows),
            "win_rate": (len(wins) / len(rows)) if rows else None,
            "total_pnl": sum(pnls),
            "pf": None if not rows or not losses else sum(wins) / -sum(losses)}


def review_stats(result: dict, acct: dict) -> dict:
    """确定性复盘：总览 + 按单元/策略段聚合 + 归因要点（不依赖 LLM）。"""
    trades = result["trades"]
    m = result["metrics"]
    by_unit: dict[tuple, list] = {}
    by_seg: dict[tuple, list] = {}
    for tr in trades:
        by_unit.setdefault((tr["symbol"], tr["timeframe"]), []).append(tr)
        by_seg.setdefault((tr["unit_idx"], tr["strategy"]), []).append(tr)
    units = acct["units"]
    seg_rows = [{"unit_idx": k[0], "symbol": units[k[0]]["symbol"],
                 "timeframe": units[k[0]]["timeframe"], "strategy": k[1], **_agg(v)}
                for k, v in sorted(by_seg.items())]
    unit_rows = [{"symbol": k[0], "timeframe": k[1], **_agg(v)}
                 for k, v in sorted(by_unit.items())]

    notes: list[str] = []
    if not trades:
        notes.append("窗口内没有完成的交易：可能起点过近、标的无信号或数据不足。")
    else:
        worst = min(unit_rows, key=lambda r: r["total_pnl"])
        if worst["total_pnl"] < 0:
            notes.append(f"亏损最大的标的：{worst['symbol']} {worst['timeframe']}，"
                         f"{worst['n']} 笔累计 {worst['total_pnl']:+.1f}，"
                         f"胜率 {(worst['win_rate'] or 0):.0%}")
        if m["total_pnl"] < 0 and m["fees_total"] > 0:
            ratio = m["fees_total"] / abs(m["total_pnl"])
            if ratio >= 0.2:
                notes.append(f"费用合计 {m['fees_total']:.1f}，占亏损的 {ratio:.0%}"
                             f"——换手频繁（平均持仓 {m['avg_hold_hours'] or 0:.1f} 小时）是损耗因素之一")
        notes.append(f"最大回撤 {m['max_drawdown[%]']:.1f}%，"
                     f"持仓暴露 {m['exposure[%]']:.0f}%")
        seg_by_unit: dict[int, set] = {}
        for ui_, strategy in by_seg:
            seg_by_unit.setdefault(ui_, set()).add(strategy)
        if any(len(v) > 1 for v in seg_by_unit.values()):  # 仅有真实策略切换时给段落对照
            seg_desc = "；".join(f"「{r['symbol']}·{r['strategy']}」{r['n']} 笔 {r['total_pnl']:+.1f}"
                                for r in seg_rows)
            notes.append(f"策略段对照：{seg_desc}（可对比切换前后表现）")
        stop_n = sum(1 for tr in trades if tr["exit_reason"] == "stop_loss")
        if stop_n:
            notes.append(f"{stop_n} 笔因止损线强平")
    return {"overall": m, "by_unit": unit_rows, "by_segment": seg_rows, "notes": notes}


def llm_review(rev: dict, acct: dict, result: dict) -> tuple[str | None, str | None]:
    """LLM 复盘建议；失败/违禁返回 (None, 原因)（调用方降级只给确定性统计）。"""
    summary = {"盘": acct.get("name"), "市场": acct["market"],
               "初始资金": acct["init_cash"], "每笔仓位%": acct["size_pct"],
               "止损线%": acct["stop_loss_pct"],
               "结束": result["end_reason"] or "进行中",
               "总览": rev["overall"], "按标的": rev["by_unit"],
               "策略段": rev["by_segment"], "归因要点": rev["notes"]}
    try:
        r = gateway.complete("explain", [
            {"role": "system", "content": _REVIEW_SYS},
            {"role": "user", "content": json.dumps(summary, ensure_ascii=False, default=str)}],
            json_mode=True, timeout=60)
        text = str(json.loads(_FENCE.sub("", r.text.strip())).get("review") or "").strip()
    except (gateway.LlmError, ValueError) as e:
        return None, f"{type(e).__name__}: {str(e)[:100]}"
    if not text:
        return None, "输出为空"
    if _BANNED.search(text):
        return None, "命中违禁表述"
    return text[:400], None
