"""P0-8 企业微信机器人推送（webhook）：markdown 文本 + K 线截图（base64+md5）。

- 企微图文卡片（template_card）要求图片有公网 URL，本地场景不可用；故一条信号推两条
  消息：markdown 信号文本 + image 截图（base64+md5）。
- 重试：仅网络类错误退避重试（1s/2s）；errcode≠0（如 93000 无效 webhook）为业务错误直接失败。
- push_log 记录 sent/failed；图表失败不阻断文本推送（error 备注）。
"""
import base64
import hashlib
import pathlib
import time

import requests

from app.adapters.chart.mplfinance_chart import render
from app.engine.indicators import enrich
from app.models import db

CHART_DIR = "data/charts"
_AUTO = object()  # push_signal img_path 的"未指定"哨兵（区分"内部渲染"与"外部已渲染/失败"）


def render_signal_md(s: dict) -> str:
    d = "买入" if s["direction"] == "entry" else "卖出"
    lines = [f"### {d}信号 {s['symbol']} {s['timeframe']}",
             f"> 规则：{s['rule']}",
             f"> 价格：{s['price']:.6g}（区间 {s['price_zone_low']:.6g} ~ {s['price_zone_high']:.6g}）",
             f"> 时间：{s['ts']:%Y-%m-%d %H:%M} UTC · {s['contract_type']}"]
    vl = _votes_line(s)
    if vl:
        lines.append(vl)
    cv = s.get("chart")
    if cv:
        lines.append(f"> 读图：{_STANCE_CN.get(cv[1]['stance'], cv[1]['stance'])} {cv[0]:.2f}"
                     f" · {cv[1]['reason'][:40]}")
    if s.get("reason"):
        lines.append(f"> AI 研判：{s['reason']}")
    lines.append("> *研究信号，非投资建议*")
    return "\n".join(lines)


_STANCE_CN = {"agree": "赞同", "oppose": "反对", "neutral": "中性"}


def _votes_line(s: dict) -> str | None:
    """三票强度行（P2-4）；无 votes 字段返回 None（P0 兼容）。"""
    v = s.get("votes") or {}
    if not v:
        return None
    parts = []
    for k, label in (("tech", "技术"), ("flow", "资金面"), ("info", "信息")):
        if k in v:
            st = v[k][0]
            parts.append(f"{label} {st:.2f}" if st is not None else f"{label} —")
    return "> 三票：" + " · ".join(parts) if parts else None


class WeComNotifier:
    channel = "wecom"

    def __init__(self, webhook: str, timeout: float = 10.0):
        self.webhook = webhook
        self.timeout = timeout

    def _post(self, payload: dict, tries: int = 3) -> None:
        for i in range(tries):
            try:
                r = requests.post(self.webhook, json=payload, timeout=self.timeout)
                r.raise_for_status()
            except requests.RequestException:
                if i == tries - 1:
                    raise
                time.sleep(2 ** i)
                continue
            body = r.json()
            if body.get("errcode") != 0:
                raise RuntimeError(f"errcode={body.get('errcode')} {body.get('errmsg')}")
            return

    def push_signal(self, s: dict, conn, img_path: str | None = _AUTO) -> bool:
        """一条信号 = markdown + image（出图成功时）。返回文本是否送达。
        img_path 缺省=内部渲染；显式传（含 None）=用外部产物（P2-6 读图票共用同一张图）。"""
        err = None
        if img_path is _AUTO:
            img_path = None
            try:
                df = db.load_ohlcv(conn, s["symbol"], s["exchange"], s["contract_type"],
                                   s["timeframe"], limit=600)
                ind = enrich(df)
                img_path = render(ind, [s], f"{CHART_DIR}/signal_{s['signal_id']}.png",
                                  title=f"{s['symbol']} {s['timeframe']} {s['rule']}")
            except Exception as e:  # 图表失败不阻断文本推送
                err = f"chart: {e}"
        try:
            self._post({"msgtype": "markdown", "markdown": {"content": render_signal_md(s)}})
        except Exception as e:
            db.insert_push_log(conn, signal_id=s["signal_id"], channel=self.channel,
                               status="failed", error=f"md: {e}")
            return False
        if img_path:
            try:
                raw = pathlib.Path(img_path).read_bytes()
                self._post({"msgtype": "image", "image": {
                    "base64": base64.b64encode(raw).decode(),
                    "md5": hashlib.md5(raw).hexdigest()}})
            except Exception as e:
                err = f"image: {e}"
        db.insert_push_log(conn, signal_id=s["signal_id"], channel=self.channel,
                           status="sent", error=err)
        return True

    def push_markdown(self, md: str, conn) -> bool:
        """纯 markdown 消息（周报等非信号推送）；push_log 记 signal_id=NULL。"""
        try:
            self._post({"msgtype": "markdown", "markdown": {"content": md}})
        except Exception as e:
            db.insert_push_log(conn, signal_id=None, channel=self.channel,
                               status="failed", error=f"md: {e}")
            return False
        db.insert_push_log(conn, signal_id=None, channel=self.channel, status="sent")
        return True
