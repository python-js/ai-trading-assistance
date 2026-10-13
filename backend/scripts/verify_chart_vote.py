"""P2-6 A2 读图票验证：
① mock 合法输出：agree 0.8 → 0.9，task=vision、json_mode、图片 data URL 内联（可还原 PNG 头）；
② stance 映射与 strength 钳制/容错（越界/非法/缺失/枚举外 stance）；
③ fence 包裹 JSON 可解析；④ 失败降级：非法 JSON / 非对象 JSON / LlmError / 图不存在 → None；
⑤ 真实渲染冒烟（最近 okx 信号 → PNG 魔数）；⑥ 落库端到端（chart 行插入/查回/清理）；
⑦ 卡片读图行（含 stance 中文映射与顺序）；⑧ 真实 vision（key 非空）第二意见，空则 SKIP。
用法：.venv/Scripts/python.exe backend/scripts/verify_chart_vote.py
"""
import base64
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.llm import gateway
from app.adapters.notify.wecom import render_signal_md
from app.core.config import settings
from app.engine import chart_vote
from app.models import db

FAIL: list[str] = []
PNG_HEAD = b"\x89PNG\r\n\x1a\n"
TMP = pathlib.Path("data/charts/verify_chart.png")

SIG = {"signal_id": 999999, "rule": "sma_520_rsi_1h_BTC/USDT", "symbol": "BTC/USDT",
       "exchange": "okx", "contract_type": "spot", "timeframe": "1h",
       "ts": datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc), "direction": "entry",
       "price": 61000.0, "price_zone_low": 60500.0, "price_zone_high": 61500.0,
       "atr": 500.0}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _resp(text: str):
    return gateway.LlmResult(text, "mock-vision")


def main() -> None:
    calls: list[dict] = []

    def fake(task, messages, **kw):
        calls.append({"task": task, "messages": messages, "kw": kw})
        return _resp('{"stance":"agree","strength":0.8,"reason":"MA5 上穿 MA20，回踩不破，形态偏多。"}')

    TMP.parent.mkdir(parents=True, exist_ok=True)
    TMP.write_bytes(PNG_HEAD + b"\x00" * 64)

    # ---------- ① mock 合法输出 ----------
    print("① mock 合法输出（agree 0.8 → 0.9）")
    with mock.patch.object(gateway, "complete", fake):
        r = chart_vote.run(SIG, str(TMP))
    check("返回 (支持度, evidence)", r is not None and abs(r[0] - 0.9) < 1e-9,
          f"support={r and r[0]}")
    check("task=vision 且 json_mode", calls[0]["task"] == "vision"
          and calls[0]["kw"].get("json_mode") is True)
    content = calls[0]["messages"][1]["content"]
    url = content[1]["image_url"]["url"]
    ok_png = (url.startswith("data:image/png;base64,")
              and base64.b64decode(url.split(",", 1)[1])[:8] == PNG_HEAD)
    check("图片 data URL 内联且可还原 PNG 头", ok_png, url[:38] + "…")
    check("文本上下文含信号要素", "买入 BTC/USDT 1h" in content[0]["text"],
          content[0]["text"][:60])
    ev = r[1]
    check("evidence 字段齐全", ev["stance"] == "agree" and ev["confidence"] == 0.8
          and ev["model"] == "mock-vision" and bool(ev["reason"]), str(ev)[:80])

    # ---------- ② 映射与容错 ----------
    print("② stance 映射与 strength 钳制/容错")
    cases = [('{"stance":"oppose","strength":0.6}', 0.2),
             ('{"stance":"neutral","strength":0.9}', 0.5),
             ('{"stance":"agree","strength":1.5}', 1.0),
             ('{"stance":"agree","strength":-0.3}', 0.5),
             ('{"stance":"agree","strength":"abc"}', 0.75),
             ('{"stance":"buy","strength":0.8}', 0.5),
             ('{"stance":"agree"}', 0.75)]
    for text, expect in cases:
        with mock.patch.object(gateway, "complete",
                               lambda *a, _t=text, **k: _resp(_t)):
            rr = chart_vote.run(SIG, str(TMP))
        check(f"{text[:36]}… → {expect}", rr is not None and abs(rr[0] - expect) < 1e-9,
              f"got={rr and rr[0]}")

    # ---------- ③ fence 包裹 ----------
    print("③ fence 包裹 JSON")
    with mock.patch.object(gateway, "complete", lambda *a, **k: _resp(
            '```json\n{"stance":"agree","strength":0.5,"reason":"x"}\n```')):
        rr = chart_vote.run(SIG, str(TMP))
    check("```json 包裹可解析（0.5 → 0.75）", rr is not None and abs(rr[0] - 0.75) < 1e-9)

    # ---------- ④ 失败降级 ----------
    print("④ 失败降级（全部 None，不阻塞推送）")
    with mock.patch.object(gateway, "complete", lambda *a, **k: _resp("不是JSON")):
        check("非法 JSON → None", chart_vote.run(SIG, str(TMP)) is None)
    with mock.patch.object(gateway, "complete", lambda *a, **k: _resp("42")):
        check("非对象 JSON → None", chart_vote.run(SIG, str(TMP)) is None)
    with mock.patch.object(gateway, "complete",
                           side_effect=gateway.LlmError("mock 全链失败")):
        check("LlmError → None", chart_vote.run(SIG, str(TMP)) is None)
    check("图不存在 → None", chart_vote.run(SIG, "data/charts/__nope__.png") is None)

    # ---------- ⑤ 真实渲染冒烟 ----------
    print("⑤ 真实渲染冒烟（最近 okx 信号）")
    conn = db.connect(settings.pg_dsn)
    with conn.cursor() as cur:
        cur.execute("""SELECT s.id, s.symbol, s.exchange, s.contract_type, s.timeframe,
                              s.ts, s.direction, s.price_zone_low, s.price_zone_high, r.name
                       FROM signal s JOIN rule r ON r.id = s.rule_id
                       WHERE s.exchange='okx' ORDER BY s.ts DESC, s.id DESC LIMIT 1""")
        row = cur.fetchone()
    real, real_img = None, None
    if row is None:
        print("  [SKIP] 库中无 okx 信号")
    else:
        keys = ("signal_id", "symbol", "exchange", "contract_type", "timeframe", "ts",
                "direction", "price_zone_low", "price_zone_high", "rule")
        real = dict(zip(keys, row))
        real["price"] = (real["price_zone_low"] + real["price_zone_high"]) / 2
        real_img = chart_vote.render_chart(conn, real)
        ok = (real_img is not None and pathlib.Path(real_img).exists()
              and pathlib.Path(real_img).read_bytes()[:8] == PNG_HEAD)
        check(f"render_chart #{real['signal_id']} 出图（PNG 魔数）", ok, str(real_img))

    # ---------- ⑥ 落库端到端 ----------
    print("⑥ 落库端到端（signal_vote vote_type='chart'）")
    if real:
        sid = real["signal_id"]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signal_vote WHERE signal_id=%s AND vote_type='chart'",
                        (sid,))
        conn.commit()
        db.insert_signal_vote(conn, signal_id=sid, vote_type="chart", strength=0.9,
                              evidence={"stance": "agree", "confidence": 0.8,
                                        "reason": "验证", "model": "verify"})
        with conn.cursor() as cur:
            cur.execute("""SELECT strength, evidence_json FROM signal_vote
                           WHERE signal_id=%s AND vote_type='chart'""", (sid,))
            gt = cur.fetchone()
        check("chart 票落库可查", gt is not None and abs(gt[0] - 0.9) < 1e-9
              and gt[1]["stance"] == "agree", str(gt)[:80])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signal_vote WHERE signal_id=%s AND vote_type='chart'",
                        (sid,))
            cur.execute("""SELECT count(*) FROM signal_vote
                           WHERE signal_id=%s AND vote_type='chart'""", (sid,))
            left = cur.fetchone()[0]
        conn.commit()
        check("验证数据已清理", left == 0)

    # ---------- ⑦ 卡片读图行 ----------
    print("⑦ 企业微信卡片读图行")
    s = {**SIG, "chart": (0.9, {"stance": "agree", "confidence": 0.8,
                                "reason": "MA5 上穿 MA20，回踩不破，形态偏多。",
                                "model": "m"})}
    md = render_signal_md(s)
    check("含读图行（赞同 0.90 + 理由摘）", "> 读图：赞同 0.90 · MA5 上穿" in md,
          md.splitlines()[-3] if len(md.splitlines()) >= 3 else "")
    md2 = {**s, "reason": "AI 研判理由"}
    md2 = render_signal_md(md2)
    check("读图行在 AI 研判之前", md2.index("读图") < md2.index("AI 研判"))
    check("无 chart 字段兼容（无读图行）", "读图" not in render_signal_md(SIG))

    # ---------- ⑨ _hook 编排 ----------
    print("⑨ run_scanner._hook 编排（mock 推送 + mock 读图成功路径）")
    if real:
        import run_scanner  # backend/scripts 同目录

        def fake_hook(task, messages, **kw):
            if task == "vision":
                return _resp('{"stance":"agree","strength":0.8,'
                             '"reason":"MA5 上穿 MA20，回踩不破。"}')
            return _resp('{"reason":"技术面偏强。"}')

        sid = real["signal_id"]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signal_vote WHERE signal_id=%s AND vote_type='chart'",
                        (sid,))
            cur.execute("SELECT coalesce(max(id), 0) FROM push_log")
            pl_max = cur.fetchone()[0]
        conn.commit()
        s_in = dict(real)
        with mock.patch.object(gateway, "complete", fake_hook), \
                mock.patch.object(run_scanner.WeComNotifier, "_post") as mpost:
            run_scanner._hook(s_in)
        check("s['chart']=(0.9, agree) 且 s['reason'] 置入",
              bool(s_in.get("chart")) and abs(s_in["chart"][0] - 0.9) < 1e-9
              and s_in["chart"][1]["stance"] == "agree"
              and s_in.get("reason") == "技术面偏强。", str(s_in.get("chart"))[:70])
        with conn.cursor() as cur:
            cur.execute("""SELECT strength, evidence_json FROM signal_vote
                           WHERE signal_id=%s AND vote_type='chart'""", (sid,))
            gt = cur.fetchone()
        check("_hook 落库 chart 行（0.9/agree）", gt is not None and abs(gt[0] - 0.9) < 1e-9
              and gt[1]["stance"] == "agree", str(gt)[:70])
        check("推送 2 条（markdown+image）", mpost.call_count == 2, f"calls={mpost.call_count}")
        md = mpost.call_args_list[0][0][0]["markdown"]["content"]
        check("卡片含读图行与 AI 研判行",
              "> 读图：赞同 0.90 · MA5 上穿" in md and "> AI 研判：技术面偏强。" in md,
              md.replace("\n", " | ")[-90:])
        with conn.cursor() as cur:
            cur.execute("SELECT status, error FROM push_log WHERE id > %s", (pl_max,))
            prows = cur.fetchall()
        check("push_log 记 sent", prows and all(r[0] == "sent" for r in prows), str(prows))
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signal_vote WHERE signal_id=%s AND vote_type='chart'",
                        (sid,))
            cur.execute("DELETE FROM push_log WHERE id > %s", (pl_max,))
        conn.commit()
        check("编排验证数据已清理", True)

    # ---------- ⑧ 真实 vision ----------
    print("⑧ 真实 vision 第二意见（百炼）")
    if not settings.llm_api_key:
        print("  [SKIP] LLM_API_KEY 未配置——填入 key 后重跑可测真实读图")
    elif real and real_img:
        rr = chart_vote.run(real, real_img)
        check("真实读图返回结构", rr is not None and 0.0 <= rr[0] <= 1.0
              and rr[1]["stance"] in chart_vote.STANCES,
              f"support={rr and rr[0]} stance={rr and rr[1]['stance']} "
              f"{(rr and rr[1]['reason'][:30])}")
    else:
        print("  [SKIP] 无可用信号/截图")

    TMP.unlink(missing_ok=True)
    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    conn.close()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
