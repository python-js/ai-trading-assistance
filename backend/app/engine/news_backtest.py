"""P2-8 信息票独立回测：news_event → 未来价格表现（IC / 方向命中率），只读不改库。

- PIT 基准价：发布时间后第一根已收盘 1h bar 的 close（其 close 时刻严格晚于发布时间，
  发布在 bar 内也只用该 bar 收盘价，不含未来）。
- 窗口收益 r_W = close(entry 后满 W 小时的第一根) / 基准 - 1（W∈{1,4,24}，数据不足=None）。
- 样本 =（事件 × 匹配到行情的符号）：score = sentiment×magnitude×confidence（与 info_vote 同口径）；
  符号按 base 匹配现货 1h 序列（缺失回退永续）；无行情/无窗口数据则剔除。
- IC：pooled Spearman(score, r_W)；命中率：impact_dir 与 r_W 方向一致占比（bullish→r>0）。
"""
from __future__ import annotations

import bisect

import pandas as pd

from app.models import db

TF = "1h"
BARS_H = 1  # TF 对应的 bar 时长（小时）
WINDOWS_H = (1, 4, 24)
_DIRS = ("bullish", "bearish")


def load_events(conn, since=None) -> list[dict]:
    """事件 + 原始发布时间（PIT 依据 published_ts）。"""
    q = """SELECT e.id, r.published_ts, e.symbols, e.event_type, e.sentiment,
                  e.impact_dir, e.magnitude, e.confidence, r.source, r.title
           FROM news_event e JOIN news_raw r ON r.id = e.news_id
           WHERE r.published_ts IS NOT NULL"""
    params: list = []
    if since is not None:
        q += " AND r.published_ts >= %s"
        params.append(since)
    q += " ORDER BY r.published_ts"
    with conn.cursor() as cur:
        cur.execute(q, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def base_price_map(conn) -> dict[str, str]:
    """base → 内部规范符号（现货优先；sorted 后写入使 spot 覆盖 perp）。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT contract_type, symbol FROM ohlcv
                       WHERE exchange='okx' AND timeframe=%s""", (TF,))
        rows = cur.fetchall()
    m: dict[str, str] = {}
    for ct, sym in sorted(rows):  # ('perp',·) 排在 ('spot',·) 前，spot 后写覆盖
        m[sym.split("/")[0]] = sym
    return m


def _bar_series(conn, symbol: str, cache: dict) -> tuple[list, list]:
    if symbol not in cache:
        df = db.load_ohlcv(conn, symbol, "okx", "spot", TF)
        if df.empty:
            df = db.load_ohlcv(conn, symbol, "okx", "perp", TF)
        cache[symbol] = ([t.to_pydatetime() for t in df["ts"]],
                         [float(c) for c in df["close"]])
    return cache[symbol]


def entry_index(ts: list, published) -> int | None:
    """第一根 close 时刻(ts+BARS_H 小时) 严格晚于发布时间的 bar（PIT）。"""
    i = bisect.bisect_right(ts, published - pd.Timedelta(hours=BARS_H))
    return i if i < len(ts) else None


def forward_return(ts: list, close: list, i: int, w: int) -> float | None:
    """r_W：entry 后满 W 小时的第一根 close / entry close - 1。"""
    j = bisect.bisect_left(ts, ts[i] + pd.Timedelta(hours=w))
    if j >= len(ts) or not close[i]:
        return None
    return close[j] / close[i] - 1.0


def build_samples(conn, events: list[dict]) -> list[dict]:
    bmap = base_price_map(conn)
    cache: dict[str, tuple] = {}
    samples: list[dict] = []
    for e in events:
        for base in sorted(set(e["symbols"] or [])):
            sym = bmap.get(base)
            if not sym:
                continue
            ts, close = _bar_series(conn, sym, cache)
            i = entry_index(ts, e["published_ts"])
            if i is None or not close[i]:
                continue
            s = (float(e["sentiment"] or 0.0)
                 * float(e["magnitude"] if e["magnitude"] is not None else 0.5)
                 * float(e["confidence"] if e["confidence"] is not None else 0.5))
            row = {"news_id": e["id"], "base": base, "symbol": sym,
                   "published_ts": e["published_ts"], "entry_ts": ts[i],
                   "score": s, "impact_dir": e["impact_dir"],
                   "event_type": e["event_type"], "source": e["source"],
                   "title": (e["title"] or "")[:60]}
            for w in WINDOWS_H:
                row[f"r{w}"] = forward_return(ts, close, i, w)
            samples.append(row)
    return samples


def ic(samples: list[dict], w: int) -> tuple[float | None, int]:
    """pooled Spearman（样本 <3 或方差为 0 → None）。"""
    pairs = [(s["score"], s[f"r{w}"]) for s in samples if s[f"r{w}"] is not None]
    if len(pairs) < 3:
        return None, len(pairs)
    a, b = zip(*pairs)
    v = pd.Series(a).corr(pd.Series(b), method="spearman")
    return (None if pd.isna(v) else round(float(v), 4)), len(pairs)


def hit_rate(samples: list[dict], w: int,
             dirs: tuple[str, ...] = _DIRS) -> tuple[float | None, int]:
    n = hit = 0
    for s in samples:
        d, r = s["impact_dir"], s[f"r{w}"]
        if r is None or d not in dirs:
            continue
        n += 1
        if (d == "bullish") == (r > 0):
            hit += 1
    return (round(hit / n, 4) if n else None), n


def render(events: list[dict], samples: list[dict], days: int) -> str:
    out = [f"### 信息票回测（近 {days} 天 · 事件 {len(events)} · 样本 {len(samples)}）"]
    if not samples:
        out.append("无事件样本（news_event 为空——需配置 LLM_API_KEY 完成资讯结构化）")
        return "\n".join(out)
    out.append("**窗口表现**")
    for w in WINDOWS_H:
        v, n = ic(samples, w)
        h, hn = hit_rate(samples, w)
        out.append(f"- {w}h：IC {v if v is not None else '—'}（n={n}）"
                   f" · 方向命中率 {h if h is not None else '—'}（n={hn}）")
    parts = []
    for d in _DIRS:
        h, hn = hit_rate(samples, 24, dirs=(d,))
        if hn:
            parts.append(f"{d} {h}（n={hn}）")
    if parts:
        out.append(f"**按方向（24h）**：{' · '.join(parts)}")
    by_type: dict[str, list] = {}
    for s in samples:
        by_type.setdefault(s["event_type"] or "other", []).append(s)
    typed = []
    for t, ss in sorted(by_type.items()):
        h, hn = hit_rate(ss, 24)
        if hn >= 3:
            typed.append(f"{t} {h}（n={hn}）")
    if typed:
        out.append(f"**按类型（24h）**：{' · '.join(typed)}")
    out.append("> 口径：entry=发布后首根收盘 1h bar；score=sent×mag×conf；"
               "命中=bullish→r>0 / bearish→r<0；IC 含全部方向样本")
    return "\n".join(out)
