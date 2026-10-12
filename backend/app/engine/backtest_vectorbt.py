"""P0-5 研究层回测（vectorbt 1.0.0）：成本模型（taker 费+滑点）、walk-forward
滚动样本外、11 分区报告雏形、固定种子可复现。

研究层为向量化近似：信号产生于 bar 收盘、次 bar 成交（shift 1）；不支持
chandelier/cooldown/部分成交——含状态算子（chandelier、cooldown）的规则由
签发层 rules_dsl.evaluate 逐 bar 复核（双层回测纪律，见方案 §9.2）。

约定：传入 df 的 index 须为"全局 bar 序号"（load_ohlcv 默认 RangeIndex 与
iloc 切片均满足），预热截断按 序号 >= WARMUP_BARS 判定；指标列须已 enrich。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt

from app.engine.rules_dsl import WARMUP_BARS, Rule

ANNUAL_BARS = {"1m": 525600, "1h": 8760, "4h": 2190, "1d": 365}
ENGINE = f"vectorbt {vbt.__version__}"


# ---------- 信号（与 rules_dsl 同源 DSL，向量化实现） ----------

def _vec(node: dict, df: pd.DataFrame) -> pd.Series:
    key, val = next(iter(node.items()))
    if key == "all":
        return pd.concat([_vec(s, df) for s in val], axis=1).all(axis=1)
    if key == "any":
        return pd.concat([_vec(s, df) for s in val], axis=1).any(axis=1)
    if key == "cross_up":
        a, b = val
        return (df[a] > df[b]) & (df[a].shift() <= df[b].shift())
    if key == "cross_down":
        a, b = val
        return (df[a] < df[b]) & (df[a].shift() >= df[b].shift())
    if key == "breakout_up":
        return df["close"] > df["high"].rolling(val).max().shift(1)
    if key == "breakout_down":
        return df["close"] < df["low"].rolling(val).min().shift(1)
    if key == "chandelier_exit":
        raise ValueError("研究层不支持 chandelier_exit（向量化近似）：请走签发层 rules_dsl.evaluate")
    op, v = next(iter(val.items()))
    x = df[key]
    return {"<": x < v, ">": x > v, "<=": x <= v, ">=": x >= v}[op]


def signals_from_rule(rule: Rule, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """入场/出场信号（预热截断 + 次 bar 成交位移，PIT 安全）。"""
    idx = df.index
    entries = _vec(rule.entry, df).fillna(False)
    exits = _vec(rule.exit, df).fillna(False)
    keep = pd.Series(np.asarray(idx) >= WARMUP_BARS, index=idx)
    return ((entries & keep).shift(1, fill_value=False),
            (exits & keep).shift(1, fill_value=False))


def _with_ts(sub: pd.DataFrame, *series: pd.Series) -> list[pd.Series]:
    idx = pd.DatetimeIndex(sub["ts"])
    return [pd.Series(s.to_numpy(), index=idx) for s in series]


# ---------- 组合与指标 ----------

def run_portfolio(close: pd.Series, entries: pd.Series, exits: pd.Series, *,
                  fees: float, slippage_bps: float, init_cash: float = 10_000.0,
                  freq: str = "1h", seed: int = 42) -> vbt.Portfolio:
    return vbt.Portfolio.from_signals(
        close, entries, exits, direction="longonly", size=1.0, size_type="percent",
        fees=fees, slippage=slippage_bps / 10_000, init_cash=init_cash,
        freq=freq, seed=seed)


def _f(x) -> float | None:
    x = float(x)
    return round(x, 6) if math.isfinite(x) else None


def metrics(pf: vbt.Portfolio, close: pd.Series, freq: str) -> dict:
    """收益概述指标集（对齐方案 §9.3 的可计算子集）。"""
    ann = ANNUAL_BARS[freq]
    ret = pf.returns().dropna()
    years = max(len(close) / ann, 1e-9)
    total = float(close.iloc[-1] / close.iloc[0] - 1)
    tr = pf.trades.records_readable
    pnl = tr["PnL"] if len(tr) else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    hold = ((tr["Exit Timestamp"] - tr["Entry Timestamp"]).dt.total_seconds() / 3600
            if len(tr) else pd.Series(dtype=float))
    streak_win = streak_loss = cur_w = cur_l = 0
    for w in pnl > 0:
        cur_w, cur_l = (cur_w + 1, 0) if w else (0, cur_l + 1)
        streak_win, streak_loss = max(streak_win, cur_w), max(streak_loss, cur_l)
    fees_paid = float(tr["Entry Fees"].sum() + tr["Exit Fees"].sum()) if len(tr) else 0.0
    var95 = -ret.quantile(0.05) if len(ret) else float("nan")
    cvar95 = -ret[ret <= ret.quantile(0.05)].mean() if len(ret) else float("nan")
    return {
        "策略收益[%]": _f(pf.total_return() * 100),
        "年化收益[%]": _f(pf.annualized_return() * 100),
        "基准收益[%](买入持有)": _f(total * 100),
        "超额收益[%]": _f((pf.annualized_return() - ((1 + total) ** (1 / years) - 1)) * 100),
        "Sharpe": _f(pf.sharpe_ratio()),
        "Sortino": _f(pf.sortino_ratio()),
        "Calmar": _f(pf.calmar_ratio()),
        "最大回撤[%]": _f(pf.max_drawdown() * 100),
        "波动率[%]": _f(ret.std() * math.sqrt(ann) * 100),
        "下行波动率[%]": _f(ret[ret < 0].std() * math.sqrt(ann) * 100),
        "VaR95[%]": _f(var95 * 100),
        "CVaR95[%]": _f(cvar95 * 100),
        "胜率[%]": _f(pf.trades.win_rate() * 100) if len(tr) else None,
        "盈亏比": _f(pf.trades.profit_factor()),
        "交易笔数": int(len(tr)),
        "平均持仓小时": _f(hold.mean()) if len(hold) else None,
        "最大连盈": streak_win,
        "最大连亏": streak_loss,
        "平均盈利": _f(wins.mean()) if len(wins) else None,
        "平均亏损": _f(losses.mean()) if len(losses) else None,
        "手续费总额": _f(fees_paid),
        "成本占毛利[%]": _f(fees_paid / wins.sum() * 100) if len(wins) and wins.sum() > 0 else None,
    }


def _sharpe(ret: pd.Series, tf: str) -> float:
    if len(ret) < 2 or ret.std() == 0:
        return 0.0
    return float(ret.mean() / ret.std() * math.sqrt(ANNUAL_BARS[tf]))


# ---------- walk-forward ----------

def walk_forward(df: pd.DataFrame, grid: list[dict], build_rule, *, tf: str,
                 train_bars: int, test_bars: int, step_bars: int,
                 fees: float, slippage_bps: float, seed: int = 42) -> dict:
    """滚动训练/样本外：训练窗内按 IS Sharpe 择优，紧邻样本外窗验证，滚动前推。"""
    folds, rows = [], []
    start = 0
    while start + train_bars + test_bars <= len(df):
        sub = df.iloc[start:start + train_bars + test_bars]
        best = None
        for params in grid:
            rule = build_rule(params)
            e, x = signals_from_rule(rule, sub)
            close, e, x = _with_ts(sub, sub["close"], e, x)
            pf = run_portfolio(close, e.astype(bool), x.astype(bool),
                               fees=fees, slippage_bps=slippage_bps, freq=tf, seed=seed)
            v = pf.value()
            v_is, v_oos = v.iloc[:train_bars], v.iloc[train_bars:]
            is_sharpe = _sharpe(v_is.pct_change().dropna(), tf)
            oos_sharpe = _sharpe(v_oos.pct_change().dropna(), tf)
            rows.append({"fold": len(folds), **params,
                         "is_sharpe": _f(is_sharpe),
                         "is_ret[%]": _f((v_is.iloc[-1] / v_is.iloc[0] - 1) * 100),
                         "oos_sharpe": _f(oos_sharpe),
                         "oos_ret[%]": _f((v_oos.iloc[-1] / v_oos.iloc[0] - 1) * 100)})
            if best is None or is_sharpe > best["is_sharpe"]:
                best = {"params": dict(params), "is_sharpe": is_sharpe, "v_oos": v_oos}
        folds.append({"fold": len(folds),
                      "train": [str(sub["ts"].iloc[0]), str(sub["ts"].iloc[train_bars - 1])],
                      "test": [str(sub["ts"].iloc[train_bars]), str(sub["ts"].iloc[-1])],
                      "params": best["params"], "is_sharpe": _f(best["is_sharpe"]),
                      "oos_sharpe": _f(_sharpe(best["v_oos"].pct_change().dropna(), tf)),
                      "oos_ret[%]": _f((best["v_oos"].iloc[-1] / best["v_oos"].iloc[0] - 1) * 100),
                      "oos_curve": best["v_oos"] / best["v_oos"].iloc[0]})
        start += step_bars
    curve = pd.concat([f.pop("oos_curve") for f in folds]) if folds else pd.Series(dtype=float)
    is_s = [f["is_sharpe"] for f in folds if f["is_sharpe"] is not None]
    oos_s = [f["oos_sharpe"] for f in folds if f["oos_sharpe"] is not None]
    return {"folds": folds, "grid": pd.DataFrame(rows), "oos_curve": curve,
            "mean_is_sharpe": _f(np.mean(is_s)) if is_s else None,
            "mean_oos_sharpe": _f(np.mean(oos_s)) if oos_s else None,
            "decay_ratio(样本内外衰减)": _f(np.mean(oos_s) / np.mean(is_s))
            if is_s and abs(np.mean(is_s)) > 1e-12 else None}


# ---------- 11 分区报告 ----------

def write_report(out_dir: Path, meta: dict, m: dict, pf: vbt.Portfolio,
                 wf: dict, rule: Rule) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tr = pf.trades.records_readable
    orders = pf.orders.records_readable
    value = pf.value()
    daily = value.resample("1D").last().dropna()

    tr.to_csv(out_dir / "trades.csv", index=False, encoding="utf-8")
    orders.to_csv(out_dir / "orders.csv", index=False, encoding="utf-8")
    daily.rename("equity").to_frame().assign(
        daily_return=daily.pct_change()).to_csv(out_dir / "equity_daily.csv", encoding="utf-8")
    wf["grid"].to_csv(out_dir / "grid.csv", index=False, encoding="utf-8")
    if len(wf["oos_curve"]):
        wf["oos_curve"].rename("oos_equity_norm").to_csv(
            out_dir / "oos_curve.csv", encoding="utf-8")
    (out_dir / "rule_snapshot.json").write_text(json.dumps(
        {"rule": rule.__dict__, "engine": ENGINE, "params": meta["params"],
         "seed": meta["seed"], "data_version": meta["data_version"]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    metrics_json = {**meta, "metrics": m,
                    "walkforward": {k: v for k, v in wf.items()
                                    if k not in ("grid", "oos_curve")}}
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics_json, ensure_ascii=False, indent=2), encoding="utf-8")

    def tbl(d: pd.DataFrame) -> str:
        return "```\n" + d.to_string(index=False) + "\n```"

    folds_tbl = pd.DataFrame([{k: v for k, v in f.items() if k != "oos_curve"}
                              for f in wf["folds"]])
    monthly = (tr.assign(月=pd.to_datetime(tr["Exit Timestamp"]).dt.strftime("%Y-%m"))
               .groupby("月")["PnL"].agg(["sum", "count"]).reset_index()) if len(tr) else pd.DataFrame()
    wf_cfg = meta.get("wf") or {}
    section = [
        "# 回测报告（P0 雏形）\n",
        f"- 规则: `{rule.name}` | 标的: {meta['symbol']} | 周期: {meta['timeframe']} "
        f"| 区间: {meta['data_version']['first_ts']} ~ {meta['data_version']['last_ts']}",
        f"- 引擎: {ENGINE} | 种子: {meta['seed']} "
        f"| 成本: taker {meta['fees']} + 滑点 {meta['slippage_bps']}bps",
        "- 研究层为向量化近似（信号次 bar 成交）；签发层（rules_dsl）逐 bar 复核\n",
        "## 1. 收益概述\n",
        tbl(pd.DataFrame({"值": m}).reset_index().rename(columns={"index": "指标"})),
        "\n## 2. 交易详情\n", tbl(tr) if len(tr) else "无交易",
        "\n## 3. 每日持仓&收益（日末净值）\n",
        tbl(daily.rename("equity").reset_index()),
        "\n## 4. 日志输出\n", tbl(orders) if len(orders) else "无订单",
        "\n## 5. 性能分析（walk-forward 优化验证）\n",
        f"- 训练 {wf_cfg.get('train_bars')} 根 / 样本外 {wf_cfg.get('test_bars')} 根 "
        f"/ 步进 {wf_cfg.get('step_bars')} 根",
        f"- 样本内平均 Sharpe {wf['mean_is_sharpe']} → 样本外平均 Sharpe {wf['mean_oos_sharpe']}"
        f"（衰减比 {wf['decay_ratio(样本内外衰减)']}）\n",
        tbl(folds_tbl) if len(folds_tbl) else "",
        "\n## 6. 成本分解\n",
        f"- 手续费总额: {m['手续费总额']} | 成本占毛利: {m['成本占毛利[%]']}%",
        f"- 滑点假设: {meta['slippage_bps']} bps（按成交额近似；逐档深度估算留签发层/P1）",
        "\n## 7. 偏差审计\n",
        f"- 预热截断: 前 {WARMUP_BARS} 根不产生信号（P0-3 指标暂态硬约束）PASS",
        "- 执行位移: 信号次 bar 成交（shift 1）PASS",
        f"- 数据覆盖: {meta['data_version']['rows']} 根，{meta['data_version']['first_ts']} ~ "
        f"{meta['data_version']['last_ts']}，缺口已在采集层校验 PASS",
        "\n## 8. 收益归因（按退出月份）\n", tbl(monthly) if len(monthly) else "无交易",
        "\n## 9. 执行分析\n",
        "- 完成率 100%（研究层不做拒单/部分成交建模，留签发层）",
        f"- 平均持仓 {m['平均持仓小时']} 小时 | 交易 {m['交易笔数']} 笔",
        "\n## 10. 策略代码（快照，可复现凭证）\n",
        "```json\n" + json.dumps(rule.__dict__, ensure_ascii=False, indent=2) + "\n```",
        "\n## 11. 导出\n",
        "- trades.csv / orders.csv / equity_daily.csv / grid.csv / oos_curve.csv "
        "/ metrics.json / rule_snapshot.json",
        "- HTML/Excel/PDF 导出留 P1\n",
    ]
    (out_dir / "report.md").write_text("\n".join(section), encoding="utf-8")
    return metrics_json


# ---------- 编排入口 ----------

def run_full(df: pd.DataFrame, rule: Rule, *, params: dict, symbol: str, timeframe: str,
             fees: float, slippage_bps: float, out_dir: Path, seed: int = 42,
             wf_cfg: dict | None = None, grid: list[dict] | None = None,
             build_rule=None) -> dict:
    e, x = signals_from_rule(rule, df)
    close, e, x = _with_ts(df, df["close"], e, x)
    pf = run_portfolio(close, e.astype(bool), x.astype(bool),
                       fees=fees, slippage_bps=slippage_bps, freq=timeframe, seed=seed)
    m = metrics(pf, close, timeframe)
    if grid and wf_cfg:
        wf = walk_forward(df, grid, build_rule, tf=timeframe, fees=fees,
                          slippage_bps=slippage_bps, seed=seed, **wf_cfg)
    else:
        wf = {"folds": [], "grid": pd.DataFrame(), "oos_curve": pd.Series(dtype=float),
              "mean_is_sharpe": None, "mean_oos_sharpe": None,
              "decay_ratio(样本内外衰减)": None}
    meta = {"run_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol, "timeframe": timeframe, "engine": ENGINE, "seed": seed,
            "fees": fees, "slippage_bps": slippage_bps, "params": params,
            "wf": wf_cfg or {},
            "data_version": {"rows": len(df), "first_ts": str(df["ts"].iloc[0]),
                             "last_ts": str(df["ts"].iloc[-1])}}
    return write_report(out_dir, meta, m, pf, wf, rule)
