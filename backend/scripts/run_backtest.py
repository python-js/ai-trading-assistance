"""P0-5 运行入口：BTC/USDT 1h 均线交叉策略族——全区间回测报告 + walk-forward 优化验证 + 复现性校验。

用法：.venv/Scripts/python.exe backend/scripts/run_backtest.py
"""
import hashlib
import json
import pathlib
import sys
import time
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.tz import CST
from app.engine import backtest_vectorbt as bt
from app.engine.indicators import enrich
from app.engine.rules_dsl import parse_rule
from app.models import db

SYMBOL, TF = "BTC/USDT", "1h"
SYMBOLS = ("BTC/USDT", "ETH/USDT", "ONDO/USDT")
FASTS, SLOWS = (10, 20, 30), (50, 60, 80)
FEES, SLIP = 0.0005, 5.0  # taker 0.05% + 滑点 5bps
MAIN = {"fast": 20, "slow": 60}
WF_CFG = {"train_bars": 720, "test_bars": 168, "step_bars": 168}  # 30d 训练 / 7d 样本外 / 步进 7d
GRID = [{"fast": f, "slow": s} for f in FASTS for s in SLOWS]


def build_rule(p: dict):
    f, s = p["fast"], p["slow"]
    return parse_rule({
        "name": f"ema{f}_{s}", "symbol": SYMBOL, "timeframe": TF,
        "entry": {"cross_up": [f"ema{f}", f"ema{s}"]},
        "exit": {"cross_down": [f"ema{f}", f"ema{s}"]}})


def load(symbol: str = SYMBOL):
    conn = db.connect(settings.pg_dsn)
    df = db.load_ohlcv(conn, symbol, "okx", "spot", TF)
    conn.close()
    return enrich(df, emas=(10, 20, 30, 50, 60, 80))


def quick_metrics(df, symbol: str) -> dict:
    """单标的主规则快照（不写报告），用于多标的对照（方案 §9.2 纪律2）。"""
    e, x = bt.signals_from_rule(build_rule(MAIN), df)
    close, e, x = bt._with_ts(df, df["close"], e, x)
    pf = bt.run_portfolio(close, e.astype(bool), x.astype(bool),
                          fees=FEES, slippage_bps=SLIP, freq=TF, seed=42)
    return bt.metrics(pf, close, TF)


def run(out_dir: pathlib.Path, df):
    return bt.run_full(df, build_rule(MAIN), params=MAIN, symbol=SYMBOL, timeframe=TF,
                       fees=FEES, slippage_bps=SLIP, out_dir=out_dir, seed=42,
                       wf_cfg=WF_CFG, grid=GRID, build_rule=build_rule)


def stable_hash(m: dict) -> str:
    payload = {"metrics": m["metrics"], "walkforward": m["walkforward"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def main() -> None:
    df = load()
    print(f"样本: {SYMBOL} {TF} {len(df)} 根 | {df.ts.iloc[0]} ~ {df.ts.iloc[-1]}")
    print(f"成本: taker {FEES*100:.3f}% + 滑点 {SLIP}bps | 主规则 ema{MAIN['fast']}_{MAIN['slow']}")
    print(f"walk-forward: 训练{WF_CFG['train_bars']}根 / 样本外{WF_CFG['test_bars']}根 / 步进{WF_CFG['step_bars']}根\n")

    stamp = datetime.now(CST).strftime("%Y%m%dT%H%M%S")
    out1 = pathlib.Path(f"data/backtest_reports/ema_{MAIN['fast']}{MAIN['slow']}_{TF}_{stamp}")
    t0 = time.time()
    m1 = run(out1, df)
    print(f"[run1] {time.time()-t0:.1f}s")

    for k in ("策略收益[%]", "年化收益[%]", "基准收益[%](买入持有)", "Sharpe", "Sortino",
              "最大回撤[%]", "胜率[%]", "盈亏比", "交易笔数", "成本占毛利[%]"):
        print(f"  {k:22} {m1['metrics'][k]}")
    print(f"  年化收益[%] vs 基准: {m1['metrics']['年化收益[%]']} vs {m1['metrics']['基准收益[%](买入持有)']}")

    wf = m1["walkforward"]
    print("\nwalk-forward 折叠:")
    for f in wf["folds"]:
        print(f"  fold{f['fold']} 训练{f['train'][0][:16]}~{f['train'][1][:16]} "
              f"测试{f['test'][0][:16]}~{f['test'][1][:16]} | 选中 fast={f['params']['fast']},slow={f['params']['slow']} "
              f"| IS Sharpe {f['is_sharpe']} → OOS Sharpe {f['oos_sharpe']} | OOS 收益 {f['oos_ret[%]']}%")
    print(f"  样本内均值 {wf['mean_is_sharpe']} → 样本外均值 {wf['mean_oos_sharpe']} | 衰减比 {wf['decay_ratio(样本内外衰减)']}")

    out2 = out1.with_name(out1.name + "_repro")
    m2 = run(out2, df)
    h1, h2 = stable_hash(m1), stable_hash(m2)
    ok = h1 == h2
    print(f"\n复现性（固定种子，两次运行 metrics+walkforward 哈希）: {'PASS' if ok else 'FAIL'} ({h1[:16]})")

    print("\n多标的对照（同主规则 ema20_60，含成本）:")
    print(f"  {'标的':10} {'策略收益%':>9} {'基准收益%':>9} {'Sharpe':>8} {'最大回撤%':>9} {'胜率%':>7} {'盈亏比':>7} {'笔数':>5}")
    for sym in SYMBOLS:
        mm = quick_metrics(df if sym == SYMBOL else load(sym), sym)
        print(f"  {sym:10} {mm['策略收益[%]']:>9} {mm['基准收益[%](买入持有)']:>9} "
              f"{mm['Sharpe']:>8} {mm['最大回撤[%]']:>9} {mm['胜率[%]']:>7} "
              f"{mm['盈亏比']:>7} {mm['交易笔数']:>5}")

    print(f"\n报告: {out1}/report.md  （复现对照: {out2.name}）")


if __name__ == "__main__":
    main()
