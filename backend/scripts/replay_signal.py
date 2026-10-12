"""P0 端到端验收：回放最近一条"自然规则事件"走全链路真实推送（与扫描器同路径）。

选已启用规则窗口内最近事件 ts（真实历史 bar，evaluate 原生判定），经 _evaluate_trigger
落库 signal（唯一索引去重）→ WeComNotifier.push_signal（图表+文本）→ 手机企微。
用法：.env 填 WECOM_WEBHOOK 后运行；可选参数限定标的（如 `replay_signal.py LINK/USDT`）。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.market.ccxt_okx import OKXAdapter
from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.engine import scanner
from app.engine.indicators import enrich
from app.engine.rules_dsl import WARMUP_BARS, evaluate
from app.models import db


def main() -> None:
    if not settings.wecom_webhook:
        print("未配置 WECOM_WEBHOOK（.env），无法真实推送")
        return
    only = sys.argv[1] if len(sys.argv) > 1 else None
    adapter = OKXAdapter(settings.okx_proxy)
    conn = db.connect(settings.pg_dsn)
    rules = scanner.load_rules(conn)
    best = None  # (ts, rule)
    for r in rules:
        if only and r["symbol"] != only:
            continue
        df = db.load_ohlcv(conn, r["symbol"], "okx", r["contract_type"], r["timeframe"],
                           limit=scanner.SCAN_WINDOW)
        if len(df) < WARMUP_BARS + 2:
            continue
        ev = evaluate(r["rule"], enrich(df))
        if ev and (best is None or ev[-1].ts > best[0]):
            best = (ev[-1].ts, r)
    if best is None:
        print("窗口内无任何自然事件可回放")
        return
    ts, r = best
    print(f"最近自然事件：{r['name']} @ {ts}（回放走扫描器同路径）")
    sigs = scanner._evaluate_trigger(conn, adapter, rules, r["symbol"], r["contract_type"],
                                     r["timeframe"], int(ts.timestamp() * 1000))
    if not sigs:
        print("去重命中（该事件此前已落库），本次不重复推送；如需再验请等待下一个自然事件")
        return
    notifier = WeComNotifier(settings.wecom_webhook)
    for s in sigs:
        ok = notifier.push_signal(s, conn)
        print(f"  {s['direction']} @ {s['price']:.6g} signal_id={s['signal_id']} "
              f"推送{'成功' if ok else '失败'}")
    with conn.cursor() as cur:
        cur.execute("SELECT signal_id, channel, status, error FROM push_log ORDER BY id DESC LIMIT 3")
        print("push_log 最近 3 条：")
        for row in cur.fetchall():
            print("  ", row)
    conn.close()
    print("端到端回放完成——请查看手机企业微信")


if __name__ == "__main__":
    main()
