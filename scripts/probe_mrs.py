"""探测 MRS 数据准备：OKX BTC-USDT-SWAP 合约参数、1m 回溯深度、拉取速度。

M0 用：核对 tick/lot/minNotional（§11 要求程序启动校验交易所实际值）。
"""
import time

import ccxt

PROXY = "http://127.0.0.1:7897"
SYM = "BTC/USDT:USDT"

ex = ccxt.okx({"proxies": {"http": PROXY, "https": PROXY}, "enableRateLimit": True})
ex.options["fetchMarkets"] = ["spot", "swap"]
ex.load_markets()

m = ex.markets[SYM]
print("==== 合约参数 ====")
print("precision(price/amount):", m["precision"])
print("limits(price/amount/cost):", m["limits"])
print("contractSize:", m.get("contractSize"), "| linear:", m.get("linear"), "| settle:", m.get("settle"))

print("\n==== 1m 回溯深度与速度 ====")
now = ex.milliseconds()
for months in (2, 6, 12, 24):
    since = now - months * 30 * 86400_000
    t0 = time.time()
    try:
        rows = ex.fetch_ohlcv(SYM, "1m", since=since, limit=100)
        dt = time.time() - t0
        if rows:
            print(f"{months:2}月前: {len(rows)} 根 | 首根 {ex.iso8601(rows[0][0])} | "
                  f"末根 {ex.iso8601(rows[-1][0])} | {dt:.2f}s")
        else:
            print(f"{months:2}月前: 空 | {dt:.2f}s")
    except Exception as e:
        print(f"{months:2}月前: ERROR {type(e).__name__} {str(e)[:120]}")

print("\n==== history-candles 连续分页速度(5 连发) ====")
since = now - 6 * 30 * 86400_000
t0 = time.time()
cursor, n = since, 0
for i in range(5):
    rows = ex.fetch_ohlcv(SYM, "1m", since=cursor, limit=100)
    if not rows:
        break
    n += len(rows)
    cursor = rows[-1][0] + 60_000
print(f"5 连发: {n} 根 | {time.time() - t0:.2f}s | 速率 {n / (time.time() - t0):.0f} 根/s")

print("\n==== funding 历史 24 个月 ====")
t0 = time.time()
fh = ex.fetch_funding_rate_history(SYM, since=now - 24 * 30 * 86400_000, limit=1000)
print(f"{len(fh)} 条 | 首条 {fh[0]['datetime'] if fh else '-'} | 末条 {fh[-1]['datetime'] if fh else '-'} | {time.time() - t0:.2f}s")

print("\n==== DONE ====")
