"""MRS 测试支架（T12/T14 共用）：确定性 1m 场景，产出 14:00 的 T2 信号。

场景设计（全部数值用手算核对过，见 T12/T14 断言）：
- 13:45–13:59：15 根铺垫 bar（close 1696、vol 50）→ m1 ATR 建立，5m/15m/30m 在 13:59 同收；
  期间 5m/15m ATR 样本不足 → 前 15 根 funnel 均 REGIME。
- prep 后调用 inject()：注入 adx15=30/FULL、ranks(30/15/5)=2/1/1、atr5=20、atr15=40、
  zones=[ZS,ZR]；ATR/ADX 同时写 RMA 种子值，防止后续 HTF 收盘把 .v 冲成 None。
- 14:00 signal()：T2 猎杀收回（under=1698、invalid=1694、trigger=1708.1）
  → plan：R=32、stop=1676.1、tp=1767.0、qty=5.4687、seg1=1746.5、be=1712.8。
- 14:01 fill()：触发成交价 = 1708.1×(1+2bp) = 1708.44162。
- 14:03 dump()：击穿 stop（low 1674 ≤ 1676.1）→ 止损价 1676.1×(1−2bp) = 1675.76478。
- flat/push 供 COOLDOWN 段与第二信号铺垫使用。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.engine import Engine
from app.mrs.ledger import Ledger
from app.mrs.types import Bar, Zone

D = 1_728_000_000  # 20000 × 86400：UTC 日界（1m 场景起点 13:45 = D+49500）

ZS = Zone(id="zs", center=1708.0, width=20.0, side="S", grade=0.5, touches=1,
          rebound=0.0, types=("swing",))
ZR = Zone(id="zr", center=1780.0, width=20.0, side="R", grade=0.5, touches=1,
          rebound=0.0, types=("swing",))


class Rig:
    def __init__(self, *, fail_place_stop: int = 0, on_halt=None,
                 equity: float = 50_000.0, ledger: Ledger | None = None):
        self.led = ledger if ledger is not None else Ledger()
        self.eng = Engine(ledger=self.led, equity=equity, on_halt=on_halt)
        self.eng.broker.fail_place_stop = fail_place_stop
        self.eng.volwin.buf.extend([50.0] * 60)  # 量分位窗口预热（min_n=40）
        self.t = D + 13 * 3600 + 45 * 60         # 13:45 UTC

    def feed(self, o: float, h: float, l: float, c: float, v: float = 50.0):
        ts = self.t
        self.t += 60
        return self.eng.on_1m_close(Bar(ts, o, h, l, c, v))

    def prep(self, n: int = 15) -> None:
        for _ in range(n):
            self.feed(1697.0, 1698.0, 1694.0, 1696.0)

    def inject(self) -> None:
        """HTF 收盘后注入指标/zone（ATR/ADX 带内部种子，防后续收盘 wipe 为 None）。"""
        a5 = self.eng.tf[300]["atr"]
        a5.v, a5.rma.v, a5.prev_close = 20.0, 20.0, 1696.0
        a15 = self.eng.tf[900]["atr"]
        a15.v, a15.rma.v, a15.prev_close = 40.0, 40.0, 1694.0
        adx = self.eng.tf[900]["adx"]
        adx.v, adx.adx = 30.0, 30.0
        self.eng.tf[1800]["rank"] = 2
        self.eng.tf[900]["rank"] = 1
        self.eng.tf[300]["rank"] = 1
        self.eng.regime = "FULL"
        self.eng.zones = [ZS, ZR]

    def signal(self):
        return self.feed(1706.0, 1708.0, 1695.0, 1706.5, 80.0)

    def fill(self):
        return self.feed(1707.0, 1710.0, 1699.0, 1700.5)

    def dump(self):
        return self.feed(1704.0, 1706.0, 1674.0, 1680.0)

    def flat(self, n: int = 1):
        r = None
        for _ in range(n):
            r = self.feed(1704.0, 1707.0, 1701.0, 1705.0)
        return r

    def push(self, n: int = 1):
        r = None
        for _ in range(n):
            r = self.feed(1697.0, 1699.0, 1694.0, 1696.0)
        return r


def ts_of(clock: str) -> int:
    """UTC 当日时刻 'HH:MM' → 绝对秒（场景日 D）。"""
    hh, mm = (int(x) for x in clock.split(":"))
    return D + hh * 3600 + mm * 60
