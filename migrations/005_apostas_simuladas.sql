-- migrations/005_apostas_simuladas.sql
-- Tabela de apostas simuladas (paper-trading)
-- Cada vez que um bot ativo encontra um tick que passa nos filtros,
-- registra aqui. Quando o jogo termina, atualiza com resultado.

CREATE TABLE IF NOT EXISTS apostas_simuladas (
    id BIGSERIAL PRIMARY KEY,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,

    -- Identificacao do tick que disparou
    tick_id BIGINT,                          -- FK soft pro ticks.id
    event_id TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    sport TEXT,
    liga TEXT,

    -- Snapshot do tick no momento da aposta
    ts_aposta TIMESTAMP NOT NULL DEFAULT NOW(),
    jogador_a TEXT,
    jogador_b TEXT,
    time_a TEXT,
    time_b TEXT,
    score_home_no_momento INTEGER,
    score_away_no_momento INTEGER,
    live_time TEXT,
    mercado TEXT,
    mercado_tipo TEXT,
    linha NUMERIC,
    selecao TEXT,
    odds NUMERIC NOT NULL,

    -- Stake/financeiro (paper)
    stake NUMERIC NOT NULL DEFAULT 10.00,

    -- Resultado (preenchido quando jogo termina)
    -- status: pendente | resolvida | cancelada
    status TEXT NOT NULL DEFAULT 'pendente',
    score_home_final INTEGER,
    score_away_final INTEGER,
    resultado TEXT,                          -- green | red | void
    pnl NUMERIC,                             -- + se green, - stake se red, 0 se void
    resolvida_em TIMESTAMP,

    -- Stats H2H que dispararam (debug/auditoria)
    stats_h2h JSONB,

    criada_em TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Indices para queries comuns
CREATE INDEX IF NOT EXISTS idx_apostas_sim_bot ON apostas_simuladas(bot_id);
CREATE INDEX IF NOT EXISTS idx_apostas_sim_status ON apostas_simuladas(status);
CREATE INDEX IF NOT EXISTS idx_apostas_sim_ts ON apostas_simuladas(ts_aposta DESC);
CREATE INDEX IF NOT EXISTS idx_apostas_sim_event ON apostas_simuladas(event_id);
CREATE INDEX IF NOT EXISTS idx_apostas_sim_pendentes ON apostas_simuladas(status, ts_aposta)
    WHERE status = 'pendente';

-- Constraint pra evitar duplicata: 1 aposta por bot+tick
-- (se o executor processar o mesmo tick 2x, nao duplica)
CREATE UNIQUE INDEX IF NOT EXISTS uq_apostas_sim_bot_tick
    ON apostas_simuladas(bot_id, tick_id)
    WHERE tick_id IS NOT NULL;

COMMENT ON TABLE apostas_simuladas IS 'Apostas geradas pelos bots em modo simulacao (paper-trading). Resultado preenchido quando jogo termina.';
