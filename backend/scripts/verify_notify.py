"""P0-8 推送离线验证（本地 mock webhook，不向真实企微发消息）：
1) payload 结构：markdown 文本 + image(base64/md5)；图片字节与 md5 与 PNG 文件一致；
2) 业务错误（errcode=93000）→ 失败 + push_log failed；
3) 网络错误 → 退避重试后成功（首连断开场景）；
4) push_log 落库 sent/failed。
真实推送验收需在 .env 填 WECOM_WEBHOOK 后由 live 扫描器触发。
"""
import base64
import hashlib
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.engine.indicators import enrich
from app.models import db

SIGNAL_ID = 999999  # 合成信号 id（无 FK），验证后清理 push_log/图表文件


class MockWeCom(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        self.server.attempts += 1
        if self.server.drop_first and not self.server.dropped:
            self.server.dropped = True
            self.connection.close()  # 模拟网络错误 → 触发退避重试
            return
        self.server.posts.append(payload)
        body = json.dumps({"errcode": self.server.errcode, "errmsg": "mock"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


def new_server(errcode=0, drop_first=False):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockWeCom)
    srv.errcode, srv.drop_first, srv.dropped = errcode, drop_first, False
    srv.posts, srv.attempts = [], 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_signal(conn) -> dict:
    df = db.load_ohlcv(conn, "BTC/USDT", "okx", "spot", "1h", limit=600)
    ind = enrich(df)
    last, atr = ind.iloc[-1], float(ind["atr14"].iloc[-1])
    return {"signal_id": SIGNAL_ID, "rule": "mock_rule", "symbol": "BTC/USDT",
            "exchange": "okx", "contract_type": "spot", "timeframe": "1h",
            "ts": last["ts"], "direction": "entry", "price": float(last["close"]),
            "price_zone_low": float(last["close"]) - 0.5 * atr,
            "price_zone_high": float(last["close"]) + 0.5 * atr, "atr": atr}


def last_log(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT status, error FROM push_log WHERE signal_id=%s ORDER BY id DESC LIMIT 1",
                    (SIGNAL_ID,))
        return cur.fetchone()


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    sig = make_signal(conn)

    # 1) 正常：markdown + image
    srv = new_server(errcode=0)
    n = WeComNotifier(f"http://127.0.0.1:{srv.server_port}/send?key=T")
    assert n.push_signal(sig, conn) is True
    assert [p["msgtype"] for p in srv.posts] == ["markdown", "image"], srv.posts
    img = srv.posts[1]["image"]
    raw = pathlib.Path(f"data/charts/signal_{SIGNAL_ID}.png").read_bytes()
    assert base64.b64decode(img["base64"]) == raw, "图片 base64 与 PNG 不一致"
    assert img["md5"] == hashlib.md5(raw).hexdigest(), "md5 不一致"
    content = srv.posts[0]["markdown"]["content"]
    assert content.startswith("### 买入信号"), content
    print(f"1) payload OK：markdown+image，图片 {len(raw)}B base64/md5 一致")
    print(f"   push_log: {last_log(conn)}")
    srv.shutdown()

    # 2) 业务错误 errcode=93000 → 失败且不重试
    srv = new_server(errcode=93000)
    n = WeComNotifier(f"http://127.0.0.1:{srv.server_port}/send?key=T", timeout=3)
    assert n.push_signal(sig, conn) is False
    assert srv.attempts == 1, f"业务错误不应重试，实际 {srv.attempts} 次"
    row = last_log(conn)
    assert row[0] == "failed" and "93000" in row[1], row
    print(f"2) 业务错误 OK：attempts=1，push_log={row}")
    srv.shutdown()

    # 3) 网络错误首连断开 → 退避重试后成功
    srv = new_server(errcode=0, drop_first=True)
    n = WeComNotifier(f"http://127.0.0.1:{srv.server_port}/send?key=T", timeout=3)
    assert n.push_signal(sig, conn) is True
    assert srv.attempts == 3, f"应为 断开1 + markdown重试1 + image1 = 3，实际 {srv.attempts}"
    assert last_log(conn)[0] == "sent"
    print(f"3) 重试 OK：attempts={srv.attempts}（断开1+md重试1+image1），push_log={last_log(conn)}")
    srv.shutdown()

    # 清理（图表文件与 push_log 均为本脚本产物）
    pathlib.Path(f"data/charts/signal_{SIGNAL_ID}.png").unlink(missing_ok=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM push_log WHERE signal_id=%s", (SIGNAL_ID,))
    conn.commit()
    conn.close()
    print("离线验证全部 PASS，测试数据已清理")


if __name__ == "__main__":
    main()
