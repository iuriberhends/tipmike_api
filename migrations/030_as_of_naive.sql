-- ============================================================
-- Migration 030: o carimbo do h2h vira NAIVE (sem fuso)
--
-- POR QUE: a 028 criou `h2h_historico.inserted_at` e
-- `backtest_jobs.h2h_as_of` como TIMESTAMPTZ, mas o projeto INTEIRO e'
-- naive (o `Z` do feed ja e' BRT). Resultado: o detector de mudanca do
-- h2h da esteira le MAX(inserted_at) -> vem com fuso -> grava em
-- esteira_jobs.h2h_ts_inicio (sem fuso) e o asyncpg estoura:
--   "can't subtract offset-naive and offset-aware datetimes"
-- (rodadas 22 e 25 morreram com 0 itens por causa disso).
--
-- Converte usando o fuso da sessao; as 3,4M linhas antigas estao todas em
-- 2000-01-01, entao o deslocamento nao muda nada na pratica.
--
-- ATENCAO: reescreve a tabela h2h_historico (alguns minutos, como a 028).
-- Rodar com a esteira e os garimpos parados.
--
--   "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb ^
--        -f migrations\030_as_of_naive.sql
-- ============================================================

ALTER TABLE h2h_historico
    ALTER COLUMN inserted_at DROP DEFAULT;

ALTER TABLE h2h_historico
    ALTER COLUMN inserted_at TYPE TIMESTAMP
    USING inserted_at AT TIME ZONE current_setting('TimeZone');

ALTER TABLE h2h_historico
    ALTER COLUMN inserted_at SET DEFAULT now();

ALTER TABLE backtest_jobs
    ALTER COLUMN h2h_as_of TYPE TIMESTAMP
    USING h2h_as_of AT TIME ZONE current_setting('TimeZone');

-- conferir (as duas tem que sair como 'timestamp without time zone'):
--   SELECT table_name, column_name, data_type
--     FROM information_schema.columns
--    WHERE (table_name='h2h_historico' AND column_name='inserted_at')
--       OR (table_name='backtest_jobs' AND column_name='h2h_as_of');
