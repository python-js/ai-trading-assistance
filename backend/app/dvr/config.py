"""DVR-1.0 配置（独立策略线：15m 环境 + 5m 背离/结构 + 可选 1m 触发）。

口径来源：2026-10-19 确认的方案。成本默认 OKX VIP0：taker 5bps、滑点 2bps；
往返成本 = 2×(taker+slip) = 14bps。R 闸门要求 R ≥ 60bps 且 R ≥ 4×往返成本。
"""
from __future__ import annotations

import hashlib
import json

CFG = {
    "symbol": {"ccxt": "BTC/USDT:USDT", "internal": "BTC/USDT",
               "exchange": "okx", "contract_type": "perp"},
    "venue": {"id": "okx", "tick_size": 0.1, "lot_step": 0.0001,
              "taker_bps": 5.0, "maker_bps": 2.0, "slip_bps": 2.0},
    "account": {"init_equity": 10_000.0, "risk_pct": 0.5,
                "notional_max_pct": 300.0, "daily_loss_pct": 2.0,
                "weekly_loss_pct": 5.0},
    "tf": {"signal": 300, "context": 900},
    "ind": {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi": 14, "atr": 14, "adx": 14, "sma_fast": 20, "sma_slow": 60},
    "swing": {"k": 2, "min_sep": 5, "max_sep": 40, "recent_bars": 20},
    "zone": {"lookback": 40, "poc_bars": 96, "poc_bins": 240,
             "tol_atr": 0.5, "fib_ratios": [0.5, 0.618, 0.786],
             "min_leg_atr": 1.5},
    "env": {"adx_min": 25.0, "adx_lookback": 6, "stretch_atr": 1.5},
    "reversal": {"engulf_body_mult": 1.2, "close_pos": 0.6,
                 "pin_shadow_mult": 2.0, "max_after_confirm": 1},
    "rsi": {"long_max": 35.0, "short_min": 65.0},
    "macd": {"hist_lookback": 3},
    "risk": {"min_r_bps": 60.0, "min_cost_r": 4.0, "r_max_atr15": None},
    "manage": {"tp1_r": 1.0, "tp1_frac": 0.5, "tp2_r": 2.0,
               "time_stop_bars": 12, "time_stop_mfe_r": 0.5,
               "max_hold_bars": 288, "cooldown_bars": 3},
    "backtest": {"wfa_train_months": 6, "wfa_test_months": 2},
}


def round_trip_bps(cfg: dict = CFG) -> float:
    return 2.0 * (cfg["venue"]["taker_bps"] + cfg["venue"]["slip_bps"])


def be_bps(cfg: dict = CFG) -> float:
    return round_trip_bps(cfg)


def cfg_hash(cfg: dict = CFG) -> str:
    raw = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()
    return "blake2:" + hashlib.blake2b(raw, digest_size=8).hexdigest()


def validate_cfg(cfg: dict = CFG) -> None:
    if cfg["risk"]["min_r_bps"] <= round_trip_bps(cfg):
        raise ValueError("min_r_bps 必须大于往返成本")
    if not 0 < cfg["manage"]["tp1_frac"] < 1:
        raise ValueError("tp1_frac 须在 (0,1)")
    if cfg["manage"]["tp1_r"] >= cfg["manage"]["tp2_r"]:
        raise ValueError("tp1_r 须小于 tp2_r")
    if cfg["tf"]["context"] <= cfg["tf"]["signal"]:
        raise ValueError("context 周期须大于 signal")
