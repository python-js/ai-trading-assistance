"""P2-2 LLM 网关验证（litellm mock，不发真实请求）：
① 回退链：A 失败 → B 成功，调用序列 openai/A → openai/B，返回 B 内容；
② 全链失败抛 LlmError；③ 空 key 快速失败且不发请求；
④ 档位映射（extract/explain/vision）+ json_mode/api_key/api_base/timeout 透传；
⑤ 真实连通：LLM_API_KEY 非空时走百炼真实调用，空则 SKIP（P2-3 端到端需先填 key）。
用法：.venv/Scripts/python.exe backend/scripts/verify_gateway.py
"""
import pathlib
import sys
import types
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.llm import gateway
from app.core.config import settings

FAIL: list[str] = []
MSGS = [{"role": "user", "content": "hi"}]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def _resp(content: str):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        usage=types.SimpleNamespace(total_tokens=7))


def _fake_settings(key: str = "sk-test"):
    return types.SimpleNamespace(llm_api_key=key, llm_base_url="http://fake-base",
                                 llm_model_extract=("model-a", "model-b"),
                                 llm_model_explain=("model-e",),
                                 llm_model_vision=("model-v",))


def _patch(fake_completion, **s):
    return (mock.patch.object(gateway, "settings", _fake_settings(**s)),
            mock.patch.object(gateway, "litellm",
                              types.SimpleNamespace(completion=fake_completion)))


def main() -> None:
    # ---------- ① 回退链 ----------
    print("① 回退链（A 失败 → B 成功）")
    calls: list[dict] = []

    def fake_ok(**kw):
        calls.append(kw)
        if kw["model"] == "openai/model-a":
            raise RuntimeError("boom")
        return _resp("ok-B")

    p_set, p_llm = _patch(fake_ok)
    with p_set, p_llm:
        out = gateway.complete("extract", MSGS)
    check("返回第二模型内容", out == "ok-B", repr(out))
    seq = [c["model"] for c in calls]
    check("调用序列 openai/A→openai/B", seq == ["openai/model-a", "openai/model-b"], str(seq))
    check("api_key/api_base 透传",
          calls[0]["api_key"] == "sk-test" and calls[0]["api_base"] == "http://fake-base")
    check("默认 timeout=60", calls[0]["timeout"] == 60.0)

    # ---------- ② 全链失败 ----------
    print("② 全链失败抛 LlmError")
    def fake_fail(**kw):
        raise RuntimeError("down")

    err = None
    p_set, p_llm = _patch(fake_fail)
    with p_set, p_llm:
        try:
            gateway.complete("extract", MSGS)
        except gateway.LlmError as e:
            err = e
    check("抛 LlmError 且含原因", err is not None and "RuntimeError" in str(err),
          str(err)[:80] if err else "未抛出")

    # ---------- ③ 空 key 快速失败 ----------
    print("③ 空 key 快速失败且不发请求")
    calls3: list[dict] = []

    def fake_never(**kw):
        calls3.append(kw)
        return _resp("x")

    err = None
    p_set, p_llm = _patch(fake_never, key="")
    with p_set, p_llm:
        try:
            gateway.complete("extract", MSGS)
        except gateway.LlmError as e:
            err = e
    check("空 key 抛 LlmError", err is not None)
    check("空 key 不发请求", calls3 == [])

    # ---------- ④ 档位映射 + 传参 ----------
    print("④ 档位映射 + json_mode/timeout 传参")
    p_set, _ = _patch(fake_never)
    with p_set:
        check("extract 档位", gateway.models_for("extract") == ("model-a", "model-b"))
        check("explain 档位", gateway.models_for("explain") == ("model-e",))
        check("vision 档位", gateway.models_for("vision") == ("model-v",))

    calls4: list[dict] = []

    def fake_rec(**kw):
        calls4.append(kw)
        return _resp("{}")

    p_set, p_llm = _patch(fake_rec)
    with p_set, p_llm:
        gateway.complete("explain", MSGS, json_mode=True, timeout=15.0)
        gateway.complete("explain", MSGS)
    check("json_mode 传 response_format",
          calls4[0].get("response_format") == {"type": "json_object"})
    check("非 json_mode 不传 response_format", "response_format" not in calls4[1])
    check("timeout 透传", calls4[0]["timeout"] == 15.0)

    # ---------- ⑤ 真实连通（或 SKIP） ----------
    print("⑤ 真实连通（百炼）")
    if not settings.llm_api_key:
        print("  [SKIP] LLM_API_KEY 未配置（.env）——P2-3 端到端前先填百炼 key")
    else:
        try:
            out = gateway.complete("extract", [{"role": "user",
                                                "content": "只回复两个字：测试"}], timeout=60)
            check("真实调用返回非空", bool(out and out.strip()),
                  (out or "").strip()[:40])
        except gateway.LlmError as e:
            check("真实调用返回非空", False, str(e)[:120])

    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
