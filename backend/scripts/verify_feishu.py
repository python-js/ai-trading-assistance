"""P2-7 飞书适配器离线验证（本地 mock webhook，不向真实飞书发消息）：
① 信号卡片 payload：interactive + header 标题 + lark_md（三票/读图/AI 研判/免责）；push_log sent；
② 业务错误（code=19001）→ 失败不重试 + push_log failed；
③ 网络错误首连断开 → 退避重试后成功（attempts=2）；
④ 签名：FEISHU_SECRET 时附 timestamp+sign（独立复算 HMAC 一致）；无 secret 时不附；
⑤ push_markdown：首行 "### " 提为卡片标题 + 正文；push_log signal_id=NULL；
⑥ 无 votes/chart/reason 字段兼容（P0 版式）；清理 push_log。
真实推送需在 .env 填 FEISHU_WEBHOOK 后由 live 扫描器触发。
"""
import base64
import hashlib
import hmac
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.feishu import FeishuNotifier, render_signal_lark
from app.core.config import settings
from app.engine.indicators import enrich
from app.models import db

SIGNAL_ID = 999998  # 合成信号 id（无 FK），验证后清理 push_log


class MockFeishu(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        self.server.attempts += 1
        if self.server.drop_first and not self.server.dropped:
            self.server.dropped = True
            self.connection.close()  # 模拟网络错误 → 触发退避重试
            return
        self.server.posts.append(payload)
        body = json.dumps({"code": self.server.code, "msg": "mock"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


def new_server(code=0, drop_first=False):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockFeishu)
    srv.code, srv.drop_first, srv.dropped = code, drop_first, False
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
            "price_zone_high": float(last["close"]) + 0.5 * atr, "atr": atr,
            "votes": {"tech": (0.75, {}), "flow": (0.62, {}), "info": (None, {})},
            "chart": (0.9, {"stance": "agree", "confidence": 0.8,
                            "reason": "EMA20 上穿 EMA60", "model": "mock"}),
            "reason": "技术面偏强，注意量能。"}


def last_log(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT status, error FROM push_log
                       WHERE channel='feishu' ORDER BY id DESC LIMIT 1""")
        return cur.fetchone()


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    sig = make_signal(conn)

    # 1) 正常：interactive 卡片（含三票/读图/AI 研判）
    srv = new_server(code=0)
    n = FeishuNotifier(f"http://127.0.0.1:{srv.server_port}/hook/T")
    assert n.push_signal(sig, conn) is True
    p = srv.posts[0]
    assert p["msg_type"] == "interactive"
    assert p["card"]["header"]["title"]["content"] == "买入信号 BTC/USDT 1h"
    body = p["card"]["elements"][0]["text"]["content"]
    for frag in ("**规则**", "**三票**：技术 0.75 · 资金面 0.62 · 信息 —",
                 "**读图**：赞同 0.90 · EMA20", "**AI 研判**：技术面偏强", "非投资建议"):
        assert frag in body, f"缺 {frag}: {body}"
    assert last_log(conn)[0] == "sent"
    print(f"1) payload OK：interactive 卡片，正文 {len(body)} 字，push_log={last_log(conn)}")
    srv.shutdown()

    # 2) 业务错误 code=19001 → 失败且不重试
    srv = new_server(code=19001)
    n = FeishuNotifier(f"http://127.0.0.1:{srv.server_port}/hook/T", timeout=3)
    assert n.push_signal(sig, conn) is False
    assert srv.attempts == 1, f"业务错误不应重试，实际 {srv.attempts} 次"
    row = last_log(conn)
    assert row[0] == "failed" and "19001" in row[1], row
    print(f"2) 业务错误 OK：attempts=1，push_log={row}")
    srv.shutdown()

    # 3) 网络错误首连断开 → 退避重试后成功
    srv = new_server(code=0, drop_first=True)
    n = FeishuNotifier(f"http://127.0.0.1:{srv.server_port}/hook/T", timeout=3)
    assert n.push_signal(sig, conn) is True
    assert srv.attempts == 2, f"应为 断开1 + 重试1 = 2，实际 {srv.attempts}"
    assert last_log(conn)[0] == "sent"
    print(f"3) 重试 OK：attempts={srv.attempts}，push_log={last_log(conn)}")
    srv.shutdown()

    # 4) 签名（独立复算）
    srv = new_server(code=0)
    n = FeishuNotifier(f"http://127.0.0.1:{srv.server_port}/hook/T", secret="s3cr3t")
    n.push_signal(sig, conn)
    p = srv.posts[0]
    assert "timestamp" in p and "sign" in p, "签名模式应附 timestamp+sign"
    expect = base64.b64encode(
        hmac.new(f"{p['timestamp']}\ns3cr3t".encode(), b"", hashlib.sha256).digest()).decode()
    assert p["sign"] == expect, f"签名不一致: {p['sign']} != {expect}"
    n2 = FeishuNotifier(f"http://127.0.0.1:{srv.server_port}/hook/T")
    n2.push_signal(sig, conn)
    assert "timestamp" not in srv.posts[1] and "sign" not in srv.posts[1], "无 secret 不应附签名"
    print(f"4) 签名 OK：HMAC-SHA256 复算一致（ts={p['timestamp']}）；无 secret 不附")
    srv.shutdown()

    # 5) push_markdown（周报）：标题提取 + NULL 记录
    srv = new_server(code=0)
    n = FeishuNotifier(f"http://127.0.0.1:{srv.server_port}/hook/T")
    md = "### 台账报表 10-05 ~ 10-12 (UTC)\n**总览**：208 笔 · 胜率 16.8%\n- 1h：12 笔"
    assert n.push_markdown(md, conn) is True
    p = srv.posts[0]
    assert p["card"]["header"]["title"]["content"] == "台账报表 10-05 ~ 10-12 (UTC)"
    assert p["card"]["elements"][0]["text"]["content"].startswith("**总览**")
    with conn.cursor() as cur:
        cur.execute("""SELECT signal_id FROM push_log WHERE channel='feishu'
                       ORDER BY id DESC LIMIT 1""")
        assert cur.fetchone()[0] is None
    print("5) push_markdown OK：标题提取 + lark_md 正文 + signal_id=NULL")
    srv.shutdown()

    # 6) P0 版式兼容（无 votes/chart/reason）
    old = {k: v for k, v in sig.items() if k not in ("votes", "chart", "reason")}
    body = render_signal_lark(old)
    assert "三票" not in body and "读图" not in body and "AI 研判" not in body
    print("6) P0 版式兼容 OK（无三票/读图/研判行）")

    # 清理（push_log 均为本脚本产物：channel='feishu'）
    with conn.cursor() as cur:
        cur.execute("DELETE FROM push_log WHERE channel='feishu'")
    conn.commit()
    conn.close()
    print("离线验证全部 PASS，测试数据已清理")


if __name__ == "__main__":
    main()
