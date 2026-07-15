-- ============================================================
-- 014_ownership.sql
-- Fase 4: dono (user_id) em bots e backtest_jobs.
-- Idempotente: pode rodar mais de uma vez.
--
-- Regras de backfill:
--   bots legados          -> admin ativo mais antigo
--   backtest_jobs legados -> dono do bot; sem bot -> admin
--
-- Pré-requisito: migration 013 aplicada e ao menos 1 admin ativo.
-- Rodar:  psql -U postgres -d mikedb -f migrations/014_ownership.sql
-- ============================================================

BEGIN;

-- ── bots.user_id ────────────────────────────────────────────
ALTER TABLE bots ADD COLUMN IF NOT EXISTS user_id INT;

UPDATE bots
SET user_id = (
    SELECT id FROM usuarios
    WHERE role = 'admin' AND ativo
    ORDER BY id
    LIMIT 1
)
WHERE user_id IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM bots WHERE user_id IS NULL) THEN
        RAISE EXCEPTION
            'Migration 014: existem bots sem dono e nenhum admin ativo em usuarios. Crie um admin (python scripts/criar_admin.py) e rode novamente.';
    END IF;
END $$;

ALTER TABLE bots ALTER COLUMN user_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bots_user_id_fkey') THEN
        ALTER TABLE bots
            ADD CONSTRAINT bots_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS bots_user_id_idx ON bots (user_id);

-- ── backtest_jobs.user_id (nullable; legados vão pro dono/admin) ──
ALTER TABLE backtest_jobs ADD COLUMN IF NOT EXISTS user_id INT;

UPDATE backtest_jobs bj
SET user_id = b.user_id
FROM bots b
WHERE bj.user_id IS NULL AND bj.bot_id = b.id;

UPDATE backtest_jobs
SET user_id = (
    SELECT id FROM usuarios
    WHERE role = 'admin' AND ativo
    ORDER BY id
    LIMIT 1
)
WHERE user_id IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'backtest_jobs_user_id_fkey') THEN
        ALTER TABLE backtest_jobs
            ADD CONSTRAINT backtest_jobs_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS backtest_jobs_user_id_idx ON backtest_jobs (user_id);

COMMIT;
