"""看门狗（外部哨兵）：扫描器进程死亡/僵死时经 webhook 告警 —— 独立于扫描器进程。

- 判据：scanner.log 尾部最后一条 "[心跳" 行距今 > --max-age-sec（默认 180s）；
  无心跳行（启动早期等）回退用日志文件修改时间；日志缺失视为死亡。
- 节流：状态文件记 down 状态；首次检出、每 30 分钟重报、恢复通知各一次；
  发送失败不落状态（下轮重试）。
- 不依赖数据库：告警直发 webhook（push_log 跳过）；自身日志追加 data/logs/watchdog.log
  （计划任务用 pythonw 运行，无控制台窗口，故自记日志）。
- 用法：python backend/scripts/watchdog.py [--log PATH] [--state PATH] [--max-age-sec N]
- 注册计划任务：scripts/register_watchdog.cmd（每 5 分钟，任务名 AI-Trading-Watchdog）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.adapters.notify.feishu import FeishuNotifier
from app.adapters.notify.wecom import WeComNotifier
from app.core.config import settings
from app.core.tz import CST

ROOT = pathlib.Path(__file__).resolve().parents[2]
REALERT_SEC = 1800  # 持续停摆的重报间隔
LOG = ROOT / "data/logs/watchdog.log"
DEFAULT_LOG = ROOT / "data/logs/scanner.log"
DEFAULT_STATE = ROOT / "data/logs/watchdog_state.json"
_HB = re.compile(r"\[心跳[^\]]*\] (\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def log(msg: str) -> None:
    line = f"{datetime.now(CST):%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def last_heartbeat(path: pathlib.Path) -> datetime | None:
    """日志尾部最后一条心跳行的时间（北京时间）；无心跳行返回 None。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    hits = _HB.findall(tail)
    if not hits:
        return None
    now = datetime.now(CST)
    t = datetime.strptime(hits[-1], "%m-%d %H:%M:%S").replace(year=now.year, tzinfo=CST)
    if t > now + timedelta(days=1):  # 跨年：日志行为去年
        t = t.replace(year=t.year - 1)
    return t


def heartbeat_age(path: pathlib.Path) -> float:
    """距最后心跳的秒数：无心跳行回退到文件 mtime；日志缺失 → inf。"""
    if not path.exists():
        return float("inf")
    last = last_heartbeat(path)
    if last is None:
        last = datetime.fromtimestamp(path.stat().st_mtime, CST)
    return (datetime.now(CST) - last).total_seconds()


def _send(md: str) -> bool:
    ok = False
    if settings.wecom_webhook:
        ok = WeComNotifier(settings.wecom_webhook).push_markdown(md)
    if settings.feishu_webhook:
        ok = FeishuNotifier(settings.feishu_webhook,
                            settings.feishu_secret).push_markdown(md) or ok
    log(f"告警{'已发送' if ok else '发送失败'}：{md.splitlines()[0]}")
    return ok


def _load_state(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: pathlib.Path, st: dict) -> None:
    path.write_text(json.dumps(st), encoding="utf-8")


def run(log_path: pathlib.Path, state_path: pathlib.Path, max_age: float) -> None:
    age = heartbeat_age(log_path)
    st = _load_state(state_path)
    now = time.time()
    ts = datetime.now(CST).strftime("%m-%d %H:%M")
    if age <= max_age:
        if st.get("down"):
            mins = int((now - st.get("down_since", now)) / 60)
            if _send(f"### 恢复：扫描器心跳正常\n**时间**：{ts} 北京时间\n"
                     f"停摆约 {mins} 分钟，已恢复。"):
                _save_state(state_path, {})
        else:
            log(f"心跳正常（{age:.0f}s 前）")
        return
    mins = "N/A" if age == float("inf") else f"{age / 60:.0f}"
    if not st.get("down"):
        if _send(f"### 告警：扫描器心跳停止\n**时间**：{ts} 北京时间\n"
                 f"**状态**：距最后心跳已 {mins} 分钟（阈值 {max_age / 60:.0f} 分钟）\n"
                 f"**排查**：data/logs/scanner.log 尾部；重启执行 start_scanner.cmd"):
            _save_state(state_path, {"down": True, "down_since": now, "last_alert": now})
    elif now - st.get("last_alert", 0) >= REALERT_SEC:
        if _send(f"### 告警（仍未恢复）：扫描器心跳停止\n**时间**：{ts} 北京时间\n"
                 f"**状态**：距最后心跳已 {mins} 分钟"):
            st["last_alert"] = now
            _save_state(state_path, st)
    else:
        log(f"停摆中（已告警，距上次 {int((now - st['last_alert']) / 60)} 分钟）")


def main() -> None:
    ap = argparse.ArgumentParser(description="扫描器心跳看门狗（外部哨兵）")
    ap.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    ap.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    ap.add_argument("--max-age-sec", type=float, default=180.0)
    args = ap.parse_args()
    if not settings.wecom_webhook and not settings.feishu_webhook:
        print("未配置 WECOM_WEBHOOK/FEISHU_WEBHOOK，跳过")
        return
    run(args.log, args.state, args.max_age_sec)


if __name__ == "__main__":
    main()
