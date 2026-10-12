"""P0-2 OKX 连通性与能力探测（只读，公共 API，无需 key）。
实测 4 项待定：fetch_liquidations 支持度 / ohlcv 单页 limit 与历史粒度 / 限频阈值 / 现货+永续方法可用性。
"""
import ccxt

PROXY = "http://127.0.0.1:7897"
ex = ccxt.okx({"proxies": {"http": PROXY, "https": PROXY}, "enableRateLimit": True})

print("==== 基本属性 ====")
print("rateLimit(ms):", ex.rateLimit)
for cap in ["fetchOHLCV", "fetchFundingRate", "fetchFundingRates", "fetchFundingRateHistory",
            "fetchOpenInterest", "fetchOpenInterestHistory", "fetchLiquidations", "fetchMyLiquidations"]:
    print(f"has.{cap}:", ex.has.get(cap))

ex.load_markets()
print("\n==== timeframes ====")
print(list(ex.timeframes.keys()))

print("\n==== symbol 可用性 ====")
for s in ["BTC/USDT", "BTC/USDT:USDT", "ETH/USDT", "ETH/USDT:USDT", "ONDO/USDT", "ONDO/USDT:USDT"]:
    m = ex.markets.get(s)
    print(f"{s:16} -> {'OK' if m else 'MISSING'}", (m.get("contractType") or m.get("spot") or "") if m else "")

print("\n==== ohlcv 单页 limit 测试 (BTC/USDT 1m) ====")
for lim in [100, 200, 300, 500]:
    try:
        o = ex.fetch_ohlcv("BTC/USDT", "1m", limit=lim)
        print(f"limit={lim:4} -> got {len(o):4} rows")
    except Exception as e:
        print(f"limit={lim:4} -> ERROR {type(e).__name__}: {str(e)[:120]}")

print("\n==== 历史回填深度 (since=90天前, 1m) ====")
try:
    since = ex.milliseconds() - 90 * 24 * 3600 * 1000
    o = ex.fetch_ohlcv("BTC/USDT", "1m", since=since, limit=100)
    print("rows:", len(o), "first ts:", o[0][0], "=>", ccxt.okx().iso8601(o[0][0]))
except Exception as e:
    print("ERROR", type(e).__name__, str(e)[:150])

print("\n==== 永续资金费率 (BTC/USDT:USDT) ====")
try:
    fr = ex.fetch_funding_rate("BTC/USDT:USDT")
    print("fundingRate:", fr.get("fundingRate"), "nextFundingTs:", fr.get("fundingDatetime"))
except Exception as e:
    print("ERROR", type(e).__name__, str(e)[:150])

print("\n==== 持仓量 open interest (BTC/USDT:USDT) ====")
try:
    oi = ex.fetch_open_interest("BTC/USDT:USDT")
    print("openInterestAmount:", oi.get("openInterestAmount"), "openInterestValue:", oi.get("openInterestValue"))
except Exception as e:
    print("ERROR", type(e).__name__, str(e)[:150])

print("\n==== 爆仓 liquidations (BTC/USDT:USDT) ====")
try:
    lq = ex.fetch_liquidations("BTC/USDT:USDT")
    print("rows:", len(lq), "sample:", lq[0] if lq else None)
except Exception as e:
    print("ERROR", type(e).__name__, str(e)[:200])

print("\n==== DONE ====")
