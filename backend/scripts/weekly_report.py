"""P1-2 周报入口：重建台账 → 聚合窗口 → 打印；--push 推送企微（markdown）。

用法：.venv/Scripts/python.exe backend/scripts/weekly_report.py [--push] [--days N]
默认窗口：上周一 00:00 ~ 本周一 00:00 UTC（与扫描器周一 01:00 调度一致）；
--days N 改为最近 N 天滚动窗口（如 M2 问"过去两周"用 --days 14）。
"""
import argparse
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.engine import ledger, report
from app.models import db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="推送到企业微信")
    ap.add_argument("--days", type=int, default=0, help="最近 N 天滚动窗口（默认=上周对齐窗口）")
    args = ap.parse_args()

    if args.days > 0:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
    else:
        start, end = report.last_week_window()

    conn = db.connect(settings.pg_dsn)
    try:
        stats = ledger.rebuild(conn)
        print(f"台账重建：共 {stats['total']} 笔（已平 {stats['closed']} / 持仓中 {stats['open']}）")
        md = report.render_markdown(report.build_report(conn, start, end))
        print(md)
        if args.push:
            if not settings.wecom_webhook:
                print("[推送] 未配置 WECOM_WEBHOOK，跳过")
                return
            ok = WeComNotifier(settings.wecom_webhook).push_markdown(md, conn)
            print(f"[推送] {'成功' if ok else '失败'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
