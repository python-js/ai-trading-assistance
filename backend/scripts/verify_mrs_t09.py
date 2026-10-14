"""MRS T09（规格书 §10 / §6.4）：manage_step 顺序与时序数值算例。

fixture：entry=65000、R=100、stop=64900、tp=65220、be=65050、atr5=40
→ trail 阈值 = 1.2×40 = 48；uR≥1 且回撤≥48 才 TRAIL_ATR。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t09.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.manage import Amend, Exit, PartialExit, Position, manage_step
from app.mrs.types import Bar

T0 = 43_200


def bar(o, h, l, c) -> Bar:
    return Bar(T0, o, h, l, c, 1.0)


def mkpos(*, dir="LONG", entry=65000.0, R=100.0, seg1=65120.0, be=65050.0,
          stop=64900.0, tp=65220.0, peak=None, bars=0) -> Position:
    return Position(sig_id="s", dir=dir, open_ts=T0, entry=entry, qty0=1.0, qty=1.0,
                    R=R, stop=stop, tp=tp, seg1_px=seg1, be_px_target=be,
                    zone_id="z", lev=3, peak=entry if peak is None else peak,
                    bars_held=bars)


def trail_atr() -> None:
    # 正例：bar1 冲高 → BE；bar2 回撤 80≥48 且 uR 1.1≥1 → TRAIL_ATR 收盘价离场
    p = mkpos(seg1=65300.0)
    acts = manage_step(p, bar(65120, 65170, 65100, 65160), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Amend(65050.0, "BE")], acts
    assert p.be_done and p.mfe_r == 1.7 and p.peak == 65160.0
    p.stop = 65050.0  # 模拟发动机依 Amend 改价
    acts = manage_step(p, bar(65110, 65165, 65080, 65110), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Exit("TRAIL_ATR", 65110.0)], acts
    # 负例 1：回撤 10 < 48 → 不动
    p2 = mkpos(seg1=65300.0, peak=65160.0)
    assert manage_step(p2, bar(65110, 65155, 65150, 65110), atr5=40.0,
                       ma5m=None, is_5m_close=False) == []
    # 负例 2：回撤 80 达标但 uR 0.9 < 1 → 不动
    p3 = mkpos(seg1=65300.0, peak=65160.0)
    assert manage_step(p3, bar(65095, 65155, 65080, 65090), atr5=40.0,
                       ma5m=None, is_5m_close=False) == []
    print("  [TRAIL_ATR] 峰值回撤 80≥48 且 uR≥1 离场 + 2 负例（回撤不足/uR 不足）通过 ✅")


def seg1() -> None:
    # SEG1：+1.2R 触及即部分止盈（限价原价成交）、只挂一次；随后 TRAIL 记 SEG1_THEN_TRAIL
    p = mkpos()
    acts = manage_step(p, bar(65100, 65170, 65100, 65160), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Amend(65050.0, "BE"), PartialExit(65120.0, 0.5, "SEG1")], acts
    assert p.be_done and p.seg_done
    p.stop = 65050.0
    acts = manage_step(p, bar(65110, 65165, 65080, 65110), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Exit("SEG1_THEN_TRAIL", 65110.0)], acts
    # MA 变体：回撤不足（45<48）但 5m 收盘破 MA → 同样记 SEG1_THEN_TRAIL
    p2 = mkpos(seg1=65300.0, peak=65160.0)
    p2.seg_done = True
    acts = manage_step(p2, bar(65100, 65155, 65115, 65100), atr5=40.0,
                       ma5m=65130.0, is_5m_close=True)
    assert acts == [Exit("SEG1_THEN_TRAIL", 65100.0)], acts
    # 负例：未触及 seg1 价 → 无 PartialExit（高 65110 < 65120）
    p3 = mkpos()
    acts = manage_step(p3, bar(65060, 65110, 65040, 65070), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [], acts
    print("  [SEG1] +1.2R 部分止盈/单次/SEG1_THEN_TRAIL（ATR 与 MA 两径）+ 负例通过 ✅")


def trail_ma() -> None:
    p = mkpos()
    acts = manage_step(p, bar(65040, 65060, 65030, 65050), atr5=40.0,
                       ma5m=65080.0, is_5m_close=True)
    assert acts == [Exit("TRAIL_MA", 65050.0)], acts
    # 负例 1：非 5m 收盘
    p2 = mkpos()
    assert manage_step(p2, bar(65040, 65060, 65030, 65050), atr5=40.0,
                       ma5m=65080.0, is_5m_close=False) == []
    # 负例 2：收盘站上 MA
    p3 = mkpos()
    assert manage_step(p3, bar(65040, 65095, 65030, 65090), atr5=40.0,
                       ma5m=65080.0, is_5m_close=True) == []
    # 负例 3：MA 值缺失（样本不足）
    p4 = mkpos()
    assert manage_step(p4, bar(65040, 65060, 65030, 65050), atr5=40.0,
                       ma5m=None, is_5m_close=True) == []
    print("  [TRAIL_MA] 5m 收盘破 MA20 离场 + 3 负例（非收盘/站上/无值）通过 ✅")


def time_stop() -> None:
    p = mkpos(seg1=65300.0, bars=59)
    acts = manage_step(p, bar(65000, 65030, 64970, 65010), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Exit("TIME_STOP", 65010.0)], acts
    assert p.bars_held == 60 and p.mfe_r == 0.3
    # 负例：mfe 0.9 ≥ 0.8 → 不放行
    p2 = mkpos(seg1=65300.0, bars=59)
    assert manage_step(p2, bar(65000, 65090, 64970, 65010), atr5=40.0,
                       ma5m=None, is_5m_close=False) == []
    # 顺序：同 bar 同时满足 TIME 与 TRAIL_MA → TIME 优先（§6.4 顺序 1→2）
    p3 = mkpos(seg1=65300.0, bars=59)
    acts = manage_step(p3, bar(65000, 65030, 64970, 65010), atr5=40.0,
                       ma5m=65050.0, is_5m_close=True)
    assert acts == [Exit("TIME_STOP", 65010.0)], acts
    print("  [TIME] 60 根且 mfe<0.8 离场 + 负例（mfe 达标）+ TIME 优先于 MA 通过 ✅")


def be_once() -> None:
    p = mkpos(seg1=65300.0)
    acts = manage_step(p, bar(65060, 65170, 65040, 65170), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Amend(65050.0, "BE")] and p.be_done
    # 第二次 uR≥1.5 不再重复改单（一次性）
    acts = manage_step(p, bar(65120, 65175, 65130, 65150), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [] and p.be_done
    print("  [BE] uR≥1.5 一次性改单（第二次不重复）通过 ✅")


def short_mirror() -> None:
    p = mkpos(dir="SHORT", seg1=64700.0, be=64950.0, stop=65100.0, tp=64780.0)
    acts = manage_step(p, bar(64990, 64960, 64830, 64840), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Amend(64950.0, "BE")], acts
    assert abs(p.mfe_r - 1.7) < 1e-9, p.mfe_r
    p.stop = 64950.0
    acts = manage_step(p, bar(64890, 64920, 64885, 64880), atr5=40.0,
                       ma5m=None, is_5m_close=False)
    assert acts == [Exit("TRAIL_ATR", 64880.0)], acts
    print("  [SHORT] 镜像 BE/追踪（回撤 64920−64840=80≥48、uR 1.2）通过 ✅")


if __name__ == "__main__":
    trail_atr()
    seg1()
    trail_ma()
    time_stop()
    be_once()
    short_mirror()
    print("T09 全部通过 ✅")
