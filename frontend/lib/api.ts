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
  sma5: { time: number; value: number }[];
  sma10: { time: number; value: number }[];
  sma20: { time: number; value: number }[];
  signals: { time: number; direction: 'entry' | 'exit'; price: number; signal_id: number }[];
};

export async function api<T>(path: string): Promise<T> {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function postAnnotation(
  signalId: number,
  userAction: string,
  note?: string
): Promise<void> {
  const r = await fetch('/api/annotations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ signal_id: signalId, user_action: userAction, note: note || null }),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

export type Knowledge = {
  id: number;
  source_type: 'manual' | 'note';
  text: string;
  meta: Record<string, unknown>;
  created_ts: string;
};

export const fetchKnowledge = (): Promise<Knowledge[]> => api<Knowledge[]>('/api/knowledge?limit=200');

export async function addKnowledge(text: string): Promise<{ id: number }> {
  const r = await fetch('/api/knowledge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
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

export async function deleteKnowledge(id: number): Promise<void> {
  const r = await fetch(`/api/knowledge/${id}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

export type RuleRow = {
  id: number;
  name: string;
  symbol: string;
  timeframe: string;
  enabled: boolean;
  version: number;
  created_ts: string;
  dsl_json: Record<string, unknown>;
};

export type RuleDraft = { id: number; name: string; dsl: Record<string, unknown> };

export type BacktestTrade = {
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  hold_bars: number;
  mae: number;
};

export type BacktestResult = {
  id: number;
  name: string;
  symbol: string;
  timeframe: string;
  days: number;
  metrics: {
    n_trades: number;
    win_rate: number | null;
    pf: number | null;
    avg_pnl_pct: number | null;
    total_pnl: number;
    'return_on_init[%]': number;
    'max_drawdown[%]': number;
    'exposure[%]': number;
    avg_hold_bars: number | null;
    fees_total: number;
  };
  n_events: number;
  trades: BacktestTrade[];
  open_pos: { entry_ts: string; entry_price: number; unrealized: number } | null;
};

export const fetchRules = (): Promise<RuleRow[]> => api<RuleRow[]>('/api/rules?limit=500');

export async function createRuleDraft(
  text: string,
  symbol: string,
  timeframe: string
): Promise<RuleDraft> {
  const r = await fetch('/api/rules/draft', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, symbol, timeframe }),
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

export async function toggleRule(id: number, enabled: boolean): Promise<void> {
  const r = await fetch(`/api/rules/${id}/toggle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

export async function runBacktest(id: number, days: number): Promise<BacktestResult> {
  const r = await fetch(`/api/rules/${id}/backtest?days=${days}`, { method: 'POST' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
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

// ---------- 模拟盘（共享资金池 · 固定比例复利 · 按需重放） ----------

export type SimUnitIn = { symbol: string; timeframe: string; rule_id?: number };

export type SimAccountRow = {
  id: number;
  name: string;
  market: 'crypto' | 'cn';
  init_cash: number;
  size_pct: number;
  stop_loss_pct: number;
  start_ts: string;
  end_ts: string | null;
  status: 'active' | 'finished';
  created_ts: string;
  units: { symbol: string; exchange: string; contract_type: string; timeframe: string }[];
};

export type SimTrade = {
  unit_idx: number;
  symbol: string;
  timeframe: string;
  strategy: string;
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  exit_price: number;
  qty: number;
  notional: number;
  pnl: number;
  pnl_pct: number;
  fees: number;
  hold_hours: number;
  mae: number;
  exit_reason: 'signal' | 'stop_loss' | 'expired' | 'manual';
};

export type SimResult = {
  status: 'active' | 'finished';
  end_reason: 'stop_loss' | 'expired' | 'manual' | null;
  end_ts: string | null;
  cash: number;
  realized: number;
  equity: { time: number; value: number }[];
  trades: SimTrade[];
  open_pos: {
    unit_idx: number;
    symbol: string;
    timeframe: string;
    strategy: string;
    entry_ts: string;
    entry_price: number;
    qty: number;
    notional: number;
    unrealized: number;
  }[];
  metrics: {
    n_trades: number;
    win_rate: number | null;
    pf: number | null;
    avg_pnl_pct: number | null;
    total_pnl: number;
    'return_on_init[%]': number;
    'max_drawdown[%]': number;
    fees_total: number;
    avg_hold_hours: number | null;
    'exposure[%]': number;
  };
  n_marks: number;
  n_events: number;
};

export type SimDetail = {
  id: number;
  name: string;
  market: 'crypto' | 'cn';
  init_cash: number;
  size_pct: number;
  stop_loss_pct: number;
  start_ts: string;
  end_ts: string | null;
  db_status: 'active' | 'finished';
  units: { symbol: string; timeframe: string; changes: { effective_from: string; name: string }[] }[];
  result: SimResult;
};

export type SimReview = {
  status: string;
  end_reason: string | null;
  stats: SimResult['metrics'];
  by_unit: {
    symbol: string;
    timeframe: string;
    n: number;
    win_rate: number | null;
    total_pnl: number;
    pf: number | null;
  }[];
  by_segment: {
    unit_idx: number;
    symbol: string;
    timeframe: string;
    strategy: string;
    n: number;
    win_rate: number | null;
    total_pnl: number;
    pf: number | null;
  }[];
  notes: string[];
  llm: { text: string | null; ok: boolean; error: string | null };
};

async function postSim<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
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

export const fetchSims = (): Promise<SimAccountRow[]> => api<SimAccountRow[]>('/api/sim');

export const fetchSim = (id: number): Promise<SimDetail> => api<SimDetail>(`/api/sim/${id}`);

export const createSim = (body: {
  name: string;
  init_cash: number;
  size_pct: number;
  stop_loss_pct: number;
  start_days_ago: number;
  duration_days: number | null;
  units: SimUnitIn[];
}): Promise<{ id: number; name: string; units: { symbol: string; timeframe: string; strategy: string }[] }> =>
  postSim('/api/sim', body);

export const changeSimStrategy = (
  id: number,
  body: { unit_idx: number; rule_id?: number; text?: string }
): Promise<{ ok: boolean; unit_idx: number; name: string }> =>
  postSim(`/api/sim/${id}/change`, body);

export const finishSim = (id: number): Promise<{ ok: boolean }> =>
  postSim(`/api/sim/${id}/finish`);

export const reviewSim = (id: number, llm: boolean): Promise<SimReview> =>
  postSim(`/api/sim/${id}/review?llm=${llm ? 1 : 0}`);

export async function deleteSim(id: number): Promise<void> {
  const r = await fetch(`/api/sim/${id}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

/** ISO 时间 → 北京时间（UTC+8 固定偏移）'YYYY-MM-DD HH:MM'。 */
export const fmtTs = (s: string | null): string => {
  if (!s) return '-';
  const t = Date.parse(s);
  if (Number.isNaN(t)) return s.slice(0, 16).replace('T', ' ');
  return new Date(t + 8 * 3600 * 1000).toISOString().slice(0, 16).replace('T', ' ');
};

export const fmtPct = (x: number | null, digits = 2): string =>
  x == null ? '-' : `${(x * 100).toFixed(digits)}%`;

export const fmtPf = (x: number | null): string => (x == null ? '-' : x.toFixed(2));

/** 价格小数位：虚拟币 ≥4 位、按量级自适应（至多 8 位）；A 股（symbol 无 '/'）固定 2 位。 */
export const priceDecimals = (x: number, symbol?: string): number => {
  if (symbol && !symbol.includes('/')) return 2;
  if (!Number.isFinite(x) || x <= 0) return 4;
  return Math.min(8, Math.max(4, 3 - Math.floor(Math.log10(x))));
};

/** 价格显示：按币种自适应小数位（虚拟币最少 4 位）；非法值返回 '-'。 */
export const fmtPrice = (x: number, symbol?: string): string =>
  Number.isFinite(x) ? x.toFixed(priceDecimals(x, symbol)) : '-';

/** 盈亏配色（国际惯例：绿涨红跌）。 */
export const pnlColor = (x: number | null): string =>
  x == null ? '#8c8c8c' : x >= 0 ? '#3f8600' : '#cf1322';
