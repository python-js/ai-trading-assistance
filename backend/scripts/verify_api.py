"""P1-4 API 验证（TestClient 离线，不监听端口）：
1) /api/health、/api/report（SQL 独立复算 n/胜率一致）、/api/positions（与 SQL count 一致）；
2) /api/signals：limit 生效、pushed 计数与 push_log 一致、带 symbol/timeframe 过滤正确；
3) /api/ohlcv：bar 数=limit、末根=库内 max(ts)、sma5/10/20 长度一致、markers 时间都在 bars 内、
   direction ∈ {entry,exit}；坏 symbol → 404；
4) /api/symbols：与 rule 表 enabled 聚合一致（7 标的）；
5) 标注回流：POST 采纳 → 201 → GET 回读 → signals 列表 annotation=采纳 → 非法 action 422、
   不存在 signal 404；测试行清理。
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.models import db

FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if not cond:
        FAIL.append(f"{name} {extra}")
    print(f"  {'PASS' if cond else 'FAIL'} {name} {extra}")


def main() -> None:
    c = TestClient(app)  # TestClient 会话内依赖会正确关闭连接
    conn = db.connect(settings.pg_dsn)

    r = c.get("/api/health")
    check("health", r.status_code == 200 and r.json() == {"ok": True}, str(r.text[:80]))

    # report 独立复算（n / 胜率）
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=14)
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE pnl > 0)
                       FROM paper_position
                       WHERE status='closed' AND exit_ts >= %s AND exit_ts < %s""",
                    (start, end))
        n, w = cur.fetchone()
    r = c.get("/api/report?days=14")
    o = r.json()["overall"]
    ok = r.status_code == 200 and o["n"] == n and abs(o["win_rate"] - w / n) < 1e-9 \
        and "台账报表" in r.json()["markdown"]
    check("report", ok, f"n={o['n']}(sql {n}) win_rate={o['win_rate']:.4f}")

    # positions
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM paper_position WHERE status='closed'")
        n_closed = cur.fetchone()[0]
    r = c.get("/api/positions?status=closed&limit=5")
    check("positions", r.status_code == 200 and len(r.json()) == 5
          and all(p["status"] == "closed" for p in r.json())
          and r.json()[0]["exit_ts"] >= r.json()[-1]["exit_ts"],
          f"total_closed={n_closed}")

    # signals：pushed 与 push_log 一致
    r = c.get("/api/signals?limit=20")
    sigs = r.json()
    pushed_ok = True
    for s in sigs:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM push_log
                           WHERE signal_id=%s AND status='sent'""", (s["id"],))
            if cur.fetchone()[0] != s["pushed"]:
                pushed_ok = False
    check("signals", r.status_code == 200 and len(sigs) == 20 and pushed_ok
          and sigs[0]["ts"] >= sigs[-1]["ts"], f"pushed 计数一致={pushed_ok}")
    r2 = c.get("/api/signals?symbol=BTC/USDT&timeframe=1m&limit=5")
    check("signals 过滤", all(s["symbol"] == "BTC/USDT" and s["timeframe"] == "1m"
                              for s in r2.json()), f"n={len(r2.json())}")

    # ohlcv
    r = c.get("/api/ohlcv?symbol=BTC/USDT&timeframe=1m&limit=300")
    d = r.json()
    with conn.cursor() as cur:
        cur.execute("""SELECT max(ts) FROM ohlcv WHERE symbol='BTC/USDT' AND exchange='okx'
                       AND contract_type='spot' AND timeframe='1m'""")
        max_ts = cur.fetchone()[0]
    bars = d["bars"]
    bar_times = {b["time"] for b in bars}
    ok = (r.status_code == 200 and len(bars) == 300
          and bars[-1]["time"] == int(max_ts.timestamp())
          and 0 < len(d["sma20"]) <= len(d["sma10"]) <= len(d["sma5"]) <= 300
          and all(p["time"] in bar_times for p in d["sma5"] + d["sma10"] + d["sma20"])
          and all(m["time"] in bar_times for m in d["signals"])
          and all(m["direction"] in ("entry", "exit") for m in d["signals"]))
    check("ohlcv", ok, f"bars={len(bars)} sma5={len(d['sma5'])} "
                       f"sma20={len(d['sma20'])} markers={len(d['signals'])}")
    r = c.get("/api/ohlcv?symbol=FAKE/USDT&timeframe=1h")
    check("ohlcv 坏标的", r.status_code == 404)

    # symbols
    r = c.get("/api/symbols")
    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT symbol) FROM rule WHERE enabled")
        n_sym = cur.fetchone()[0]
    ok = r.status_code == 200 and len(r.json()) == n_sym \
        and all(p["last_ts"] for s in r.json() for p in s["periods"])
    check("symbols", ok, f"symbols={len(r.json())}(db {n_sym})")

    # 标注回流
    with conn.cursor() as cur:
        cur.execute("SELECT min(id) FROM signal")
        sid = cur.fetchone()[0]
    r = c.post("/api/annotations", json={"signal_id": sid, "user_action": "采纳", "note": "verify"})
    aid = r.json().get("id")
    ok = r.status_code == 201 and aid
    check("annotation 写入", ok, f"id={aid}")
    r = c.get(f"/api/annotations?signal_id={sid}")
    ok = r.status_code == 200 and any(a["id"] == aid and a["user_action"] == "采纳"
                                      for a in r.json())
    check("annotation 回读", ok)
    check("signals 带标注", any(s["id"] == sid and s["annotation"] == "采纳"
                                for s in c.get(f"/api/signals?limit=1000").json()))
    check("annotation 非法 action", c.post("/api/annotations",
                                           json={"signal_id": sid, "user_action": "x"}).status_code == 422)
    check("annotation 无信号", c.post("/api/annotations",
                                      json={"signal_id": 999999999, "user_action": "忽略"}).status_code == 404)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM annotation WHERE id=%s", (aid,))
        # P3-1 起标注 note 自动向量化入知识库：同步清理本测试笔记产生的条目
        cur.execute("""SELECT id FROM knowledge_chunk WHERE source_type='note'
                       AND meta_json->>'signal_id' = %s ORDER BY id DESC LIMIT 1""", (str(sid),))
        krow = cur.fetchone()
        if krow:
            cur.execute("DELETE FROM knowledge_chunk WHERE id=%s", (krow[0],))
    conn.commit()
    conn.close()
    if FAIL:
        print("\n".join(f"FAIL {f}" for f in FAIL))
        sys.exit(1)
    print("P1-4 API 验证全部 PASS，测试数据已清理")


if __name__ == "__main__":
    main()
