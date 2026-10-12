/** 后端 API 类型与请求封装（/api/* 由 next rewrites 代理到 FastAPI）。 */

export type ReportStats = {
  n: number;
  win_rate: number | null;
  pf: number | null;
  avg_pnl_pct: number | null;
  total_pnl: number;
};

export type Report = {
  start: string;
  end: string;
  overall: ReportStats;
  by_timeframe: Record<string, ReportStats>;
  by_symbol: Record<string, ReportStats>;
  markdown: string;
};

export type Position = {
  id: number;
  signal_id: number;
  symbol: string;
  timeframe: string;
  status: 'closed' | 'open';
  entry_ts: string;
  entry_price: number;
  qty: number;
  exit_ts: string | null;
  exit_price: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  max_adverse_excursion: number | null;
};

export type Signal = {
  id: number;
  rule: string;
  symbol: string;
  timeframe: string;
  contract_type: string;
  ts: string;
  direction: 'entry' | 'exit';
  price_zone_low: number;
  price_zone_high: number;
  pushed: number;
  annotation: string | null;
};

export type SymbolPool = {
  symbol: string;
  periods: { timeframe: string; n_rules: number; last_ts: string }[];
};

export type SymbolSearchHit = { symbol: string; in_pool: boolean };

export type AddSymbolResult = {
  symbol: string;
  timeframes: string[];
  bars: number;
  signals: number;
  ledger: { total: number; closed: number };
};

export type Ohlcv = {
  bars: { time: number; open: number; high: number; low: number; close: number; volume: number }[];
  ema20: { time: number; value: number }[];
  ema60: { time: number; value: number }[];
  signals: { time: number; direction: 'entry' | 'exit'; price: number; signal_id: number }[];
};

export async function api<T>(path: string): Promise<T> {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function postAnnotation(signalId: number, userAction: string): Promise<void> {
  const r = await fetch('/api/annotations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ signal_id: signalId, user_action: userAction }),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

export const searchSymbols = (q: string): Promise<SymbolSearchHit[]> =>
  api<SymbolSearchHit[]>(`/api/symbols/search?q=${encodeURIComponent(q)}`);

export async function addSymbol(symbol: string, timeframes: string[]): Promise<AddSymbolResult> {
  const r = await fetch('/api/symbols', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, timeframes }),
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      msg = (await r.json()).detail ?? msg;
    } catch {
      // 保持状态码
    }
    throw new Error(msg);
  }
  return r.json();
}

export const fmtTs = (s: string | null): string => (s ? s.slice(0, 16).replace('T', ' ') : '-');

export const fmtPct = (x: number | null, digits = 2): string =>
  x == null ? '-' : `${(x * 100).toFixed(digits)}%`;

export const fmtPf = (x: number | null): string => (x == null ? '-' : x.toFixed(2));

/** 盈亏配色（国际惯例：绿涨红跌）。 */
export const pnlColor = (x: number | null): string =>
  x == null ? '#8c8c8c' : x >= 0 ? '#3f8600' : '#cf1322';
