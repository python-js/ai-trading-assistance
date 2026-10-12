"""P2-6（A2）多模态读图票：K 线截图 → 视觉模型第二意见 → signal_vote(vote_type='chart')。

- 定位：辅助票，不单独决策（增强不拦）；strength ∈ [0,1] 为对信号方向的支持度
  （stance=agree → 0.5+0.5*strength；oppose → 0.5-0.5*strength；neutral → 0.5）。
- 只走视觉模型（LLM_MODEL_VISION 链）；渲染/传输/解析任一失败 → None（不阻塞推送）。
- 图片经 data URL 内联（base64），与推送截图共用同一渲染产物。
"""
from __future__ import annotations

import base64
import json
import pathlib
import re

from app.adapters.chart.mplfinance_chart import render
from app.adapters.llm import gateway
from app.core.fmt import fmt_price
from app.core.tz import to_bj
from app.engine.indicators import enrich
from app.models import db

CHART_DIR = "data/charts"
STANCES = ("agree", "oppose", "neutral")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

_SYSTEM = ("你是加密交易形态分析师。请观察K线图（含 EMA20/60 均线与成交量）与给定信号，"
           "独立给出第二意见。仅输出 JSON："
           '{"stance":"agree|oppose|neutral","strength":0~1,"reason":"≤80字中文"}。'
           "strength 为对所选 stance 的置信强度；reason 概述形态依据（如趋势结构、量价配合、"
           "关键位）。不要给出确定性价格承诺。")


def render_chart(conn, s: dict) -> str | None:
    """渲染信号截图（与推送同路径 signal_{id}.png）；失败返回 None（不抛）。"""
    try:
        df = db.load_ohlcv(conn, s["symbol"], s["exchange"], s["contract_type"],
                           s["timeframe"], limit=600)
        ind = enrich(df)
        return render(ind, [s], f"{CHART_DIR}/signal_{s['signal_id']}.png",
                      title=f"{s['symbol']} {s['timeframe']} {s['rule']}",
                      exchange=s["exchange"])
    except Exception as e:
        print(f"[读图] 截图渲染失败: {type(e).__name__}: {e}", flush=True)
        return None


def run(s: dict, img_path: str) -> tuple[float, dict] | None:
    """读图票：返回 (支持度, evidence) 或 None（失败降级）。"""
    try:
        b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
        d = "买入" if s["direction"] == "entry" else "卖出"
        ctx = (f"信号：{d} {s['symbol']} {s['timeframe']}，价格 {fmt_price(s['price'], s['exchange'])}"
               f"（区间 {fmt_price(s['price_zone_low'], s['exchange'])}~{fmt_price(s['price_zone_high'], s['exchange'])}），"
               f"规则 {s['rule']}，时间 {to_bj(s['ts']):%Y-%m-%d %H:%M} 北京时间")
        r = gateway.complete("vision", [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": ctx},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            json_mode=True, timeout=60)
        data = json.loads(_FENCE.sub("", r.text.strip()))
        if not isinstance(data, dict):
            raise ValueError(f"JSON 非对象: {type(data).__name__}")
    except (gateway.LlmError, ValueError, OSError) as e:
        print(f"[读图] 降级（{type(e).__name__}: {str(e)[:80]}）", flush=True)
        return None
    stance = data.get("stance") if data.get("stance") in STANCES else "neutral"
    try:
        conf = max(0.0, min(1.0, float(data.get("strength", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    support = {"agree": 0.5 + 0.5 * conf, "oppose": 0.5 - 0.5 * conf,
               "neutral": 0.5}[stance]
    evidence = {"stance": stance, "confidence": round(conf, 4),
                "reason": str(data.get("reason") or "")[:120], "model": r.model}
    return round(support, 4), evidence
