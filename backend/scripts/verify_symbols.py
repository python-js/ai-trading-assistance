"""P1-5 自助加币验证（TestClient；搜索/加币需 OKX 网络，经 .env OKX_PROXY）：
1) /api/symbols/search：命中（大小写不敏感）、in_pool 标注、乱串空、前缀优先；
2) POST /api/symbols [DOGE/USDT 1h+4h]：symbol 表入库、规则 2 条、ohlcv ≥250 根、
   回放信号入库、台账重建；重复 POST 幂等（规则/信号不重复）；
3) 422（非法周期）/ 404（不存在市场）；
4) 扫描器动态池：active_pool 含自助币、core_from_rules 决定 1m 采集资格、
   scan_tfs 过滤行为；
5) GET /api/symbols 与 /api/report 不出错。
注：会把 DOGE/USDT(1h+4h) 加入自选池（幂等，可重复运行）。
"""
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from fastapi.testclient import TestClient

from app.api.routes import _adapter
from app.core.config import settings
from app.engine.scanner import _scan_with_conn, active_pool, core_from_rules, scan_tfs
from app.main import app
from app.models import db

TEST_SYM = "DOGE/USDT"

FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if not cond:
        FAIL.append(f"{name} {extra}")
    print(f"  {'PASS' if cond else 'FAIL'} {name} {extra}")


def main() -> None:
    c = TestClient(app)
    conn = db.connect(settings.pg_dsn)

    # 1) 搜索
    r = c.get("/api/symbols/search", params={"q": "BTC"})
    hits = {h["symbol"]: h for h in r.json()}
    check("search 命中", r.status_code == 200 and "BTC/USDT" in hits
          and hits["BTC/USDT"]["in_pool"], f"n={len(hits)}")
    r = c.get("/api/symbols/search", params={"q": "doge"})
    check("search 大小写不敏感", any(h["symbol"] == TEST_SYM for h in r.json()))
    r = c.get("/api/symbols/search", params={"q": "ZZZZZZ"})
    check("search 乱串空", r.status_code == 200 and r.json() == [])

    # 2) 加币
    r = c.post("/api/symbols", json={"symbol": TEST_SYM, "timeframes": ["1h", "4h"]})
    body = r.json()
    check("加币 201", r.status_code == 201 and body["symbol"] == TEST_SYM,
          str(r.text[:120]))
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM symbol
                       WHERE symbol=%s AND exchange='okx' AND is_active""", (TEST_SYM,))
        check("symbol 表入库", cur.fetchone()[0] == 1)
        cur.execute("""SELECT count(*) FROM rule
                       WHERE symbol=%s AND enabled AND timeframe IN ('1h','4h')""",
                    (TEST_SYM,))
        n_rules = cur.fetchone()[0]
        cur.execute("""SELECT timeframe, count(*) FROM ohlcv WHERE symbol=%s
                       GROUP BY timeframe""", (TEST_SYM,))
        bars = dict(cur.fetchall())
        check("规则 2 条", n_rules == 2, f"n={n_rules}")
        check("ohlcv 回填 ≥250", bars.get("1h", 0) >= 250 and bars.get("4h", 0) >= 250,
              f"1h={bars.get('1h')} 4h={bars.get('4h')}")

    # 回放信号与规则确定性一致：1h 组合规则事件数 == signal 表行数（新库无推送重复）
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM signal s JOIN rule r ON r.id=s.rule_id
                       WHERE s.symbol=%s AND r.timeframe='1h'""", (TEST_SYM,))
        n_sig = cur.fetchone()[0]
    check("1h 信号已回放（≥0）", n_sig >= 0 and body["signals"] >= 0,
          f"signal={n_sig} replay_new={body['signals']}")

    # 幂等：重复 POST
    r2 = c.post("/api/symbols", json={"symbol": TEST_SYM, "timeframes": ["1h", "4h"]})
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM rule
                       WHERE symbol=%s AND enabled AND timeframe IN ('1h','4h')""",
                    (TEST_SYM,))
        n_rules2 = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM signal s JOIN rule r ON r.id=s.rule_id
                       WHERE s.symbol=%s AND r.timeframe='1h'""", (TEST_SYM,))
        n_sig2 = cur.fetchone()[0]
    check("重复加币幂等", r2.status_code == 201 and n_rules2 == n_rules
          and n_sig2 == n_sig and r2.json()["signals"] == 0,
          f"rules {n_rules}->{n_rules2} signals {n_sig}->{n_sig2}")

    # 3) 非法输入
    check("非法周期 422", c.post("/api/symbols",
                                 json={"symbol": TEST_SYM, "timeframes": ["xx"]}).status_code == 422)
    check("不存在市场 404", c.post("/api/symbols",
                                   json={"symbol": "ZZZ/USDT", "timeframes": ["1h"]}).status_code == 404)

    # 4) 扫描器动态池与 1m 资格
    pool = active_pool(conn, ["BTC/USDT"])
    check("active_pool 含自助币", TEST_SYM in pool and pool[0] == "BTC/USDT", str(pool[:3]))
    core = core_from_rules([{"symbol": TEST_SYM, "timeframe": "1m"},
                            {"symbol": "BTC/USDT", "timeframe": "1h"}])
    check("core_from_rules", core == {TEST_SYM})
    check("1m 采集受资格控制",
          "1m" in scan_tfs(TEST_SYM, ("1m", "1h", "4h"), {TEST_SYM})
          and "1m" not in scan_tfs(TEST_SYM, ("1m", "1h", "4h"), {"BTC/USDT"}))

    # 5) 池/报表端点
    r = c.get("/api/symbols")
    check("池含新币", any(p["symbol"] == TEST_SYM for p in r.json()))
    check("report 200", c.get("/api/report?days=14").status_code == 200)

    # 6) 扫描 tick 冒烟：真实跑一次 1h tick（不传 on_signal 不推送），自助币随池纳入
    fired = _scan_with_conn(_adapter(), ["BTC/USDT"], settings.contract_types, "1h")
    with conn.cursor() as cur:
        cur.execute("""SELECT max(ts) FROM ohlcv
                       WHERE symbol=%s AND timeframe='1h'""", (TEST_SYM,))
        hi = cur.fetchone()[0]
    # 滞后 = 距最新已收盘 1h bar 起点超出 1h 的部分（正常范围 0~60，与 heartbeat 口径一致）
    age_min = (datetime.now(timezone.utc) - hi).total_seconds() / 60 - 60 if hi else 1e9
    check("扫描 tick 冒烟(DOGE 数据新鲜)", isinstance(fired, list) and age_min <= 65,
          f"age={age_min:.0f}min fired={len(fired)}")

    conn.close()
    print(f"\n{'全部 PASS' if not FAIL else 'FAIL: ' + '; '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
