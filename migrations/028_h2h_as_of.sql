-- ============================================================
-- Migration 028: CARIMBO DO H2H (as-of) — numero reproduzivel
--
-- POR QUE: o h2h_historico recebe jogos novos E retroativos (o seeder
-- preenche buracos pra tras). Sem carimbo, o mesmo job rodado 40 min
-- depois enxerga outro historico e o chip muda: entre o garimpo 27
-- (18:52) e a esteira 20 (19:30) a MESMA config passou de 108 pra 608
-- apostas. Com o carimbo, o job so' enxerga o que ja estava la'.
--
-- ADITIVO: job sem carimbo (h2h_as_of NULL) roda exatamente como antes.
--
-- Aplicar:
--   set PGPASSWORD=mikedb0702
--   "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb ^
--        -f migrations\028_h2h_as_of.sql
-- ============================================================

-- 1) quando cada linha do historico ENTROU no banco
ALTER TABLE h2h_historico
    ADD COLUMN IF NOT EXISTS inserted_at TIMESTAMPTZ;

-- as linhas que ja existem sao anteriores a qualquer carimbo futuro:
-- data antiga = sempre visiveis (nunca somem de job nenhum)
UPDATE h2h_historico
   SET inserted_at = TIMESTAMPTZ '2000-01-01 00:00:00+00'
 WHERE inserted_at IS NULL;

ALTER TABLE h2h_historico
    ALTER COLUMN inserted_at SET DEFAULT now();
ALTER TABLE h2h_historico
    ALTER COLUMN inserted_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_h2h_hist_inserted_at
    ON h2h_historico (inserted_at);

-- 2) o carimbo do job
ALTER TABLE backtest_jobs
    ADD COLUMN IF NOT EXISTS h2h_as_of TIMESTAMPTZ;

COMMENT ON COLUMN backtest_jobs.h2h_as_of IS
 'v28: chip so enxerga h2h_historico com inserted_at <= este instante. '
 'NULL = ve o banco inteiro (comportamento historico).';

-- conferir:
--   SELECT count(*) FILTER (WHERE inserted_at < '2001-01-01') AS antigas,
--          count(*) FILTER (WHERE inserted_at >= '2001-01-01') AS novas
--     FROM h2h_historico;
