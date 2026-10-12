"""P0-4 验证：规则 DSL 能否在历史数据上复现指定买卖点（独立参考实现比对）。

1) ema_cross：状态机 vs 向量化交叉掩码，逐事件严格比对（强校验）
2) ema_cross + RSI 过滤 + chandelier 出场 + cooldown=6：逐事件条件/冷却复核
3) breakout_20（唐奇安突破）：首个入场点对齐 + 逐事件必要条件
4) 解析器非法输入报错用例
用法：.venv/Scripts/python.exe backend/scripts/verify_dsl.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate, parse_rule
from app.models import db

RULES_RAW = [
    {
        "name": "ema_cross", "symbol": "BTC/USDT", "timeframe": "1h",
        "entry": {"cross_up": ["ema20", "ema60"]},
        "exit": {"cross_down": ["ema20", "ema60"]},
    },
    {
        "name": "ema_cross_rsi_flt", "symbol": "BTC/USDT", "timeframe": "1h",
        "entry": {"all": [{"cross_up": ["ema20", "ema60"]}, {"rsi14": {"<": 70}}]},
        "exit": {"any": [{"cross_down": ["ema20", "ema60"]},
                         {"chandelier_exit": {"atr_mult": 3}}]},
        "cooldown_bars": 6,
    },
    {
        "name": "breakout_20", "symbol": "ETH/USDT", "timeframe": "1h",
        "entry": {"breakout_up": 20},
        "exit": {"breakout_down": 20},
    },
]

BAD_RAW = [
    ("算子拼写错误", {"name": "x", "symbol": "BTC/USDT", "timeframe": "1h",
                  "entry": {"rsi14": {"=<": 70}}, "exit": {"cross_down": ["ema20", "ema60"]}}),
    ("entry 使用 chandelier", {"name": "x", "symbol": "BTC/USDT", "timeframe": "1h",
                            "entry": {"chandelier_exit": {"atr_mult": 3}},
                            "exit": {"cross_down": ["ema20", "ema60"]}}),
    ("叶子缺比较值", {"name": "x", "symbol": "BTC/USDT", "timeframe": "1h",
                 "entry": {"rsi14": 70}, "exit": {"cross_down": ["ema20", "ema60"]}}),
    ("缺 exit 字段", {"name": "x", "symbol": "BTC/USDT", "timeframe": "1h",
                  "entry": {"rsi14": {"<": 70}}}),
]


def load(symbol: str, tf: str):
    conn = db.connect(settings.pg_dsn)
    df = db.load_ohlcv(conn, symbol, "okx", "spot", tf)
    conn.close()
    return enrich(df)


def fmt(events, n=3):
    head = ", ".join(f"{e.ts:%m-%d %H:%M} {e.direction}@{e.price:.1f}" for e in events[:n])
    tail = ", ".join(f"{e.ts:%m-%d %H:%M} {e.direction}@{e.price:.1f}" for e in events[-n:])
    return f"{len(events)} 个事件 | 首: {head} | 末: {tail}"


def check_cross(df, rule) -> bool:
    """纯交叉策略：事件序列必须与向量化掩码严格一致且 entry/exit 交替。"""
    events = evaluate(rule, df)
    if not events or events[0].direction != "entry":
        print("    FAIL: 首个事件应为 entry")
        return False
    up = ((df["ema20"] > df["ema60"]) & (df["ema20"].shift() <= df["ema60"].shift())).to_numpy()
    dn = ((df["ema20"] < df["ema60"]) & (df["ema20"].shift() >= df["ema60"].shift())).to_numpy()
    ref = [df["ts"].iloc[i] for i in range(WARMUP_BARS, len(df)) if up[i] or dn[i]]
    ref = [t for t in ref if t >= events[0].ts]
    if [e.ts for e in events] != ref:
        print(f"    FAIL: 与向量化参考不一致 got={len(events)} ref={len(ref)}")
        return False
    if any(e.direction != ("entry" if k % 2 == 0 else "exit") for k, e in enumerate(events)):
        print("    FAIL: 事件未 entry/exit 交替")
        return False
    return True


def check_recheck(df, rule, cooldown_expected: int) -> bool:
    """逐事件复核入场/出场条件与冷却（独立于评估器重算）。"""
    events = evaluate(rule, df)
    ts2i = {t: i for i, t in enumerate(df["ts"])}
    entry_i = last_exit_i = None
    for e in events:
        i = ts2i[e.ts]
        if i < WARMUP_BARS:
            print(f"    FAIL: 预热期内出现事件 @ {e.ts}")
            return False
        if e.direction == "entry":
            cross_up = (df["ema20"].iloc[i - 1] <= df["ema60"].iloc[i - 1]
                        and df["ema20"].iloc[i] > df["ema60"].iloc[i])
            if not cross_up or not df["rsi14"].iloc[i] < 70:
                print(f"    FAIL: entry 条件不成立 @ {e.ts}")
                return False
            if last_exit_i is not None and i - last_exit_i < cooldown_expected:
                print(f"    FAIL: 冷却未满足（间隔 {i - last_exit_i}）@ {e.ts}")
                return False
            entry_i = i
        else:
            cross_dn = (df["ema20"].iloc[i - 1] >= df["ema60"].iloc[i - 1]
                        and df["ema20"].iloc[i] < df["ema60"].iloc[i])
            chand = df["close"].iloc[i] < (df["high"].iloc[entry_i:i + 1].max()
                                           - 3 * df["atr14"].iloc[i])
            if not (cross_dn or chand):
                print(f"    FAIL: exit 条件不成立 @ {e.ts}")
                return False
            last_exit_i = i
    if any(e.direction != ("entry" if k % 2 == 0 else "exit") for k, e in enumerate(events)):
        print("    FAIL: 事件未 entry/exit 交替")
        return False
    return True


def check_breakout(df, rule, window: int) -> bool:
    """突破策略：首个入场点与向量化参考一致，逐事件必要条件成立。"""
    events = evaluate(rule, df)
    ts2i = {t: i for i, t in enumerate(df["ts"])}
    up_first = next((i for i in range(WARMUP_BARS, len(df))
                     if df["close"].iloc[i] > df["high"].iloc[i - window:i].max()), None)
    if not events or events[0].direction != "entry" or ts2i[events[0].ts] != up_first:
        print("    FAIL: 首个入场点与参考不一致")
        return False
    for e in events:
        i = ts2i[e.ts]
        if e.direction == "entry" and not df["close"].iloc[i] > df["high"].iloc[i - window:i].max():
            print(f"    FAIL: entry 非突破 @ {e.ts}")
            return False
        if e.direction == "exit" and not df["close"].iloc[i] < df["low"].iloc[i - window:i].min():
            print(f"    FAIL: exit 非跌破 @ {e.ts}")
            return False
    return True


def main() -> None:
    df_btc = load("BTC/USDT", "1h")
    df_eth = load("ETH/USDT", "1h")
    print(f"样本: BTC {len(df_btc)} 根 / ETH {len(df_eth)} 根 (1h)，预热丢弃前 {WARMUP_BARS} 根\n")

    r1 = parse_rule(RULES_RAW[0])
    ev1 = evaluate(r1, df_btc)
    print(f"[1] ema_cross: {fmt(ev1)}")
    print("    向量化严格比对:", "PASS" if check_cross(df_btc, r1) else "FAIL")

    r2 = parse_rule(RULES_RAW[1])
    ev2 = evaluate(r2, df_btc)
    print(f"[2] ema_cross+RSI过滤+chandelier+cooldown6: {fmt(ev2)}")
    print("    逐事件条件/冷却复核:", "PASS" if check_recheck(df_btc, r2, 6) else "FAIL")

    r3 = parse_rule(RULES_RAW[2])
    ev3 = evaluate(r3, df_eth)
    print(f"[3] breakout_20 (ETH): {fmt(ev3)}")
    print("    参考入场点/事件条件:", "PASS" if check_breakout(df_eth, r3, 20) else "FAIL")

    print("\n[4] 解析器非法输入:")
    for tag, raw in BAD_RAW:
        try:
            parse_rule(raw)
            print(f"    FAIL: {tag} 未报错")
        except ValueError as ex:
            print(f"    PASS: {tag} -> {ex}")


if __name__ == "__main__":
    main()
