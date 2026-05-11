-- ============================================================
-- Migration 004: tabela backtest_jobs
--
-- Aplicar:
--   "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb -f migrations\004_backtest_jobs.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS backtest_jobs (
    id SERIAL PRIMARY KEY,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,

    -- Configuração
    data_inicio DATE NOT NULL,
    data_fim DATE NOT NULL,
    stake_modo TEXT NOT NULL DEFAULT 'fixo',           -- 'fixo' | 'ratchet'
    stake_valor NUMERIC(10, 2) NOT NULL,                -- valor fixo OU % no ratchet
    banca_inicial NUMERIC(12, 2) DEFAULT 1000.00,       -- só usado em ratchet

    -- Status
    status TEXT NOT NULL DEFAULT 'pendente',            -- pendente | rodando | concluido | erro
    progresso INTEGER NOT NULL DEFAULT 0,                -- 0..100
    progresso_msg TEXT,
    erro TEXT,

    -- Snapshot do bot (reprodutibilidade)
    bot_snapshot JSONB,

    -- Métricas agregadas
    total_ticks_avaliados INTEGER,
    total_apostas INTEGER,
    green INTEGER,
    red INTEGER,
    void_count INTEGER,                                  -- 'void' é palavra reservada em alguns contextos
    pnl NUMERIC(12, 2),
    roi NUMERIC(8, 4),
    win_rate NUMERIC(8, 4),
    drawdown_max NUMERIC(12, 2),
    max_streak_red INTEGER,
    dias_verdes INTEGER,
    dias_total INTEGER,

    -- Detalhamento
    equity_curve JSONB,                                  -- [{n, banca, pnl_acum, ts}, ...]
    apostas_detalhe JSONB,                               -- [{event_id, mt, linha, odd, stake, resultado, pnl, ts}, ...]
    pnl_por_dia JSONB,                                   -- [{data, apostas, pnl}, ...]

    -- Timestamps
    iniciado_em TIMESTAMPTZ DEFAULT NOW(),
    concluido_em TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_backtest_jobs_bot_id ON backtest_jobs(bot_id);
CREATE INDEX IF NOT EXISTS idx_backtest_jobs_status ON backtest_jobs(status);
CREATE INDEX IF NOT EXISTS idx_backtest_jobs_iniciado ON backtest_jobs(iniciado_em DESC);

COMMENT ON TABLE backtest_jobs IS 'Jobs de backtest dos bots — cada execução é uma row';
COMMENT ON COLUMN backtest_jobs.bot_snapshot IS 'Snapshot do formState do bot pra reprodutibilidade';
COMMENT ON COLUMN backtest_jobs.equity_curve IS 'Pontos do gráfico equity';
COMMENT ON COLUMN backtest_jobs.apostas_detalhe IS 'Detalhamento de cada aposta simulada';
