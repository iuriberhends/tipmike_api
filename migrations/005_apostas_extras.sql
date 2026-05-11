-- migrations/005_apostas_extras.sql
-- Adiciona colunas faltantes na tabela `apostas` pra suportar bots de simulacao
--
-- CONTEXTO: a tabela `apostas` ja existe com campo `modo` (real/simulado).
-- Estamos adicionando campos extras pra:
-- 1. Auditoria/debug: stats_h2h que disparou a aposta
-- 2. Snapshot do momento: live_time, mercado_tipo (pra correlacionar com tick_id)
-- 3. Tracking: tick_id que originou a aposta (FK soft pro ticks)
-- 4. Stake: pra calcular ROI/PnL da simulacao

ALTER TABLE apostas
    ADD COLUMN IF NOT EXISTS tick_id BIGINT,
    ADD COLUMN IF NOT EXISTS bookmaker TEXT,
    ADD COLUMN IF NOT EXISTS liga TEXT,
    ADD COLUMN IF NOT EXISTS live_time TEXT,
    ADD COLUMN IF NOT EXISTS mercado_tipo TEXT,
    ADD COLUMN IF NOT EXISTS selecao TEXT,
    ADD COLUMN IF NOT EXISTS stake NUMERIC NOT NULL DEFAULT 10.00,
    ADD COLUMN IF NOT EXISTS pnl NUMERIC,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pendente',
    ADD COLUMN IF NOT EXISTS stats_h2h JSONB,
    ADD COLUMN IF NOT EXISTS score_home_no_momento INTEGER,
    ADD COLUMN IF NOT EXISTS score_away_no_momento INTEGER;

-- Indexes para queries comuns do bot_executor
-- Nota: idx_apostas_modo ja deve existir (modo eh queryable). Confirme com \d apostas

CREATE INDEX IF NOT EXISTS idx_apostas_status_pendente
    ON apostas(status, apostado_em)
    WHERE status = 'pendente';

CREATE INDEX IF NOT EXISTS idx_apostas_bot_modo
    ON apostas(bot_id, modo);

-- Constraint pra evitar duplicata: 1 aposta por bot+tick
-- (se o executor processar o mesmo tick 2x, nao duplica)
CREATE UNIQUE INDEX IF NOT EXISTS uq_apostas_bot_tick
    ON apostas(bot_id, tick_id)
    WHERE tick_id IS NOT NULL;

COMMENT ON COLUMN apostas.tick_id IS 'ID do tick que disparou esta aposta (FK soft pro ticks)';
COMMENT ON COLUMN apostas.status IS 'pendente | resolvida | cancelada';
COMMENT ON COLUMN apostas.stats_h2h IS 'Snapshot dos stats H2H que dispararam a aposta (debug/auditoria)';
COMMENT ON COLUMN apostas.stake IS 'Valor apostado (R$). Default 10 pra simulacao.';
COMMENT ON COLUMN apostas.pnl IS 'Lucro/prejuizo em R$. green: stake*(odd-1), red: -stake, void: 0.';
