"""P3 A5 个人研判知识库 RAG：向量化入库 + 推送前召回。

- 语料 = 人工（手动条目 + 标注笔记），AI 生成的理由不入库（避免自我循环）。
- 召回失败（LLM/DB 任何异常）静默降级返回 []，不阻塞推送主链路。
"""
from __future__ import annotations

from app.adapters.llm import gateway
from app.models import db

RECALL_K = 3


def add_chunk(conn, text: str, *, source_type: str = "manual", meta: dict | None = None) -> int:
    """向量化并入库；embed 全链失败抛 gateway.LlmError（调用方决定降级方式）。"""
    emb = gateway.embed([text.strip()])[0]
    return db.insert_knowledge(conn, source_type=source_type, text=text.strip(),
                               embedding=emb, meta=meta or {})


def recall(conn, s: dict, k: int = RECALL_K) -> list[str]:
    """按信号上下文（标的/周期/方向/规则）召回本人历史研判文本；失败返回 []。"""
    try:
        q = (f"{s['symbol']} {s['timeframe']} "
             f"{'买入' if s['direction'] == 'entry' else '卖出'} {s['rule']}")
        hits = db.search_knowledge(conn, gateway.embed([q])[0], limit=k)
        return [h["text"] for h in hits]
    except Exception as e:
        print(f"[知识] 召回跳过（{type(e).__name__}: {str(e)[:60]}）", flush=True)
        return []
