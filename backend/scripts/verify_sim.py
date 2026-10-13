"""A6 模拟盘验证（mock 喂数离线手算 + TestClient 端到端）：

① mock 单笔+复利：qty/pnl 手算一致；第二笔 notional=(init+pnl1)*size%（空仓权益复利定仓）；
② 止损线：全仓深跌触线 → 强平冻结（stop_loss、无持仓、强平笔 exit_reason、终值≤线）；
③ 到期清算 expired 与手动结束 manual（持仓按最后收盘价清算）；
④ 策略时间线：变更生效点（切换后新策略事件、早于生效点的事件被过滤）；
⑤ 确定性：真实库 BTC+ETH 1h 30 天重放两次逐字段一致；
⑥ API 链路（TestClient）：创建 → 列表 → 详情重放 → 调整策略(rule_id) → 复盘(关LLM) → 结束 → 删除；
⑦ 校验边界：混市场/参数越界/规则错配/重复单元 422、无数据 404。

mock 手法：monkeypatch db.load_ohlcv 喂合成 K 线（不触库、不算价，手算可对照）。
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from fastapi.testclient import TestClient

from app.core.config import settings
from app.engine import ledger, sim
from app.main import app
from app.models import db

FAIL: list[str] = []
SLIP = ledger.SLIP_BPS / 10_000
FEE = ledger.FEES


def check(name: str, cond: bool, extra: str = "") -> None:
    if not cond:
        FAIL.append(f"{name} {extra}")
    print(f"  {'PASS' if cond else 'FAIL'} {name} {extra}")


def mock_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range(pd.Timestamp("2026-01-01", tz="UTC"), periods=n, freq="1h")
    return pd.DataFrame({"ts": ts, "open": closes,
                         "high": [c + 2 for c in closes], "low": [c - 2 for c in closes],
                         "close": closes, "volume": [1.0] * n})


def seq(pos: dict | None = None, n: int = 212) -> list[float]:
    arr = [100.0] * n
    for i, v in (pos or {}).items():
        arr[int(i)] = v
    return arr


_orig_load = db.load_ohlcv


def run_mock(closes: list[float], acct: dict) -> dict:
    db.load_ohlcv = lambda conn, *a, **k: mock_df(closes)  # type: ignore[assignment]
    try:
        conn = db.connect(settings.pg_dsn)
        try:
            return sim.run_account(conn, acct)
        finally:
            conn.close()
    finally:
        db.load_ohlcv = _orig_load  # type: ignore[assignment]


def dsl(name: str, entry: dict, exit_: dict, tf: str = "1h") -> dict:
    return {"name": name, "symbol": "MOCK/USDT", "timeframe": tf,
            "entry": entry, "exit": exit_, "cooldown_bars": 0}


def acct_mock(closes: list[float], *, init=10_000.0, size=30.0, stop=50.0,
              end=None, status="active", changes=None) -> dict:
    start = mock_df(closes)["ts"].iloc[0]
    ch = changes or [{"effective_from": start, "name": "S",
                      "dsl_json": dsl("S", {"close": {">": 105}}, {"close": {"<": 105}})}]
    return {"init_cash": init, "size_pct": size, "stop_loss_pct": stop,
            "start_ts": start, "end_ts": end, "status": status,
            "units": [{"symbol": "MOCK/USDT", "exchange": "okx", "contract_type": "spot",
                       "timeframe": "1h", "changes": ch}]}


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    ts0 = mock_df(seq())["ts"]

    print("① mock 单笔 + 复利定仓（手算对照）")
    # 204 入场(110)；205/206 持有(110 不触发 close<105)；207 出场(100)；
    # 209 再入场(110)；210 持有；211 出场(100)。
    closes = seq({204: 110.0, 205: 110.0, 206: 110.0, 207: 100.0,
                  209: 110.0, 210: 110.0, 211: 100.0})
    res = run_mock(closes, acct_mock(closes, stop=0))
    check("事件数=4", res["n_events"] == 4, f"n={res['n_events']}")
    check("两笔完成", len(res["trades"]) == 2 and not res["open_pos"],
          f"trades={len(res['trades'])}")
    t1, t2 = res["trades"]
    qty1_exp = 3000.0 / (110.0 * (1 + SLIP))
    pnl1_exp = qty1_exp * 100.0 * (1 - SLIP) * (1 - FEE) - 3000.0 * (1 + FEE)
    check("第1笔 qty 手算一致", abs(t1["qty"] - qty1_exp) < 1e-9,
          f"{t1['qty']:.8f} vs {qty1_exp:.8f}")
    check("第1笔 pnl 手算一致", abs(t1["pnl"] - pnl1_exp) < 1e-9,
          f"{t1['pnl']:.6f} vs {pnl1_exp:.6f}")
    check("第1笔已剔除成本（fees≈双边）",
          abs(t1["fees"] - (3000.0 * FEE + qty1_exp * 100.0 * FEE)) < 1e-9,
          f"fees={t1['fees']:.4f}")
    n2_exp = (10_000.0 + pnl1_exp) * 0.30  # 空仓时权益=cash(init+已实现) → 复利定仓
    check("第2笔 notional=权益×30%（复利）", abs(t2["notional"] - n2_exp) < 1e-6,
          f"{t2['notional']:.4f} vs {n2_exp:.4f}")
    check("第2笔 qty 手算一致", abs(t2["qty"] - n2_exp / (110.0 * (1 + SLIP))) < 1e-9)
    check("entry_ts/exit_ts 为 bar 收盘时刻",
          t1["entry_ts"] == ts0[204].isoformat() and
          t1["exit_ts"] == ts0[207].isoformat(),
          f"{t1['entry_ts']} -> {t1['exit_ts']}")

    print("② 止损线：全仓深跌触线强平冻结")
    closes2 = seq({204: 110.0}) 
    for i in range(205, 212):
        closes2[i] = 20.0
    res2 = run_mock(closes2, acct_mock(closes2, size=100.0, stop=50.0))
    check("触线结束", res2["status"] == "finished" and res2["end_reason"] == "stop_loss",
          f"{res2['status']}/{res2['end_reason']}")
    check("强平后无持仓", not res2["open_pos"] and len(res2["trades"]) == 1
          and res2["trades"][0]["exit_reason"] == "stop_loss",
          f"trades={len(res2['trades'])}")
    check("触线时点=跌首根（205）", res2["end_ts"] == str(ts0[205].isoformat())[:19] or
          res2["trades"][0]["exit_ts"] == ts0[205].isoformat(),
          f"end_ts={res2['end_ts']}")
    check("终值≤止损线且>0", 0 < res2["equity"][-1]["value"] <= 5000.0,
          f"final={res2['equity'][-1]['value']:.2f}")

    print("③ 到期清算 / 手动结束")
    closes3 = seq({204: 110.0})  # entry@204，exit 条件不触发（close<50）
    ch3 = [{"effective_from": ts0[0], "name": "HOLD",
            "dsl_json": dsl("HOLD", {"close": {">": 105}}, {"close": {"<": 50}})}]
    res3 = run_mock(closes3, acct_mock(closes3, stop=0, end=ts0[208], changes=ch3))
    check("expired 清算", res3["status"] == "finished" and res3["end_reason"] == "expired"
          and len(res3["trades"]) == 1
          and res3["trades"][0]["exit_reason"] == "expired",
          f"{res3['end_reason']}")
    check("清算价=最后收盘", res3["trades"][0]["exit_price"] == 100.0
          and res3["trades"][0]["exit_ts"] == ts0[208].isoformat())
    res3b = run_mock(closes3, acct_mock(closes3, stop=0, end=ts0[208],
                                        status="finished", changes=ch3))
    check("手动结束 manual", res3b["end_reason"] == "manual", f"{res3b['end_reason']}")

    print("④ 策略时间线：变更生效点")
    closes4 = seq({203: 100.0, 204: 110.0, 207: 100.0})
    chA = {"effective_from": ts0[0], "name": "A",
           "dsl_json": dsl("A", {"close": {">": 120}}, {"close": {"<": 95}})}
    chB = {"effective_from": ts0[203], "name": "B",
           "dsl_json": dsl("B", {"close": {">": 105}}, {"close": {"<": 105}})}
    res4 = run_mock(closes4, acct_mock(closes4, stop=0, changes=[chA, chB]))
    check("切换后按 B 成交", len(res4["trades"]) == 1
          and res4["trades"][0]["strategy"] == "B"
          and res4["trades"][0]["entry_ts"] == ts0[204].isoformat(),
          f"n={len(res4['trades'])}")
    chB2 = {**chB, "effective_from": ts0[205]}  # entry@204 早于生效点 → 被段窗口过滤
    res4b = run_mock(closes4, acct_mock(closes4, stop=0, changes=[chA, chB2]))
    check("生效点前事件被过滤（空仓忽略孤立 exit）", len(res4b["trades"]) == 0
          and res4b["n_events"] == 1, f"n={len(res4b['trades'])} ev={res4b['n_events']}")

    print("⑤ 确定性（真实库 BTC+ETH 1h 30 天）")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    def real_unit(sym):
        return {"symbol": sym, "exchange": "okx", "contract_type": "spot",
                "timeframe": "1h",
                "changes": [{"effective_from": start, "name": "sma_520_1h",
                             "dsl_json": dsl("sma_520_1h", {"cross_up": ["sma5", "sma20"]},
                                             {"cross_down": ["sma5", "sma20"]})}]}

    acct5 = {"init_cash": 10_000.0, "size_pct": 30.0, "stop_loss_pct": 50.0,
             "start_ts": start, "end_ts": None, "status": "active",
             "units": [real_unit("BTC/USDT"), real_unit("ETH/USDT")]}
    r5a = sim.run_account(conn, acct5)
    r5b = sim.run_account(conn, acct5)
    check("重放两次逐字段一致",
          r5a == r5b and r5a["n_events"] > 0 and len(r5a["trades"]) > 0,
          f"trades={len(r5a['trades'])} ev={r5a['n_events']}")
    rev = sim.review_stats(r5a, acct5)
    check("复盘统计（notes/by_unit）", rev["notes"] and rev["by_unit"]
          and rev["overall"]["n_trades"] == len(r5a["trades"]),
          f"units={len(rev['by_unit'])}")

    print("⑥ API 链路（TestClient）")
    c = TestClient(app)
    r = c.post("/api/sim", json={"name": "_verify_sim_测试盘", "init_cash": 20_000,
                                 "size_pct": 25, "stop_loss_pct": 0, "start_days_ago": 5,
                                 "units": [{"symbol": "BTC/USDT", "timeframe": "1h"}]})
    check("创建 201", r.status_code == 201, r.text[:120])
    aid = r.json()["id"]
    r = c.get("/api/sim")
    check("列表含新盘", r.status_code == 200
          and any(x["id"] == aid for x in r.json()), f"n={len(r.json())}")
    r = c.get(f"/api/sim/{aid}")
    d = r.json()
    check("详情重放", r.status_code == 200 and "metrics" in d["result"]
          and d["result"]["status"] in ("active", "finished"),
          f"keys={sorted(d.keys())[:6]}")
    with conn.cursor() as cur:
        cur.execute("""SELECT id FROM rule WHERE enabled AND symbol='BTC/USDT'
                       AND timeframe='1h' LIMIT 1""")
        rid = cur.fetchone()[0]
    r = c.post(f"/api/sim/{aid}/change", json={"unit_idx": 0, "rule_id": rid})
    check("调整策略（rule_id 快照）", r.status_code == 200 and r.json()["name"],
          r.text[:100])
    r = c.get(f"/api/sim/{aid}")
    check("策略时间线 2 条", len(r.json()["units"][0]["changes"]) == 2)
    r = c.post(f"/api/sim/{aid}/review?llm=0")
    rv = r.json()
    check("复盘（关 LLM 降级）", r.status_code == 200 and rv["stats"]["n_trades"] >= 0
          and rv["llm"]["ok"] is False and rv["llm"]["error"] == "已按请求关闭",
          f"n={rv['stats']['n_trades']}")
    r = c.post(f"/api/sim/{aid}/finish")
    check("结束 ok", r.status_code == 200)
    r = c.get(f"/api/sim/{aid}")
    check("结束后重放 manual", r.json()["db_status"] == "finished"
          and r.json()["result"]["end_reason"] == "manual",
          f"{r.json()['result']['end_reason']}")
    check("删除 ok", c.delete(f"/api/sim/{aid}").status_code == 200)
    check("删除后 404", c.get(f"/api/sim/{aid}").status_code == 404)

    print("⑦ 校验边界")
    base = {"name": "bad", "init_cash": 1000, "units": [
        {"symbol": "BTC/USDT", "timeframe": "1h"}]}
    r = c.post("/api/sim", json={**base, "units": [
        {"symbol": "BTC/USDT", "timeframe": "1h"},
        {"symbol": "000157.SZ", "timeframe": "1d"}]})
    check("混市场 422", r.status_code == 422, r.text[:80])
    r = c.post("/api/sim", json={**base, "size_pct": 0})
    check("size_pct=0 → 422", r.status_code == 422)
    r = c.post("/api/sim", json={**base, "stop_loss_pct": 100})
    check("stop_loss_pct=100 → 422", r.status_code == 422)
    r = c.post("/api/sim", json={**base, "units": [
        {"symbol": "BTC/USDT", "timeframe": "1h"},
        {"symbol": "BTC/USDT", "timeframe": "1h"}]})
    check("重复单元 422", r.status_code == 422, r.text[:80])
    r = c.post("/api/sim", json={**base, "units": [
        {"symbol": "NOPE/USDT", "timeframe": "1h"}]})
    check("无数据 404", r.status_code == 404)
    with conn.cursor() as cur:
        cur.execute("""SELECT id FROM rule WHERE enabled AND symbol='ETH/USDT'
                       AND timeframe='1h' LIMIT 1""")
        eth_rid = cur.fetchone()[0]
    r = c.post("/api/sim", json={**base, "units": [
        {"symbol": "BTC/USDT", "timeframe": "1h", "rule_id": eth_rid}]})
    check("规则错配 422", r.status_code == 422, r.text[:80])

    conn.close()
    print(f"\n{'全部 PASS' if not FAIL else 'FAIL: ' + '; '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
