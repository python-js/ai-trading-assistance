"""P3 A3 自然语言 → 规则 DSL 草稿：LLM(explain 档) 生成 + 严格校验 + 失败回喂重试一次。

- 模型只输出 entry/exit/cooldown_bars；name/symbol/timeframe 由服务端组装（防改写标的）。
- 校验 = rules_dsl.parse_rule（结构）+ 列白名单（防虚构指标名——等到回测才报错太晚）；
  失败把出错原文与原因回喂 LLM 重试一次，再失败抛 RuleGenError（API 层转 422）。
- 草稿是否入库/是否启用由 API 层决定（默认 enabled=false，人审=开关）。
"""
from __future__ import annotations

import json
import re

from app.adapters.llm import gateway
from app.engine.rules_dsl import parse_rule

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

# 评估时 df 可用列（indicators.enrich + flow.attach 产物；增列需同步这两处）
COLS = {"sma5", "sma10", "sma20", "rsi14", "atr14", "close", "high", "low",
        "funding_rate", "oi_change_24h"}

_SYSTEM = (
    "你是量化规则设计助手，把中文交易想法翻译成规则 DSL（JSON）。"
    "只输出一个 JSON 对象：{\"entry\": <节点>, \"exit\": <节点>, \"cooldown_bars\": <非负整数>}，"
    "无其他文字。\n"
    "节点语法（每层只允许一个键）：\n"
    "1. 比较 {\"指标\": {\"<\": 70}}：指标在外层、算子在里层；算子 ∈ < > <= >=；"
    "可用指标：sma5、sma10、sma20（简单移动平均，与 K 线图 MA 线同口径）、"
    "rsi14、atr14（绝对波幅，单位为报价货币）、close（收盘价）、"
    "funding_rate（资金费率，小数）、oi_change_24h（持仓量 24h 变化率，小数）。\n"
    "2. 组合 {\"all\": [节点,...]} 全部满足；{\"any\": [节点,...]} 任一满足。\n"
    "3. 交叉 {\"cross_up\": [\"sma5\",\"sma20\"]} 前线上穿后线；cross_down 为下穿。\n"
    "4. 突破 {\"breakout_up\": 20} 收盘价上破前 20 根最高价；breakout_down 为下破最低价。\n"
    "5. 出场专用 {\"chandelier_exit\": {\"atr_mult\": 3}}（吊灯止损，只可用于 exit）。\n"
    "完整示例（想法：RSI 低于 30 买入，高于 70 卖出）："
    "{\"entry\": {\"rsi14\": {\"<\": 30}}, \"exit\": {\"rsi14\": {\">\": 70}}, "
    "\"cooldown_bars\": 0}\n"
    "特别注意：比较节点是「指标在外、算子在里」，如 {\"rsi14\": {\"<\": 30}}；"
    "禁止写成 {\"<\": {\"rsi14\": 30}} 这种算子在外的形式。\n"
    "约束：entry/exit 必填且只能使用上述指标；chandelier_exit 仅 exit；"
    "cooldown_bars 常用 0~30；不得编造指标名；不确定时用简单组合，宁简勿繁。"
)


class RuleGenError(ValueError):
    """生成两轮仍无法通过校验。"""


def _check_cols(node: dict) -> None:
    """列白名单（在 parse_rule 通过后调用，节点结构已合法）。"""
    key, val = next(iter(node.items()))
    if key in ("all", "any"):
        for sub in val:
            _check_cols(sub)
    elif key in ("cross_up", "cross_down"):
        bad = [c for c in val if c not in COLS]
        if bad:
            raise ValueError(f"未支持的指标列: {bad}")
    elif key in ("breakout_up", "breakout_down", "chandelier_exit"):
        return
    elif key not in COLS:
        raise ValueError(f"未支持的指标列: {key}")


def generate(text: str, symbol: str, timeframe: str) -> dict:
    """返回 {entry, exit, cooldown_bars}（已校验）。全链 LLM 失败抛 gateway.LlmError。"""
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"标的：{symbol}（{timeframe}）\n想法：{text}"}]
    last_err: Exception | None = None
    for attempt in range(2):
        raw = ""
        try:
            r = gateway.complete("explain", messages, json_mode=True, timeout=30)
            raw = r.text.strip()
            data = json.loads(_FENCE.sub("", raw))
            if not isinstance(data, dict):
                raise ValueError("输出须为 JSON 对象")
            full = {"name": "_draft_", "symbol": symbol, "timeframe": timeframe,
                    "entry": data.get("entry"), "exit": data.get("exit"),
                    "cooldown_bars": data.get("cooldown_bars", 0)}
            parse_rule(full)
            _check_cols(full["entry"])
            _check_cols(full["exit"])
            return {"entry": full["entry"], "exit": full["exit"],
                    "cooldown_bars": full["cooldown_bars"]}
        except gateway.LlmError:
            raise
        except ValueError as e:
            last_err = e
            print(f"[规则生成] 第 {attempt + 1} 轮校验未过：{e}", flush=True)
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw[:2000]})
                messages.append({"role": "user",
                                 "content": f"校验失败：{e}\n请修正后重新只输出 JSON。"})
    raise RuleGenError(str(last_err))
