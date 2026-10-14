"""MRS T10（规格书 §10 / §6.3-D）：执行层幂等与重复事件无副作用。

口径：同一 sig_id 单组恒存（open 不复活/不新建）；重复触发事件对已成交订单无副作用；
cancel 幂等；TTL 边界 = sig 收盘 +120s（§5.6）。
用法：.venv/Scripts/python.exe backend/scripts/verify_mrs_t10.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.mrs.broker import PaperBroker
from app.mrs.config import CFG
from app.mrs.ledger import Ledger
from app.mrs.plan import Bundle
from app.mrs.types import Bar

T0 = 43_200


def bundle(e_sig="s1", dir="LONG", trigger=65000.0, stop=64900.0, tp=65220.0) -> Bundle:
    return Bundle(e_sig=e_sig, now_ts=T0, dir=dir, zone_id="z", trigger=trigger,
                  stop=stop, tp=tp, qty=1.0, R=100.0, seg1_px=65120.0,
                  be_px_target=65050.0, leverage=3, fee_bps=5.0, slip_bps=2.0)


def mkbr() -> tuple[PaperBroker, Ledger]:
    led = Ledger()
    return PaperBroker(fee_bps=5.0, slip_bps=2.0, ledger=led, cfg=CFG), led


def entry_lifecycle() -> None:
    br, led = mkbr()
    b = bundle()
    g1 = br.open(b, ts=T0)
    for _ in range(10):
        assert br.open(b, ts=T0) is g1, "同 sig_id open 应返回同组对象"
    assert len(led.orders) == 1, "重复 open 不得新建订单"
    # 未成交：重复喂不触价的 bar 无副作用
    miss = Bar(T0 + 60, 64_900.0, 64_990.0, 64_890.0, 64_920.0, 1.0)
    for _ in range(10):
        assert br.on_pending_bar(g1, miss) is None
    # TTL 边界：sig 收盘（T0+60）前未过期、到点过期（bar[n+2] 口径 = +120s）
    assert br.ttl_expired(g1, Bar(T0, 65_000, 65_000, 65_000, 65_000, 1.0)) is False
    assert br.ttl_expired(g1, Bar(T0 + 60, 65_000, 65_000, 65_000, 65_000, 1.0)) is True
    # 触发：bar[n+1] 触价成交（不利滑点 2bp）
    hit = Bar(T0 + 60, 65_000.0, 65_010.0, 64_990.0, 65_005.0, 1.0)
    f = br.on_pending_bar(g1, hit)
    assert f is not None and f.kind == "entry" and f.qty == 1.0
    assert abs(f.px - 65013.0) < 1e-9, f.px  # 65000×(1+2bp)
    assert abs(f.fee_usdt - 32.5065) < 1e-9, f.fee_usdt
    assert g1.stage == "PROTECTED" and g1.protect_fills == 1
    for _ in range(10):  # 已成交后重复触发事件 → None（无副作用）
        assert br.on_pending_bar(g1, hit) is None
    assert len(led.orders) == 4, "TRIGGER SENT/FILLED + TP/STOP PLACED 共 4 条"
    assert all(r["client_oid"] == "s1" for r in led.orders), "clientOrderId 恒 sig_id"
    print("  [开单/成交] open×10 同组、漏空×10、TTL 边界、成交后重复事件无副作用通过 ✅")

    # 改单幂等：重复 amend 仅改价与记单，不产生副作用
    br.amend_stop("s1", 65_100.0, T0 + 120)
    for _ in range(9):
        br.amend_stop("s1", 65_100.0, T0 + 120)
    assert g1.stop_px == 65_100.0 and len(led.orders) == 14
    # 止损成交一次；重复命中不再成交
    stop_hit = Bar(T0 + 120, 65_090.0, 65_095.0, 65_070.0, 65_090.0, 1.0)
    f2 = br.fill_stop(g1, stop_hit, be_done=False)
    assert f2 is not None and f2.reason == "STOP"
    assert abs(f2.px - 65086.98) < 1e-9, f2.px  # 65100×(1−2bp)
    for _ in range(10):
        assert br.fill_stop(g1, stop_hit, be_done=False) is None
    print("  [改单/止损] amend×10 幂等、止损成交一次且重复命中无副作用通过 ✅")


def tp_lifecycle() -> None:
    br, led = mkbr()
    b = bundle(e_sig="s2")
    g = br.open(b, ts=T0)
    # 未成交阶段：fill_tp / fill_stop 均不动作（stage≠PROTECTED）
    bar1 = Bar(T0 + 60, 64_990.0, 64_995.0, 64_980.0, 64_990.0, 1.0)
    assert br.fill_tp(g, bar1) is None and br.fill_stop(g, bar1, False) is None
    f0 = br.on_pending_bar(g, Bar(T0 + 60, 65_000.0, 65_010.0, 64_990.0, 65_005.0, 1.0))
    assert f0 is not None and br.get("s2") is g
    tp_bar = Bar(T0 + 120, 65_100.0, 65_230.0, 65_090.0, 65_225.0, 1.0)
    f = br.fill_tp(g, tp_bar)
    assert f is not None and f.reason == "TP" and abs(f.px - 65220.0) < 1e-9
    for _ in range(10):
        assert br.fill_tp(g, tp_bar) is None, "止盈成交一次，重复事件不重复成交"
    print("  [止盈] 限价按挂单价成交一次、重复事件无副作用通过 ✅")


def cancel_idempotent() -> None:
    br, led = mkbr()
    b = bundle(e_sig="s3")
    g = br.open(b, ts=T0)
    g2 = br.cancel("s3", T0 + 60, "TTL")
    assert g2 is g and g.stage == "CANCELLED"
    n = len(led.orders)
    for _ in range(10):
        br.cancel("s3", T0 + 60, "TTL")
    assert len(led.orders) == n, "重复 cancel 不得新增订单记录"
    # 已撤单组：open 不复活、不新建（同一 sig_id 单组恒存）
    assert br.open(b, ts=T0 + 120) is g and g.stage == "CANCELLED"
    assert br.on_pending_bar(g, Bar(T0 + 120, 65_000, 65_010, 64_990, 65_005, 1.0)) is None
    print("  [撤单] cancel×10 幂等、已撤组 open 不复活/不新建通过 ✅")


if __name__ == "__main__":
    entry_lifecycle()
    tp_lifecycle()
    cancel_idempotent()
    print("T10 全部通过 ✅")
