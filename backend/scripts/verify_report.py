"""P1-2 报表验证（离线；mock webhook，不触达真实企微）：
1) 独立复算：SQL 聚合（DB 引擎计算路径）与 report.build_report 全字段一致（总览/周期/标的）；
2) 窗口边界：last_week_window 恒为"上周一 00:00 ~ 本周一 00:00 北京时间"（周一 09:00 北京运行恰为完整上周）；
3) 空窗口：1970 窗口 0 笔不崩、渲染含"无已平仓交易"；
4) mock webhook 端到端：push_markdown 内容一致 + push_log sent（signal_id NULL）；
5) mock 业务错误 errcode=93000 → False + push_log failed。
"""
import json
import math
import pathlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.core.tz import CST
from app.engine import report
from app.models import db

FAIL: list[str] = []

_AGG = """
SELECT count(*),
       count(*) FILTER (WHERE p.pnl > 0),
       sum(p.pnl),
       avg(p.pnl_pct),
       sum(p.pnl) FILTER (WHERE p.pnl > 0),
       sum(p.pnl) FILTER (WHERE p.pnl < 0)
FROM paper_position p
WHERE p.status = 'closed' AND p.exit_ts >= %s AND p.exit_ts < %s
"""

_AGG_TF = """
SELECT r.timeframe, count(*), count(*) FILTER (WHERE p.pnl > 0), sum(p.pnl),
       avg(p.pnl_pct), sum(p.pnl) FILTER (WHERE p.pnl > 0),
       sum(p.pnl) FILTER (WHERE p.pnl < 0)
FROM paper_position p
JOIN signal s ON s.id = p.signal_id
JOIN rule r   ON r.id = s.rule_id
WHERE p.status = 'closed' AND p.exit_ts >= %s AND p.exit_ts < %s
GROUP BY r.timeframe
"""

_AGG_SYM = _AGG_TF.replace("r.timeframe", "p.symbol").replace("GROUP BY r.timeframe",
                                                              "GROUP BY p.symbol")


def _expected(vals) -> dict:
    """vals = (n, wins, total, avg_pct, sum_pos, sum_neg)，DB 引擎独立计算路径。"""
    n, wins, total, avg_pct, sum_pos, sum_neg = vals
    win = wins or 0
    if not n:
        pf = None
    elif not sum_neg:
        pf = float("inf")
    else:
        pf = (sum_pos or 0.0) / -sum_neg
    return {"n": n, "win_rate": (win / n) if n else None, "pf": pf,
            "avg_pnl_pct": (float(avg_pct) if avg_pct is not None else None),
            "total_pnl": float(total or 0.0)}


def _eq(name: str, got, exp, tol: float = 1e-9) -> None:
    if isinstance(got, float) or isinstance(exp, float):
        if got is None or exp is None:
            ok = got == exp
        elif math.isinf(got) or math.isinf(exp):
            ok = got == exp
        else:
            ok = abs(got - exp) <= max(tol, tol * abs(exp))
    else:
        ok = got == exp
    if not ok:
        FAIL.append(f"{name}: got={got!r} exp={exp!r}")


def check_group(name: str, got: dict, exp: dict) -> None:
    for k in ("n", "win_rate", "pf", "avg_pnl_pct", "total_pnl"):
        _eq(f"{name}.{k}", got[k], exp[k])


class MockWeCom(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        self.server.posts.append(payload)
        body = json.dumps({"errcode": self.server.errcode, "errmsg": "mock"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


def new_server(errcode=0):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockWeCom)
    srv.errcode, srv.posts = errcode, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM push_log")
        log_id0 = cur.fetchone()[0] or 0
        cur.execute("SELECT min(exit_ts), max(exit_ts) FROM paper_position WHERE status='closed'")
        lo, hi = cur.fetchone()

    # 1) 独立复算（全量窗口 = 数据实际范围外扩 1s）
    start, end = lo - timedelta(seconds=1), hi + timedelta(seconds=1)
    rep = report.build_report(conn, start, end)
    with conn.cursor() as cur:
        cur.execute(_AGG, (start, end))
        check_group("overall", rep["overall"], _expected(cur.fetchone()))
        cur.execute(_AGG_TF, (start, end))
        sql_tf = {r[0]: _expected(r[1:]) for r in cur.fetchall()}
        cur.execute(_AGG_SYM, (start, end))
        sql_sym = {r[0]: _expected(r[1:]) for r in cur.fetchall()}
    assert set(sql_tf) == set(rep["by_timeframe"]), (set(sql_tf), set(rep["by_timeframe"]))
    assert set(sql_sym) == set(rep["by_symbol"]), (set(sql_sym), set(rep["by_symbol"]))
    for tf in sql_tf:
        check_group(f"tf[{tf}]", rep["by_timeframe"][tf], sql_tf[tf])
    for sym in sql_sym:
        check_group(f"sym[{sym}]", rep["by_symbol"][sym], sql_sym[sym])
    print(f"1) 独立复算：n={rep['overall']['n']}（周期 {sorted(sql_tf)}，标的 {len(sql_sym)} 个），"
          f"字段比对 {'PASS' if not FAIL else 'FAIL'}")

    # 2) 窗口边界：周一 09:00 北京运行 → [上周一 00:00, 本周一 00:00) 北京
    now = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)  # = 周一 09:00 北京
    exp_e = datetime(2026, 10, 12, tzinfo=CST)               # = 本周一 00:00 北京
    s, e = report.last_week_window(now)
    if not (e == exp_e and (e - s) == timedelta(days=7)
            and s.weekday() == 0 and e.weekday() == 0):
        FAIL.append(f"window: now={now} → [{s},{e})")
    print(f"2) 窗口边界：{now:%m-%d %H:%M}(UTC) → [{s:%m-%d %H:%M}, {e:%m-%d %H:%M}) 北京 "
          f"{'PASS' if not FAIL else 'FAIL'}")

    # 3) 空窗口
    rep0 = report.build_report(conn, datetime(1970, 1, 1, tzinfo=timezone.utc),
                               datetime(1970, 1, 8, tzinfo=timezone.utc))
    md0 = report.render_markdown(rep0)
    if not (rep0["overall"]["n"] == 0 and rep0["overall"]["win_rate"] is None
            and rep0["overall"]["pf"] is None and not rep0["by_timeframe"]
            and "无已平仓交易" in md0):
        FAIL.append(f"empty: {rep0['overall']} / {md0!r}")
    print(f"3) 空窗口：0 笔渲染 {'PASS' if '无已平仓交易' in md0 else 'FAIL'}")

    # 4) mock webhook 端到端（用真实报表内容推送）
    md = report.render_markdown(rep)
    srv = new_server(errcode=0)
    ok = WeComNotifier(f"http://127.0.0.1:{srv.server_port}/send?key=T",
                       timeout=3).push_markdown(md, conn)
    got = srv.posts[0] if srv.posts else None
    if not (ok and got == {"msgtype": "markdown", "markdown": {"content": md}}):
        FAIL.append(f"push body: ok={ok} got={got}")
    with conn.cursor() as cur:
        cur.execute("""SELECT status, error, signal_id FROM push_log
                       WHERE id > %s ORDER BY id DESC LIMIT 1""", (log_id0,))
        row = cur.fetchone()
    if not (row and row[0] == "sent" and row[1] is None and row[2] is None):
        FAIL.append(f"push_log sent: {row}")
    srv.shutdown()
    print(f"4) mock 推送：内容一致 + push_log={row} {'PASS' if not FAIL else 'FAIL'}")

    # 5) 业务错误
    srv = new_server(errcode=93000)
    ok = WeComNotifier(f"http://127.0.0.1:{srv.server_port}/send?key=T",
                       timeout=3).push_markdown(md, conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT status, error FROM push_log
                       WHERE id > %s AND status='failed' ORDER BY id DESC LIMIT 1""", (log_id0,))
        row = cur.fetchone()
    if not (ok is False and row and "93000" in row[1]):
        FAIL.append(f"biz error: ok={ok} row={row}")
    srv.shutdown()
    print(f"5) 业务错误：ok={ok}，push_log={row} {'PASS' if not FAIL else 'FAIL'}")

    # 清理本脚本产生的 push_log（按 id 精确删除）
    with conn.cursor() as cur:
        cur.execute("DELETE FROM push_log WHERE id > %s", (log_id0,))
    conn.commit()
    conn.close()

    if FAIL:
        print("\n".join(f"FAIL {f}" for f in FAIL))
        sys.exit(1)
    print("P1-2 报表验证全部 PASS，测试数据已清理")


if __name__ == "__main__":
    main()
