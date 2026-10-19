"""MRS 状态指纹快照（T06 未来函数审计 / 一致性双跑对照共用）。

snap(obj)：把 MRS 对象图序列化为可比较的纯数据结构（dict/list/数值/字符串），
跳过函数等非状态引用（→ None）；set 排序归一（无序遍历不确定性）、dict 按键排序。
fingerprint(engine)：引擎全状态指纹 JSON 字符串——两份指纹相等 ⇔ 决策路径与
全部中间变量（指标/分型/区/日快照/量分位/账户/持仓/订单组/台账）一致。
"""
from __future__ import annotations

import dataclasses
import json
from collections import deque


def snap(obj, _depth: int = 0):
    if _depth > 64:
        return None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [snap(x, _depth + 1) for x in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(snap(x, _depth + 1) for x in obj)
    if isinstance(obj, deque):
        return [snap(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): snap(v, _depth + 1)
                for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if dataclasses.is_dataclass(obj):
        return {f.name: snap(getattr(obj, f.name), _depth + 1)
                for f in dataclasses.fields(obj)}
    if callable(obj):
        return None
    slots = getattr(type(obj), "__slots__", None)
    if slots:
        return {s: snap(getattr(obj, s, None), _depth + 1) for s in slots}
    if hasattr(obj, "__dict__"):
        return {k: snap(v, _depth + 1) for k, v in vars(obj).items() if not callable(v)}
    return None


def fingerprint(eng) -> str:
    """引擎全状态指纹（含台账与订单组）。"""
    state = {
        "equity": eng.equity, "last_ts": eng.last_ts, "regime": eng.regime,
        "pending": eng.pending_sig_id, "vol_pctile": eng._vol_pctile,
        "atr5": eng._atr5, "atr15": eng._atr15, "fees": list(eng.fees),
        "funding_imputed": eng.funding_imputed,
        "funding_settlements": eng.funding_settlements,
        "m1_atr": snap(eng.m1_atr), "volwin": snap(eng.volwin), "day": snap(eng.day),
        "hist": snap(eng.hist), "tf": snap(eng.tf), "agg": snap(eng._agg),
        "zones": snap(eng.zones), "acct": snap(eng.acct), "pos": snap(eng.pos),
        "broker": {"fail": eng.broker.fail_place_stop, "groups": snap(eng.broker.groups)},
        "ledger": {"signals": snap(eng.ledger.signals), "trades": snap(eng.ledger.trades),
                   "orders": snap(eng.ledger.orders), "funnel": snap(eng.ledger.funnel_counts),
                   "plan_reject": snap(eng.ledger.plan_reject_counts)},
    }
    return json.dumps(state, sort_keys=True, ensure_ascii=False)
