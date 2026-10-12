"""P2-1（P0-4b）资金面序列：DB 读取 + asof PIT 对齐 → DSL 叶子算子列。

- funding_rate：OKX 永续 8h 结算费率；bar ts 取"≤ ts 的最新已结算值"（asof 前向填充）。
- oi_change_24h：OI asof(ts) / OI asof(ts - 24h) - 1；任一端无值则为 NaN（比较不触发）。
- PIT：对齐只使用 ts ≤ 当前 bar 的数据；序列起点之前的 bar 为 NaN。
- 资金面天然属永续：表内 symbol 用内部规范串（与现货同串），现货规则评估亦取同 base 永续序列。
- 三态同源：实盘扫描 / 历史回放 / 签发回测均经 attach_if_needed 注入，保证信号一致。
"""
from __future__ import annotations

import json

import pandas as pd

FLOW_COLS = ("funding_rate", "oi_change_24h")


def load_funding(conn, symbol: str, exchange: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""SELECT ts, rate FROM funding_rate
                       WHERE symbol=%s AND exchange=%s AND rate IS NOT NULL ORDER BY ts""",
                    (symbol, exchange))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ts", "rate"])
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_oi(conn, symbol: str, exchange: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""SELECT ts, oi FROM open_interest
                       WHERE symbol=%s AND exchange=%s AND oi IS NOT NULL ORDER BY ts""",
                    (symbol, exchange))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ts", "oi"])
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def needs_flow(rules: list[dict]) -> bool:
    """规则集是否引用资金面列（避免无谓查询）。"""
    return any(any(c in json.dumps(r.get("dsl_json", {})) for c in FLOW_COLS) for r in rules)


def attach(df: pd.DataFrame, funding: pd.DataFrame | None = None,
           oi: pd.DataFrame | None = None) -> pd.DataFrame:
    """注入 funding_rate / oi_change_24h 列（asof 对齐；无数据为 NaN）。df 须含 ts 列（升序）。"""
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    if funding is not None and len(funding):
        df = pd.merge_asof(df, funding.rename(columns={"rate": "funding_rate"}),
                           on="ts", direction="backward")
    else:
        df["funding_rate"] = float("nan")
    if oi is not None and len(oi):
        df = pd.merge_asof(df, oi.rename(columns={"oi": "_oi"}), on="ts", direction="backward")
        oi24 = oi.copy()
        oi24["ts"] = oi24["ts"] + pd.Timedelta(hours=24)
        df = pd.merge_asof(df, oi24.rename(columns={"oi": "_oi24"}), on="ts", direction="backward")
        df["oi_change_24h"] = df["_oi"] / df["_oi24"] - 1.0
        df = df.drop(columns=["_oi", "_oi24"])
    else:
        df["oi_change_24h"] = float("nan")
    return df


def attach_if_needed(conn, df: pd.DataFrame, rules: list[dict],
                     symbol: str, exchange: str) -> pd.DataFrame:
    """规则集引用资金面列时读库对齐注入；否则原样返回。"""
    if not needs_flow(rules):
        return df
    return attach(df, load_funding(conn, symbol, exchange), load_oi(conn, symbol, exchange))
