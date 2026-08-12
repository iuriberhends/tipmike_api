-- ===========================================================================
--  VARREDURA COMO JOB DO SISTEMA — migration
--  Rodar:
--    set PGPASSWORD=mikedb0702&& "C:\Program Files\PostgreSQL\18\bin\psql.exe" ^
--        -U postgres -d mikedb -f migration_varredura.sql
--
--  Puramente ADITIVA: cria uma tabela nova, nao toca em nada existente.
--  Segura pra rodar 2x (tudo IF NOT EXISTS).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS varredura_jobs (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER,
    -- de onde vem o dado: um backtest JA RODADO. O varredor le o
    -- apostas_detalhe desse job (que ja tem event_id), entao nao ha upload
    -- manual nem risco de a planilha estar defasada.
    job_backtest_id   INTEGER NOT NULL,
    nome              TEXT,

    -- o que foi pedido (modo, janelas, min_apostas, guardar, nlmax, ...)
    params            JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- o que a rodada REALMENTE cobriu (grades de linha, janelas em uso, eixos,
    -- cego, total estimado). Sai do --plano. Guardado junto do resultado pra
    -- daqui a um mes dar pra saber o que aquele garimpo varreu de fato.
    contrato          JSONB,

    -- PRE-COMPROMISSO: a busca so enxerga ate esta data; o resto vira holdout
    -- e e' medido no fim pelo repontua. NULL = o worker calcula 70/30.
    data_corte        DATE,

    status            TEXT NOT NULL DEFAULT 'pendente',
    -- pendente | planejando | planejado | rodando | concluido | erro | cancelado
    progresso         INTEGER NOT NULL DEFAULT 0,
    progresso_msg     TEXT,
    erro              TEXT,

    arquivo_saida     TEXT,      -- xlsx do garimpo
    arquivo_tudo      TEXT,      -- csv completo
    arquivo_holdout   TEXT,      -- csv do repontua no holdout
    resumo            JSONB,     -- configs, aprovadas, sobreviventes, gate

    pid               INTEGER,   -- pra matar/detectar orfao
    criado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
    iniciado_em       TIMESTAMPTZ,
    concluido_em      TIMESTAMPTZ
);

-- fila: o daemon busca por status + ordem de chegada
CREATE INDEX IF NOT EXISTS idx_varredura_status
    ON varredura_jobs (status, criado_em);
CREATE INDEX IF NOT EXISTS idx_varredura_user
    ON varredura_jobs (user_id, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_varredura_origem
    ON varredura_jobs (job_backtest_id);

-- guarda-chuva: status so pode ser um dos previstos (pega bug de digitacao
-- antes de virar job fantasma na fila)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'varredura_status_valido') THEN
        ALTER TABLE varredura_jobs
            ADD CONSTRAINT varredura_status_valido CHECK (status IN (
                'pendente', 'planejando', 'planejado', 'rodando',
                'concluido', 'erro', 'cancelado'));
    END IF;
END $$;

-- conferencia
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = 'varredura_jobs'
 ORDER BY ordinal_position;
