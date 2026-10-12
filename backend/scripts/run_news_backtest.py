"""P2-8 信息票独立回测入口（IC/命中率；只读不改库）。用法：
  .venv/Scripts/python.exe backend/scripts/run_news_backtest.py [--days 30]
输出：控制台 + data/backtest_reports/news_ic_YYYYMMDD.md
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.tz import CST
from app.engine import news_backtest
from app.models import db


def main() -> None:
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    conn = db.connect(settings.pg_dsn)
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        events = news_backtest.load_events(conn, since=since)
        samples = news_backtest.build_samples(conn, events)
        md = news_backtest.render(events, samples, days)
    finally:
        conn.close()
    print(md)
    out = pathlib.Path("data/backtest_reports") / f"news_ic_{datetime.now(CST):%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"\n已存 {out}")


if __name__ == "__main__":
    main()
