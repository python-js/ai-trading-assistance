"""P2-4 S7 三票合议验证：
① 技术票：确定性（两算一致）+ 方向性（entry/exit 不同）+ 证据字段齐全；
② 资金面票：asof 独立复算（bisect，不复用 flow.attach 逻辑）与证据一致；
③ 信息票：PIT（信号 ts 之后发布的事件必须排除）+ 方向支持度 + 半衰期加权分数；
④ 端到端落库（最近真实 okx 信号）：signal 四分数 + signal_vote 三行；total 独立加权复算；
⑤ 缺席票：A股 无资金面 → None 且 evidence 记原因；
⑥ 清理 verify 合成资讯。
用法：.venv/Scripts/python.exe backend/scripts/verify_votes.py
"""
import bisect
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.engine import flow, votes
from app.engine.indicators import enrich
from app.models import db

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _asof(times: list, values: list, t):
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


def main() -> None:
    conn = db.connect(settings.pg_dsn)

    with conn.cursor() as cur:
        cur.execute("""SELECT id, symbol, exchange, contract_type, timeframe, ts, direction
                       FROM signal WHERE exchange='okx' ORDER BY ts DESC, id DESC LIMIT 1""")
        cols = [d.name for d in cur.description]
        sig = dict(zip(cols, cur.fetchone()))
    print(f"样本信号: #{sig['id']} {sig['symbol']} {sig['contract_type']} {sig['timeframe']} "
          f"{sig['ts']:%m-%d %H:%M} {sig['direction']}")
    df = db.load_ohlcv(conn, sig["symbol"], sig["exchange"], sig["contract_type"],
                       sig["timeframe"], limit=600)
    ind = enrich(df)

    # ---------- ① 技术票 ----------
    print("① 技术票（确定性 / 方向性 / 证据）")
    s1, ev1 = votes.tech_vote(ind, sig["ts"], "entry")
    s2, _ = votes.tech_vote(ind, sig["ts"], "entry")
    s_short, _ = votes.tech_vote(ind, sig["ts"], "exit")
    check("确定性（两算一致）", s1 is not None and s1 == s2, f"s={s1}")
    check("方向性（entry≠exit）", s_short is not None and abs(s1 - s_short) > 1e-9,
          f"entry={s1} exit={s_short}")
    need = {"close", "ema20", "ema60", "rsi14", "vol_ratio", "atr_pct"}
    check("证据字段齐全", need <= set(ev1), str(sorted(ev1)))
    check("strength ∈ [0,1]", 0.0 <= s1 <= 1.0 and 0.0 <= s_short <= 1.0)

    # ---------- ② 资金面票 asof 独立复算 ----------
    print("② 资金面票（bisect 独立复算）")
    sv, evf = votes.flow_vote(conn, sig["symbol"], "okx", sig["ts"], "entry")
    f = flow.load_funding(conn, sig["symbol"], "okx")
    o = flow.load_oi(conn, sig["symbol"], "okx")
    t = sig["ts"]
    ftimes = [x.to_pydatetime() for x in f["ts"]]
    otimes = [x.to_pydatetime() for x in o["ts"]]
    fr_i = _asof(ftimes, [float(x) for x in f["rate"]], t)
    oi_i = _asof(otimes, [float(x) for x in o["oi"]], t)
    oi24_i = _asof(otimes, [float(x) for x in o["oi"]], t - timedelta(hours=24))
    check("资金面票在场", sv is not None and evf.get("funding_rate") is not None,
          f"strength={sv}")
    check("funding asof 与 bisect 一致",
          fr_i is not None and abs(evf["funding_rate"] - fr_i) < 1e-12,
          f"evidence={evf['funding_rate']} bisect={fr_i}")
    expect_oi = (oi_i / oi24_i - 1) if (oi_i and oi24_i) else None
    check("oi_change_24h 与 bisect 一致",
          expect_oi is not None and evf["oi_change_24h"] is not None
          and abs(evf["oi_change_24h"] - expect_oi) < 1e-6,
          f"evidence={evf['oi_change_24h']} bisect={round(expect_oi, 6) if expect_oi else None}")

    # ---------- ③ 信息票 PIT + 方向 ----------
    print("③ 信息票（PIT / 方向 / 加权分数）")
    now = datetime.now(timezone.utc)
    rid = db.insert_news_raw_if_new(
        conn, source="verify", url=f"https://verify.local/votes-{int(time.time() * 1000)}",
        title="验证：某项目获大型机构战略投资（合成）", content="合成测试正文",
        published_ts=now - timedelta(hours=10))
    db.insert_news_events(conn, rid, [{
        "symbols": ["VFY"], "event_type": "project", "sentiment": 0.8,
        "impact_dir": "bullish", "magnitude": 0.9, "confidence": 1.0}], "verify-mock")
    sl, evl = votes.info_vote(conn, "VFY/USDT", now, "entry")
    ss, evs = votes.info_vote(conn, "VFY/USDT", now, "exit")
    check("事件在窗内被聚合", sl is not None and evl["n_events"] == 1, f"strength={sl}")
    check("加权分数≈0.72（0.8*0.9*1.0）", abs(evl["score"] - 0.72) < 1e-4,
          f"score={evl['score']}")
    check("方向支持：long>0.5>short", sl > 0.5 > ss, f"long={sl} short={ss}")
    check("方向对称（sl-0.5≈0.5-ss）", abs((sl - 0.5) - (0.5 - ss)) < 1e-4)
    check("溯源 top 含 url/标题", evl["top"] and evl["top"][0]["url"].startswith("https://"),
          str(evl["top"][0]["title"])[:40])
    sp, evp = votes.info_vote(conn, "VFY/USDT", now - timedelta(hours=20), "entry")
    check("PIT：ts 之前未发布的事件被排除", sp is None and evp["n_events"] == 0,
          f"strength={sp} n={evp['n_events']}")

    # ---------- ④ 端到端落库 ----------
    print("④ 端到端落库（真实信号 compute+save）")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM signal_vote WHERE signal_id=%s", (sig["id"],))
    conn.commit()
    vres = votes.compute(conn, sig, ind)
    votes.save(conn, sig["id"], vres)
    with conn.cursor() as cur:
        cur.execute("""SELECT tech_score, flow_score, info_score, total_score
                       FROM signal WHERE id=%s""", (sig["id"],))
        tech, fl, inf, total = cur.fetchone()
        cur.execute("""SELECT vote_type, strength FROM signal_vote
                       WHERE signal_id=%s ORDER BY vote_type""", (sig["id"],))
        vrows = cur.fetchall()
    check("signal.tech_score 落库", tech is not None and abs(tech - vres["votes"]["tech"][0]) < 1e-9,
          f"db={tech}")
    check("signal.flow_score 与票一致", (fl is None) == (vres["votes"]["flow"][0] is None)
          and (fl is None or abs(fl - vres["votes"]["flow"][0]) < 1e-9), f"db={fl}")
    check("signal_vote 三行（tech/flow/info）",
          [r[0] for r in vrows] == ["flow", "info", "tech"], str(vrows))
    num = den = 0.0
    for k, w in votes.WEIGHTS.items():
        s = vres["votes"][k][0]
        if s is not None:
            num += w * s
            den += w
    check("total 独立加权复算一致",
          abs(total - num / den) < 1e-4, f"db={total} 复算={round(num / den, 4)}")

    # ---------- ⑤ 缺席票 ----------
    print("⑤ 缺席票（A股 无资金面）")
    sm, evm = votes.flow_vote(conn, "000157.SZ", "akshare", now, "entry")
    check("A股 flow 票缺席且记原因", sm is None and "skip" in evm, str(evm))

    # ---------- ⑥ 清理 ----------
    print("⑥ 清理 verify 合成资讯")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM news_raw WHERE source='verify'")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_raw WHERE source='verify'")
        left = cur.fetchone()[0]
    check("verify 数据已清理", left == 0)

    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    conn.close()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
