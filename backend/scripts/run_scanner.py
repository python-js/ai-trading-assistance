"""P0-6 扫描器入口（P0-8 接入企业微信推送，P1-2 周一 01:00 UTC 周报，
P2-4/5/6 推送含三票强度、读图第二意见与 AI 研判理由，LLM 失败自动降级纯信号版，
A6 心跳停更/恢复经 on_alert 推送告警卡片）。用法：
  --once  顺序扫描全部周期一次（冒烟验证）
  无参    常驻调度（各 tf 收盘 +3s 触发；周一 01:00 UTC 台账周报；Ctrl+C 退出）
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.feishu import FeishuNotifier
from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.engine import chart_vote, explain, ledger, rag, report
from app.engine.scanner import main
from app.models import db


def _hook(s: dict) -> None:
    if not settings.wecom_webhook and not settings.feishu_webhook:
        print("  [推送] 未配置 WECOM_WEBHOOK/FEISHU_WEBHOOK，跳过")
        return
    conn = db.connect(settings.pg_dsn)
    try:
        s["knowledge"] = rag.recall(conn, s)  # A5：失败静默 []，不阻塞推送
        s["reason"] = explain.build_reason(s)  # LLM 不可用/违禁时 None（降级纯信号推送）
        img = chart_vote.render_chart(conn, s)  # 推送与读图票共用同一张截图
        if img:
            cv = chart_vote.run(s, img)
            if cv:
                s["chart"] = cv
                db.insert_signal_vote(conn, signal_id=s["signal_id"], vote_type="chart",
                                      strength=cv[0], evidence=cv[1])
                print(f"  [读图] {cv[1]['stance']} {cv[0]:.2f} {cv[1]['reason'][:40]}")
        if settings.wecom_webhook:
            ok = WeComNotifier(settings.wecom_webhook).push_signal(s, conn, img_path=img)
            print(f"  [推送] {'成功' if ok else '失败'} signal_id={s['signal_id']}")
        if settings.feishu_webhook:
            ok = FeishuNotifier(settings.feishu_webhook,
                                settings.feishu_secret).push_signal(s, conn)
            print(f"  [推送/飞书] {'成功' if ok else '失败'} signal_id={s['signal_id']}")
    finally:
        conn.close()


def _alert(md: str) -> bool:
    """心跳停更/恢复告警（A6）：经已配置通道推送；返回是否有任一通道送达。"""
    conn = db.connect(settings.pg_dsn)
    try:
        ok = False
        if settings.wecom_webhook:
            ok = WeComNotifier(settings.wecom_webhook).push_markdown(md, conn)
        if settings.feishu_webhook:
            ok = FeishuNotifier(settings.feishu_webhook,
                                settings.feishu_secret).push_markdown(md, conn) or ok
        print(f"  [告警] {'已推送' if ok else '推送失败'}：{md.splitlines()[0]}", flush=True)
        return ok
    finally:
        conn.close()


def _weekly() -> None:
    conn = db.connect(settings.pg_dsn)
    try:
        stats = ledger.rebuild(conn)
        start, end = report.last_week_window()
        md = report.render_markdown(report.build_report(conn, start, end))
        print(f"[周报] 台账 {stats['total']} 笔（已平 {stats['closed']}），窗口 {start:%m-%d}~{end:%m-%d}（北京）", flush=True)
        if settings.wecom_webhook:
            ok = WeComNotifier(settings.wecom_webhook).push_markdown(md, conn)
            print(f"  [推送] {'成功' if ok else '失败'}", flush=True)
        if settings.feishu_webhook:
            ok = FeishuNotifier(settings.feishu_webhook,
                                settings.feishu_secret).push_markdown(md, conn)
            print(f"  [推送/飞书] {'成功' if ok else '失败'}", flush=True)
        if not settings.wecom_webhook and not settings.feishu_webhook:
            print("  [推送] 未配置 WECOM_WEBHOOK/FEISHU_WEBHOOK，跳过")
    finally:
        conn.close()


if __name__ == "__main__":
    _alert_hook = _alert if (settings.wecom_webhook or settings.feishu_webhook) else None
    main(once="--once" in sys.argv, on_signal=_hook, on_weekly=_weekly, on_alert=_alert_hook)
