"""P2-2 LiteLLM 模型网关：OpenAI 兼容（阿里百炼）+ 按任务选模 + 回退链。

任务档位（.env 可配，逗号分隔多个模型即为回退链）：
- extract：信息结构化（便宜档）  LLM_MODEL_EXTRACT 默认 qwen-plus
- explain：信号解释文案           LLM_MODEL_EXPLAIN 默认 qwen-plus
- vision：K线读图（多模态）        LLM_MODEL_VISION  默认 qwen-vl-max

调用经 litellm（model 前缀 openai/ 走 OpenAI 兼容端点）；全链失败抛 LlmError，
调用方按"降级不阻塞"处理（如推送降级为纯信号版）。
"""
from __future__ import annotations

import os
import time

# 跳过远端价格表拉取（GitHub raw 不可达环境下会重试阻塞启动）；本地备份足够
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm

from app.core.config import settings


class LlmError(RuntimeError):
    pass


def models_for(task: str) -> tuple[str, ...]:
    return {"extract": settings.llm_model_extract,
            "explain": settings.llm_model_explain,
            "vision": settings.llm_model_vision}[task]


def complete(task: str, messages: list[dict], *, json_mode: bool = False,
             timeout: float = 60.0, **kw) -> str:
    """按任务选模 + 回退链调用；返回首个成功响应文本。全链失败抛 LlmError。"""
    if not settings.llm_api_key:
        raise LlmError("LLM_API_KEY 未配置（.env）")
    last: Exception | None = None
    for model in models_for(task):
        t0 = time.time()
        try:
            params = {"response_format": {"type": "json_object"}} if json_mode else {}
            r = litellm.completion(model=f"openai/{model}", messages=messages,
                                   api_key=settings.llm_api_key,
                                   api_base=settings.llm_base_url,
                                   timeout=timeout, **params, **kw)
            usage = getattr(r, "usage", None)
            print(f"[LLM] {task} {model} {time.time() - t0:.1f}s"
                  f"{f' tokens={usage.total_tokens}' if usage else ''}", flush=True)
            return r.choices[0].message.content
        except Exception as e:
            print(f"[LLM] {task} {model} 失败({type(e).__name__})，尝试下一个", flush=True)
            last = e
    raise LlmError(f"{task} 全部模型失败: {type(last).__name__}: {last}")
