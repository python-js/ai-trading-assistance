"""M6/§9.5 报表生成器：harness --dump-stats 的 JSON → markdown 硬性报表。

v1 口径登记（报表层）：
  1) 期望 CI = 95% 正态近似 1.96·s/√n（s 为样本标准差；n<30 表格处附注低样本）。
  2) maxDD：R 口径 = 逐笔 pnl_R 累加对前高；% 口径 = 初始权益 + 逐笔 pnl_usdt 累加对前高
     （均含 funding/费用；首笔之前无回撤，锚点取首笔）。
  3) 最长水下：低于前高的最长连续区间（按 exit ts 计，天）；末段未恢复按最后一笔计。
  4) funnel 占比分母 = 窗口 1m 根数（= funnel 逐根行数）。
  5) 分层 = exit × signal 按 sig_id 连接（reason 自 exit；type/dir/regime/hour/zone_grade 自 signal）。
  6) zone_grade 分桶 = round(grade,1)；UTC 小时 = init 成交 ts 的小时。
  7) MFE/MAE 分桶 0.5R 步长（<0 / [k,k+0.5) / ≥2）。
  8) funding 降色标注条件 = imputed / 结算总数 > 30%（§9.2）；结算总数=0 时记"无跨结算持仓"。
  9) 有 init 无 exit = 在管（窗口末未平），不计入统计，单列一行注明。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/report_mrs.py --stats a.json --label "VIP0 24个月" --out r.md
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MRS §9.5 报表生成器")
    ap.add_argument("--stats", required=True, help="harness --dump-stats JSON 路径")
    ap.add_argument("--label", default="未命名场景", help="报表标题标签（如 VIP0 / maker）")
    ap.add_argument("--out", required=True, help="markdown 落盘路径")
    return ap.parse_args(argv)


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _mean_ci(xs: list[float]) -> tuple[float, float]:
    """返回 (均值, 半宽 95%CI)；n<2 → 半宽 0。"""
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, 1.96 * math.sqrt(var) / math.sqrt(n)


def _pf(xs: list[float]) -> float:
    gw = sum(x for x in xs if x > 0)
    gl = -sum(x for x in xs if x < 0)
    return gw / gl if gl > 0 else (math.inf if gw > 0 else 0.0)


def _max_dd(points: list[tuple[int, float]]) -> tuple[float, float, float]:
    """points: (ts, level) 按 ts 升序；返回 (maxDD_abs, maxDD_pct, 最长水下天)。"""
    peak, peak_ts = points[0][1], points[0][0]
    dd_abs = dd_pct = longest = 0.0
    for ts, level in points:
        if level >= peak:
            peak, peak_ts = level, ts
        else:
            dd_abs = max(dd_abs, peak - level)
            dd_pct = max(dd_pct, (peak - level) / peak if peak > 0 else 0.0)
            longest = max(longest, (ts - peak_ts) / 86_400)
    return dd_abs, dd_pct, longest


def _bucket(x: float, step: float = 0.5) -> str:
    if x < 0:
        return "<0"
    if x >= 2.0:
        return "≥2"
    k = math.floor(x / step) * step
    return f"[{k:.1f},{k + step:.1f})"


def _layer_table(rows: list[tuple], title: str, key_col: str) -> list[str]:
    """rows: (key, pnl_R) 列表。返回 markdown 行。"""
    groups: dict = defaultdict(list)
    for k, r in rows:
        groups[k].append(r)
    out = [f"**{title}**", "", f"| {key_col} | n | 期望R | 胜率 |", "|---|---|---|---|"]
    for k in sorted(groups, key=lambda x: str(x)):
        rs = groups[k]
        out.append(f"| {k} | {len(rs)} | {sum(rs) / len(rs):+.3f} | "
                   f"{sum(x > 0 for x in rs) / len(rs) * 100:.0f}% |")
    out.append("")
    return out


def build(stats: dict, label: str) -> str:
    w = stats["window"]
    nb = w["n_bars"]
    init_by_id = {t["sig_id"]: t for t in stats["trades"] if t["event"] == "init"}
    exits = sorted((t for t in stats["trades"] if t["event"] == "exit"),
                   key=lambda t: t["ts"])
    sig_by_id = {s["sig_id"]: s for s in stats["signals"]}
    open_cnt = len(init_by_id) - len(exits)
    rs = [t["pnl_R"] for t in exits]
    n = len(exits)

    L: list[str] = [f"# MRS §9.5 回测报表 — {label}", ""]
    L += [f"- 窗口：{w['start']} → {w['end']} UTC（{nb} 根 1m，{w['days']:.1f} 天）",
          f"- cfg_hash：`{stats['cfg_hash']}` | 费用档：fee={stats['fees_bps']['fee']}bp / "
          f"slip={stats['fees_bps']['slip']}bp | 初始权益 {stats['equity_init']:.0f} → "
          f"{stats['equity_final']:.2f}（Δ{stats['equity_final'] - stats['equity_init']:+.2f}）",
          f"- 末态：{stats['state_final']} | 信号 {len(stats['signals'])} 条 | "
          f"成交 init={len(init_by_id)} exit={n}（在管 {open_cnt}）", ""]

    L += ["## 1. 笔量与日均", ""]
    if n:
        L += [f"- 成交 {n} 笔 | 窗口 {w['days']:.1f} 天 | 日均 {n / w['days']:.3f} 笔/天", ""]
    else:
        L += ["- **0 笔成交**（无开仓通过全闸门；见 §4 funnel 拒单分布）", ""]

    L += ["## 2. 核心指标（按平仓笔）", ""]
    if n:
        m, ci = _mean_ci(rs)
        holds = [t["bars_held"] for t in exits]
        eq = stats["equity_init"]
        eq_points, r_points = [], []
        r_cum = 0.0
        for t in exits:
            eq += t["pnl_usdt"]
            r_cum += t["pnl_R"]
            eq_points.append((t["ts"], eq))
            r_points.append((t["ts"], r_cum))
        dd_r, _dd_r_pct, _ = _max_dd([(exits[0]["ts"], 0.0)] + r_points)
        _dd_abs, dd_eq_pct, longest = _max_dd([(exits[0]["ts"], stats["equity_init"])] + eq_points)
        pf = _pf(rs)
        L += [f"- 期望：**{m:+.3f}R ± {ci:.3f}**（95% CI，n={n}"
              + ("，n<30 低样本" if n < 30 else "") + "）",
              f"- 胜率 {sum(x > 0 for x in rs) / n * 100:.1f}% | PF "
              + (f"{pf:.2f}" if math.isfinite(pf) else "∞") + f" | ΣR={sum(rs):+.2f}",
              f"- 平均持有 {sum(holds) / n:.1f} bar | MFE 均值 "
              f"{sum(t['mfe_R'] for t in exits) / n:+.2f}R | MAE 均值 "
              f"{sum(t['mae_R'] for t in exits) / n:+.2f}R",
              f"- maxDD：{dd_r:.2f}R / {dd_eq_pct * 100:.2f}% | 最长水下 {longest:.2f} 天", ""]
    else:
        L += ["- 无平仓笔，核心指标不适用。", ""]

    L += ["## 3. 分层期望", ""]
    if n:
        rows = []
        for t in exits:
            s = sig_by_id.get(t["sig_id"], {})
            hour = (init_by_id.get(t["sig_id"], {}).get("ts", 0) % 86400) // 3600
            rows.append((t, s, hour))
        L += _layer_table([(t["reason"], t["pnl_R"]) for t, s, h in rows],
                          "按 reason（离场原因）", "reason")
        L += _layer_table([(s.get("type", "?"), t["pnl_R"]) for t, s, h in rows], "按 type", "type")
        L += _layer_table([(s.get("dir", "?"), t["pnl_R"]) for t, s, h in rows], "按 dir", "dir")
        L += _layer_table([(s.get("regime", "?"), t["pnl_R"]) for t, s, h in rows],
                          "按 regime", "regime")
        L += _layer_table([(f"{h:02d}时", t["pnl_R"]) for t, s, h in rows],
                          "按 UTC 小时（入场）", "小时")
        L += _layer_table([(("—" if s.get("zone_grade") is None
                             else f"{round(s['zone_grade'], 1):.1f}"), t["pnl_R"])
                           for t, s, h in rows], "按 zone_grade（round 0.1）", "grade")
    else:
        L += ["- 无成交，不适用。", ""]

    L += ["## 4. funnel 全层拒单（健康度）", ""]
    fc = stats["funnel_counts"]
    L += ["| stop_at | 计数 | 占窗口% |", "|---|---|---|"]
    for k, v in fc.items():
        L.append(f"| {k} | {v} | {v / nb * 100:.2f}% |")
    prc = stats.get("plan_reject_counts", {})
    if prc:
        L += ["", "| PLAN 拒绝原因 | 计数 |", "|---|---|"]
        for k, v in prc.items():
            L.append(f"| {k} | {v} |")
    nt = fc.get("NO_TRIGGER", 0)
    cost = prc.get("COST", 0)
    plan = fc.get("PLAN", 0)
    L += ["", f"- NO_TRIGGER 占比：{nt / nb * 100:.2f}%（{nt}/{nb} 根）",
          f"- COST 拒绝占比：{cost / nb * 100:.3f}%（{cost}/{nb} 根）"
          + (f"；占 PLAN 拒单 {cost / plan * 100:.0f}%（{cost}/{plan}）" if plan else ""),
          ""]

    L += ["## 5. MFE / MAE 分布（0.5R 步长）", ""]
    if n:
        order = ["<0", "[0.0,0.5)", "[0.5,1.0)", "[1.0,1.5)", "[1.5,2.0)", "≥2"]
        mfe = Counter(_bucket(t["mfe_R"]) for t in exits)
        mae = Counter(_bucket(t["mae_R"]) for t in exits)
        L += ["| 区间 | MFE | MAE |", "|---|---|---|"]
        for k in order:
            L.append(f"| {k} | {mfe.get(k, 0)} | {mae.get(k, 0)} |")
        L.append("")
    else:
        L += ["- 无成交，不适用。", ""]

    L += ["## 6. funding 结算（§9.2）", ""]
    ssett, simp = stats["funding_settlements"], stats["funding_imputed"]
    if ssett:
        share = simp / ssett * 100
        mark = " ⚠ **imputed>30%，本段报表降色标注**" if share > 30 else ""
        L += [f"- 持仓跨结算 {ssett} 次 | imputed（无真实费率按 0）×{simp} = {share:.1f}%{mark}", ""]
    else:
        L += ["- 无跨结算持仓（结算总数 0）。", ""]

    L += ["## 7. 成交明细", ""]
    if n:
        L += ["| 入场 ts(UTC) | dir | type | grade | reason | pnl_R | bars | MFE | MAE |",
              "|---|---|---|---|---|---|---|---|---|"]
        for t in exits:
            s = sig_by_id.get(t["sig_id"], {})
            i = init_by_id.get(t["sig_id"], {})
            g = s.get("zone_grade")
            L.append(f"| {_fmt_ts(i.get('ts', 0))} | {s.get('dir', '?')} | {s.get('type', '?')} "
                     f"| {'—' if g is None else f'{g:.2f}'} | {t['reason']} | {t['pnl_R']:+.3f} "
                     f"| {t['bars_held']} | {t['mfe_R']:+.2f} | {t['mae_R']:+.2f} |")
        L.append("")
    no_fill = [s for s in stats["signals"] if s["sig_id"] not in init_by_id]
    if no_fill:
        L += [f"- 挂单未成（TTL 等）{len(no_fill)} 条："
              + ", ".join(s["sig_id"][:8] + f"@{_fmt_ts(s['ts'])}" for s in no_fill[:10])
              + ("…" if len(no_fill) > 10 else ""), ""]

    L += ["## 口径登记", "",
          "- 期望 CI 为 95% 正态近似；maxDD 走逐笔权益（含 funding/费用）；最长水下按 exit ts 天计。",
          "- funnel 占比分母 = 窗口 1m 根数；分层按 sig_id 连接 signal×exit；zone_grade round 0.1。",
          "- 数据源：harness --dump-stats（同源引擎，§9.0）。", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    stats = json.loads(pathlib.Path(args.stats).read_text(encoding="utf-8"))
    md = build(stats, args.label)
    pathlib.Path(args.out).write_text(md, encoding="utf-8")
    print(f"[报表] {args.out}（{len(md.splitlines())} 行）")


if __name__ == "__main__":
    main()
