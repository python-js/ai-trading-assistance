"""P2-3 A1 信息挖掘验证：
① RSS 抓取冒烟（真实网络，4 源各 ≥1 条，字段完整）；
② 入库去重（collect 真实入库 + 已存在 url 重插全为 None）；
③ PIT（全库 published_ts ≤ now+5min 容源钟差；无链接/无时间条目被拒）；
④ 解析单测（mock LLM：schema 规范化/钳制/坏 JSON→ValueError/json_mode 传参/模型归因）；
⑤ DB 管道隔离（source='verify'：OK 条落事件+置 processed；坏 JSON 条 0 事件仍置位；
   LLM 传输失败条不置位；空事件条置位）；
⑥ 真实 LLM 结构化（LLM_API_KEY 非空时对真实条目跑一条；空则 SKIP）。
用法：.venv/Scripts/python.exe backend/scripts/verify_news.py
"""
import pathlib
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.llm import gateway
from app.adapters.news import rss
from app.core.config import settings
from app.engine import news
from app.models import db

FAIL: list[str] = []
PROBE_LIMIT = 120  # 单源抓取上限（防异常巨 feed）


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _raw_state(conn, rid: int):
    with conn.cursor() as cur:
        cur.execute("SELECT processed_ts FROM news_raw WHERE id=%s", (rid,))
        pt = cur.fetchone()[0]
        cur.execute("""SELECT symbols, event_type, sentiment, impact_dir, magnitude,
                              confidence, extracted_by_model
                       FROM news_event WHERE news_id=%s ORDER BY id""", (rid,))
        evs = cur.fetchall()
    return pt, evs


def _ins_verify(conn, tag: str, title: str) -> int:
    rid = db.insert_news_raw_if_new(
        conn, source="verify", url=f"https://verify.local/{int(time.time() * 1000)}-{tag}",
        title=title, content=f"测试正文 {tag}",
        published_ts=datetime.now(timezone.utc) - timedelta(seconds=5))
    return rid


def _ok_resp(text: str, model: str = "mock-model"):
    return gateway.LlmResult(text, model)


def _fake_for_item(item: dict):
    """按标题关键字返回不同 mock 输出。"""
    t = item["title"]
    if "VERIFY-OK" in t:
        return _ok_resp('{"events":[{"symbols":["BTC"],"event_type":"etf",'
                        '"sentiment":1.5,"impact_dir":"bullish","magnitude":-0.2,'
                        '"confidence":0.9}]}', "mock-fallback-model")
    if "VERIFY-BAD" in t:
        return _ok_resp("这不是JSON")
    return _ok_resp('{"events":[]}')


def main() -> None:
    conn = db.connect(settings.pg_dsn)

    # ---------- ① RSS 抓取冒烟 ----------
    print("① RSS 抓取冒烟（真实网络）")
    for src in rss.SOURCES:
        try:
            items = rss.fetch(src, proxy=settings.okx_proxy or None)[:PROBE_LIMIT]
        except Exception as e:
            check(f"{src['name']} 抓取", False, f"{type(e).__name__}: {e}")
            continue
        fields = all(it["url"] and it["title"] and it["source"] for it in items)
        dated = sum(1 for it in items if it["published_ts"])
        check(f"{src['name']} ≥1 条且字段完整", bool(items) and fields and dated >= len(items) * 0.9,
              f"n={len(items)} 含时间={dated}")

    # ---------- ② 入库去重 ----------
    print("② 入库去重")
    got, new = news.collect(conn, proxy=settings.okx_proxy or None)
    check("collect 抓取成功", got > 0, f"got={got} new={new}")
    items = rss.fetch(rss.SOURCES[0], proxy=settings.okx_proxy or None)
    with conn.cursor() as cur:
        cur.execute("SELECT url FROM news_raw WHERE source=%s", (rss.SOURCES[0]["name"],))
        existing = {r[0] for r in cur.fetchall()}
    dups = [it for it in items if it["url"] in existing]
    n_dup = sum(1 for it in dups if db.insert_news_raw_if_new(
        conn, source=it["source"], url=it["url"], title=it["title"],
        content=it["summary"], published_ts=it["published_ts"]) is None)
    check(f"已存在 {len(dups)} 条重插全部去重", len(dups) > 0 and n_dup == len(dups))

    # ---------- ③ PIT ----------
    print("③ PIT（published_ts 不晚于 now+5min；无时间/无链接条目不入库为可结构化态）")
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM news_raw
                       WHERE published_ts > now() + interval '5 minutes'""")
        future = cur.fetchone()[0]
        cur.execute("SELECT max(published_ts) FROM news_raw")
        mx = cur.fetchone()[0]
    check("无未来时间戳条目", future == 0, f"未来={future} max={mx}")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_raw WHERE url IS NULL")
        no_url = cur.fetchone()[0]
    check("无 url 条目不入库", no_url == 0)

    # ---------- ④ 解析单测（mock LLM） ----------
    print("④ 解析单测（mock LLM）")
    calls: list[dict] = []

    def fake_complete(task, messages, **kw):
        calls.append({"task": task, "kw": kw})
        return _ok_resp('{"events":[{"symbols":["btc","BTC","$%bad"],"event_type":"xyz",'
                        '"sentiment":1.5,"magnitude":2,"confidence":"0.7"}]}')

    with mock.patch.object(gateway, "complete", fake_complete):
        evs, model = news.structure_one({"source": "t", "title": "T", "content": "C"})
    e = evs[0]
    check("符号去重大写+坏符号过滤", e["symbols"] == ["BTC"], str(e["symbols"]))
    check("枚举外 event_type→other", e["event_type"] == "other")
    check("sentiment 钳制 1.5→1.0", e["sentiment"] == 1.0)
    check("magnitude 钳制 2→1.0", e["magnitude"] == 1.0)
    check("confidence 字符串→0.7", e["confidence"] == 0.7)
    check("缺 impact_dir 按 sentiment 推导", e["impact_dir"] == "bullish")
    check("json_mode 传参", calls[0]["kw"].get("json_mode") is True)
    check("任务档位=extract", calls[0]["task"] == "extract")
    check("模型归因透传", model == "mock-model")

    with mock.patch.object(gateway, "complete",
                           lambda *a, **k: _ok_resp("不是JSON")):
        bad = None
        try:
            news.structure_one({"source": "t", "title": "T", "content": "C"})
        except ValueError as e2:
            bad = e2
    check("坏 JSON 抛 ValueError", bad is not None)

    # ---------- ⑤ DB 管道隔离（source='verify'） ----------
    print("⑤ DB 管道隔离（source='verify'）")
    r_ok = _ins_verify(conn, "ok", "VERIFY-OK 某公司申请比特币ETF")
    r_bad = _ins_verify(conn, "bad", "VERIFY-BAD 坏输出测试")
    check("verify 条目已入库", bool(r_ok and r_bad), f"{r_ok},{r_bad}")

    with mock.patch.object(gateway, "complete",
                           lambda task, messages, **kw: _fake_for_item(
                               {"title": messages[1]["content"]})):
        n, ev = news.extract_pending(conn, 10, sources=("verify",))
    check("处理 2 条", n == 2, f"n={n} ev={ev}")
    pt_ok, evs_ok = _raw_state(conn, r_ok)
    pt_bad, evs_bad = _raw_state(conn, r_bad)
    check("OK 条 processed 置位", pt_ok is not None)
    check("OK 条事件落库且字段正确",
          len(evs_ok) == 1 and evs_ok[0][0] == ["BTC"] and evs_ok[0][2] == 1.0
          and evs_ok[0][3] == "bullish" and evs_ok[0][6] == "mock-fallback-model",
          str(evs_ok))
    check("坏 JSON 条 0 事件但 processed 置位", len(evs_bad) == 0 and pt_bad is not None)

    r_err = _ins_verify(conn, "err", "VERIFY-ERR 传输失败测试")
    with mock.patch.object(gateway, "complete",
                           side_effect=gateway.LlmError("mock 全链失败")):
        n2, _ = news.extract_pending(conn, 10, sources=("verify",))
    pt_err, evs_err = _raw_state(conn, r_err)
    check("LLM 传输失败：本轮 0 处理且该条不置位", n2 == 0 and pt_err is None,
          f"n={n2} pt={pt_err}")

    with mock.patch.object(gateway, "complete", lambda *a, **k: _ok_resp('{"events":[]}')):
        n3, ev3 = news.extract_pending(conn, 10, sources=("verify",))
    pt_err2, _ = _raw_state(conn, r_err)
    check("下轮重试成功：空事件条置位", n3 == 1 and ev3 == 0 and pt_err2 is not None)

    # ---------- ⑥ 真实 LLM 结构化 ----------
    print("⑥ 真实 LLM 结构化（百炼）")
    if not settings.llm_api_key:
        print("  [SKIP] LLM_API_KEY 未配置——填入 key 后重跑本脚本可测真实结构化")
    else:
        real = db.load_unextracted_news(conn, 1) or [
            {"id": 0, "source": "synthetic", "content": "",
             "title": "Bitcoin ETF sees record daily inflows, analysts raise targets"}]
        try:
            evs, model = news.structure_one(real[0])
            check("真实结构化返回合法事件列表（可为空）", isinstance(evs, list) and model != "",
                  f"model={model} events={len(evs)} {str(evs[:1])[:80]}")
        except (gateway.LlmError, ValueError) as e:
            check("真实结构化返回合法事件列表（可为空）", False, f"{type(e).__name__}: {e}")

    # ---------- ⑦ 清理 verify 源 ----------
    print("⑦ 清理 verify 测试数据（级联删事件）")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM news_raw WHERE source='verify'")
        deleted = cur.rowcount
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_raw WHERE source='verify'")
        left = cur.fetchone()[0]
    check("verify 数据已清理", left == 0, f"deleted={deleted}")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_raw")
        total = cur.fetchone()[0]
    print(f"\n库内资讯总量: {total} 条（真实数据，供 P2-4 信息票与扫描器消费）")
    print(f"{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    conn.close()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
