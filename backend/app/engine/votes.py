"""P2-4 S7 三票合议：技术票 + 资金面票 + 信息票，落库 signal 分数与 signal_vote 明细。

- 增强不拦（已确认决策）：票分如实落库并附推送，P2 阶段不拦信号（passed 保持全量）先积累数据。
- PIT：信息票仅聚合 published_ts ≤ 信号 ts 的事件；资金面取 asof ≤ ts（与 flow.py 同口径）。
- 缺席票（数据不可得，如 A股 无资金面）strength=None，evidence 记原因；total=在场票加权平均。
- 启发式基线 v1（0.5=中性，±调整见常量）；evidence_json 留全量特征供审计与迭代。

票语义（strength ∈ [0,1]，对"信号方向"的支持度）：
- tech：价格与 ema60 同侧(+0.2)、RSI 有利区(+0.15)/过狂热(-0.2)、量比≥1.2(+0.15)/<0.8(-0.1)
- flow：funding 反向持仓拥挤（-fr 支持多头 / +fr 支持空头，|0.05%/8h| 为满档 ±0.3）；
        OI 24h 变化 |5%| 满档 ±0.2（新仓进场增强 / 平仓退潮减弱，方向无关）
- info：72h 窗口事件按 24h 半衰期加权平均 sent*mag*conf（∈[-1,1]），±0.5 映射为支持度
"""
from __future__ import annotations

import pandas as pd

from app.engine import flow
from app.models import db

WEIGHTS = {"tech": 0.4, "flow": 0.35, "info": 0.25}
INFO_WINDOW_H = 72
INFO_HALF_LIFE_H = 24.0


def _sgn(direction: str) -> int:
    return 1 if direction == "entry" else -1


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------- 技术票 ----------

def tech_vote(ind: pd.DataFrame, ts, direction: str) -> tuple[float | None, dict]:
    pos = ind.index[ind["ts"] == pd.Timestamp(ts)]
    if len(pos) == 0:
        return None, {"skip": "信号 bar 未在指标窗口内"}
    i = pos[-1]
    r = ind.loc[i]
    sgn = _sgn(direction)
    close, ema20, ema60 = float(r["close"]), float(r["ema20"]), float(r["ema60"])
    rsi = float(r["rsi14"])
    vm = ind["volume"].iloc[max(0, i - 19): i + 1]
    vol_ratio = float(r["volume"]) / float(vm.mean()) if vm.mean() else 0.0
    atr_pct = float(r["atr14"]) / close if close else 0.0

    score = 0.5
    if sgn * (close - ema60) > 0:
        score += 0.2
    if sgn > 0:  # 多头区
        if 40 <= rsi <= 70:
            score += 0.15
        elif rsi > 75:
            score -= 0.2
        elif rsi < 30:
            score -= 0.1
    else:
        if 30 <= rsi <= 60:
            score += 0.15
        elif rsi < 25:
            score -= 0.2
        elif rsi > 70:
            score -= 0.1
    if vol_ratio >= 1.2:
        score += 0.15
    elif vol_ratio < 0.8:
        score -= 0.1
    evidence = {"close": close, "ema20": ema20, "ema60": ema60,
                "ema_spread_pct": round((ema20 - ema60) / ema60 * 100, 4) if ema60 else None,
                "rsi14": round(rsi, 2), "vol_ratio": round(vol_ratio, 3),
                "atr_pct": round(atr_pct, 5)}
    return round(_clamp(score, 0.0, 1.0), 4), evidence


# ---------- 资金面票（perp 序列；现货信号取同标的永续作市场情绪代理，A股无） ----------

def flow_vote(conn, symbol: str, exchange: str, ts, direction: str) -> tuple[float | None, dict]:
    f = flow.load_funding(conn, symbol, exchange)
    o = flow.load_oi(conn, symbol, exchange)
    if f.empty and o.empty:
        return None, {"skip": "无资金面数据（现货/休市或未采集）"}
    sub = flow.attach(pd.DataFrame({"ts": [pd.Timestamp(ts)]}), f, o).iloc[0]
    fr, oic = sub["funding_rate"], sub["oi_change_24h"]
    if pd.isna(fr) and pd.isna(oic):
        return None, {"skip": "资金面均未覆盖至信号时间"}
    score = max_abs = 0.0
    if not pd.isna(fr):
        max_abs += 0.3
        score += 0.3 * _sgn(direction) * _clamp(-float(fr) / 0.0005, -1.0, 1.0)
    if not pd.isna(oic):
        max_abs += 0.2
        score += 0.2 * _clamp(float(oic) / 0.05, -1.0, 1.0)
    strength = _clamp(0.5 + score / max_abs * 0.5, 0.0, 1.0)
    evidence = {"funding_rate": None if pd.isna(fr) else float(fr),
                "oi_change_24h": None if pd.isna(oic) else round(float(oic), 6)}
    return round(strength, 4), evidence


# ---------- 信息票（news_event PIT 聚合） ----------

def info_vote(conn, symbol: str, ts, direction: str,
              window_h: int = INFO_WINDOW_H) -> tuple[float | None, dict]:
    base = symbol.split("/")[0].split(".")[0]
    ts = pd.Timestamp(ts)
    lo = ts - pd.Timedelta(hours=window_h)
    q = """SELECT e.sentiment, e.magnitude, e.confidence, r.title, r.url, r.source,
                  r.published_ts
           FROM news_event e JOIN news_raw r ON r.id = e.news_id
           WHERE %s = ANY(e.symbols) AND r.published_ts <= %s AND r.published_ts >= %s
           ORDER BY r.published_ts DESC"""
    with conn.cursor() as cur:
        cur.execute(q, (base, ts.to_pydatetime(), lo.to_pydatetime()))
        rows = cur.fetchall()
    ev = {"window_h": window_h, "n_events": len(rows)}
    if not rows:
        ev["skip"] = "窗口内无相关事件"
        return None, ev
    num = den = 0.0
    top = []
    for sent, mag, conf, title, url, source, pub in rows:
        s = float(sent or 0.0) * float(mag if mag is not None else 0.5) \
            * float(conf if conf is not None else 0.5)
        age_h = (ts - pd.Timestamp(pub)).total_seconds() / 3600
        w = 0.5 ** (age_h / INFO_HALF_LIFE_H)
        num += w * s
        den += w
        top.append((abs(s), {"title": (title or "")[:80], "url": url, "source": source,
                             "sentiment": sent, "impact": round(s, 4),
                             "published_ts": pub.isoformat()}))
    score = num / den if den else 0.0
    strength = _clamp(0.5 + 0.5 * score * _sgn(direction), 0.0, 1.0)
    ev.update({"score": round(score, 4),
               "top": [t[1] for t in sorted(top, key=lambda x: -x[0])[:3]]})
    return round(strength, 4), ev


# ---------- 合议与落库 ----------

def compute(conn, sig: dict, ind: pd.DataFrame | None = None) -> dict:
    """sig: 含 symbol/exchange/ts/direction。返回 {"votes": {类型: (strength, evidence)}, "total": …}"""
    votes = {"tech": tech_vote(ind, sig["ts"], sig["direction"]) if ind is not None
             else (None, {"skip": "无 bar 指标上下文"}),
             "flow": flow_vote(conn, sig["symbol"], sig["exchange"], sig["ts"], sig["direction"]),
             "info": info_vote(conn, sig["symbol"], sig["ts"], sig["direction"])}
    num = den = 0.0
    for k, w in WEIGHTS.items():
        s = votes[k][0]
        if s is not None:
            num += w * s
            den += w
    return {"votes": votes, "total": round(num / den, 4) if den else None}


def save(conn, signal_id: int, res: dict) -> None:
    v = res["votes"]
    db.update_signal_scores(conn, signal_id, tech=v["tech"][0], flow=v["flow"][0],
                            info=v["info"][0], total=res["total"])
    for k, (strength, evidence) in v.items():
        db.insert_signal_vote(conn, signal_id=signal_id, vote_type=k,
                              strength=strength, evidence=evidence)
