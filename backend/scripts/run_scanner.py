"""P0-6 扫描器入口（P0-8 接入企业微信推送，P1-2 周一 01:00 UTC 周报）。用法：
  --once  顺序扫描全部周期一次（冒烟验证）
  无参    常驻调度（各 tf 收盘 +3s 触发；周一 01:00 UTC 台账周报；Ctrl+C 退出）
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.engine import ledger, report
from app.engine.scanner import main
from app.models import db


def _hook(s: dict) -> None:
    if not settings.wecom_webhook:
        print("  [推送] 未配置 WECOM_WEBHOOK，跳过")
        return
    conn = db.connect(settings.pg_dsn)
    try:
        ok = WeComNotifier(settings.wecom_webhook).push_signal(s, conn)
        print(f"  [推送] {'成功' if ok else '失败'} signal_id={s['signal_id']}")
    finally:
        conn.close()


def _weekly() -> None:
    conn = db.connect(settings.pg_dsn)
    try:
        stats = ledger.rebuild(conn)
        start, end = report.last_week_window()
        md = report.render_markdown(report.build_report(conn, start, end))
        print(f"[周报] 台账 {stats['total']} 笔（已平 {stats['closed']}），窗口 {start:%m-%d}~{end:%m-%d}", flush=True)
        if not settings.wecom_webhook:
            print("  [推送] 未配置 WECOM_WEBHOOK，跳过")
            return
        ok = WeComNotifier(settings.wecom_webhook).push_markdown(md, conn)
        print(f"  [推送] {'成功' if ok else '失败'}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main(once="--once" in sys.argv, on_signal=_hook, on_weekly=_weekly)
