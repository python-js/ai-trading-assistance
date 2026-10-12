"""A股行情适配器（P1-6）：akshare 新浪源日线，后复权入库（PIT 口径）。

- 周期仅 1d：akshare 分钟线仅保留最近约 5 个交易日，无法满足回填/回测样本量。
- 复权：后复权 hfq——历史 bar 值不随新除权除息变化，PIT 与幂等的前提（前复权不可用）。
- 数据源锁定新浪（stock_zh_a_daily）：东财 kline API 在本机被限频 RST 不可靠，且两源
  hfq 基准不同（实测同 bar 差 1.59 倍），跨源 fallback 会造成序列跳变——故单源 + 重试；
  单日失败由扫描器"次日补评"语义兜底（见 scanner.scan_daily）。
- bar ts = 所属交易日 00:00 UTC；可用时刻 = ts + 7h（=15:00 CST 收盘，中国无夏令时）。
  未到可用时刻的 bar（盘中实时快照）丢弃，防止收盘前入库破坏 PIT。
- 内部 symbol "000157.SZ" ↔ akshare 代码 "sz000157"。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import akshare as ak

_AVAIL_OFFSET_MS = 7 * 3600 * 1000  # 15:00 CST = 07:00 UTC


def _fetch_hist(symbol: str, start: str, end: str, retries: int = 3) -> list:
    """新浪源；统一输出 [(YYYY-MM-DD, o, h, l, c, v), ...]，量单位手（股÷100）。"""
    pre = "sh" if symbol.startswith("6") else ("bj" if symbol.startswith(("4", "8")) else "sz")
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    last: Exception | None = None
    for i in range(retries):
        try:
            df = ak.stock_zh_a_daily(symbol=pre + symbol, start_date=s, end_date=e, adjust="hfq")
            return [(str(r["date"])[:10], float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r["volume"]) / 100)
                    for _, r in df.iterrows()]
        except Exception as err:  # 网络抖动（ProxyError/RemoteDisconnected）重试
            last = err
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


class AkshareAdapter:
    exchange = "akshare"

    def to_ccxt_symbol(self, internal: str, contract_type: str) -> str:
        return internal.split(".")[0]

    def server_time(self) -> int:
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1d", since: int | None = None,
                    limit: int | None = None, paginate: bool = False) -> list:
        """symbol 为 6 位纯代码；返回 ccxt 风格 [[ms, o, h, l, c, v], ...] 升序。"""
        if timeframe != "1d":
            raise ValueError(f"akshare 适配器仅支持日线: {timeframe}")
        start = (datetime.fromtimestamp(since / 1000, tz=timezone.utc).strftime("%Y%m%d")
                 if since else "19900101")
        now = datetime.now(timezone.utc)
        end = (now + timedelta(hours=8)).strftime("%Y%m%d")  # CST 当日（含盘中，由下方过滤丢弃）
        df = _fetch_hist(symbol, start, end)
        now_ms = int(now.timestamp() * 1000)
        rows: list = []
        for ds, o, h, l, c, v in df:
            day = datetime.strptime(ds[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ts = int(day.timestamp() * 1000)
            if ts + _AVAIL_OFFSET_MS > now_ms:
                continue
            rows.append([ts, o, h, l, c, v])
        return rows
