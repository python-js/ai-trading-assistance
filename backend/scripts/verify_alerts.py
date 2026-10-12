"""A6 告警验证（离线 mock，不向真实 webhook 发消息）：
A. _StaleAlerter 状态机：正常静默 / 停更告警一次（明细截断 10 项）/ 持续停更不重发 /
   发送失败下轮重试 / 恢复通知（含持续分钟）/ 再次停更重新告警。
B. run_scanner._alert 端到端：mock 企微 webhook 收 markdown 卡片，push_log 记 sent；
   notifier push_markdown(conn=None) 不落库不崩溃（看门狗路径）。
C. watchdog 子进程：心跳新鲜→静默；停更→告警+状态落盘；节流期不重发；
   恢复→通知+清状态；发送失败→状态不落盘（下轮重试成功后补发）。
"""
import json
import os
import subprocess
import sys
import pathlib
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.core.config import settings
from app.core.tz import CST
from app.models import db

WATCHDOG = pathlib.Path(__file__).parent / "watchdog.py"
DEAD_URL = "http://127.0.0.1:1/hook"  # 连接必拒


class MockHook(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.server.posts.append(json.loads(self.rfile.read(n)))
        body = json.dumps({"errcode": 0, "errmsg": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def new_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHook)
    srv.posts = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------- A：_StaleAlerter 状态机 ----------

def test_alerter():
    from app.engine.scanner import _StaleAlerter
    calls = []
    ok = {"v": True}
    a = _StaleAlerter(lambda md: (calls.append(md), ok["v"])[1])
    a.update([])  # 正常 → 静默
    assert calls == []
    stale = [f"S{i}/spot/1m:滞后3min" for i in range(15)]
    a.update(stale)  # 停更 → 告警一次（明细截断）
    assert len(calls) == 1 and "停更（15 项）" in calls[0], calls
    assert calls[0].count("- S") == 10 and "…共 15 项" in calls[0]
    a.update(stale)  # 持续停更 → 不重发
    assert len(calls) == 1
    a.update([])  # 恢复 → 通知一次（含持续分钟）
    assert len(calls) == 2 and "恢复" in calls[1] and "约 0 分钟" in calls[1]
    a.update(stale)  # 再次停更 → 重新告警
    assert len(calls) == 3

    calls2 = []
    ok["v"] = False
    b = _StaleAlerter(lambda md: (calls2.append(md), ok["v"])[1])
    b.update(["X/spot/1m:滞后9min"])
    assert len(calls2) == 1
    b.update(["X/spot/1m:滞后9min"])  # 发送失败 → 下轮重试
    assert len(calls2) == 2
    ok["v"] = True
    b.update(["X/spot/1m:滞后9min"])  # 重试送达
    assert len(calls2) == 3
    b.update(["X/spot/1m:滞后9min"])  # 已送达 → 静默
    assert len(calls2) == 3
    print("A) _StaleAlerter OK：静默/告警截断/不重发/失败重试/恢复/再告警")


# ---------- B：run_scanner._alert 端到端 ----------

def test_alert_e2e():
    import run_scanner
    from app.adapters.notify.wecom import WeComNotifier
    srv = new_server()
    url = f"http://127.0.0.1:{srv.server_port}/hook"
    conn = db.connect(settings.pg_dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM push_log")
        base = cur.fetchone()[0]
    old = settings.wecom_webhook
    object.__setattr__(settings, "wecom_webhook", url)
    try:
        assert run_scanner._alert("### 测试告警\n正文") is True
        p = srv.posts[0]
        assert p["msgtype"] == "markdown" and "测试告警" in p["markdown"]["content"]
        assert WeComNotifier(url).push_markdown("### 无库告警\nx") is True  # conn=None 路径
        assert len(srv.posts) == 2
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*), min(status) FROM push_log
                           WHERE id > %s AND channel='wecom'""", (base,))
            cnt, st = cur.fetchone()
        assert (cnt, st) == (1, "sent"), (cnt, st)  # 仅 _alert 写库；conn=None 不写
    finally:
        object.__setattr__(settings, "wecom_webhook", old)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM push_log WHERE id > %s AND channel='wecom'", (base,))
        conn.commit()
        conn.close()
        srv.shutdown()
    print("B) _alert OK：mock 收 markdown 卡片；push_log 仅记有库路径；conn=None 兼容")


# ---------- C：watchdog 子进程 ----------

def _write_log(path: pathlib.Path, age_min: float) -> None:
    t = datetime.now(CST) - timedelta(minutes=age_min)
    path.write_text(f"[心跳] {t:%m-%d %H:%M:%S} 池2标的 | 停更: 无\n", encoding="utf-8")


def _run_wd(log_p: pathlib.Path, state_p: pathlib.Path, url: str):
    env = {**os.environ, "WECOM_WEBHOOK": url, "FEISHU_WEBHOOK": "",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(WATCHDOG), "--log", str(log_p),
                           "--state", str(state_p)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120, env=env)


def test_watchdog(srv):
    url = f"http://127.0.0.1:{srv.server_port}/hook"
    with tempfile.TemporaryDirectory() as td:
        log_p, state_p = pathlib.Path(td) / "scanner.log", pathlib.Path(td) / "state.json"
        # 1) 心跳新鲜 → 静默、无状态
        _write_log(log_p, 0)
        r = _run_wd(log_p, state_p, url)
        assert "心跳正常" in r.stdout and not state_p.exists(), (r.stdout, r.stderr)
        # 2) 停更 10 分钟 → 告警 + 状态落盘
        n0 = len(srv.posts)
        _write_log(log_p, 10)
        r = _run_wd(log_p, state_p, url)
        assert len(srv.posts) == n0 + 1 and "扫描器心跳停止" in srv.posts[-1]["markdown"]["content"]
        assert "距最后心跳已 10 分钟" in srv.posts[-1]["markdown"]["content"]
        assert json.loads(state_p.read_text(encoding="utf-8")).get("down") is True
        # 3) 节流期 → 不重发
        r = _run_wd(log_p, state_p, url)
        assert len(srv.posts) == n0 + 1 and "已告警" in r.stdout
        # 4) 恢复 → 通知 + 清状态
        _write_log(log_p, 0)
        r = _run_wd(log_p, state_p, url)
        assert len(srv.posts) == n0 + 2 and "恢复" in srv.posts[-1]["markdown"]["content"]
        assert json.loads(state_p.read_text(encoding="utf-8")) == {}
        # 5) 发送失败 → 状态不落盘（下轮重试）
        _write_log(log_p, 10)
        r = _run_wd(log_p, state_p, DEAD_URL)
        assert r.returncode == 0 and "发送失败" in r.stdout
        assert not state_p.exists() or not json.loads(state_p.read_text(encoding="utf-8"))
        # 6) 恢复 webhook → 补发告警
        r = _run_wd(log_p, state_p, url)
        assert len(srv.posts) == n0 + 3 and "扫描器心跳停止" in srv.posts[-1]["markdown"]["content"]
        assert json.loads(state_p.read_text(encoding="utf-8")).get("down") is True
        # 7) 日志缺失 → 视作停摆
        log_p.unlink()
        state_p.unlink()
        r = _run_wd(log_p, state_p, url)
        assert len(srv.posts) == n0 + 4 and "N/A" in srv.posts[-1]["markdown"]["content"]
    print("C) watchdog OK：新鲜静默/停更告警/节流/恢复清理/失败重试/日志缺失检测")


def main() -> None:
    test_alerter()
    srv = new_server()
    try:
        test_watchdog(srv)
    finally:
        srv.shutdown()
    test_alert_e2e()
    print("A6 告警验证全部 PASS（离线 mock；测试数据已清理）")


if __name__ == "__main__":
    main()
