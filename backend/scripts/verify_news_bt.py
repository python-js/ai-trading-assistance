"""P2-8 信息票回测验证：
① PIT entry 边界：发布=bar 开盘 / bar 内 / bar 收盘（=下根开盘）三种情况 entry 选取正确；
② 收益复算：r1/r4/r24 与直接 close 比值一致（线性扫描独立复算，不依赖 bisect 实现）；
③ IC 数学：score 单调同序 → 1.0、逆序 → -1.0、样本 <3 → None；score 同 info_vote 口径（sent×mag×conf）；
④ 命中率：按实际 r 方向构造 impact_dir → 全命中；反转一条 → 2/3；
⑤ 多符号展开（无行情符号跳过）；⑥ 报告渲染（含 IC/命中率行，空样本提示）；
⑦ 清理 verify 合成资讯。
用法：.venv/Scripts/python.exe backend/scripts/verify_news_bt.py
"""
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.engine import news_backtest
from app.models import db

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _next_idx(ts: list, t) -> int | None:
    """线性扫描独立复算：第一根 close 时刻 > t 的 bar。"""
    for i, x in enumerate(ts):
        if x + timedelta(hours=1) > t:
            return i
    return None


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    with conn.cursor() as cur:  # 幂等：清理上次失败运行的残留
        cur.execute("DELETE FROM news_raw WHERE source='verify'")
    conn.commit()
    df = db.load_ohlcv(conn, "BTC/USDT", "okx", "spot", "1h")
    ts = [t.to_pydatetime() for t in df["ts"]]
    close = [float(c) for c in df["close"]]
    n = len(ts)
    print(f"价格样本: BTC/USDT spot 1h {n} 根（{ts[0]:%m-%d %H:%M} ~ {ts[-1]:%m-%d %H:%M} UTC）")

    # ---------- ① PIT entry 边界 ----------
    print("① PIT entry 边界（bisect vs 线性扫描）")
    for tag, pub, offset in (("发布=bar 开盘", ts[-25], -25),
                             ("发布=bar 中段", ts[-20] + timedelta(minutes=30), -20),
                             ("发布=bar 收盘", ts[-15] + timedelta(hours=1), -14)):
        i = news_backtest.entry_index(ts, pub)
        j = _next_idx(ts, pub)
        check(f"{tag} → entry={ts[offset]:%m-%d %H:%M}（两法一致）",
              i == n + offset and i == j, f"i={i} j={j}")

    # ---------- ② 收益复算 ----------
    print("② 窗口收益独立复算")
    i = news_backtest.entry_index(ts, ts[-25])
    for w, off in ((1, -24), (4, -21), (24, -1)):
        r = news_backtest.forward_return(ts, close, i, w)
        j = _next_idx(ts, ts[-25] + timedelta(hours=w))
        expect = close[j] / close[n - 25] - 1
        check(f"r{w} = close[{j}]/close[{n - 25}]-1", r is not None
              and abs(r - expect) < 1e-12, f"got={r} expect={round(expect, 8)}")
    check("窗口内数据不足 → None",
          news_backtest.forward_return(ts, close, n - 2, 24) is None)

    # ---------- 合成事件入库（位置保证 r24 有后续数据） ----------
    now_ms = int(time.time() * 1000)
    for k, (pub, syms) in enumerate(((ts[-80], ["BTC", "NOPE"]),
                                     (ts[-60] + timedelta(minutes=30), ["BTC"]),
                                     (ts[-40] + timedelta(hours=1), ["BTC"])), 1):
        rid = db.insert_news_raw_if_new(
            conn, source="verify", url=f"https://verify.local/nbt-{now_ms}-{k}",
            title=f"验证：合成事件{k}（BTC）", content="x", published_ts=pub)
        db.insert_news_events(conn, rid, [{
            "symbols": syms, "event_type": "project", "sentiment": 0.8,
            "impact_dir": "bullish", "magnitude": 1.0, "confidence": 1.0}], "verify-mock")

    # ---------- ③ 样本构建 + IC 数学 ----------
    print("③ 样本构建与 IC（Spearman）")
    events = [e for e in news_backtest.load_events(conn) if e["source"] == "verify"]
    samples = news_backtest.build_samples(conn, events)
    check("样本=3（BTC 匹配、NOPE 无行情跳过）", len(samples) == 3, f"n={len(samples)}")
    check("score 口径 = sent×mag×conf（0.8）",
          all(abs(s["score"] - 0.8) < 1e-12 for s in samples))
    check("entry 与 PIT 推演一致（开盘/中段/收盘三口径）",
          [s["entry_ts"] for s in samples] == [ts[-80], ts[-60], ts[-39]],
          str([f"{s['entry_ts']:%m-%d %H:%M}" for s in samples]))
    check("三样本 r24 均可用", all(s["r24"] is not None for s in samples))
    order = sorted(range(3), key=lambda k: samples[k]["r24"])
    for rank, k in enumerate(order):
        samples[k]["score"] = float(rank + 1)
    v, m = news_backtest.ic(samples, 24)
    check("score 与 r24 同序 → IC=1.0", v == 1.0 and m == 3, f"IC={v} n={m}")
    for rank, k in enumerate(order):
        samples[k]["score"] = float(3 - rank)
    v, _ = news_backtest.ic(samples, 24)
    check("逆序 → IC=-1.0", v == -1.0, f"IC={v}")
    v, m = news_backtest.ic(samples[:2], 24)
    check("样本 <3 → None", v is None and m == 2)

    # ---------- ④ 命中率 ----------
    print("④ 命中率")
    for s in samples:
        s["impact_dir"] = "bullish" if s["r24"] > 0 else "bearish"
    h, m = news_backtest.hit_rate(samples, 24)
    check("按 r 构造方向 → 全命中", h == 1.0 and m == 3, f"rate={h} n={m}")
    s0 = samples[0]
    s0["impact_dir"] = "bearish" if s0["r24"] > 0 else "bullish"
    h, m = news_backtest.hit_rate(samples, 24)
    check("反转一条 → 2/3", h is not None and abs(h - 2 / 3) < 1e-4 and m == 3,
          f"rate={h} n={m}")
    h, m = news_backtest.hit_rate(samples, 24, dirs=("neutral",))
    check("无匹配方向 → None", h is None and m == 0)

    # ---------- ⑤ 报告渲染 ----------
    print("⑤ 报告渲染")
    md = news_backtest.render(events, samples, 30)
    check("含窗口 IC 与命中率行", "IC" in md and "命中率" in md and "24h" in md,
          md.splitlines()[2][:60])
    md0 = news_backtest.render([], [], 30)
    check("空样本提示", "无事件样本" in md0, md0.splitlines()[1][:50])

    # ---------- ⑥ 清理 ----------
    print("⑥ 清理 verify 合成资讯")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM news_raw WHERE source='verify'")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_raw WHERE source='verify'")
        left = cur.fetchone()[0]
    check("verify 数据已清理（news_event 级联）", left == 0)

    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    conn.close()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
