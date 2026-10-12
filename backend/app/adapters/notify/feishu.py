"""P2-7 飞书自定义机器人推送（webhook）：交互卡片（lark_md）。

- 渠道差异：飞书自定义机器人 webhook 无法直传图片（image_key 需应用 token 上传），
  故仅推卡片文本；K 线截图由企业微信通道承载（主通道）。
- 签名：配置 FEISHU_SECRET 时附 timestamp+sign（官方算法：HMAC-SHA256，key=ts\\nsecret）。
- 重试：与企微一致——仅网络类错误退避重试（1s/2s）；code≠0 业务错误直接失败。
- push_log 记录 sent/failed（channel='feishu'）。
"""
import base64
import hashlib
import hmac
import time

import requests

from app.core.tz import to_bj
from app.models import db

_STANCE_CN = {"agree": "赞同", "oppose": "反对", "neutral": "中性"}


def render_signal_lark(s: dict) -> str:
    """信号卡片正文（lark_md；飞书无 "> " 引用语法，改用加粗标签）。"""
    lines = [f"**规则**：{s['rule']}",
             f"**价格**：{s['price']:.6g}（区间 {s['price_zone_low']:.6g} ~ {s['price_zone_high']:.6g}）",
             f"**时间**：{to_bj(s['ts']):%Y-%m-%d %H:%M} 北京时间 · {s['contract_type']}"]
    v = s.get("votes") or {}
    if v:
        parts = []
        for k, label in (("tech", "技术"), ("flow", "资金面"), ("info", "信息")):
            if k in v:
                st = v[k][0]
                parts.append(f"{label} {st:.2f}" if st is not None else f"{label} —")
        if parts:
            lines.append("**三票**：" + " · ".join(parts))
    cv = s.get("chart")
    if cv:
        lines.append(f"**读图**：{_STANCE_CN.get(cv[1]['stance'], cv[1]['stance'])} {cv[0]:.2f}"
                     f" · {cv[1]['reason'][:40]}")
    ev = v.get("info")
    top = (ev[1].get("top") or []) if ev else []
    if top and top[0].get("url"):
        t = top[0]
        lines.append(f"**溯源**：[{(t.get('title') or t['url'])[:40]}]({t['url']})")
    if s.get("reason"):
        lines.append(f"**AI 研判**：{s['reason']}")
    lines.append("*研究信号，非投资建议*")
    return "\n".join(lines)


class FeishuNotifier:
    channel = "feishu"

    def __init__(self, webhook: str, secret: str = "", timeout: float = 10.0):
        self.webhook = webhook
        self.secret = secret
        self.timeout = timeout

    def _signed(self, payload: dict) -> dict:
        if not self.secret:
            return payload
        ts = str(int(time.time()))
        sign = base64.b64encode(
            hmac.new(f"{ts}\n{self.secret}".encode(), b"", hashlib.sha256).digest()).decode()
        return {**payload, "timestamp": ts, "sign": sign}

    def _post(self, payload: dict, tries: int = 3) -> None:
        for i in range(tries):
            try:
                r = requests.post(self.webhook, json=self._signed(payload), timeout=self.timeout)
                r.raise_for_status()
            except requests.RequestException:
                if i == tries - 1:
                    raise
                time.sleep(2 ** i)
                continue
            body = r.json()
            if body.get("code") != 0:
                raise RuntimeError(f"code={body.get('code')} {body.get('msg')}")
            return

    @staticmethod
    def _card(title: str, body: str) -> dict:
        return {"msg_type": "interactive",
                "card": {"header": {"title": {"tag": "plain_text", "content": title}},
                         "elements": [{"tag": "div",
                                       "text": {"tag": "lark_md", "content": body}}]}}

    def push_signal(self, s: dict, conn) -> bool:
        """信号卡片推送。返回是否送达。"""
        d = "买入" if s["direction"] == "entry" else "卖出"
        try:
            self._post(self._card(f"{d}信号 {s['symbol']} {s['timeframe']}",
                                  render_signal_lark(s)))
        except Exception as e:
            db.insert_push_log(conn, signal_id=s["signal_id"], channel=self.channel,
                               status="failed", error=f"card: {e}")
            return False
        db.insert_push_log(conn, signal_id=s["signal_id"], channel=self.channel, status="sent")
        return True

    def push_markdown(self, md: str, conn=None) -> bool:
        """周报/告警等：首行 "### 标题" 提为卡片标题，其余作 lark_md 正文；
        conn 为空时跳过 push_log（看门狗不依赖 DB）。"""
        lines = md.splitlines()
        title = "行情简报"
        if lines and lines[0].startswith("### "):
            title, lines = lines[0][4:], lines[1:]
        try:
            self._post(self._card(title, "\n".join(lines)))
        except Exception as e:
            if conn:
                db.insert_push_log(conn, signal_id=None, channel=self.channel,
                                   status="failed", error=f"card: {e}")
            return False
        if conn:
            db.insert_push_log(conn, signal_id=None, channel=self.channel, status="sent")
        return True
