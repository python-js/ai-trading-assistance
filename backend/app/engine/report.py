"""P1-2 台账报表（S6）：按 exit_ts 窗口聚合已平仓 → 指标 + 企微 markdown。

指标：n 笔数 / 胜率（pnl>0）/ 盈亏比 PF=Σ盈/|Σ亏| / 平均收益 pnl_pct / 累计盈亏 USDT。
分组：总览 + 按周期 + 按标的。窗口左闭右开 [start, end)，均为 UTC。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_QUERY = """
SELECT p.symbol, r.timeframe, p.exit_ts, p.pnl, p.pnl_pct
FROM paper_position p
JOIN signal s ON s.id = p.signal_id
JOIN rule r   ON r.id = s.rule_id
WHERE p.status = 'closed' AND p.exit_ts >= %s AND p.exit_ts < %s
ORDER BY p.exit_ts
"""


def last_week_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """上周一 00:00 ~ 本周一 00:00 UTC（周报窗口；周一 01:00 调度运行时恰为完整上周）。"""
    now = now or datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=7), end


def load_closed(conn, start: datetime, end: datetime) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_QUERY, (start, end))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    pnls = [float(r["pnl"]) for r in rows]
    pcts = [float(r["pnl_pct"]) for r in rows]
    win = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p < 0]
    if n == 0:
        pf = None
    elif not loss:
        pf = float("inf")
    else:
        pf = sum(win) / -sum(loss)
    return {"n": n,
            "win_rate": (len(win) / n) if n else None,
            "pf": pf,
            "avg_pnl_pct": (sum(pcts) / n) if n else None,
            "total_pnl": sum(pnls)}


def build_report(conn, start: datetime, end: datetime) -> dict:
    rows = load_closed(conn, start, end)
    return {
        "start": start, "end": end,
        "overall": summarize(rows),
        "by_timeframe": {tf: summarize([r for r in rows if r["timeframe"] == tf])
                         for tf in sorted({r["timeframe"] for r in rows})},
        "by_symbol": {sym: summarize([r for r in rows if r["symbol"] == sym])
                      for sym in sorted({r["symbol"] for r in rows})},
    }


def _pf(pf) -> str:
    if pf is None:
        return "-"
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _rate(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def _group_line(label: str, s: dict) -> str:
    return (f"- {label}：{s['n']} 笔 · 胜率 {_rate(s['win_rate'])} · "
            f"盈亏比 {_pf(s['pf'])} · 累计 {s['total_pnl']:+.1f} USDT")


def render_markdown(rep: dict) -> str:
    o = rep["overall"]
    lines = [f"### 台账报表 {rep['start']:%m-%d} ~ {rep['end']:%m-%d} (UTC)"]
    if o["n"] == 0:
        lines.append("**总览**：窗口内无已平仓交易")
    else:
        lines.append(f"**总览**：{o['n']} 笔 · 胜率 {_rate(o['win_rate'])} · 盈亏比 {_pf(o['pf'])} · "
                     f"均收益 {o['avg_pnl_pct']:+.2%} · 累计 {o['total_pnl']:+.1f} USDT")
    if rep["by_timeframe"]:
        lines.append("**按周期**")
        lines += [_group_line(tf, s) for tf, s in rep["by_timeframe"].items()]
    if rep["by_symbol"]:
        lines.append("**按标的**")
        lines += [_group_line(sym, s) for sym, s in rep["by_symbol"].items()]
    lines.append("> 口径：信号 bar 收盘价成交，taker 0.05% + 滑点 5bps 双边 · *模拟台账，非投资建议*")
    return "\n".join(lines)
