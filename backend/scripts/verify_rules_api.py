"""P3 A3 规则生成与沙箱验证：
① GET /api/rules：200、含种子规则、字段齐全；
② draft mock 回喂重试：第 1 轮非法（算子错）→ 第 2 轮合法 → 201；第 2 轮 prompt 含"校验失败"；
   入库 dsl_json 可 parse_rule、enabled=false、name 前缀 gen_；
③ draft 恒非法 → 422 且不新增 rule 行；
④ draft 虚构指标（macd）→ 422（列白名单拦截）；
⑤ 无数据标的 → 404 且不调用 LLM；
⑥ 真实 LLM draft：中文想法 → 201 + parse_rule 通过（打印 dsl）；
⑦ toggle：false/true 切换、不存在 404；
⑧ 沙箱回测：mock 草稿 days=30 → 200、metrics/trades 结构；伪造缺列规则 → 422；不存在 404；
⑨ 清理本脚本创建的 rule 行（仅 gen_*）。
用法：.venv/Scripts/python.exe backend/scripts/verify_rules_api.py
"""
import pathlib
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from fastapi.testclient import TestClient

from app.adapters.llm import gateway
from app.core.config import settings
from app.engine.rules_dsl import parse_rule
from app.main import app
from app.models import db

FAIL: list[str] = []

_VALID = ('{"entry": {"all": [{"cross_up": ["ema20","ema60"]}, {"rsi14": {"<": 70}}]}, '
          '"exit": {"any": [{"cross_down": ["ema20","ema60"]}, '
          '{"chandelier_exit": {"atr_mult": 3}}]}, "cooldown_bars": 5}')
_BAD_OP = ('{"entry": {"ema20": {"~": 3}}, '
           '"exit": {"cross_down": ["ema20","ema60"]}}')
_BAD_MACD = ('{"entry": {"macd": {">": 0}}, '
             '"exit": {"cross_down": ["ema20","ema60"]}}')


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def make_fake(outputs: list[str], calls: list[list[dict]]):
    def fake(task, messages, **kw):
        calls.append(messages)
        return gateway.LlmResult(outputs[min(len(calls) - 1, len(outputs) - 1)], "mock")
    return fake


def rule_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rule")
        return cur.fetchone()[0]


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    c = TestClient(app)
    created: list[int] = []

    # ---------- ① 规则列表 ----------
    print("① GET /api/rules")
    r = c.get("/api/rules?limit=500")
    rows = r.json() if r.status_code == 200 else []
    ok = r.status_code == 200 and len(rows) > 0
    check("列表 200 非空", ok, f"n={len(rows)}")
    seed = next((x for x in rows if x["name"] == "ema_2060_rsi_1h_BTC/USDT"), None)
    check("含种子规则且字段齐全",
          seed is not None and {"id", "enabled", "dsl_json", "timeframe"} <= set(seed or {}))

    # ---------- ② mock 回喂重试 ----------
    print("② draft mock 回喂重试")
    calls: list[list[dict]] = []
    body = {"text": "EMA 金叉且 RSI 不超买时买入，死叉或吊灯止损出场，冷却 5 根",
            "symbol": "BTC/USDT", "timeframe": "1h"}
    with mock.patch.object(gateway, "complete", make_fake([_BAD_OP, _VALID], calls)):
        r = c.post("/api/rules/draft", json=body)
    ok = r.status_code == 201
    check("重试后成功 201", ok, str(r.text[:80]))
    if ok:
        rid = r.json()["id"]
        created.append(rid)
        check("调用 2 轮且第 2 轮含回喂", len(calls) == 2
              and any("校验失败" in m["content"] for m in calls[1]),
              f"calls={len(calls)}")
        with conn.cursor() as cur:
            cur.execute("""SELECT name, enabled, dsl_json, cooldown_bars, symbol
                           FROM rule WHERE id = %s""", (rid,))
            name, enabled, dsl, cd, sym = cur.fetchone()
        check("入库 enabled=false", enabled is False)
        check("name 前缀 gen_ 且带标的", name.startswith("gen_BTC/USDT_1h_"), name)
        check("dsl 可 parse_rule 且冷却=5", cd == 5 and parse_rule(dsl).cooldown_bars == 5)
        check("响应 dsl 三要素", {"name", "symbol", "timeframe"} <= set(r.json()["dsl"]))

    # ---------- ③ 恒非法 ----------
    print("③ draft 两轮皆非法 → 422")
    n0 = rule_count(conn)
    calls3: list[list[dict]] = []
    with mock.patch.object(gateway, "complete", make_fake([_BAD_OP], calls3)):
        r = c.post("/api/rules/draft", json=body)
    check("422", r.status_code == 422, str(r.text[:80]))
    check("调用 2 轮后放弃", len(calls3) == 2)
    check("未新增 rule 行", rule_count(conn) == n0)

    # ---------- ④ 列白名单 ----------
    print("④ draft 虚构指标 → 422")
    calls4: list[list[dict]] = []
    with mock.patch.object(gateway, "complete", make_fake([_BAD_MACD], calls4)):
        r = c.post("/api/rules/draft", json=body)
    check("422 且指未支持列", r.status_code == 422 and "未支持" in r.text, str(r.text[:100]))

    # ---------- ⑤ 无数据标的 ----------
    print("⑤ 无数据标的 → 404 且不调 LLM")
    calls5: list[list[dict]] = []
    with mock.patch.object(gateway, "complete", make_fake([_VALID], calls5)):
        r = c.post("/api/rules/draft", json={**body, "symbol": "NOEXIST/USDT"})
    check("404", r.status_code == 404 and len(calls5) == 0, str(r.text[:80]))

    # ---------- ⑥ 真实 LLM ----------
    print("⑥ 真实 LLM draft")
    if not settings.llm_api_key:
        print("  [SKIP] LLM_API_KEY 未配置")
    else:
        r = c.post("/api/rules/draft", json={
            "text": "RSI14 低于 30 时买入，RSI14 高于 70 时卖出，冷却 5 根",
            "symbol": "BTC/USDT", "timeframe": "1h"})
        ok = r.status_code == 201
        check("真实生成 201", ok, str(r.text[:120]))
        if ok:
            rid = r.json()["id"]
            created.append(rid)
            dsl = r.json()["dsl"]
            ok2 = parse_rule(dsl) is not None
            check("parse_rule 通过", ok2, str(dsl)[:160])
            rl = c.get("/api/rules?limit=10")
            check("出现在规则列表且 enabled=false",
                  any(x["id"] == rid and x["enabled"] is False for x in rl.json()))

    # ---------- ⑦ toggle ----------
    print("⑦ toggle 启停")
    rid = created[0]  # mock 草稿
    check("停用 → false", c.post(f"/api/rules/{rid}/toggle",
                                 json={"enabled": False}).json() == {"id": rid, "enabled": False})
    check("启用 → true", c.post(f"/api/rules/{rid}/toggle",
                                json={"enabled": True}).json() == {"id": rid, "enabled": True})
    check("不存在 404", c.post("/api/rules/999999999/toggle",
                               json={"enabled": True}).status_code == 404)
    with conn.cursor() as cur:
        cur.execute("SELECT enabled FROM rule WHERE id = %s", (rid,))
        check("DB 状态一致（true）", cur.fetchone()[0] is True)
        cur.execute("UPDATE rule SET enabled = FALSE WHERE id = %s", (rid,))
    conn.commit()

    # ---------- ⑧ 沙箱回测 ----------
    print("⑧ 沙箱回测")
    r = c.post(f"/api/rules/{rid}/backtest?days=30")
    ok = r.status_code == 200
    check("mock 草稿回测 200", ok, str(r.text[:100]))
    if ok:
        d = r.json()
        m = d["metrics"]
        keys = {"n_trades", "win_rate", "pf", "max_drawdown[%]", "return_on_init[%]"}
        check("metrics 结构", keys <= set(m), f"n_trades={m['n_trades']}")
        check("trades 为列表且 ts 为 ISO 串",
              isinstance(d["trades"], list)
              and all(isinstance(t["entry_ts"], str) and "T" in t["entry_ts"]
                      for t in d["trades"]),
              f"trades={len(d['trades'])} events={d['n_events']}")
        print(f"    摘要：{m['n_trades']} 笔 · 胜率 {m['win_rate']} · 盈亏比 {m['pf']} "
              f"· 累计 {m['total_pnl']:.1f} USDT · 回撤 {m['max_drawdown[%]']:.2f}%")
    check("不存在 404", c.post("/api/rules/999999999/backtest").status_code == 404)
    # 伪造缺列规则 → 422（列白名单只拦生成；直接入库的坏 DSL 由回测兜底）
    bad_id = db.insert_draft_rule(
        conn, name=f"gen_test_badcol_{rid}", symbol="BTC/USDT", exchange="okx",
        contract_type="spot", timeframe="1h",
        dsl={"name": f"gen_test_badcol_{rid}", "symbol": "BTC/USDT", "timeframe": "1h",
             "entry": {"macd": {">": 0}}, "exit": {"cross_down": ["ema20", "ema60"]},
             "cooldown_bars": 0})
    created.append(bad_id)
    r = c.post(f"/api/rules/{bad_id}/backtest?days=30")
    check("缺列规则回测 422", r.status_code == 422 and "缺少列" in r.text, str(r.text[:100]))

    # ---------- ⑨ 清理 ----------
    with conn.cursor() as cur:
        if created:
            cur.execute("DELETE FROM rule WHERE id = ANY(%s)", (created,))
        cur.execute("SELECT count(*) FROM rule WHERE name LIKE 'gen_%'")
        left = cur.fetchone()[0]
    conn.commit()
    conn.close()
    check("清理完成（无残留 gen_）", left == 0, f"left={left}")
    print(f"\n{'全部通过' if not FAIL else '失败: ' + ', '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
