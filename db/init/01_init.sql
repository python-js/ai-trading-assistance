-- P0 数据库初始化（扩展 / hypertable / 唯一约束 / 索引 / 业务表）
-- 设计依据: 开发计划与方案.md §7 + docs/P0-阶段1-设计.md §2
-- 容错原则: timescaledb / pgvector 任一不可用时降级为普通表并告警，不中断初始化。

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- pgvector 仅 P2(RAG) 需要；镜像无此扩展时跳过 knowledge_chunk
DO $vec$ BEGIN
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id          BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector,
    meta_json   JSONB,
    created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'pgvector unavailable, skip knowledge_chunk: %', SQLERRM;
END $vec$;

-- ============ 维度 ============
CREATE TABLE IF NOT EXISTS symbol (
  id              BIGSERIAL PRIMARY KEY,
  symbol          TEXT NOT NULL,                 -- 内部规范 base/quote, e.g. BTC/USDT
  exchange        TEXT NOT NULL DEFAULT 'okx',
  asset_class     TEXT NOT NULL DEFAULT 'crypto',-- crypto / astock
  contract_type   TEXT NOT NULL DEFAULT 'spot',  -- spot / perp
  base            TEXT NOT NULL,
  quote           TEXT NOT NULL,
  exchange_symbol TEXT NOT NULL,                 -- ccxt 原串, e.g. BTC/USDT:USDT
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  delisted_ts     TIMESTAMPTZ,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (exchange, symbol, contract_type)
);

-- ============ 时序（hypertable） ============
CREATE TABLE IF NOT EXISTS ohlcv (
  ts            TIMESTAMPTZ NOT NULL,
  symbol        TEXT NOT NULL,
  exchange      TEXT NOT NULL,
  contract_type TEXT NOT NULL,
  timeframe     TEXT NOT NULL,
  open          DOUBLE PRECISION,
  high          DOUBLE PRECISION,
  low           DOUBLE PRECISION,
  close         DOUBLE PRECISION,
  volume        DOUBLE PRECISION,
  quote_volume  DOUBLE PRECISION,
  UNIQUE (symbol, exchange, contract_type, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS funding_rate (
  ts              TIMESTAMPTZ NOT NULL,
  symbol          TEXT NOT NULL,
  exchange        TEXT NOT NULL,
  rate            DOUBLE PRECISION,
  next_funding_ts TIMESTAMPTZ,
  UNIQUE (symbol, exchange, ts)
);

CREATE TABLE IF NOT EXISTS open_interest (
  ts       TIMESTAMPTZ NOT NULL,
  symbol   TEXT NOT NULL,
  exchange TEXT NOT NULL,
  oi       DOUBLE PRECISION,
  oi_value DOUBLE PRECISION,
  UNIQUE (symbol, exchange, ts)
);

CREATE TABLE IF NOT EXISTS liquidations (
  ts       TIMESTAMPTZ NOT NULL,
  symbol   TEXT NOT NULL,
  exchange TEXT NOT NULL,
  side     TEXT,
  qty      DOUBLE PRECISION,
  price    DOUBLE PRECISION
);

DO $ht$ BEGIN
  PERFORM create_hypertable('ohlcv',         'ts', if_not_exists => TRUE);
  PERFORM create_hypertable('funding_rate',  'ts', if_not_exists => TRUE);
  PERFORM create_hypertable('open_interest', 'ts', if_not_exists => TRUE);
  PERFORM create_hypertable('liquidations',  'ts', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'timescaledb hypertable creation skipped: %', SQLERRM;
END $ht$;

CREATE INDEX IF NOT EXISTS idx_ohlcv_query      ON ohlcv (symbol, exchange, contract_type, timeframe, ts DESC);
CREATE INDEX IF NOT EXISTS idx_funding_query    ON funding_rate (symbol, exchange, ts DESC);
CREATE INDEX IF NOT EXISTS idx_oi_query         ON open_interest (symbol, exchange, ts DESC);
CREATE INDEX IF NOT EXISTS idx_liquidations_q   ON liquidations (symbol, exchange, ts DESC);

-- ============ 资讯 / 信息 ============
CREATE TABLE IF NOT EXISTS news_raw (
  id                BIGSERIAL PRIMARY KEY,
  source            TEXT NOT NULL,
  url               TEXT UNIQUE,
  title             TEXT,
  content           TEXT,
  published_ts      TIMESTAMPTZ,
  fetched_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw_snapshot_path TEXT
);

CREATE TABLE IF NOT EXISTS news_event (
  id               BIGSERIAL PRIMARY KEY,
  news_id          BIGINT REFERENCES news_raw(id) ON DELETE CASCADE,
  symbols          TEXT[],
  event_type       TEXT,
  sentiment        DOUBLE PRECISION,
  impact_dir       TEXT,
  magnitude        DOUBLE PRECISION,
  extracted_by_model TEXT,
  extracted_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  confidence       DOUBLE PRECISION
);

-- ============ 规则 / 信号 ============
CREATE TABLE IF NOT EXISTS rule (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  exchange      TEXT NOT NULL DEFAULT 'okx',
  contract_type TEXT NOT NULL DEFAULT 'spot',
  timeframe     TEXT NOT NULL,
  dsl_json      JSONB NOT NULL,
  vote_min      INT NOT NULL DEFAULT 2,
  cooldown_bars INT NOT NULL DEFAULT 6,
  confidence    DOUBLE PRECISION NOT NULL DEFAULT 0.6,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  version       INT NOT NULL DEFAULT 1,
  created_ts    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signal (
  id              BIGSERIAL PRIMARY KEY,
  rule_id         BIGINT REFERENCES rule(id) ON DELETE CASCADE,
  symbol          TEXT NOT NULL,
  exchange        TEXT NOT NULL,
  contract_type   TEXT NOT NULL,
  timeframe       TEXT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  direction       TEXT NOT NULL,
  price_zone_low  DOUBLE PRECISION,
  price_zone_high DOUBLE PRECISION,
  tech_score      DOUBLE PRECISION,
  flow_score      DOUBLE PRECISION,
  info_score      DOUBLE PRECISION,
  total_score     DOUBLE PRECISION,
  passed          BOOLEAN NOT NULL,
  cooldown_until  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_signal_query ON signal (symbol, ts DESC);
-- P0-6 去重约束：同规则同 bar 同方向唯一（扫描重启后不重复推送）
CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_dedupe
  ON signal (rule_id, symbol, contract_type, timeframe, ts, direction);
-- P0-6 规则 upsert 幂等键
CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_name_ver ON rule (name, version);

CREATE TABLE IF NOT EXISTS signal_vote (
  id           BIGSERIAL PRIMARY KEY,
  signal_id    BIGINT NOT NULL REFERENCES signal(id) ON DELETE CASCADE,
  vote_type    TEXT NOT NULL,
  strength     DOUBLE PRECISION,
  evidence_json JSONB
);

-- ============ 台账 / 评测 ============
CREATE TABLE IF NOT EXISTS paper_position (
  id                    BIGSERIAL PRIMARY KEY,
  signal_id             BIGINT REFERENCES signal(id) ON DELETE SET NULL,
  symbol                TEXT NOT NULL,
  exchange              TEXT NOT NULL,
  contract_type         TEXT NOT NULL,
  direction             TEXT NOT NULL,
  entry_ts              TIMESTAMPTZ,
  entry_price           DOUBLE PRECISION,
  qty                   DOUBLE PRECISION,
  status                TEXT NOT NULL DEFAULT 'open',
  exit_ts               TIMESTAMPTZ,
  exit_price            DOUBLE PRECISION,
  pnl                   DOUBLE PRECISION,
  pnl_pct               DOUBLE PRECISION,
  max_adverse_excursion DOUBLE PRECISION
);
-- P1-1 台账幂等键：一笔仓位对应一个入场信号
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_position_signal ON paper_position (signal_id);

CREATE TABLE IF NOT EXISTS push_log (
  id         BIGSERIAL PRIMARY KEY,
  signal_id  BIGINT,
  channel    TEXT NOT NULL,
  sent_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
  status     TEXT NOT NULL,
  message_id TEXT,
  error      TEXT
);

CREATE TABLE IF NOT EXISTS annotation (
  id          BIGSERIAL PRIMARY KEY,
  signal_id   BIGINT NOT NULL REFERENCES signal(id) ON DELETE CASCADE,
  user_action TEXT NOT NULL,
  note        TEXT,
  created_ts  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ 回测 ============
CREATE TABLE IF NOT EXISTS backtest_run (
  id               BIGSERIAL PRIMARY KEY,
  rule_id          BIGINT REFERENCES rule(id) ON DELETE CASCADE,
  dataset_version  TEXT,
  seed             INT,
  params_json      JSONB,
  metrics_json     JSONB,
  walkforward_json JSONB,
  benchmark_symbol TEXT,
  engine           TEXT,
  status           TEXT NOT NULL DEFAULT 'pending',
  progress         DOUBLE PRECISION NOT NULL DEFAULT 0,
  created_ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_trade (
  id           BIGSERIAL PRIMARY KEY,
  run_id       BIGINT NOT NULL REFERENCES backtest_run(id) ON DELETE CASCADE,
  order_id     TEXT,
  side         TEXT,
  ts           TIMESTAMPTZ,
  price        DOUBLE PRECISION,
  qty          DOUBLE PRECISION,
  fee          DOUBLE PRECISION,
  funding      DOUBLE PRECISION,
  slippage_est DOUBLE PRECISION,
  status       TEXT
);

CREATE TABLE IF NOT EXISTS backtest_daily (
  run_id         BIGINT NOT NULL REFERENCES backtest_run(id) ON DELETE CASCADE,
  ts             TIMESTAMPTZ NOT NULL,
  equity         DOUBLE PRECISION,
  position_value DOUBLE PRECISION,
  daily_pnl      DOUBLE PRECISION,
  buy_value      DOUBLE PRECISION,
  sell_value     DOUBLE PRECISION,
  UNIQUE (run_id, ts)
);

CREATE TABLE IF NOT EXISTS backtest_cost (
  run_id         BIGINT PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  fee_total      DOUBLE PRECISION,
  funding_total  DOUBLE PRECISION,
  slippage_total DOUBLE PRECISION,
  cost_ratio     DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS backtest_bias_audit (
  run_id             BIGINT PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  survivorship_check TEXT,
  lookahead_check    TEXT,
  data_coverage_json JSONB
);

CREATE TABLE IF NOT EXISTS backtest_code_snapshot (
  run_id         BIGINT PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  strategy_script TEXT,
  config_json    JSONB
);
