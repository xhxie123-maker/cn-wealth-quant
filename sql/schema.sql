-- ============================================================================
-- CN-WQDS V1.0 · PostgreSQL 建表脚本（23 张核心表）
-- 说明：V1.0 可以用 CSV/内存跑通，本脚本用于落库、审计与多设备同步。
-- 执行：psql -U postgres -d cnwq -f sql/schema.sql
-- 约定：
--   * 金额 numeric(20,2)，权重 numeric(10,6)，比率 numeric(12,6)
--   * 所有外部数据保留 event_time / published_at / available_at
--   * 历史数据不原地修改；更正走新版本或新记录
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS cnwq;
SET search_path TO cnwq, public;

-- ---------------------------------------------------------------------------
-- 1. instruments 标的静态信息
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id     VARCHAR(20) PRIMARY KEY,              -- 600519.SH / 510300.SH
    name              VARCHAR(80) NOT NULL,
    asset_type        VARCHAR(20) NOT NULL,                 -- stock/etf/bond_etf/money_etf/gold_etf
    exchange          VARCHAR(10) NOT NULL,                 -- SSE/SZSE
    board             VARCHAR(20) NOT NULL DEFAULT 'main',  -- main/gem/star
    industry          VARCHAR(40),
    currency          CHAR(3) NOT NULL DEFAULT 'CNY',
    lot_size          INTEGER NOT NULL DEFAULT 100,
    listed_date       DATE,
    delisted_date     DATE,
    is_st             BOOLEAN NOT NULL DEFAULT FALSE,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE instruments IS '标的静态信息（股票、场内 ETF、债券/货币/黄金 ETF）';

CREATE INDEX IF NOT EXISTS idx_instruments_type ON instruments (asset_type, is_active);
CREATE INDEX IF NOT EXISTS idx_instruments_industry ON instruments (industry);

-- ---------------------------------------------------------------------------
-- 2. trading_calendar 交易日历
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trading_calendar (
    exchange          VARCHAR(10) NOT NULL,
    trade_date        DATE NOT NULL,
    is_open           BOOLEAN NOT NULL DEFAULT TRUE,
    prev_trade_date   DATE,
    next_trade_date   DATE,
    note              VARCHAR(120),
    PRIMARY KEY (exchange, trade_date)
);
COMMENT ON TABLE trading_calendar IS '交易日历；周末与节假日 is_open=false';

CREATE INDEX IF NOT EXISTS idx_calendar_open ON trading_calendar (trade_date) WHERE is_open;

-- ---------------------------------------------------------------------------
-- 3. market_bars 日行情（含三时间戳与复权因子）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_bars (
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    trade_date        DATE NOT NULL,
    open              NUMERIC(20,4) NOT NULL,
    high              NUMERIC(20,4) NOT NULL,
    low               NUMERIC(20,4) NOT NULL,
    close             NUMERIC(20,4) NOT NULL,
    volume            BIGINT NOT NULL DEFAULT 0,
    amount            NUMERIC(20,2) NOT NULL DEFAULT 0,
    adj_factor        NUMERIC(20,8) NOT NULL DEFAULT 1,
    event_time        TIMESTAMPTZ NOT NULL,
    published_at      TIMESTAMPTZ NOT NULL,
    available_at      TIMESTAMPTZ NOT NULL,
    source            VARCHAR(40) NOT NULL DEFAULT 'sample',
    quality_flag      VARCHAR(20) NOT NULL DEFAULT 'OK',
    PRIMARY KEY (instrument_id, trade_date)
);
COMMENT ON TABLE market_bars IS '日行情；因子用复权价，成交与成本用原始价';

CREATE INDEX IF NOT EXISTS idx_bars_date ON market_bars (trade_date);
CREATE INDEX IF NOT EXISTS idx_bars_available ON market_bars (instrument_id, available_at);
CREATE INDEX IF NOT EXISTS idx_bars_quality ON market_bars (quality_flag) WHERE quality_flag <> 'OK';

-- ---------------------------------------------------------------------------
-- 4. corporate_actions 公司行动
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    id                BIGSERIAL PRIMARY KEY,
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    ex_date           DATE NOT NULL,
    action_type       VARCHAR(30) NOT NULL,                 -- cash_dividend/split/merge
    value             NUMERIC(20,6) NOT NULL DEFAULT 0,     -- 每股现金或拆股比例
    ratio             NUMERIC(12,6),
    currency          CHAR(3) NOT NULL DEFAULT 'CNY',
    event_time        TIMESTAMPTZ NOT NULL,
    published_at      TIMESTAMPTZ NOT NULL,
    available_at      TIMESTAMPTZ NOT NULL,
    source            VARCHAR(40) NOT NULL DEFAULT 'sample'
);
COMMENT ON TABLE corporate_actions IS '分红、拆股等公司行动；回测按除权日调整持仓';

CREATE UNIQUE INDEX IF NOT EXISTS uq_corp_action ON corporate_actions (instrument_id, ex_date, action_type);
CREATE INDEX IF NOT EXISTS idx_corp_action_available ON corporate_actions (instrument_id, available_at);

-- ---------------------------------------------------------------------------
-- 5. fundamentals 财务数据（PIT）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fundamentals (
    instrument_id       VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    period_end          DATE NOT NULL,
    published_at        TIMESTAMPTZ NOT NULL,
    available_at        TIMESTAMPTZ NOT NULL,
    report_type         VARCHAR(20) NOT NULL DEFAULT 'quarterly',
    revenue             NUMERIC(20,2),
    net_profit          NUMERIC(20,2),
    total_equity        NUMERIC(20,2),
    total_assets        NUMERIC(20,2),
    operating_cash_flow NUMERIC(20,2),
    roe                 NUMERIC(12,6),
    roa                 NUMERIC(12,6),
    debt_ratio          NUMERIC(12,6),
    profit_growth       NUMERIC(12,6),
    earnings_stability  NUMERIC(12,6),
    shares_outstanding  NUMERIC(20,2),
    eps                 NUMERIC(20,6),
    bps                 NUMERIC(20,6),
    source              VARCHAR(40) NOT NULL DEFAULT 'sample',
    PRIMARY KEY (instrument_id, period_end)
);
COMMENT ON TABLE fundamentals IS '季度/年度财务数据；决策只使用 available_at <= 决策时点的最近一期';

CREATE INDEX IF NOT EXISTS idx_fundamentals_available ON fundamentals (instrument_id, available_at);
CREATE INDEX IF NOT EXISTS idx_fundamentals_period ON fundamentals (period_end);

-- ---------------------------------------------------------------------------
-- 6. macro_indicators 宏观指标
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS macro_indicators (
    indicator         VARCHAR(40) NOT NULL,                 -- CPI_YOY/PMI/POLICY_RATE/YIELD_10Y
    period            DATE NOT NULL,                        -- 统计期（月末）
    value             NUMERIC(20,6) NOT NULL,
    unit              VARCHAR(20),
    published_at      TIMESTAMPTZ NOT NULL,
    available_at      TIMESTAMPTZ NOT NULL,
    source            VARCHAR(40) NOT NULL DEFAULT 'sample',
    PRIMARY KEY (indicator, period)
);
COMMENT ON TABLE macro_indicators IS '宏观指标；available_at 为发布后次一交易日 09:30';

CREATE INDEX IF NOT EXISTS idx_macro_available ON macro_indicators (indicator, available_at);

-- ---------------------------------------------------------------------------
-- 7. factor_definitions 因子定义
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_definitions (
    factor_code       VARCHAR(40) PRIMARY KEY,              -- ep/bp/roe/mom_252/vol_120 ...
    factor_group      VARCHAR(20) NOT NULL,                 -- value/quality/momentum/risk
    name_cn           VARCHAR(80) NOT NULL,
    direction         SMALLINT NOT NULL DEFAULT 1,          -- +1 越大越好；-1 越小越好
    formula           TEXT,
    description       TEXT,
    version           VARCHAR(20) NOT NULL DEFAULT '1.0.0',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE factor_definitions IS '因子元数据；direction 用于方向统一';

-- ---------------------------------------------------------------------------
-- 8. factor_values 因子值
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_values (
    factor_code       VARCHAR(40) NOT NULL REFERENCES factor_definitions (factor_code),
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    trade_date        DATE NOT NULL,
    raw_value         NUMERIC(24,8),
    zscore            NUMERIC(16,8),
    percentile        NUMERIC(12,6),
    coverage          NUMERIC(12,6),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (factor_code, instrument_id, trade_date)
);
COMMENT ON TABLE factor_values IS '因子原始值/标准化值；量级最大，建议按月分区';

CREATE INDEX IF NOT EXISTS idx_factor_values_date ON factor_values (trade_date, factor_code);
CREATE INDEX IF NOT EXISTS idx_factor_values_instrument ON factor_values (instrument_id, trade_date);

-- ---------------------------------------------------------------------------
-- 9. factor_snapshots 因子快照
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_snapshots (
    snapshot_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of             DATE NOT NULL,
    strategy_id       VARCHAR(40) NOT NULL,
    universe_hash     VARCHAR(64) NOT NULL,
    payload           JSONB NOT NULL,                       -- 因子值 + 贡献度 + 覆盖率
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE factor_snapshots IS '某日某策略的完整因子快照，用于复现与解释';

CREATE INDEX IF NOT EXISTS idx_snapshots_asof ON factor_snapshots (as_of, strategy_id);

-- ---------------------------------------------------------------------------
-- 10. market_regimes 市场状态
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_regimes (
    trade_date          DATE PRIMARY KEY,
    regime              CHAR(1) NOT NULL,                   -- A/B/C/D
    benchmark_trend     NUMERIC(12,6),                      -- 收盘/MA200 - 1
    realized_vol_60     NUMERIC(12,6),
    realized_vol_120    NUMERIC(12,6),
    volume_percentile   NUMERIC(12,6),
    cpi_yoy             NUMERIC(12,6),
    pmi                 NUMERIC(12,6),
    policy_rate         NUMERIC(12,6),
    yield_10y           NUMERIC(12,6),
    explanation         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE market_regimes IS '市场状态 A/B/C/D；只用于解释与仓位约束，不用于收益预测';

-- ---------------------------------------------------------------------------
-- 11. strategy_definitions 策略定义
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_definitions (
    strategy_id       VARCHAR(40) NOT NULL,
    version           VARCHAR(20) NOT NULL,
    name              VARCHAR(80) NOT NULL,
    params            JSONB NOT NULL,
    param_hash        VARCHAR(64) NOT NULL,
    code_path         VARCHAR(200),
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, version)
);
COMMENT ON TABLE strategy_definitions IS '策略参数与参数哈希；新参数必须新版本号';

CREATE INDEX IF NOT EXISTS idx_strategy_enabled ON strategy_definitions (enabled) WHERE enabled;

-- ---------------------------------------------------------------------------
-- 12. strategy_signals 策略信号
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_signals (
    id                BIGSERIAL PRIMARY KEY,
    strategy_id       VARCHAR(40) NOT NULL,
    version           VARCHAR(20) NOT NULL,
    as_of             DATE NOT NULL,
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    signal            VARCHAR(12) NOT NULL,                 -- 买入/加仓/持有/减仓/卖出/观望
    score             NUMERIC(16,8),
    rank              INTEGER,
    rationale         TEXT,
    invalidation      TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (strategy_id, version) REFERENCES strategy_definitions (strategy_id, version)
);
COMMENT ON TABLE strategy_signals IS '单标的信号与理由；唯一键防止重复生成';

CREATE UNIQUE INDEX IF NOT EXISTS uq_signal ON strategy_signals (strategy_id, version, as_of, instrument_id);
CREATE INDEX IF NOT EXISTS idx_signal_asof ON strategy_signals (as_of, strategy_id);

-- ---------------------------------------------------------------------------
-- 13. target_portfolios 目标组合
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS target_portfolios (
    id                BIGSERIAL PRIMARY KEY,
    strategy_id       VARCHAR(40) NOT NULL,
    version           VARCHAR(20) NOT NULL,
    as_of             DATE NOT NULL,
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    target_weight     NUMERIC(10,6) NOT NULL,
    approved_weight   NUMERIC(10,6) NOT NULL,
    risk_state        VARCHAR(12) NOT NULL,
    approval_token    VARCHAR(120),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (strategy_id, version) REFERENCES strategy_definitions (strategy_id, version)
);
COMMENT ON TABLE target_portfolios IS '策略目标权重 + 风控后权重；二者差异用于解释裁剪';

CREATE UNIQUE INDEX IF NOT EXISTS uq_target_portfolio
    ON target_portfolios (strategy_id, version, as_of, instrument_id);
CREATE INDEX IF NOT EXISTS idx_target_asof ON target_portfolios (as_of, strategy_id);

-- ---------------------------------------------------------------------------
-- 14. risk_policies 风控政策版本
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_policies (
    policy_version    VARCHAR(40) PRIMARY KEY,              -- RISK_POLICY_V1.0
    effective_from    DATE NOT NULL,
    effective_to      DATE,
    payload           JSONB NOT NULL,                       -- YAML 全文快照
    approved_by       VARCHAR(80),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE risk_policies IS '风控政策版本；一经签发不覆盖';

-- ---------------------------------------------------------------------------
-- 15. risk_assessments 风控评估
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_assessments (
    id                BIGSERIAL PRIMARY KEY,
    as_of             DATE NOT NULL,
    strategy_id       VARCHAR(40) NOT NULL,
    policy_version    VARCHAR(40) NOT NULL REFERENCES risk_policies (policy_version),
    state             VARCHAR(12) NOT NULL,                 -- GREEN/YELLOW/RED/SYSTEM_HALT
    passed            BOOLEAN NOT NULL,
    system_halt       BOOLEAN NOT NULL DEFAULT FALSE,
    nav_value         NUMERIC(20,2),
    metrics           JSONB NOT NULL DEFAULT '{}'::jsonb,
    checks            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE risk_assessments IS '某日某策略的风控评估汇总';

CREATE INDEX IF NOT EXISTS idx_risk_assessment_asof ON risk_assessments (as_of, strategy_id);
CREATE INDEX IF NOT EXISTS idx_risk_assessment_state ON risk_assessments (state);

-- ---------------------------------------------------------------------------
-- 16. risk_events 风控事件明细
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_events (
    id                BIGSERIAL PRIMARY KEY,
    assessment_id     BIGINT NOT NULL REFERENCES risk_assessments (id) ON DELETE CASCADE,
    code              VARCHAR(40) NOT NULL,
    name              VARCHAR(80) NOT NULL,
    level             VARCHAR(12) NOT NULL,
    passed            BOOLEAN NOT NULL,
    value             NUMERIC(20,8),
    limit_value       NUMERIC(20,8),
    message           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE risk_events IS '逐项风控检查明细，便于解释“为什么被否决”';

CREATE INDEX IF NOT EXISTS idx_risk_event_assessment ON risk_events (assessment_id);
CREATE INDEX IF NOT EXISTS idx_risk_event_level ON risk_events (level, passed);

-- ---------------------------------------------------------------------------
-- 17. approval_tokens 审批令牌
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval_tokens (
    token             VARCHAR(120) PRIMARY KEY,
    as_of             DATE NOT NULL,
    strategy_id       VARCHAR(40) NOT NULL,
    state             VARCHAR(12) NOT NULL,
    payload_digest    VARCHAR(64) NOT NULL,
    weights           JSONB NOT NULL,
    issued_at         TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    signature         VARCHAR(128) NOT NULL,
    verified_at       TIMESTAMPTZ,
    revoked           BOOLEAN NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE approval_tokens IS 'HMAC-SHA256 审批令牌；签名绑定日期/策略/权重/状态';

CREATE INDEX IF NOT EXISTS idx_approval_asof ON approval_tokens (as_of, strategy_id);
CREATE INDEX IF NOT EXISTS idx_approval_expiry ON approval_tokens (expires_at) WHERE NOT revoked;

-- ---------------------------------------------------------------------------
-- 18. backtest_runs 回测任务
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id            VARCHAR(40) PRIMARY KEY,              -- BT-20261107-001
    strategy_id       VARCHAR(40) NOT NULL,
    version           VARCHAR(20) NOT NULL,
    start_date        DATE NOT NULL,
    end_date          DATE NOT NULL,
    initial_cash      NUMERIC(20,2) NOT NULL,
    config_digest     VARCHAR(64) NOT NULL,
    data_digest       VARCHAR(64),
    engine_version    VARCHAR(20) NOT NULL DEFAULT '1.0.0',
    status            VARCHAR(20) NOT NULL DEFAULT 'RUNNING', -- RUNNING/SUCCESS/FAILED
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    error_message     TEXT,
    FOREIGN KEY (strategy_id, version) REFERENCES strategy_definitions (strategy_id, version)
);
COMMENT ON TABLE backtest_runs IS '回测任务；config_digest + data_digest 用于复现';

CREATE INDEX IF NOT EXISTS idx_backtest_status ON backtest_runs (status, started_at);

-- ---------------------------------------------------------------------------
-- 19. backtest_metrics 回测指标
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_metrics (
    run_id                VARCHAR(40) PRIMARY KEY REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    cagr                  NUMERIC(12,6),
    annualized_volatility NUMERIC(12,6),
    sharpe                NUMERIC(12,6),
    sortino               NUMERIC(12,6),
    max_drawdown          NUMERIC(12,6),
    max_drawdown_peak     DATE,
    max_drawdown_trough   DATE,
    max_drawdown_recovery DATE,
    calmar                NUMERIC(12,6),
    var_95                NUMERIC(12,6),
    es_95                 NUMERIC(12,6),
    beta                  NUMERIC(12,6),
    alpha                 NUMERIC(12,6),
    information_ratio     NUMERIC(12,6),
    annualized_turnover   NUMERIC(12,6),
    win_rate              NUMERIC(12,6),
    trades                INTEGER,
    final_nav             NUMERIC(20,2),
    raw_metrics           JSONB NOT NULL DEFAULT '{}'::jsonb
);
COMMENT ON TABLE backtest_metrics IS '回测绩效与风险指标；raw_metrics 保留完整 JSON';

-- ---------------------------------------------------------------------------
-- 20. backtest_trades 成交与被拒订单
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_trades (
    id                BIGSERIAL PRIMARY KEY,
    run_id            VARCHAR(40) NOT NULL REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    trade_date        DATE NOT NULL,
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    side              VARCHAR(10) NOT NULL,                 -- BUY/SELL
    shares            BIGINT NOT NULL DEFAULT 0,
    price             NUMERIC(20,4),
    gross_amount      NUMERIC(20,2),
    commission        NUMERIC(20,2) NOT NULL DEFAULT 0,
    stamp_duty        NUMERIC(20,2) NOT NULL DEFAULT 0,
    transfer_fee      NUMERIC(20,2) NOT NULL DEFAULT 0,
    slippage_cost     NUMERIC(20,2) NOT NULL DEFAULT 0,
    impact_cost       NUMERIC(20,2) NOT NULL DEFAULT 0,
    total_cost        NUMERIC(20,2) NOT NULL DEFAULT 0,
    status            VARCHAR(20) NOT NULL DEFAULT 'FILLED', -- FILLED/REJECTED/PARTIAL
    reject_reason     VARCHAR(120),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE backtest_trades IS '成交与被拒订单；费用拆项便于成本归因';

CREATE INDEX IF NOT EXISTS idx_trades_run ON backtest_trades (run_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_reject ON backtest_trades (reject_reason) WHERE status = 'REJECTED';

-- ---------------------------------------------------------------------------
-- 21. decision_cards 决策卡
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_cards (
    id                BIGSERIAL PRIMARY KEY,
    as_of             DATE NOT NULL,
    strategy_id       VARCHAR(40) NOT NULL,
    version           VARCHAR(20) NOT NULL,
    instrument_id     VARCHAR(20) NOT NULL REFERENCES instruments (instrument_id),
    signal            VARCHAR(12) NOT NULL,
    current_weight    NUMERIC(10,6),
    target_weight     NUMERIC(10,6),
    suggested_shares  BIGINT,
    suggested_amount  NUMERIC(20,2),
    risk_level        VARCHAR(12),
    rationale         TEXT,
    invalidation      TEXT,
    horizon           VARCHAR(40),
    data_as_of        TIMESTAMPTZ,
    approval_token    VARCHAR(120) REFERENCES approval_tokens (token),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (strategy_id, version) REFERENCES strategy_definitions (strategy_id, version)
);
COMMENT ON TABLE decision_cards IS '决策卡十字段；缺 approval_token 不得视为可执行建议';

CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_card
    ON decision_cards (as_of, strategy_id, version, instrument_id);
CREATE INDEX IF NOT EXISTS idx_decision_risk ON decision_cards (risk_level, as_of);

-- ---------------------------------------------------------------------------
-- 22. data_quality_reports 数据质量报告
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_quality_reports (
    id                BIGSERIAL PRIMARY KEY,
    as_of             DATE NOT NULL,
    dataset_digest    VARCHAR(64),
    passed            BOOLEAN NOT NULL,
    errors            INTEGER NOT NULL DEFAULT 0,
    warnings          INTEGER NOT NULL DEFAULT 0,
    issues            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE data_quality_reports IS '数据质量检查结果；错误默认阻断决策';

CREATE INDEX IF NOT EXISTS idx_quality_asof ON data_quality_reports (as_of, passed);

-- ---------------------------------------------------------------------------
-- 23. audit_log 哈希链审计日志
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    stream_id         VARCHAR(40) NOT NULL DEFAULT 'default',
    seq               BIGINT NOT NULL,
    event_type        VARCHAR(40) NOT NULL,
    actor             VARCHAR(80) NOT NULL DEFAULT 'system',
    payload           JSONB NOT NULL,
    prev_hash         CHAR(64) NOT NULL,
    hash              CHAR(64) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, seq)
);
COMMENT ON TABLE audit_log IS '只追加哈希链；应用层用 prev_hash/hash 校验完整性';

CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log (event_type, created_at);

-- ---------------------------------------------------------------------------
-- 可选：初始参考数据（按需启用）
-- ---------------------------------------------------------------------------
-- INSERT INTO risk_policies (policy_version, effective_from, payload)
-- VALUES ('RISK_POLICY_V1.0', DATE '2026-01-01', '{}'::jsonb);
