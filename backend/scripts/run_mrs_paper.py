"""M7 实时纸面 Runner（§11）：OKX BTC-USDT-SWAP 1m 收盘驱动，PaperBroker 模拟撮合。

口径登记（v1）：
  1) 永不产生真实订单：只走 PaperBroker（内存组）+ 本地台账；无任何交易 API 调用。
  2) 数据：REST 轮询 ccxt fetch_ohlcv 1m；每轮只处理「开盘 ts ≤ now-60」的已收盘 bar。
     缺口先回补（重取区间），仍缺 → 不跳 bar（引擎 §3.2-H3 缺口语义），下一轮再试。
  3) 重启：mrs_state.json + mrs_orders.jsonl → recover.plan_restart（损坏/账实不符拒绝启动）；
     活动订单组从 state["runtime"]（engine.persist 本地扩展）重建；缺组挂单 → 拒绝启动，
     缺组持仓 → 按持仓字段合成 PROTECTED 组续管。孤儿动作在首个 bar 执行（平仓记 FLATTEN）。
  4) 预热：从 REST 取最近 warm_bars 根，截取「以结束 ts 收尾的连续段」喂引擎重建指标；
     预热期间换用临时 Ledger 并阻断决策路径（不产生信号/落盘）。恢复需预热段恰好收尾于
     state.ts-60，否则拒绝启动（避免重复处理已处理 bar）。
  5) 重启补跑：预热后从 state.ts 起连续喂到当前收盘（管理/挂单/决策照常，事件标迟到）。
  6) funding：结算点用 REST funding history 查真实费率，缺失按 0 并计 imputed（§9.2）。
  7) 推送：WECOM_WEBHOOK 存在时推 markdown（标题含「纸面」）；失败只记日志不阻断。
  8) 台账每 bar persist（mrs_state.json 原子写）；心跳 mrs_heartbeat.json 每轮覆盖。
用法（项目根）：
  .venv/Scripts/python.exe backend/scripts/run_mrs_paper.py --out-dir data/mrs_paper
  ... --once            # 处理一轮后退出（巡检/测试）
  ... --no-notify       # 不推送
"""
from __future__ import annotations

import argparse
import json
import pathlib
import signal as _signal
import sys
import time
from datetime import datetime, timezone

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))  # backend/

from app.core.config import settings  # noqa: E402
from app.mrs.broker import OrderGroup  # noqa: E402
from app.mrs.config import CFG, validate_cfg  # noqa: E402
from app.mrs.engine import Engine  # noqa: E402
from app.mrs.ledger import Ledger  # noqa: E402
from app.mrs.manage import Position  # noqa: E402
from app.mrs.plan import Bundle  # noqa: E402
from app.mrs.recover import RecoverError, plan_restart, to_position  # noqa: E402
from app.mrs.types import Bar  # noqa: E402

WARM_BARS = 7200  # 5 天：覆盖 30m SMA60（30h）+ zones 回看（4600 根）
_POLL_GRACE = 4    # 收盘后等待秒数（REST 可见性）


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 容忍崩溃截断尾行
    return rows


def _read_state(path: pathlib.Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RecoverError(f"STATE_CORRUPT: JSON 解析失败 {e}") from e


def contiguous_tail(rows: list[Bar], end_ts: int) -> list[Bar]:
    """取以 end_ts 收尾的最长连续 1m 段（升序）；end_ts 不在数据中 → []。"""
    by_ts = {b.ts: b for b in rows}
    if end_ts not in by_ts:
        return []
    out = [by_ts[end_ts]]
    ts = end_ts
    while ts - 60 in by_ts:
        ts -= 60
        out.append(by_ts[ts])
    out.reverse()
    return out


def _bundle(d: dict) -> Bundle:
    return Bundle(**d)


def _synth_bundle(pos: Position, cfg: dict) -> Bundle:
    return Bundle(e_sig=pos.sig_id, now_ts=pos.open_ts, dir=pos.dir,
                  zone_id=pos.zone_id, trigger=pos.entry, stop=pos.stop, tp=pos.tp,
                  qty=pos.qty0, R=pos.R, seg1_px=pos.seg1_px,
                  be_px_target=pos.be_px_target, leverage=pos.lev,
                  fee_bps=cfg["venue"]["taker_bps"],
                  slip_bps=cfg["venue"]["slip_base_bps"])


def restore_runtime(eng: Engine, state: dict) -> None:
    """state["runtime"]（engine.persist 落盘）→ PaperBroker 活动组。"""
    for g in (state.get("runtime") or {}).get("groups", []):
        eng.broker.groups[g["sig_id"]] = OrderGroup(
            sig_id=g["sig_id"], bundle=_bundle(g["bundle"]), stage=g["stage"],
            qty=g["qty"], stop_px=g["stop_px"], tp_px=g["tp_px"],
            seg1_px=g["seg1_px"], protect_fills=g["protect_fills"],
            stop_filled=g["stop_filled"], tp_filled=g["tp_filled"])


def apply_plan(eng: Engine, plan, cfg: dict = CFG) -> dict | None:
    """恢复账户/持仓/挂单；返回需在首 bar 平掉的孤儿仓 dict（或 None）。"""
    a = eng.acct
    a.state = plan.resume_state
    a.day = plan.day
    a.cooldowns = dict(plan.cooldowns)
    a.trades_today = plan.trades_today
    a.consec_losses = plan.consec_losses
    a.day_pnl_r = plan.day_pnl_r
    a.error_stop_fail = plan.error_stop_fail
    a.halt_reason = plan.halt_reason
    if plan.position is not None:
        eng.pos = to_position(plan.position)
        if eng.pos.sig_id not in eng.broker.groups:
            b = _synth_bundle(eng.pos, cfg)
            eng.broker.groups[eng.pos.sig_id] = OrderGroup(
                sig_id=eng.pos.sig_id, bundle=b, stage="PROTECTED", qty=eng.pos.qty,
                stop_px=eng.pos.stop, tp_px=eng.pos.tp, seg1_px=eng.pos.seg1_px)
    if plan.pending is not None:
        eng.pending_sig_id = plan.pending
        if plan.pending not in eng.broker.groups:
            raise RecoverError(f"RUNTIME_MISSING: 挂单 {plan.pending} 无订单组快照")
    orphan = None
    for act in plan.actions:
        if act.kind == "FLATTEN_ORPHAN_POSITION":
            orphan = dict(act.detail, sig_id=act.sig_id)
        else:
            eng.ledger.order({"ts": int(time.time()), "sig_id": act.sig_id,
                              "client_oid": act.sig_id, "order_id": f"{act.sig_id}:all",
                              "type": "CANCEL", "state": "CANCELLED", "px": None,
                              "qty": 0.0, "why": "RECONCILE_ORPHAN"})
    return orphan


class WecomPush:
    def __init__(self, webhook: str, enabled: bool = True):
        self.webhook = webhook
        self.enabled = enabled and bool(webhook)

    def __call__(self, text: str) -> None:
        if not self.enabled:
            _log(f"[推送跳过] {text.splitlines()[0]}")
            return
        try:
            import requests
            r = requests.post(self.webhook, json={"msgtype": "markdown",
                                                  "markdown": {"content": text}},
                              timeout=5)
            if r.status_code != 200 or r.json().get("errcode", 0) != 0:
                _log(f"[推送失败] HTTP {r.status_code} {r.text[:120]}")
        except Exception as e:  # 网络类失败不阻断交易循环
            _log(f"[推送异常] {e}")


class FundingFeed:
    """结算 ts → 真实 funding 费率（REST 历史）；缺失返回 None（引擎按 0 + imputed）。"""

    def __init__(self, adapter, symbol: str):
        self.adapter = adapter
        self.symbol = symbol

    def __call__(self, ts: int) -> float | None:
        try:
            rows = self.adapter.fetch_funding_rate_history(
                self.symbol, since=(ts - 8 * 3600) * 1000, limit=10)
            for r in rows:
                if int(r["timestamp"]) // 1000 == ts:
                    return float(r["fundingRate"])
        except Exception as e:
            _log(f"[funding 查询失败] ts={_fmt(ts)} {e}")
        return None


class PaperRunner:
    def __init__(self, out_dir: str | pathlib.Path, fetch, *, cfg: dict = CFG,
                 funding_fn=None, push_fn=None, warm_bars: int = WARM_BARS,
                 now_fn=time.time):
        self.out_dir = pathlib.Path(out_dir)
        self.fetch = fetch                    # (since_s, end_s) -> list[Bar]
        self.cfg = cfg
        self.funding_fn = funding_fn
        self.push_fn = push_fn or (lambda text: _log(f"[推送关闭] {text.splitlines()[0]}"))
        self.warm_bars = warm_bars
        self.now_fn = now_fn
        self.ledger = Ledger(self.out_dir)
        self._halted: str | None = None
        self._orphan: dict | None = None
        self._stop = False

    # ---------- 启动 ----------
    def prepare(self):
        state = _read_state(self.out_dir / "mrs_state.json")
        orders = _read_jsonl(self.out_dir / "mrs_orders.jsonl")
        plan = plan_restart(state, orders, self.cfg)
        eq = float(plan.equity)
        eng = Engine(cfg=self.cfg, ledger=self.ledger, equity=eq,
                     funding_fn=self.funding_fn, on_halt=self._on_halt)
        for n in plan.notes:
            _log(f"[恢复] {n}")
        _log(f"[恢复] 快照={self.out_dir / 'mrs_state.json'} fresh={plan.fresh} "
             f"state={plan.resume_state} equity={eq:.2f} pending={plan.pending} "
             f"pos={(plan.position or {}).get('sig_id')}")
        return eng, state, plan

    def restore(self, eng: Engine, state: dict | None, plan) -> None:
        if state is not None:
            restore_runtime(eng, state)
        self._orphan = apply_plan(eng, plan, self.cfg)

    def _on_halt(self, reason: str) -> None:
        self._halted = reason
        self.push_fn(f"### MRS 纸面 · 熔断\n> 原因：{reason}\n> 时间：{_fmt(int(self.now_fn()))} UTC")

    # ---------- 预热 ----------
    def warm(self, eng: Engine, end_ts: int) -> None:
        rows = self.fetch(end_ts - self.warm_bars * 60 + 60, end_ts)
        seg = contiguous_tail(rows, end_ts)
        if not seg:
            raise RecoverError(f"WARMUP_INCOMPLETE: 无收尾于 {_fmt(end_ts)} 的连续 1m 段")
        live, b_live = eng.ledger, eng.broker.ledger
        eng.ledger = Ledger()
        eng.broker.ledger = eng.ledger
        orig = eng._entry_phase
        eng._entry_phase = lambda bar, now: ("BUSY", None)  # 预热不产生信号
        try:
            for b in seg:
                eng.on_1m_close(b)
        finally:
            eng._entry_phase = orig
            eng.ledger, eng.broker.ledger = live, b_live
        _log(f"[预热] {len(seg)} 根：{_fmt(seg[0].ts)} → {_fmt(seg[-1].ts)}")

    # ---------- 喂 bar ----------
    def _flatten_orphan(self, eng: Engine, bar: Bar) -> None:
        d = self._orphan
        pos = Position(sig_id=d["sig_id"], dir=d["dir"], open_ts=bar.ts, entry=bar.open,
                       qty0=d["qty"], qty=d["qty"], R=d["R"], stop=bar.close, tp=bar.close,
                       seg1_px=bar.close, be_px_target=bar.close, zone_id="", lev=1,
                       peak=bar.open, be_done=False, seg_done=False, legs=[(bar.open, d["qty"], "ENTRY")])
        eng.pos = pos
        b = _synth_bundle(pos, self.cfg)
        eng.broker.groups[pos.sig_id] = OrderGroup(sig_id=pos.sig_id, bundle=b,
                                                   stage="UNPROTECTED", qty=pos.qty)
        f = eng.broker.market_close(eng.broker.get(pos.sig_id), bar, "FLATTEN")
        eng._close_position("FLATTEN", f, bar)
        self._orphan = None
        _log(f"[恢复] 孤儿仓 {pos.sig_id} 已按 {bar.ts} 市价平")

    def feed(self, eng: Engine, bar: Bar) -> None:
        n_sig, n_tr = len(self.ledger.signals), len(self.ledger.trades)
        if self._orphan is not None:
            self._flatten_orphan(eng, bar)
        eng.on_1m_close(bar)
        eng.persist()
        for s in self.ledger.signals[n_sig:]:
            self.push_signal(s)
        for t in self.ledger.trades[n_tr:]:
            self.push_trade(t)
        self._heartbeat(eng)

    def ingest(self, eng: Engine, target_ts: int) -> int:
        """把 eng.last_ts+60 .. target_ts 连续喂入；返回实际喂入根数（缺口停在缺口前）。"""
        n = 0
        while eng.last_ts is not None and eng.last_ts < target_ts:
            start = eng.last_ts + 60
            rows = self.fetch(start, target_ts)
            by = {b.ts: b for b in rows}
            while start in by:
                self.feed(eng, by[start])
                n += 1
                start += 60
            if start <= target_ts:
                _log(f"[缺口] 缺 {_fmt(start)} 起的 1m bar，等待回补（不跳 bar）")
                break
        return n

    # ---------- 推送 ----------
    def push_signal(self, s: dict) -> None:
        b = s.get("bundle", {})
        g = s.get("zone_grade")
        late = "（补记）" if int(self.now_fn()) - s["ts"] > 300 else ""
        self.push_fn(
            f"### MRS 纸面 · 信号 {s.get('dir')} {s.get('type')}{late}\n"
            f"> 时间：{_fmt(s['ts'])} UTC\n"
            f"> 触发 {b.get('trigger')} / 止损 {b.get('stop')} / 止盈 {b.get('tp')}\n"
            f"> R={b.get('R')} qty={b.get('qty')} grade="
            f"{'—' if g is None else f'{g:.2f}'}")

    def push_trade(self, t: dict) -> None:
        late = "（补记）" if int(self.now_fn()) - t["ts"] > 300 else ""
        if t["event"] == "init":
            self.push_fn(f"### MRS 纸面 · 成交{late}\n> 时间：{_fmt(t['ts'])} UTC\n"
                         f"> {t['sig_id'][:8]} @ {t['entry_px_real']} qty={t['qty']} "
                         f"R={t['R']}")
        else:
            self.push_fn(f"### MRS 纸面 · 离场 {t['reason']}{late}\n"
                         f"> 时间：{_fmt(t['ts'])} UTC\n"
                         f"> {t['sig_id'][:8]} {t['pnl_R']:+.3f}R / {t['pnl_usdt']:+.2f} USDT "
                         f"({t['bars_held']} bars)")

    def _heartbeat(self, eng: Engine) -> None:
        hb = {"ts": int(self.now_fn()), "last_bar": eng.last_ts,
              "state": eng.acct.state, "equity": eng.equity,
              "pending": eng.pending_sig_id,
              "pos": None if eng.pos is None else eng.pos.sig_id,
              "halt": self._halted or eng.acct.halt_reason}
        try:
            tmp = self.out_dir / "mrs_heartbeat.json.tmp"
            tmp.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.out_dir / "mrs_heartbeat.json")
        except OSError as e:  # 心跳失败不阻断交易循环（state 已单独原子落盘）
            _log(f"[心跳失败] {e}")

    # ---------- 主循环 ----------
    def run(self, once: bool = False) -> None:
        validate_cfg(self.cfg)
        eng, state, plan = self.prepare()
        latest = (int(self.now_fn()) // 60) * 60 - 60
        end_ts = state["ts"] - 60 if state is not None else latest
        for attempt in range(5):
            try:
                self.warm(eng, end_ts)
                break
            except Exception as e:
                if attempt == 4 or once:
                    raise
                _log(f"[预热] {_fmt(end_ts)} 尚不可用（{type(e).__name__}: {e}），5s 后重试")
                time.sleep(5)
        if state is not None and end_ts != eng.last_ts:
            raise RecoverError("WARMUP_MISMATCH: 预热末根与快照 ts 不一致")
        self.restore(eng, state, plan)
        while not self._stop:
            target = (int(self.now_fn()) // 60) * 60 - 60
            if eng.last_ts < target:
                try:
                    self.ingest(eng, target)
                except Exception as e:  # 网络/数据异常：下一轮重试，不破坏状态
                    _log(f"[循环异常] {type(e).__name__}: {e}")
            if once:
                break
            time.sleep(max(1.0, 60 - (time.time() % 60) + _POLL_GRACE))
        eng.persist()


def make_fetch(adapter, symbol: str):
    def fetch(since_s: int, end_s: int) -> list[Bar]:
        rows = adapter.fetch_ohlcv(symbol, "1m", since=since_s * 1000,
                                   limit=(end_s - since_s) // 60 + 1, paginate=True)
        return [Bar(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]),
                    float(r[4]), float(r[5])) for r in rows
                if since_s <= int(r[0]) // 1000 <= end_s]
    return fetch


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="MRS M7 实时纸面 Runner")
    ap.add_argument("--out-dir", default="data/mrs_paper", help="台账目录")
    ap.add_argument("--proxy", default=settings.okx_proxy, help="OKX 代理（默认取 OKX_PROXY）")
    ap.add_argument("--warm-bars", type=int, default=WARM_BARS)
    ap.add_argument("--once", action="store_true", help="处理一轮后退出")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)

    from app.adapters.market.ccxt_okx import OKXAdapter
    from app.mrs.config import verify_venue
    adapter = OKXAdapter(proxy=args.proxy)
    verify_venue(adapter, CFG)
    runner = PaperRunner(args.out_dir, make_fetch(adapter, CFG["symbol"]["ccxt"]),
                         funding_fn=FundingFeed(adapter, CFG["symbol"]["ccxt"]),
                         push_fn=WecomPush(settings.wecom_webhook, not args.no_notify),
                         warm_bars=args.warm_bars)
    _signal.signal(_signal.SIGINT, lambda *_: setattr(runner, "_stop", True))
    try:
        runner.run(once=args.once)
    except RecoverError as e:
        _log(f"[拒绝启动] {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
