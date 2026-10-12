"""P0-4 规则 DSL：JSON 解析/校验 + 逐 bar 事件式评估（PIT 安全）。

约束（见 docs/P0-阶段1-设计.md）：
- 逐 bar 循环，第 i 根只用 <= i 的数据；信号按 bar 收盘价观测，执行时点由上层（回测/推送）决定。
- 预热：从第 WARMUP_BARS 根开始评估（P0-3 实测 RSI/ATR 种子暂态约 200 根后收敛）。
- cooldown 在出场后生效；状态机内置于评估器，实盘扫描与事件驱动回测复用同一入口。
- 顶层未实现的键（vote/cost_model）暂忽略，待 P0-5/P2 接入；节点层非法结构一律报错（不静默）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

WARMUP_BARS = 200  # P0-3 实测：EMA 精确，RSI/ATR 约 200 根后与 Wilder 标准收敛

_OPS = {"<", ">", "<=", ">="}
_COMBINE = {"all", "any"}
_PAIR = {"cross_up", "cross_down"}
_BREAK = {"breakout_up", "breakout_down"}


@dataclass
class SignalEvent:
    ts: datetime    # bar 收盘时间
    direction: str  # "entry" | "exit"
    price: float    # 该 bar 收盘价


@dataclass
class Rule:
    name: str
    symbol: str
    timeframe: str
    entry: dict
    exit: dict
    cooldown_bars: int = 0


def parse_rule(raw: dict) -> Rule:
    """校验并归一化 DSL；非法结构抛 ValueError。"""
    for key in ("name", "symbol", "timeframe", "entry", "exit"):
        if key not in raw:
            raise ValueError(f"rule 缺少字段: {key}")
    _check_node(raw["entry"], where="entry", allow_chandelier=False)
    _check_node(raw["exit"], where="exit", allow_chandelier=True)
    cd = raw.get("cooldown_bars", 0)
    if not isinstance(cd, int) or cd < 0:
        raise ValueError("cooldown_bars 须为非负整数")
    return Rule(raw["name"], raw["symbol"], raw["timeframe"], raw["entry"], raw["exit"], cd)


def _check_node(node: dict, *, where: str, allow_chandelier: bool) -> None:
    if not isinstance(node, dict) or len(node) != 1:
        raise ValueError(f"{where} 节点须为单键对象: {node!r}")
    key, val = next(iter(node.items()))
    if key in _COMBINE:
        if not isinstance(val, list) or not val:
            raise ValueError(f"{where}.{key} 须为非空列表")
        for sub in val:
            _check_node(sub, where=where, allow_chandelier=allow_chandelier)
    elif key in _PAIR:
        if not (isinstance(val, list) and len(val) == 2 and all(isinstance(x, str) for x in val)):
            raise ValueError(f"{key} 须为 [col_a, col_b]: {node!r}")
    elif key in _BREAK:
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"{key} 须为正整数 window: {node!r}")
    elif key == "chandelier_exit":
        if not allow_chandelier:
            raise ValueError("chandelier_exit 仅可用于 exit")
        if not isinstance(val, dict) or not isinstance(val.get("atr_mult"), (int, float)):
            raise ValueError("chandelier_exit 须为 {atr_mult: 数值}")
    else:
        if not isinstance(val, dict) or len(val) != 1:
            raise ValueError(f"{where} 指标叶子须为 {{'{key}': {{op: 数值}}}}: {node!r}")
        op, v = next(iter(val.items()))
        if op not in _OPS:
            raise ValueError(f"不支持的算子: {op}（仅 {sorted(_OPS)}）")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"比较值须为数值: {node!r}")


def _required_cols(node: dict, out: set[str]) -> None:
    key, val = next(iter(node.items()))
    if key in _COMBINE:
        for sub in val:
            _required_cols(sub, out)
    elif key in _PAIR:
        out.update(val)
    elif key == "chandelier_exit":
        out.add("atr14")
    elif key in _BREAK:
        pass  # close/high/low 由 evaluate 统一校验
    else:
        out.add(key)


def evaluate(rule: Rule, df: pd.DataFrame) -> list[SignalEvent]:
    """逐 bar 评估，返回按时间升序的 entry/exit 事件。

    PIT 约定：事件产生于第 i 根收盘（只用 <= i 数据）；chandelier 以收盘价触发，
    止损线取"入场以来最高价 - atr_mult * ATR"（含当根 high，不假设盘中路径）。
    """
    cols = {"close", "high", "low", "ts"}
    _required_cols(rule.entry, cols)
    _required_cols(rule.exit, cols)
    missing = cols - set(df.columns)
    if missing:
        raise ValueError(f"df 缺少列: {sorted(missing)}（请先 enrich 或补列）")
    if not df["ts"].is_monotonic_increasing:
        raise ValueError("df 须按 ts 升序")

    h, c, ts = df["high"], df["close"], df["ts"]
    events: list[SignalEvent] = []
    in_pos = False
    cooldown_until = 0
    max_high = 0.0
    for i in range(WARMUP_BARS, len(df)):
        if in_pos:
            if h.iloc[i] > max_high:
                max_high = h.iloc[i]
            if _eval(rule.exit, df, i, max_high=max_high):
                events.append(SignalEvent(ts.iloc[i], "exit", float(c.iloc[i])))
                in_pos = False
                cooldown_until = i + rule.cooldown_bars
        elif i >= cooldown_until and _eval(rule.entry, df, i):
            events.append(SignalEvent(ts.iloc[i], "entry", float(c.iloc[i])))
            in_pos = True
            max_high = h.iloc[i]
    return events


def _eval(node: dict, df: pd.DataFrame, i: int, *, max_high: float | None = None) -> bool:
    key, val = next(iter(node.items()))
    if key == "all":
        return all(_eval(s, df, i, max_high=max_high) for s in val)
    if key == "any":
        return any(_eval(s, df, i, max_high=max_high) for s in val)
    if key == "cross_up":
        a, b = val
        return df[a].iloc[i - 1] <= df[b].iloc[i - 1] and df[a].iloc[i] > df[b].iloc[i]
    if key == "cross_down":
        a, b = val
        return df[a].iloc[i - 1] >= df[b].iloc[i - 1] and df[a].iloc[i] < df[b].iloc[i]
    if key == "breakout_up":
        return df["close"].iloc[i] > df["high"].iloc[i - val:i].max()
    if key == "breakout_down":
        return df["close"].iloc[i] < df["low"].iloc[i - val:i].min()
    if key == "chandelier_exit":
        return df["close"].iloc[i] < max_high - val["atr_mult"] * df["atr14"].iloc[i]
    op, v = next(iter(val.items()))
    x = df[key].iloc[i]
    if op == "<":
        return x < v
    if op == ">":
        return x > v
    if op == "<=":
        return x <= v
    return x >= v
