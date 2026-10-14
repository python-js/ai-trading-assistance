"""MRS-1.0 配置（规格书 §2）：单品种 OKX BTC-USDT-SWAP 永续。

- 键名与规格书 §2 一一对应；时间/周期键统一折算为秒（§0.2：时间 UTC、单位秒）。
- venue 值为 OKX 实测映射（2026-10-14 探测）：tick 0.1、lot 0.01 张 × 面值 0.01 BTC/张 = 0.0001 BTC。
- validate_cfg() 启动时强制执行，任一失败拒绝启动（§2 末尾规则）；verify_venue() 为 §11 联网核对。
"""
from __future__ import annotations

import hashlib
import json

CFG = {
    "symbol": {"ccxt": "BTC/USDT:USDT", "internal": "BTC/USDT",
               "exchange": "okx", "contract_type": "perp"},
    "venue": {"id": "okx", "tick_size": 0.1, "lot_step": 0.0001, "contract_size": 0.01,
              "maker_bps": 2.0, "taker_bps": 5.0, "slip_base_bps": 2.0},
    "funding_hours_utc": [0, 8, 16],
    "tf": {"base": 60, "state": [300, 900, 1800], "context": 900},
    "ma_periods": {"fast": 5, "slow": 20, "long": 60},
    "atr_periods": {"m1": 14, "m5": 14, "m15": 14},
    "adx_period": 14,
    "swings": {"k_confirm": 2, "lookback_tf": {300: 4, 900: 3, 1800: 2}},
    "regime": {"adx_min": 20.0, "adx_full": 25.0, "half_size_factor": 0.5},
    "resonance": {"w30": 2, "w15": 1, "w5": 1, "req_score": 3, "veto_rank": 2},
    "zones": {"cluster_atr_mult": 0.5,
              "width_min_atr": 0.35, "width_atr_mult": 0.35,
              "width_std_mult": 0.9, "width_cap_atr": 0.9,
              "near_atr": 0.35, "top_n": 3,
              "touch_weight": 0.4, "rebound_weight": 0.4, "multi_weight": 0.2,
              "poc": {"bins": 240}, "round_steps": [10, 50, 100, 250, 500]},
    "entry": {"atr1_period": 14, "t1_shadow_atr": 1.0, "t2_max_bars": 3,
              "t2_close_pos": 0.6, "vol_pctile_n": 60, "vol_pctile_min": 0.60,
              "spread_max": 6.0, "room_rr_min": 1.5, "max_zone_touches": 2},
    "risk": {"risk_pct": 0.35, "atr_bounds_mult": [0.8, 1.6], "min_r_ticks_mult": 120,
             "notional_max_pct": 250, "liq_buffer_r": 3.0, "leverage_default": 3,
             "cooldown_min_bars": 25, "zone_daily_reentry": 2},
    "manage": {"seg1_r": 1.2, "seg1_frac": 0.5, "be_trigger_r": 1.5, "be_lock_min_r": 0.05,
               "tp_r_base": 2.2, "trail_atr_mult": 1.2, "trail_ma_tf": 300, "trail_ma_n": 20,
               "time_stop_bars": 60, "time_stop_mfe_r": 0.8,
               "session_windows_utc": ["13:00-17:00"], "event_blackout_min": 30,
               "no_new_entry_after_loss": 0,
               "kill": {"daily_loss_r": 3.0, "consec_stops": 3,
                        "error_stop_fail": 2, "flatten_by_utc": "22:55"}},
    "recon": {"cron": "*/30 * * * *", "heartbeat_sec": 60},
    # 规格书 §2 之外的本地扩展：模拟账户初始净值（§6.6 算例口径）。
    "account": {"init_equity": 50_000.0},
}

_TF_LABEL = {60: "1m", 300: "5m", 900: "15m", 1800: "30m"}


def tf_label(sec: int) -> str:
    return _TF_LABEL.get(sec, f"{sec}s")


def validate_cfg(cfg: dict = CFG) -> None:
    """§2 启动规则（静态四项）；任一失败拒绝启动。"""
    r = cfg["resonance"]
    if r["req_score"] > r["w30"] * 2 + r["w15"] * 2:
        raise ValueError("req_score 超过权重上限（永不开闸）")
    if cfg["entry"]["room_rr_min"] > cfg["manage"]["tp_r_base"]:
        raise ValueError("room_rr_min > tp_r_base（空间预检必死）")
    lo, hi = cfg["risk"]["atr_bounds_mult"]
    if not lo < hi:
        raise ValueError("atr_bounds_mult 下限须 < 上限")
    if cfg["manage"]["seg1_r"] >= cfg["manage"]["tp_r_base"]:
        raise ValueError("seg1_r 须 < tp_r_base")


def verify_venue(adapter, cfg: dict = CFG) -> None:
    """§11：交易所实际 tick/lot 与配置核对，不符拒绝启动。"""
    m = adapter.ex.markets.get(cfg["symbol"]["ccxt"])
    if m is None:
        raise ValueError(f"市场不存在: {cfg['symbol']['ccxt']}")
    tick = float(m["precision"]["price"])
    lot_btc = float(m["precision"]["amount"]) * float(m.get("contractSize") or 1)
    v = cfg["venue"]
    if abs(tick - v["tick_size"]) > 1e-12:
        raise ValueError(f"tick_size 不符: 配置 {v['tick_size']} vs 交易所 {tick}")
    if abs(lot_btc - v["lot_step"]) > 1e-12:
        raise ValueError(f"lot_step 不符: 配置 {v['lot_step']} vs 交易所 {lot_btc}")


def cfg_hash(cfg: dict = CFG) -> str:
    """§8 日志 cfg_hash：配置指纹。"""
    raw = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()
    return "blake2:" + hashlib.blake2b(raw, digest_size=8).hexdigest()
