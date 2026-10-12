"""P2-5 A4 信号解释文案：信号上下文 + 三票明细 → LLM ≤200 字中文理由（强制 JSON）。

- 只"解释已有信号"，不参与是否推送的决策（增强不拦）；理由不落库（展示层字段 s["reason"]）。
- 幻觉防护：后置过滤违禁表述（确定性价格承诺/收益保证/诱导）→ 命中即降级。
- 失败降级：LLM 不可用/输出非法/空/违禁 → 返回 None，推送回落纯信号版（不阻塞）。
"""
from __future__ import annotations

import json
import re

from app.adapters.llm import gateway
from app.core.tz import to_bj

MAX_LEN = 200
_BANNED = re.compile(r"保证|必涨|必跌|稳赚|包赚|无风险|一定会|100%|百分百|必定|梭哈|重仓")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

_SYSTEM = ("你是交易研究助理。基于给定信号与三票数据写一段 ≤200 字的中文研判理由："
           "说明信号逻辑、三票佐证与主要风险。仅输出 JSON：{\"reason\":\"…\"}，无其他文字。"
           "禁止确定性价格承诺、收益保证与诱导性表述（如'保证/必涨/稳赚'）；"
           "不确定处用'偏/可能'等措辞。")

_LABELS = (("tech", "技术票"), ("flow", "资金面票"), ("info", "信息票"))


def _context(s: dict) -> str:
    lines = [f"标的：{s['symbol']}（{s['contract_type']}）  周期：{s['timeframe']}",
             f"信号：{'买入' if s['direction'] == 'entry' else '卖出'}  "
             f"价格 {s['price']:.6g}（区间 {s['price_zone_low']:.6g}~{s['price_zone_high']:.6g}）",
             f"规则：{s['rule']}  时间：{to_bj(s['ts']):%Y-%m-%d %H:%M} 北京时间"]
    v = s.get("votes") or {}
    for k, label in _LABELS:
        if k not in v:
            continue
        strength, ev = v[k]
        if strength is None:
            lines.append(f"{label}：缺席（{ev.get('skip', '无数据')}）")
        else:
            lines.append(f"{label}：{strength:.2f}（{_ev_brief(k, ev)}）")
    return "\n".join(lines)


def _ev_brief(kind: str, ev: dict) -> str:
    if kind == "tech":
        return (f"RSI {ev.get('rsi14')}，量比 {ev.get('vol_ratio')}，"
                f"ATR {(ev.get('atr_pct') or 0) * 100:.2f}%")
    if kind == "flow":
        fr, oic = ev.get("funding_rate"), ev.get("oi_change_24h")
        parts = []
        if fr is not None:
            parts.append(f"funding {fr * 100:.4f}%/结算")
        if oic is not None:
            parts.append(f"OI24h {oic * 100:+.2f}%")
        return "，".join(parts) or "—"
    top = (ev.get("top") or [{}])[0]
    return f"{ev.get('n_events')} 条事件，分数 {ev.get('score')}，代表：{top.get('title', '—')}"


def build_reason(s: dict) -> str | None:
    """返回 ≤200 字理由；任何失败/违禁返回 None（调用方按纯信号推送降级）。"""
    try:
        r = gateway.complete("explain", [{"role": "system", "content": _SYSTEM},
                                         {"role": "user", "content": _context(s)}],
                             json_mode=True, timeout=30)
        data = json.loads(_FENCE.sub("", r.text.strip()))
        reason = str(data.get("reason") or "").strip()
    except (gateway.LlmError, ValueError) as e:
        print(f"[解释] 降级为纯信号推送（{type(e).__name__}: {str(e)[:80]}）", flush=True)
        return None
    if not reason:
        print("[解释] 输出为空，降级", flush=True)
        return None
    if _BANNED.search(reason):
        print(f"[解释] 命中违禁表述，降级：{reason[:60]}", flush=True)
        return None
    return reason[:MAX_LEN]
