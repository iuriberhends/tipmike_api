-- ============================================================
-- 016_backtest_unidades.sql
-- Auditoria job 42: resultado do backtest tambem em UNIDADES.
--   pnl_unidades       = lucro acumulado em unidades (1u = stake da aposta)
--   drawdown_unidades  = drawdown maximo pico->vale, cronologico, em unidades
-- DOUBLE PRECISION de proposito: asyncpg devolve float direto e o
-- _row_to_job_dict do router nao precisa de mudanca (SELECT * ja inclui).
-- Idempotente (IF NOT EXISTS) - pode rodar mais de uma vez sem erro.
-- ============================================================

ALTER TABLE backtest_jobs
    ADD COLUMN IF NOT EXISTS pnl_unidades       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS drawdown_unidades  DOUBLE PRECISION;

-- confere
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'backtest_jobs'
  AND column_name IN ('pnl_unidades', 'drawdown_unidades');
