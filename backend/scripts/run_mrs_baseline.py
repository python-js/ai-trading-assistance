"""M6/§9.3 随机基准运行器（对照组 bootstrap）。

v1 口径登记：
  1) 入场可行域（"同闸门"） = 生产 funnel 中 stop_at ∈ {NO_TRIGGER, VOL, PLAN, OK, NO_FILL}
     的 bar —— 即策略全部前置闸门（SESSION/EVENT/REGIME/RESON/ZONE/COOLDOWN/SPREAD、
     HALT/NO_DATA/TRADES_CAP 除外）已通过的 bar 池；决策内容（rank/共振/触发/区/房检）
     本身被"随机入场"替换，这是对照设计定义。方向 50/50 随机。
  2) 每轨迹独立真实 Engine：24h 指标预热（决策路径经 _entry_phase 阻断，预热期不产生
     真实信号）+ 注入 Bundle 后走生产 pending→fill→manage→exit 全路径，
     入场时间随机抽自有放回采样（seed 固定默认 20261014，输出记录）。
  3) R 定界 = 1.2×ATR15（结构项被随机化替代，取 §6.2 夹紧带 [0.8,1.6] 中点）；
     COST 闸门同式（R < max(4rt, 120×tick) → 弃抽重来，计数）。触发价 = 信号 bar 收盘
     （对齐 tick，不利方向）；止损/止盈/seg1/be 同 plan.py 公式（r=1.2×ATR15, tp 取 2.2R 支）。
  4) qty = floor_lot(min(equity×0.35%/R, equity×250%/e))，equity = 初始权益（定额，不随轨迹盈亏）。
  5) 判定（§9.3）：|Δ期望| < 0.1R 或 95% CI 混零 → 无边际信息。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/run_mrs_baseline.py \\
      --funnel <maker_run>/mrs_funnel.jsonl --n 2000 --set venue.taker_bps=2 \\
      --strategy-stats <maker_full>.json --out baseline.md --out-stats baseline.json
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import pathlib
import random
import sys
import time
from collections import Counter

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))       # scripts/
sys.path.insert(0, str(_HERE.parents[1]))   # backend/

from app.core.config import settings  # noqa: E402
from app.mrs.config import CFG, cfg_hash, validate_cfg  # noqa: E402
from app.mrs.engine import Engine  # noqa: E402
from app.mrs.ledger import Ledger  # noqa: E402
from app.mrs.plan import Bundle, _align, _floor_lot  # noqa: E402
from app.mrs.types import Bar  # noqa: E402
from app.models import db  # noqa: E402

POOL_LABELS = {"NO_TRIGGER", "VOL", "PLAN", "OK", "NO_FILL"}
WARMUP_BARS = 1440      # 24h 指标预热（RMA(13/14)^96≈0.3% 种子残差，atr5/atr15/ma5m 充分）
MAX_HOLD_BARS = 620     # 兜底（flatten_by_utc 22:55 保证 ≤~10h）


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MRS §9.3 随机基准（bootstrap）")
    ap.add_argument("--funnel", required=True, help="生产 funnel JSONL（bar 池来源）")
    ap.add_argument("--start", help="窗口起（UTC YYYY-MM-DD；默认随 funnel 全部）")
    ap.add_argument("--end", help="窗口止")
    ap.add_argument("--n", type=int, default=2000, help="轨迹数（≥2000，§9.3）")
    ap.add_argument("--seed", type=int, default=20261014)
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    ap.add_argument("--strategy-stats", help="策略侧 stats JSON（maker 全量）用于对照判定")
    ap.add_argument("--out", required=True, help="markdown 报表")
    ap.add_argument("--out-stats", help="轨迹明细 JSON")
    return ap.parse_args(argv)


def _apply_sets(cfg: dict, sets: list[str]) -> None:
    for item in sets:
        path, _sep, raw = item.partition("=")
        keys = path.strip().split(".")
        node = cfg
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                sys.exit(f"--set 路径不存在: {path}")
            node = node[k]
        try:
            node[keys[-1]] = json.loads(raw)
        except json.JSONDecodeError:
            node[keys[-1]] = raw
        print(f"[覆盖] {path}: → {node[keys[-1]]!r}")


def load_pool(path: str, t_lo: int, t_hi: int) -> list[int]:
    pool = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)  # 容忍读取运行中回测的截断尾行
            except json.JSONDecodeError:
                continue
            if r["stop_at"] in POOL_LABELS and t_lo <= r["ts"] <= t_hi:
                pool.append(r["ts"] - 60)  # funnel ts = bar 收盘；入场信号 bar = ts-60
    return pool


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = copy.deepcopy(CFG)
    _apply_sets(cfg, args.set)
    validate_cfg(cfg)
    sym, ex_, ct = (cfg["symbol"]["internal"], cfg["symbol"]["exchange"],
                    cfg["symbol"]["contract_type"])
    tick = cfg["venue"]["tick_size"]
    lot = cfg["venue"]["lot_step"]
    fee_bps, slip_bps = cfg["venue"]["taker_bps"], cfg["venue"]["slip_base_bps"]
    init_eq = cfg["account"]["init_equity"]

    conn = db.connect(settings.pg_dsn)
    try:
        df = db.load_ohlcv(conn, sym, ex_, ct, "1m")
        with conn.cursor() as cur:
            cur.execute("""SELECT ts, rate FROM funding_rate
                           WHERE symbol=%s AND exchange=%s AND rate IS NOT NULL""",
                        (sym, ex_))
            fmap = {int(t.timestamp()): float(r) for t, r in cur.fetchall()}
    finally:
        conn.close()
    ts_s = (df["ts"].astype("int64") // 1_000_000_000).tolist()
    bars = [Bar(int(t), float(o), float(h), float(l), float(c), float(v))
            for t, o, h, l, c, v in zip(ts_s, df["open"], df["high"], df["low"],
                                        df["close"], df["volume"])]
    idx = {t: i for i, t in enumerate(ts_s)}
    print(f"[数据] {len(bars)} 根 | {ts_s[0]} → {ts_s[-1]}")

    # ATR15 序列（与引擎 _feed_tf 15m 合成 + ATR 同源，start 同 df 首根）
    from app.mrs.indicators import ATR  # noqa: E402
    atr15_arr = [None] * len(bars)
    atr = ATR(cfg["atr_periods"]["m15"])
    agg = None
    for i, b in enumerate(bars):
        w0 = b.ts - (b.ts % 900)
        if agg is None or agg["ts"] != w0:
            agg = {"ts": w0, "h": b.high, "l": b.low, "c": b.close}
        else:
            agg["h"] = max(agg["h"], b.high)
            agg["l"] = min(agg["l"], b.low)
            agg["c"] = b.close
        if b.ts % 900 == 840:
            atr.update(agg["h"], agg["l"], agg["c"])
        atr15_arr[i] = atr.v
    print("[预计算] ATR15 序列就绪")

    # 下界 +60：funnel ts=收盘 → 入场 bar ts；保证 i ≥ WARMUP_BARS（预热切片不回绕）
    pool = load_pool(args.funnel, ts_s[WARMUP_BARS] + 60, ts_s[-1] - MAX_HOLD_BARS * 60)
    print(f"[池] 闸门已过 bar {len(pool)} 个（funnel 标签 {sorted(POOL_LABELS)}）")

    rng = random.Random(args.seed)
    params = cfg["risk"]
    mg = cfg["manage"]
    stats_traj: list[dict] = []
    costs = Counter()
    t0 = time.time()
    draws = 0
    while len(stats_traj) < args.n and draws < args.n * 20:
        draws += 1
        ts = rng.choice(pool)
        i = idx[ts]
        atr15 = atr15_arr[i]
        if atr15 is None:
            costs["NO_ATR15"] += 1
            continue
        d = 1 if rng.random() < 0.5 else -1
        e = _align(bars[i].close, tick, d)
        r = 1.2 * atr15
        rt = 2 * e * (fee_bps + slip_bps) / 1e4
        if r < max(4 * rt, params["min_r_ticks_mult"] * tick):
            costs["COST"] += 1
            continue
        stop = _align(e - d * r, tick, d)
        tp = _align(e + d * mg["tp_r_base"] * r, tick, d)
        seg1 = _align(e + d * mg["seg1_r"] * r, tick, d)
        be = _align(e + d * max(2 * rt, mg["be_lock_min_r"] * r), tick, d)
        qty = _floor_lot(min(init_eq * params["risk_pct"] / 100 / r,
                             init_eq * params["notional_max_pct"] / 100 / e), lot)
        if qty < lot:
            costs["QTY_MIN"] += 1
            continue
        sig = f"base{draws}"
        bundle = Bundle(e_sig=sig, now_ts=ts + 60, dir="LONG" if d > 0 else "SHORT",
                        zone_id="BASE", trigger=bars[i].close, stop=stop, tp=tp, qty=qty,
                        R=r, seg1_px=seg1, be_px_target=be, leverage=3,
                        fee_bps=fee_bps, slip_bps=slip_bps)
        eng = Engine(cfg=cfg, ledger=Ledger(), equity=init_eq, funding_fn=fmap.get)
        # v1 口径 2：基准轨迹仅测"随机入场 + 生产管理"，决策路径（含预热期）全程阻断
        eng._entry_phase = lambda bar, now: ("BUSY", None)
        for b in bars[i - WARMUP_BARS: i + 1]:
            eng.on_1m_close(b)
        eng.broker.open(bundle, ts=ts)
        eng.pending_sig_id = sig
        eng.acct.state = "ARMED"
        n_trades0 = len(eng.ledger.trades)
        exit_rec = None
        censored = False
        for b in bars[i + 1: i + 1 + MAX_HOLD_BARS]:
            eng.on_1m_close(b)
            if eng.pos is None and eng.pending_sig_id is None:
                break
        else:
            if eng.pos is not None:  # 兜底截断（理论不可达）
                f = eng.broker.market_close(eng.broker.get(sig), bars[i + MAX_HOLD_BARS], "CUTOFF")
                eng._close_position("CUTOFF", f, bars[i + MAX_HOLD_BARS])
                censored = True
        tr = eng.ledger.trades[n_trades0:]
        init_rec = next((t for t in tr if t["event"] == "init"), None)
        if init_rec is None:
            costs["NO_FILL"] += 1
            continue
        exit_rec = next((t for t in tr if t["event"] == "exit"), None)
        if exit_rec is None:
            costs["INIT_NO_EXIT"] += 1
            continue
        stats_traj.append({
            "ts": ts, "dir": bundle.dir, "R": r, "atr15": atr15, "qty": qty,
            "pnl_R": exit_rec["pnl_R"], "reason": exit_rec["reason"],
            "bars_held": exit_rec["bars_held"], "mfe_R": exit_rec["mfe_R"],
            "mae_R": exit_rec["mae_R"], "funding_paid": exit_rec["funding_paid"],
            "censored": censored,
        })
        if len(stats_traj) % 200 == 0:
            el = time.time() - t0
            print(f"  [{len(stats_traj)}/{args.n}] {el:.0f}s 弃抽={dict(costs)}", flush=True)

    rs = [t["pnl_R"] for t in stats_traj]
    n = len(rs)
    m = sum(rs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1))
    ci = 1.96 * sd / math.sqrt(n)
    pf_gw = sum(x for x in rs if x > 0)
    pf_gl = -sum(x for x in rs if x < 0)
    pf = pf_gw / pf_gl if pf_gl > 0 else float("inf")
    print(f"[基准] n={n} 期望 {m:+.3f}R ± {ci:.3f} | 胜率 "
          f"{sum(x > 0 for x in rs) / n * 100:.1f}% | PF {pf:.2f} | 弃抽 {dict(costs)}")

    L = ["# MRS §9.3 随机基准对照（bootstrap）", "",
         f"- 轨迹 n={n}（seed={args.seed}，有放回；入场池=funnel 全闸门已过 bar，"
         f"范围 {ts_s[WARMUP_BARS]} → {ts_s[-1] - MAX_HOLD_BARS * 60}）",
         f"- 参数：R=1.2×ATR15、同止损/止盈/seg1/be 管理（真实 Engine 24h 预热 + 生产撮合路径）"
         f" | 费用 fee/slip={fee_bps}/{slip_bps}bp | cfg_hash={cfg_hash(cfg)}",
         f"- 弃抽统计：{dict(costs)}（COST=入场可行域外；NO_FILL=次 2 根未触发）", "",
         "## 基准统计", "",
         f"- 期望 {m:+.3f}R ± {ci:.3f}（95% CI） | 胜率 {sum(x > 0 for x in rs) / n * 100:.1f}% "
         f"| PF {pf:.2f} | ΣR={sum(rs):+.2f}",
         f"- 平均持有 {sum(t['bars_held'] for t in stats_traj) / n:.1f} bar | "
         f"MFE 均值 {sum(t['mfe_R'] for t in stats_traj) / n:+.2f} | "
         f"MAE 均值 {sum(t['mae_R'] for t in stats_traj) / n:+.2f}", ""]
    reasons = Counter(t["reason"] for t in stats_traj)
    L += ["## 基准离场构成", "",
          "| reason | n | 期望R |", "|---|---|---|"]
    for k, v in reasons.most_common():
        sub = [t["pnl_R"] for t in stats_traj if t["reason"] == k]
        L.append(f"| {k} | {v} | {sum(sub) / len(sub):+.3f} |")
    L.append("")

    if args.strategy_stats:
        st = json.loads(pathlib.Path(args.strategy_stats).read_text(encoding="utf-8"))
        exs = [t for t in st["trades"] if t["event"] == "exit"]
        rs_s = [t["pnl_R"] for t in exs]
        if rs_s:
            ns = len(rs_s)
            ms = sum(rs_s) / ns
            sds = math.sqrt(sum((x - ms) ** 2 for x in rs_s) / (ns - 1)) if ns > 1 else 0.0
            diff = ms - m
            se = math.sqrt((sds ** 2 / ns if ns > 1 else 0.0) + (sd ** 2 / n))
            lo_c, hi_c = diff - 1.96 * se, diff + 1.96 * se
            no_edge = abs(diff) < 0.1 or (lo_c <= 0 <= hi_c)
            L += ["## 策略 vs 基准判定（§9.3）", "",
                  f"- 策略：n={ns} 期望 {ms:+.3f}R（std {sds:.3f}）",
                  f"- 基准：n={n} 期望 {m:+.3f}R（std {sd:.3f}）",
                  f"- 期望差 Δ={diff:+.3f}R | 95% CI [{lo_c:+.3f}, {hi_c:+.3f}]",
                  f"- 判定：{'**无边际信息（|Δ|<0.1R 或 CI 混零）→ 停止实盘化**' if no_edge else '边际超出基准（需样本量复核）'}",
                  "", "- 注：策略侧 n 小时检验低效力；判定按 v1 口径（正态近似双样本差）。", ""]
        else:
            L += ["## 策略 vs 基准判定（§9.3）", "",
                  "- 策略侧 0 笔成交（成本可行性域外），无法计算差值——基准统计独立成立。", ""]
    pathlib.Path(args.out).write_text("\n".join(L), encoding="utf-8")
    if args.out_stats:
        pathlib.Path(args.out_stats).write_text(
            json.dumps({"seed": args.seed, "n": n, "mean_R": m, "ci95": ci,
                        "trajectories": stats_traj, "costs": dict(costs)},
                       ensure_ascii=False), encoding="utf-8")
    print(f"[报表] {args.out}")


if __name__ == "__main__":
    main()
