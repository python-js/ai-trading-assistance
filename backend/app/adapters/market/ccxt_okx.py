"""OKX 行情适配器（ccxt 封装）。现货 + 永续；分页回填；限频退避。
实测约束见 docs/P0-阶段1-设计.md §6：单页上限 300、rateLimit=110ms、不支持 fetchLiquidations。
"""
import time

import ccxt

_PAGE = 300  # OKX fetch_ohlcv 单页上限（实测）
_OI_PAGE = 100  # OKX fetch_open_interest_history 单页上限（实测）


class OKXAdapter:
    exchange = "okx"

    def __init__(self, proxy: str = ""):
        cfg = {"enableRateLimit": True}
        if proxy:
            cfg["proxies"] = {"http": proxy, "https": proxy}
        self.ex = ccxt.okx(cfg)
        self.ex.load_markets()

    def to_ccxt_symbol(self, internal: str, contract_type: str) -> str:
        """内部 base/quote → ccxt 串。spot: BTC/USDT；perp: BTC/USDT:USDT。"""
        if contract_type == "spot":
            return internal
        quote = internal.split("/")[1]
        return f"{internal}:{quote}"

    def _retry(self, fn, *a, tries: int = 4, base: float = 1.0, **k):
        """网络类错误指数退避重试（限频退避）。"""
        for i in range(tries):
            try:
                return fn(*a, **k)
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
                if i == tries - 1:
                    raise
                time.sleep(base * (2 ** i))

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int | None = None,
                    limit: int | None = None, paginate: bool = False) -> list:
        if not paginate:
            return self._retry(self.ex.fetch_ohlcv, symbol, timeframe, since=since, limit=limit)
        rows: list = []
        step = self.ex.parse_timeframe(timeframe) * 1000
        cursor = since
        while True:
            batch = self._retry(self.ex.fetch_ohlcv, symbol, timeframe, since=cursor, limit=_PAGE)
            if not batch:
                break
            rows += batch
            if len(batch) < _PAGE or (limit and len(rows) >= limit):
                break
            cursor = batch[-1][0] + step
        return rows[:limit] if limit else rows

    def fetch_funding_rate(self, symbol: str) -> dict:
        return self._retry(self.ex.fetch_funding_rate, symbol)

    def fetch_open_interest(self, symbol: str) -> dict:
        return self._retry(self.ex.fetch_open_interest, symbol)

    def fetch_funding_rate_history(self, symbol: str, since: int | None = None, limit: int | None = None) -> list:
        # 实测：8h 粒度，30 天约 90 条，单次 limit 足够，无需分页。
        return self._retry(self.ex.fetch_funding_rate_history, symbol, since=since, limit=limit)

    def fetch_open_interest_history(self, symbol: str, timeframe: str = "1h", since: int | None = None,
                                    limit: int | None = None, paginate: bool = False) -> list:
        # 实测：1h 粒度单页上限 100；5m 返回空（不支持）。30 天需分页。
        if not paginate:
            return self._retry(self.ex.fetch_open_interest_history, symbol, timeframe, since=since, limit=limit)
        rows: list = []
        step = self.ex.parse_timeframe(timeframe) * 1000
        cursor = since
        while True:
            batch = self._retry(self.ex.fetch_open_interest_history, symbol, timeframe, since=cursor, limit=_OI_PAGE)
            if not batch:
                break
            rows += batch
            if len(batch) < _OI_PAGE or (limit and len(rows) >= limit):
                break
            cursor = batch[-1]["timestamp"] + step
        return rows[:limit] if limit else rows

    def server_time(self) -> int:
        return self._retry(self.ex.fetch_time)

    def top_symbols(self, n: int, quote: str = "USDT") -> list[str]:
        """现货 USDT 市场按 24h 成交额(quoteVolume)降序取前 n，返回内部规范串（base/quote）。"""
        stable = {"USDT", "USDC", "DAI", "USD", "TUSD", "FDUSD"}
        tickers = self._retry(self.ex.fetch_tickers, params={"type": "spot"})
        rows = []
        for sym, t in tickers.items():
            m = self.ex.markets.get(sym, {})
            if not m.get("spot") or m.get("quote") != quote or m.get("base") in stable:
                continue
            rows.append((t.get("quoteVolume") or 0, sym))
        rows.sort(reverse=True)
        return [sym for _, sym in rows[:n]]
