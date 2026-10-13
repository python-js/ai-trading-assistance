"""P3 A5 知识库验证：
① gateway.embed：真实调用（有 key）维度 1024；mock 全链失败 → LlmError；
② 入库-召回闭环（真实 DB + 真实 embed）：相关条目排第一、无关条目靠后；
③ explain 注入：带 knowledge → prompt 含"历史研判参考"＋原文；无 knowledge → 不含；
④ 降级：embed 失败 → rag.recall 返回 []（不阻塞）；explain 无知识段正常出理由；
⑤ API：POST/GET/DELETE /api/knowledge；空文本 422；重复 DELETE 404；
⑥ 标注 note 自动入库：带 note → knowledge_chunk 出现 source_type=note 条目；
   note 空 → 不入库；测试数据清理。
用法：.venv/Scripts/python.exe backend/scripts/verify_knowledge.py
"""
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from fastapi.testclient import TestClient

from app.adapters.llm import gateway
from app.core.config import settings
from app.engine import explain, rag
from app.main import app
from app.models import db

FAIL: list[str] = []

SIG = {"signal_id": 1, "rule": "sma_520_rsi_1h_BTC/USDT", "symbol": "BTC/USDT",
       "exchange": "okx", "contract_type": "spot", "timeframe": "1h",
       "ts": datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc), "direction": "entry",
       "price": 61000.0, "price_zone_low": 60500.0, "price_zone_high": 61500.0}

KNOWN = "BTC/USDT 1h 金叉买入后追高被套过，教训：等回踩 MA20 企稳再进，勿追第一根。"
OTHER = "茅台日线放量突破买点偏晚，量价背离时应观望，避免尾盘冲动。"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    c = TestClient(app)
    created: list[int] = []  # 本脚本创建的知识行，最后清理

    # ---------- ① embed 网关 ----------
    print("① gateway.embed")
    if settings.llm_api_key:
        embs = gateway.embed(["测试文本一", "测试文本二"])
        check("真实调用返回 2×1024", len(embs) == 2 and all(len(e) == 1024 for e in embs),
              f"dim={len(embs[0])}")
        check("非零向量", any(abs(x) > 1e-6 for x in embs[0]))
    else:
        print("  [SKIP] LLM_API_KEY 未配置")
    with mock.patch.object(gateway, "models_for", lambda t: ("mock-a", "mock-b")), \
         mock.patch.object(gateway.litellm, "embedding", side_effect=RuntimeError("x")):
        try:
            gateway.embed(["t"])
            check("全链失败 → LlmError", False)
        except gateway.LlmError:
            check("全链失败 → LlmError", True)

    # ---------- ② 入库-召回闭环 ----------
    print("② 入库-召回闭环（真实）")
    if not settings.llm_api_key:
        print("  [SKIP] 需 LLM_API_KEY")
    else:
        id_a = rag.add_chunk(conn, KNOWN, source_type="manual", meta={"case": "known"})
        id_b = rag.add_chunk(conn, OTHER, source_type="manual", meta={"case": "other"})
        created += [id_a, id_b]
        hits = rag.recall(conn, SIG, k=2)
        check("召回 2 条", len(hits) == 2, f"n={len(hits)}")
        check("相关条目排第一", bool(hits) and "MA20" in hits[0] and "茅台" not in hits[0],
              (hits[0] if hits else "")[:40])
        with conn.cursor() as cur:
            cur.execute("SELECT embedding IS NOT NULL FROM knowledge_chunk WHERE id = %s", (id_a,))
            check("embedding 已落库", cur.fetchone()[0] is True)

    # ---------- ③ explain 注入 ----------
    print("③ explain 注入知识段")
    calls: list[dict] = []

    def fake_complete(task, messages, **kw):
        calls.append(messages)
        return gateway.LlmResult('{"reason":"结合历史复盘，等待回踩确认。"}', "mock")

    with mock.patch.object(gateway, "complete", fake_complete):
        explain.build_reason({**SIG, "knowledge": [KNOWN, OTHER]})
        explain.build_reason(SIG)
    with_kn = calls[0][1]["content"]
    without = calls[1][1]["content"]
    check("含历史研判参考段与原文", "历史研判参考" in with_kn and "MA20" in with_kn,
          with_kn.splitlines()[-1][:50])
    check("无知识时不含该段", "历史研判参考" not in without)

    # ---------- ④ 降级 ----------
    print("④ 降级不阻塞")
    with mock.patch.object(gateway, "embed", side_effect=gateway.LlmError("mock 挂")):
        check("recall 失败 → []", rag.recall(conn, SIG) == [])
    with mock.patch.object(gateway, "complete", fake_complete):
        r = explain.build_reason(SIG)  # SIG 无 knowledge
    check("无知识仍正常出理由", bool(r))

    # ---------- ⑤ API ----------
    print("⑤ /api/knowledge API")
    r = c.post("/api/knowledge", json={"text": "验证条目：ETH 4h 破位后反弹失败，空头延续。"})
    ok = r.status_code == 201 and isinstance(r.json().get("id"), int)
    check("POST 201", ok, str(r.text[:60]))
    if ok:
        created.append(r.json()["id"])
    r = c.post("/api/knowledge", json={"text": "   "})
    check("空文本 422", r.status_code == 422)
    r = c.get("/api/knowledge?limit=5")
    ok = r.status_code == 200 and any(x["id"] in created for x in r.json())
    check("GET 列表含新条目", ok, f"n={len(r.json()) if r.status_code == 200 else '-'}")
    kid = created[-1]
    check("DELETE 200", c.delete(f"/api/knowledge/{kid}").status_code == 200)
    check("重复 DELETE 404", c.delete(f"/api/knowledge/{kid}").status_code == 404)
    created.remove(kid)

    # ---------- ⑥ 标注 note 自动入库 ----------
    print("⑥ 标注 note 自动入库")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM signal ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        print("  [SKIP] 库内无 signal")
    else:
        sid = row[0]
        r = c.post("/api/annotations", json={"signal_id": sid, "user_action": "采纳",
                                             "note": "验证笔记：此次入场位置合理，出场可更早。"})
        check("带 note 标注 201", r.status_code == 201, str(r.text[:60]))
        aid = r.json()["id"] if r.status_code == 201 else None
        with conn.cursor() as cur:
            cur.execute("""SELECT id, text FROM knowledge_chunk
                           WHERE source_type='note' AND meta_json->>'signal_id' = %s
                           ORDER BY id DESC LIMIT 1""", (str(sid),))
            hit = cur.fetchone()
        check("note 已向量化入库", hit is not None and "验证笔记" in hit[1],
              (hit[1] if hit else "无"))
        if hit:
            created.append(hit[0])
        r2 = c.post("/api/annotations", json={"signal_id": sid, "user_action": "忽略"})
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM knowledge_chunk
                           WHERE source_type='note' AND meta_json->>'signal_id' = %s""",
                        (str(sid),))
            n_note = cur.fetchone()[0]
        check("无 note 不入库", r2.status_code == 201 and n_note == (1 if hit else 0),
              f"note 行数={n_note}")
        # 清理：删测试 annotation 行
        with conn.cursor() as cur:
            cur.execute("DELETE FROM annotation WHERE id IN (%s, %s)",
                        (aid, r2.json().get("id")))
        conn.commit()

    # ---------- 清理 ----------
    with conn.cursor() as cur:
        if created:
            cur.execute("DELETE FROM knowledge_chunk WHERE id = ANY(%s)", (created,))
    conn.commit()
    conn.close()
    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
