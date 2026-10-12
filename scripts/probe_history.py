"""探测 OKX funding/OI 历史接口的粒度与回溯深度（P0-2 资金面票数据）。"""
import ccxt

PROXY = "http://127.0.0.1:7897"
ex = ccxt.okx({"proxies": {"http": PROXY, "https": PROXY}, "enableRateLimit": True})
ex.load_markets()

SYM = "BTC/USDT:USDT"

print("==== fetch_funding_rate_history (BTC perp) ====")
try:
    since = ex.milliseconds() - 30 * 86400 * 1000
    fh = ex.fetch_funding_rate_history(SYM, since=since, limit=1000)
    print("rows:", len(fh))
    if fh:
        print("oldest:", fh[0]["datetime"], "newest:", fh[-1]["datetime"])
        # 推算粒度：相邻两条间隔
        ts = [x["timestamp"] for x in fh]
        deltas = {ts[i + 1] - ts[i] for i in range(min(5, len(ts) - 1))}
        print("间隔(ms)样本:", deltas, "=> 约", [d / 3600000 for d in deltas], "小时")
        print("字段样本:", {k: fh[-1][k] for k in ("datetime", "fundingRate", "markPrice", "indexPrice") if k in fh[-1]})
except Exception as e:
    print("ERROR", type(e).__name__, str(e)[:200])

print("\n==== fetch_open_interest_history (BTC perp) ====")
for tf in ["5m", "1h", "1d"]:
    try:
        since = ex.milliseconds() - 30 * 86400 * 1000
        oi = ex.fetch_open_interest_history(SYM, timeframe=tf, since=since, limit=100)
        print(f"tf={tf} rows:", len(oi), end=" ")
        if oi:
            print("| oldest:", oi[0]["datetime"], "newest:", oi[-1]["datetime"],
                  "| 字段:", [k for k in ("openInterestAmount", "openInterestValue") if k in oi[-1]])
        else:
            print()
    except Exception as e:
        print(f"tf={tf} ERROR", type(e).__name__, str(e)[:150])

print("\n==== DONE ====")
