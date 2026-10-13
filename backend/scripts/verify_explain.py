"""P2-5 A4 解释文案验证：
① mock 合法输出：reason 返回、task=explain、json_mode 传参、上下文含三票行（含缺席原因）；
② 超长输出截断至 200 字；③ 违禁表述（确定性承诺/诱导）→ None；
④ 非法 JSON / LlmError → None（降级不阻塞）；⑤ 空 key（真实配置）→ None；
⑥ 卡片渲染：三票行 + AI 研判行；无 votes/reason 时兼容原版式；
⑦ 真实 LLM（key 非空）→ 真实理由；空则 SKIP。
用法：.venv/Scripts/python.exe backend/scripts/verify_explain.py
"""
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.llm import gateway
from app.adapters.notify.wecom import render_signal_md
from app.core.config import settings
from app.engine import explain

FAIL: list[str] = []

SIG = {"signal_id": 1, "rule": "sma_520_rsi_1h_BTC/USDT", "symbol": "BTC/USDT",
       "exchange": "okx", "contract_type": "spot", "timeframe": "1h",
       "ts": datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc), "direction": "entry",
       "price": 61000.0, "price_zone_low": 60500.0, "price_zone_high": 61500.0,
       "atr": 500.0,
       "votes": {"tech": (0.75, {"rsi14": 55.2, "vol_ratio": 1.3, "atr_pct": 0.008}),
                 "flow": (0.62, {"funding_rate": -0.00005, "oi_change_24h": 0.031}),
                 "info": (None, {"window_h": 72, "n_events": 0, "skip": "窗口内无相关事件"})}}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _resp(text: str):
    return gateway.LlmResult(text, "mock-explain")


def main() -> None:
    calls: list[dict] = []

    def fake(task, messages, **kw):
        calls.append({"task": task, "messages": messages, "kw": kw})
        return _resp('{"reason":"MA5 上穿 MA20 且量能温和放大，技术面偏强；'
                     '资金面 funding 微负显示多头拥挤度低。风险：量能萎缩可能假突破。"}')

    # ---------- ① 合法输出 ----------
    print("① mock 合法输出")
    with mock.patch.object(gateway, "complete", fake):
        reason = explain.build_reason(SIG)
    check("返回理由文本", bool(reason and "MA" in reason), (reason or "")[:50])
    check("task=explain 且 json_mode", calls[0]["task"] == "explain"
          and calls[0]["kw"].get("json_mode") is True)
    user = calls[0]["messages"][1]["content"]
    check("上下文含三票行（含缺席原因）",
          "技术票：0.75" in user and "资金面票：0.62" in user and "信息票：缺席" in user,
          user.replace("\n", " | ")[:120])

    # ---------- ② 超长截断 ----------
    print("② 超长输出截断")
    with mock.patch.object(gateway, "complete",
                           lambda *a, **k: _resp('{"reason":"' + "测" * 250 + '"}')):
        reason = explain.build_reason(SIG)
    check("截断至 200 字", reason is not None and len(reason) == explain.MAX_LEN,
          f"len={len(reason) if reason else None}")

    # ---------- ③ 违禁表述 ----------
    print("③ 违禁表述拒绝")
    for bad in ("该币保证涨到 10 万", "可以稳赚，建议梭哈"):
        with mock.patch.object(gateway, "complete",
                               lambda *a, _b=bad, **k: _resp('{"reason":"' + _b + '"}')):
            r = explain.build_reason(SIG)
        check(f"违禁拒绝：{bad[:12]}…", r is None)

    # ---------- ④ 非法 JSON / LlmError ----------
    print("④ 解析失败与传输失败降级")
    with mock.patch.object(gateway, "complete", lambda *a, **k: _resp("不是JSON")):
        r = explain.build_reason(SIG)
    check("非法 JSON → None", r is None)
    with mock.patch.object(gateway, "complete",
                           side_effect=gateway.LlmError("mock 全链失败")):
        r = explain.build_reason(SIG)
    check("LlmError → None", r is None)

    # ---------- ⑤ 真实配置（空 key）----------
    print("⑤ 空 key 降级（真实配置）")
    if settings.llm_api_key:
        print("  [SKIP] LLM_API_KEY 已配置，空 key 场景由 mock 覆盖")
    else:
        r = explain.build_reason(SIG)
        check("空 key → None（降级纯信号推送）", r is None)

    # ---------- ⑥ 卡片渲染 ----------
    print("⑥ 企业微信卡片渲染")
    s = {**SIG, "reason": "MA5 上穿 MA20，量能配合，技术面偏强。"}
    md = render_signal_md(s)
    check("含三票强度行", "> 三票：技术 0.75 · 资金面 0.62 · 信息 —" in md)
    check("含 AI 研判行", "> AI 研判：MA5 上穿 MA20" in md)
    check("保留免责声明", "非投资建议" in md)
    s_src = {**s, "votes": {**SIG["votes"], "info": (
        0.7, {"n_events": 2, "score": 0.4,
              "top": [{"title": "SEC 批准 X 现货 ETF",
                       "url": "https://example.com/n1"}]})}}
    check("含溯源链接行（信息票 top 命中）",
          "> 溯源：[SEC 批准 X 现货 ETF](https://example.com/n1)" in render_signal_md(s_src))
    check("无 top 命中无溯源行", "溯源" not in md)
    old = {k: v for k, v in SIG.items() if k not in ("votes",)}
    md_old = render_signal_md(old)
    check("无 votes/reason 兼容原版式",
          "三票" not in md_old and "AI 研判" not in md_old and "溯源" not in md_old)

    # ---------- ⑦ 真实 LLM ----------
    print("⑦ 真实 LLM 解释（百炼）")
    if not settings.llm_api_key:
        print("  [SKIP] LLM_API_KEY 未配置——填入 key 后重跑可测真实解释")
    else:
        r = explain.build_reason(SIG)
        check("真实理由非空且 ≤200 字", bool(r) and len(r) <= explain.MAX_LEN,
              (r or "降级 None")[:80])

    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
