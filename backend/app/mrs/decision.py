"""MRS 决策层（规格书 §5）：rank/共振/regime/关键区/活动区/触发器/时间闸门。

全部为显式输入纯函数（§3.3/§3.4）：不读模块单例、不取"最新数据"，回测与实盘同源（§9.0）。

规格书留白处的 v1 口径（逐条登记，便于后续复核/调整）：
  1) §5.1 struct：取最近 5 个已确认摆动，要求严格交替；三次同侧相邻比较（s2-s0、s3-s1、
     s4-s2）全升 → +1、全降 → -1。规格书示例 (更高高,更高低,更高高) 对应 [H,L,H,L,H] 布局；
     "1m摆动"按"该 tf 自身的摆动"实现（§3.3 仅定义 5m/15m/30m 的 TFState）。
  2) §5.4 整数阶梯：仅取"间距 > cluster_atr_mult×ATR15"的阶梯值（≤ 簇距会链式合并成一坨），
     BTC 默认档位下即 250/500（ATR15 大时自动降为只留 500 或不留）。
  3) §5.4 权重：swing/fib/vwap/int/prevday=1.0，ma/poc=0.8；width 用未加权 std（n=1 → 0）。
  4) §5.4 触碰/后效：触碰=近 3 日 1m 收盘落于区±width 内计数 + 每个摆动成员 +1；后效=每个
     触碰点后 60 根内有利偏移（支撑向上/阻力向下，ATR15 倍数、单点截 3）的均值，无触碰 → 0。
  5) §5.4 zone.id = blake2(round(center/tick), round(width/tick))（宽度量化保证重建稳定）。
  6) §5.5/§6.1：room 预检统一在 make_plan 内以最终 R 判定（§6.1/§6.6 口径），本层不做早退版。
  7) §3.1 点差闸门：spread_bps=None（回测/纸盘无盘口源）→ 放行并在 gates_passed 记
     "SPREAD-NA"；实时运行由 Runner 传入真实值，过期/超限用 inf 强制不通过（§11）。
  8) §5.6 T1 日内去重键 = ("T1", zone_id)，由调用方按 UTC 日维护复位。
"""
from __future__ import annotations

import hashlib
import math
import uuid
from statistics import pstdev

from .config import CFG
from .indicators import Swing
from .types import Bar, Material, Signal, Zone

# §5.4 权重表（v1 口径 3）：未列出的原料按 1.0。
_MAT_W = {"ma": 0.8, "poc": 0.8}
_NEAR_RR = 1.5

_UUID_NS = uuid.UUID("6d72732d-0000-4000-8000-000000000001")  # MRS 命名空间（固定值）


# ---------- §5.1 周期方向 rank ----------

def struct_component(swings: list[Swing], now_ts: int) -> int:
    """v1 口径 1：最近 5 个已确认摆动严格交替且三次同侧比较同向 → ±1，否则 0。"""
    pts = [s for s in swings if s.confirm_ts <= now_ts][-5:]
    if len(pts) < 5:
        return 0
    sides = [s.side for s in pts]
    if sides not in (["H", "L", "H", "L", "H"], ["L", "H", "L", "H", "L"]):
        return 0
    up = pts[2].price > pts[0].price and pts[3].price > pts[1].price and pts[4].price > pts[2].price
    dn = pts[2].price < pts[0].price and pts[3].price < pts[1].price and pts[4].price < pts[2].price
    return 1 if up else (-1 if dn else 0)


def compute_rank(close: float, ma5, ma20, ma60, swings: list[Swing], now_ts: int) -> int:
    """§5.1：排列分 + struct，clip 到 [-2, +2]。MA 任一为 None → 排列分 0（§4.1）。"""
    base = 0
    if None not in (ma5, ma20, ma60):
        if ma5 > ma20 > ma60 and close > ma60:
            base = 2
        elif close > ma20 > ma60:
            base = 1
        elif ma5 < ma20 < ma60 and close < ma60:
            base = -2
        elif close < ma20 < ma60:
            base = -1
    return max(-2, min(2, base + struct_component(swings, now_ts)))


# ---------- §5.2 共振 / §5.3 regime ----------

def resonance(rank30: int, rank15: int, rank5: int, cfg: dict = CFG) -> str:
    """→ "LONG"|"SHORT"|"BOTH_OFF"（§5.2 真值表口径）。"""
    r = cfg["resonance"]
    score = r["w30"] * rank30 + r["w15"] * rank15 + r["w5"] * rank5
    v = r["veto_rank"]
    if score >= r["req_score"] and not (rank30 <= -v or rank15 <= -v or rank5 <= -v):
        return "LONG"
    if score <= -r["req_score"] and not (rank30 >= v or rank15 >= v or rank5 >= v):
        return "SHORT"
    return "BOTH_OFF"


def regime_from_adx(adx15, cfg: dict = CFG) -> str:
    """§5.3：None 或 < adx_min → NO_TRADE；< adx_full → HALF；其余 FULL。"""
    g = cfg["regime"]
    if adx15 is None or adx15 < g["adx_min"]:
        return "NO_TRADE"
    return "HALF" if adx15 < g["adx_full"] else "FULL"


# ---------- §5.4 关键区 ----------

def build_int_materials(close: float, atr15: float, tick: float,
                        cfg: dict = CFG) -> list[Material]:
    """整数阶梯原料（v1 口径 2）：间距 > cluster_atr_mult×ATR15 的阶梯值，±20ATR15 内取倍数。"""
    z = cfg["zones"]
    merge = z["cluster_atr_mult"] * atr15
    unit = max(1.0, tick * 10.0)
    lo, hi = close - 20.0 * atr15, close + 20.0 * atr15
    prices: set[float] = set()
    for step in z["round_steps"]:
        st = step * unit
        if st <= merge:
            continue
        m = math.ceil(lo / st) * st
        while m <= hi:
            if m > 0:
                prices.add(m)
            m += st
    return [Material(p, "int") for p in sorted(prices)]


def build_materials(*, day_snap: dict | None, ma_levels: list[float | None],
                    swings: list[Swing], fib: list[float] | None,
                    close: float, atr15: float, tick: float,
                    cfg: dict = CFG) -> list[Material]:
    """汇总原料位（§5.4）：昨 H/L/C/VWAP/POC、15m/30m MA20/60、各 tf 摆动（调用方按
    lookback_tf 截取）、FIB、整数阶梯。"""
    out: list[Material] = []
    if day_snap:
        out.append(Material(day_snap["high"], "prevday"))
        out.append(Material(day_snap["low"], "prevday"))
        out.append(Material(day_snap["close"], "prevday"))
        if day_snap.get("vwap") is not None:
            out.append(Material(day_snap["vwap"], "vwap"))
        if day_snap.get("poc") is not None:
            out.append(Material(day_snap["poc"], "poc"))
    for v in ma_levels:
        if v is not None:
            out.append(Material(v, "ma"))
    for s in swings:
        out.append(Material(s.price, "swing"))
    if fib:
        out.extend(Material(p, "fib") for p in fib)
    out.extend(build_int_materials(close, atr15, tick, cfg))
    return out


def _weighted_median(members: list[Material]) -> float:
    ms = sorted(members, key=lambda m: m.price)
    ws = [_MAT_W.get(m.kind, 1.0) for m in ms]
    half = sum(ws) / 2.0
    cum = 0.0
    for m, w in zip(ms, ws):
        cum += w
        if cum >= half:
            return m.price
    return ms[-1].price


def _zone_id(center: float, width: float, tick: float) -> str:
    key = f"{round(center / tick)}:{round(width / tick)}"
    return hashlib.blake2b(key.encode(), digest_size=6).hexdigest()


def _touch_metrics(zone_lo: float, zone_hi: float, is_support: bool,
                   members: list[Material], hist: list[Bar], atr15: float) -> tuple[int, float]:
    """v1 口径 4。hist 已按时间升序且 ts ≥ now−3d。"""
    idx = [i for i, b in enumerate(hist) if zone_lo <= b.close <= zone_hi]
    n_sw = sum(1 for m in members if m.kind == "swing")
    touches = len(idx) + n_sw
    if not idx:
        return touches, 0.0
    exs = []
    for i in idx:
        fwd = hist[i + 1:i + 61]
        if not fwd:
            continue
        if is_support:
            ex = max(b.high for b in fwd) - hist[i].close
        else:
            ex = hist[i].close - min(b.low for b in fwd)
        exs.append(min(3.0, max(0.0, ex) / atr15))
    return touches, (sum(exs) / len(exs) if exs else 0.0)


def rebuild_zones(*, materials: list[Material], close: float, atr15: float,
                  bars_1m_recent: list[Bar], now_ts: int, tick: float,
                  cfg: dict = CFG) -> list[Zone]:
    """§5.4 六步重建；确定性（同输入同输出）。"""
    z = cfg["zones"]
    merge = z["cluster_atr_mult"] * atr15
    ms = sorted((m for m in materials if abs(m.price - close) <= 20.0 * atr15),
                key=lambda m: m.price)
    if not ms:
        return []
    clusters: list[list[Material]] = [[ms[0]]]
    for m in ms[1:]:
        if m.price - clusters[-1][-1].price < merge:
            clusters[-1].append(m)
        else:
            clusters.append([m])

    hist = [b for b in bars_1m_recent if b.ts >= now_ts - 3 * 86_400]
    zones: list[Zone] = []
    for cl in clusters:
        center = round(_weighted_median(cl) / tick) * tick
        std = pstdev([m.price for m in cl]) if len(cl) > 1 else 0.0
        width = min(z["width_cap_atr"] * atr15,
                    max(z["width_min_atr"] * atr15, z["width_std_mult"] * std))
        width = round(width / tick) * tick
        side = "S" if center <= close else "R"
        touches, rebound = _touch_metrics(center - width, center + width,
                                          side == "S", cl, hist, atr15)
        types = tuple(sorted({m.kind for m in cl}))
        grade = (z["touch_weight"] * math.log1p(touches)
                 + z["rebound_weight"] * rebound
                 + z["multi_weight"] * (len(types) - 1))
        zones.append(Zone(id=_zone_id(center, width, tick), center=center, width=width,
                          side=side, grade=grade, touches=touches, rebound=rebound,
                          types=types))

    out: list[Zone] = []
    for side in ("S", "R"):
        picked = sorted((x for x in zones if x.side == side),
                        key=lambda x: x.grade, reverse=True)[:z["top_n"]]
        out.extend(sorted(picked, key=lambda x: x.center))
    return out


def pick_zone(zones: list[Zone], direction: str, close: float, atr15: float,
              cfg: dict = CFG) -> Zone | None:
    """§5.4 当前活动区：|close−edge_side| ≤ max(width/2, near_atr×ATR15)；同侧取最近。"""
    side = "S" if direction == "LONG" else "R"
    near = cfg["zones"]["near_atr"] * atr15
    best = None
    for zn in zones:
        if zn.side != side:
            continue
        edge = zn.center + zn.width / 2 if side == "S" else zn.center - zn.width / 2
        dist = abs(close - edge)
        if dist <= max(zn.width / 2, near):
            if best is None or (dist, zn.center) < best[0]:
                best = ((dist, zn.center), zn)
    return best[1] if best else None


# ---------- §5.6 触发器 ----------

def eval_t2(*, bars: list[Bar], zone: Zone, direction: str, atr1: float,
            tick: float, cfg: dict = CFG) -> tuple[float, float] | None:
    """T2 猎杀收回（做多版；做空镜像）。bars 升序、bars[-1]=当前 n。→ (invalid_px, trigger_px)。"""
    n_bars = cfg["entry"]["t2_max_bars"] + 1
    if len(bars) < n_bars:
        return None
    w = bars[-n_bars:]
    cur = w[-1]
    pos = cfg["entry"]["t2_close_pos"]
    if direction == "LONG":
        under = zone.center - zone.width / 2
        if not any(b.low < under for b in w):
            return None
        if not cur.close > under:
            return None
        if any(b.close > under for b in w[:-1]):
            return None
        rng = cur.high - cur.low
        if rng <= 0 or cur.close < cur.low + pos * rng:
            return None
        if not cur.low > w[-2].low - 0.1 * atr1:
            return None
        return min(b.low for b in w), cur.high + tick
    over = zone.center + zone.width / 2
    if not any(b.high > over for b in w):
        return None
    if not cur.close < over:
        return None
    if any(b.close < over for b in w[:-1]):
        return None
    rng = cur.high - cur.low
    if rng <= 0 or cur.close > cur.high - pos * rng:
        return None
    if not cur.high < w[-2].high + 0.1 * atr1:
        return None
    return max(b.high for b in w), cur.low - tick


def eval_t1(*, bars: list[Bar], zone: Zone, direction: str, atr1: float, atr5: float,
            atr15: float, tick: float, t1_used_today: set, cfg: dict = CFG) -> tuple[float, float] | None:
    """T1 回踩承接（做多版；做空镜像）。t1_used_today：该 UTC 日 (T1, zone_id) 键集合。"""
    if len(bars) < 2:
        return None
    cur, prev = bars[-1], bars[-2]
    near = cfg["zones"]["near_atr"] * atr15
    shadow = cfg["entry"]["t1_shadow_atr"] * atr1
    if ("T1", zone.id) in t1_used_today:
        return None
    if direction == "LONG":
        edge = zone.center + zone.width / 2
        if abs(cur.close - edge) > max(zone.width / 2, near):
            return None
        if min(cur.open, cur.close) - cur.low < shadow:
            return None
        if not cur.close > prev.close:
            return None
        if cur.low < prev.low - 0.1 * atr1:
            return None
        return cur.low - 0.05 * atr5, cur.high + tick
    edge = zone.center - zone.width / 2
    if abs(cur.close - edge) > max(zone.width / 2, near):
        return None
    if cur.high - max(cur.open, cur.close) < shadow:
        return None
    if not cur.close < prev.close:
        return None
    if cur.high > prev.high + 0.1 * atr1:
        return None
    return cur.high + 0.05 * atr5, cur.low - tick


# ---------- §5.7 时间与事件闸门 ----------

def in_session(now_ts: int, windows: list[str]) -> bool:
    """UTC 窗口表命中；空表 = 全天允许。支持跨日窗口（如 23:00-02:00）。"""
    if not windows:
        return True
    m = (now_ts % 86_400) // 60
    for w in windows:
        a, b = w.split("-")
        h0, m0 = map(int, a.split(":"))
        h1, m1 = map(int, b.split(":"))
        s0, s1 = h0 * 60 + m0, h1 * 60 + m1
        if s0 <= s1:
            if s0 <= m < s1:
                return True
        elif m >= s0 or m < s1:
            return True
    return False


def event_block(now_ts: int, funding_hours: list[int], blackout_min: int,
                events: list[dict] | None = None) -> bool:
    """§5.7：funding±blackout 或外部事件清单命中 → 禁止新开仓。"""
    m = (now_ts % 86_400) // 60
    for h in funding_hours:
        c = h * 60
        if (m - c) % 1440 <= blackout_min or (c - m) % 1440 <= blackout_min:
            return True
    for e in events or ():
        if e["start_ts"] <= now_ts < e["end_ts"]:
            return True
    return False


# ---------- §7.2 第 3–4 步：无状态决策序列 ----------

def _sig_id(now_ts: int, typ: str, zone_id: str, direction: str) -> str:
    """确定性 sig_id（uuid5）：同一 bar/形态/区 → 同 id，供 §9.4 逐行 diff。"""
    return str(uuid.uuid5(_UUID_NS, f"{now_ts}:{typ}:{zone_id}:{direction}"))


def eval_entry(*, bars: list[Bar], now_ts: int, rank30: int, rank15: int, rank5: int,
               adx15, atr1: float, atr5: float, atr15: float, vol_pctile,
               spread_bps: float | None, zones: list[Zone], zone_touch_today: dict,
               zone_blacklist: set, t1_used_today: set, tick: float,
               session_windows: list[str], funding_hours: list[int], blackout_min: int,
               cooldowns: dict | None = None,
               events: list[dict] | None = None, cfg: dict = CFG) -> tuple[Signal | None, str]:
    """§7.2 第 3-4 步纯决策（挂单/持仓由 Runner 先行判断）。

    返回 (Signal, "OK") 或 (None, reason)；reason ∈ §8.2 漏斗枚举
    {SESSION, EVENT, REGIME, RESON, ZONE, COOLDOWN, SPREAD, VOL, NO_TRIGGER}。
    zone_blacklist 元素为 (zone_id, dir)（§6.5 拉黑按 zone+方向）；zone_touch_today 按 zone 计。
    cooldowns：§6.5 平仓 25 根同向禁开（dir→截止 ts，按方向分存；TTL 撤单为双向 5 根）。
    """
    e = cfg["entry"]
    if not in_session(now_ts, session_windows):
        return None, "SESSION"
    if event_block(now_ts, funding_hours, blackout_min, events):
        return None, "EVENT"
    if regime_from_adx(adx15, cfg) == "NO_TRADE":
        return None, "REGIME"
    res = resonance(rank30, rank15, rank5, cfg)
    if res == "BOTH_OFF":
        return None, "RESON"
    close = bars[-1].close
    act = pick_zone(zones, res, close, atr15, cfg)
    if act is None:
        return None, "ZONE"
    if (act.id, res) in zone_blacklist or zone_touch_today.get(act.id, 0) >= e["max_zone_touches"]:
        return None, "ZONE"
    if now_ts < (cooldowns or {}).get(res, 0):
        return None, "COOLDOWN"   # §7.2 门序：zone 之后、spread 之前
    passed = ["SESSION", "EVENT", "REGIME", "RESON", "ZONE"]
    if spread_bps is None:
        passed.append("SPREAD-NA")          # v1 口径 7：无盘口源放行
    elif spread_bps > e["spread_max"]:
        return None, "SPREAD"
    else:
        passed.append("SPREAD")
    if vol_pctile is None or vol_pctile < e["vol_pctile_min"]:
        return None, "VOL"
    passed.append("VOL")

    kw = dict(bars=bars, zone=act, direction=res, atr1=atr1, tick=tick, cfg=cfg)
    out = eval_t2(**kw)
    typ, suppressed = "T2", None
    if out is None:
        t1 = eval_t1(bars=bars, zone=act, direction=res, atr1=atr1, atr5=atr5,
                     atr15=atr15, tick=tick, t1_used_today=t1_used_today, cfg=cfg)
        if t1 is None:
            return None, "NO_TRIGGER"
        out, typ = t1, "T1"
    else:
        t1 = eval_t1(bars=bars, zone=act, direction=res, atr1=atr1, atr5=atr5,
                     atr15=atr15, tick=tick, t1_used_today=set(), cfg=cfg)
        if t1 is not None:
            suppressed = "T1"               # §5.6：只发 T2，T1 抑制留痕
    invalid, trigger_px = out
    sig = Signal(id=_sig_id(now_ts, typ, act.id, res), now_ts=now_ts, type=typ, dir=res,
                 zone_id=act.id, trigger_px=trigger_px, invalid_px=invalid, close=close,
                 suppressed=suppressed, gates_passed=passed + [typ])
    return sig, "OK"
