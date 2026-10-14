"""M5/§9.2 funding 结算验证（正例 + 空头镜像 + 缺数据；T12 仅覆盖"无仓不结算"边界）。

- A 集成：Rig 开仓（14:01）后连续喂 119 根平盘 bar 至 16:00（UTC 结算点，ts%28800==0）——
  价格路径 close=1737.5 恒定：mfe=0.924≥0.8（抑制 TIME 止损）/uR=0.908<1（不触 TRAIL_ATR）/
  close≥5m MA（不触 TRAIL_MA）/远于 SEG1·TP·BE·stop。结算走 on_1m_close 真实分支：
  funding_paid=rate×qty×bar.open、equity 同步扣减、imputed=0。
- B 空头镜像（白盒单点 _settle_funding）：dir=SHORT → 正费率收 funding（equity 增加）。
- C 缺数据：funding_fn=None → rate 按 0 计提 + imputed+1（§9.2 标记）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_funding.py
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))       # scripts/
sys.path.insert(0, str(_HERE.parents[1]))   # backend/

from app.mrs.types import Bar  # noqa: E402
from mrs_rig import Rig, ts_of  # noqa: E402

RATE = 0.0001


def case_a() -> Rig:
    rig = Rig()
    rig.prep()
    rig.inject()
    assert rig.signal() == ("OK", None)
    assert rig.fill() == ("BUSY", None)
    qty = rig.led.signals[0]["bundle"]["qty"]
    rig.eng.funding_fn = lambda ts: RATE
    for _ in range(119):  # 14:02 → 16:00（含 16:00 结算 bar）
        rig.feed(1737.5, 1738.0, 1736.5, 1737.5, 50.0)
    assert rig.eng.pos is not None, "持仓被意外离场（测试路径设计失效）"
    amt = RATE * qty * 1737.5
    assert abs(rig.eng.pos.funding_paid - amt) < 1e-9, rig.eng.pos.funding_paid
    assert abs(rig.eng.equity - (50_000.0 - amt)) < 1e-9
    assert rig.eng.funding_imputed == 0 and rig.eng.pos.bars_held == 119
    print(f"  [A] 16:00 真实结算：funding_paid={amt:.6f}（=1bp×{qty}×1737.5）、"
          f"equity 同步扣减、持仓在管 通过 ✅")
    return rig


def case_bc(rig: Rig) -> None:
    eng = rig.eng
    qty = eng.pos.qty
    eng.pos.dir = "SHORT"  # B 空头镜像（白盒单点）
    p0, e0 = eng.pos.funding_paid, eng.equity
    eng._settle_funding(Bar(ts_of("16:08"), 1737.5, 1738.0, 1736.5, 1737.5, 50.0))
    amt = -RATE * qty * 1737.5
    assert abs(eng.pos.funding_paid - (p0 + amt)) < 1e-12
    assert abs(eng.equity - (e0 - amt)) < 1e-12, "空头收 funding → equity 增加"
    p1, im0 = eng.pos.funding_paid, eng.funding_imputed  # C 缺数据
    eng.funding_fn = None
    eng._settle_funding(Bar(ts_of("16:16"), 1737.5, 1738.0, 1736.5, 1737.5, 50.0))
    assert eng.funding_imputed == im0 + 1 and eng.pos.funding_paid == p1
    print("  [B] SHORT 镜像：正费率结算 equity 增加 通过 ✅")
    print("  [C] 缺数据：rate=0 计提 + imputed+1 通过 ✅")


if __name__ == "__main__":
    case_bc(case_a())
    print("funding 结算验证 全部通过 ✅")
