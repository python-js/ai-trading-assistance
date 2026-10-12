"""P2-3 A1 资讯抓取：RSS → 归一化条目（供 news_raw 入库）。

定源（2026-10-12 实测直连可用；Odaily/BlockBeats RSS 已下线故未纳入）：
panews(中文快讯) / coindesk / cointelegraph / theblock。
published_ts 缺失或条目无链接时跳过；summary 去 HTML 标签并截断（LLM 输入）。
"""
from __future__ import annotations

import calendar
import re
import time
from datetime import datetime, timezone

import feedparser
import requests

SOURCES: tuple[dict, ...] = (
    {"name": "panews", "url": "https://rss.panewslab.com/zh/tvsq/rss"},
    {"name": "coindesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "cointelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "theblock", "url": "https://www.theblock.co/rss.xml"},
)

_UA = {"User-Agent": "Mozilla/5.0"}
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
MAX_TITLE = 300
MAX_CONTENT = 500


def _plain(html: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", html)).strip()


def _summary(entry) -> str:
    raw = entry.get("summary") or ""
    if not raw:
        content = entry.get("content") or []
        raw = content[0].get("value", "") if content else ""
    return _plain(raw)[:MAX_CONTENT]


def _entries(content: bytes, source: dict) -> list[dict]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        pub = e.get("published_parsed") or e.get("updated_parsed")
        ts = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc) if pub else None
        out.append({"source": source["name"], "url": e["link"],
                    "title": _plain(e.get("title") or "")[:MAX_TITLE],
                    "summary": _summary(e), "published_ts": ts})
    return out


def fetch(source: dict, timeout: float = 15.0, proxy: str | None = None) -> list[dict]:
    """抓取单源并归一化。每路由最多 2 次；直连失败且配置了代理时改走代理
    （feed CDN 瞬断/阈值拦截常见）。"""
    routes = [None] + ([{"http": proxy, "https": proxy}] if proxy else [])
    last: Exception | None = None
    for proxies in routes:
        for i in range(2):
            try:
                r = requests.get(source["url"], headers=_UA, timeout=timeout,
                                 proxies=proxies)
                r.raise_for_status()
                return _entries(r.content, source)
            except requests.RequestException as e:
                last = e
                tag = "代理" if proxies else "直连"
                print(f"[资讯] {source['name']} {tag}第 {i + 1} 次失败({type(e).__name__})",
                      flush=True)
                time.sleep(2.0)
    raise last
