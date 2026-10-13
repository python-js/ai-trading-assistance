"""环境对齐分段度量（只读，研究用）：信号周期 × 近环境周期 的同向/逆向表现对照。

口径（2026-10-13 定）：
- 数据源：paper_position(closed) JOIN signal（okx spot 全多）；营收/损益口径与台账一致（含费+滑点）。
- 环境定义：环境周期 SMA5 > SMA20 = 多；SMA 由 app.engine.indicators.enrich 计算（生产同款）。
- PIT 严格截止：信号在其 bar 收盘后产生，故环境 bar 必须已收盘：
  环境 bar ts <= 信号 bar ts + (信号周期 - 环境周期)，取满足条件的最后一根（bar ts=开盘时间）。
  注：据此修正了两轮临时分析的宽松口径（当时含未收盘环境 bar，存在一根 K 线的前视）。
- 样本守卫：n < 30 标注"样本不足"。

用法：.venv/Scripts/python.exe backend/scripts/align_report.py
"""
import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd

from app.core.config import settings
from app.engine.indicators import enrich
from app.models import db

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
PAIRS = (("1m", "5m"), ("1m", "15m"), ("5m", "15m"), ("15m", "1h"), ("1h", "4h"))
MIN_N = 30

_TRADES = """
SELECT p.symbol, s.timeframe, p.entry_ts, p.pnl_pct
FROM paper_position p JOIN signal s ON s.id = p.signal_id
WHERE p.status='closed' AND p.exchange='okx' AND p.entry_ts IS NOT NULL
"""


def _stats(g: pd.DataFrame) -> dict:
    n = len(g)
    if not n:
        return {"n": 0}
    w, l = g[g.pnl_pct > 0], g[g.pnl_pct < 0]
    gl = -l.pnl_pct.sum()
    pf = (w.pnl_pct.sum() / gl) if gl > 0 else None
    ratio = (w.pnl_pct.mean() / abs(l.pnl_pct.mean())) if len(w) and len(l) else None
    need = (1 - len(w) / n) / (len(w) / n) if len(w) else None
    return {"n": n, "win%": len(w) / n * 100, "pf": pf, "实比": ratio, "需比": need,
            "exp%": g.pnl_pct.mean() * 100}


def _fmt(tag: str, s: dict) -> str:
    if not s["n"]:
        return f"  {tag}: 无样本"
    flag = "（样本不足）" if s["n"] < MIN_N else ""
    pf = f"{s['pf']:.2f}" if s["pf"] is not None else "—"
    r = f"{s['实比']:.2f}" if s["实比"] is not None else "—"
    nd = f"{s['需比']:.2f}" if s["需比"] is not None else "—"
    return (f"  {tag}: n={s['n']:<5} 胜率={s['win%']:.1f}%  PF={pf}  "
            f"实比={r} 平衡需={nd}  期望={s['exp%']:+.4f}%/笔{flag}")


def main() -> None:
    conn = db.connect(settings.pg_dsn)
    with conn.cursor() as cur:
        cur.execute(_TRADES)
        cols = [d[0] for d in cur.description]
        tr = pd.DataFrame(cur.fetchall(), columns=cols)
    tr["entry_ts"] = pd.to_datetime(tr["entry_ts"], utc=True)

    cache: dict = {}

    def env_df(sym: str, tf: str) -> pd.DataFrame:
        k = (sym, tf)
        if k not in cache:
            df = db.load_ohlcv(conn, sym, "okx", "spot", tf)
            if len(df):
                df["ts"] = pd.to_datetime(df["ts"], utc=True)
                df = enrich(df)
            cache[k] = df
        return cache[k]

    def aligned(sym: str, sig_tf: str, env_tf: str, ts) -> bool | None:
        df = env_df(sym, env_tf)
        if df is None or len(df) == 0:
            return None
        cutoff = ts + timedelta(minutes=TF_MIN[sig_tf] - TF_MIN[env_tf])
        s = df[df.ts <= cutoff]
        if len(s) == 0:
            return None
        last = s.iloc[-1]
        if pd.isna(last.sma20) or pd.isna(last.sma5):
            return None
        return bool(last.sma5 > last.sma20)

    print("=== 环境对齐分段度量（okx spot；环境=高周期 SMA5>SMA20；PIT 严格截止）===")
    for sig_tf, env_tf in PAIRS:
        g = tr[tr.timeframe == sig_tf]
        if len(g) == 0:
            print(f"\n[{sig_tf} × {env_tf}] 无信号样本")
            continue
        al, ct, skip = [], [], 0
        for r in g.itertuples():
            b = aligned(r.symbol, sig_tf, env_tf, r.entry_ts)
            if b is None:
                skip += 1
            elif b:
                al.append(r)
            else:
                ct.append(r)
        win = f"{g.entry_ts.min():%Y-%m-%d %H:%M} → {g.entry_ts.max():%Y-%m-%d %H:%M}"
        print(f"\n[{sig_tf} × {env_tf}]  {sig_tf} 已平 {len(g)} 笔（{win}），"
              f"环境不可判定跳过 {skip} 笔")
        print(_fmt("全体（该周期）", _stats(g)))
        print(_fmt("同向        ", _stats(pd.DataFrame(al)) if al else {"n": 0}))
        print(_fmt("逆向        ", _stats(pd.DataFrame(ct)) if ct else {"n": 0}))
    conn.close()


if __name__ == "__main__":
    main()
