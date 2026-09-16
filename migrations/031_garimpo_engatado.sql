-- ============================================================
-- Migration 031: garimpo ENGATADO no job-mae (status 'aguardando_origem')
--
-- O botao "Job-mae pra garimpo" cria o backtest (candidatos + carimbo) e
-- ja deixa a varredura criada com status 'aguardando_origem'; o daemon a
-- solta ('pendente') quando o backtest concluir. Se a tabela tiver CHECK
-- de status, ele barraria o valor novo — este script remove qualquer CHECK
-- sobre varredura_jobs.status (os status sao geridos pelo codigo).
-- Idempotente; nao mexe em dados.
-- ============================================================
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT con.conname
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
         WHERE rel.relname = 'varredura_jobs'
           AND con.contype = 'c'
           AND pg_get_constraintdef(con.oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE varredura_jobs DROP CONSTRAINT %I', r.conname);
        RAISE NOTICE 'removido CHECK % de varredura_jobs.status', r.conname;
    END LOOP;
END $$;

-- conferir:
--   SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--    WHERE conrelid = 'varredura_jobs'::regclass;
