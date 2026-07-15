-- ============================================================
-- 015_convites.sql
-- Fase 5: convites de registro (uso único, gerados pelo admin).
-- O código cru NUNCA é salvo — apenas o SHA-256 (codigo_hash).
-- Idempotente: pode rodar mais de uma vez.
--
-- Rodar:  psql -U postgres -d mikedb -f migrations/015_convites.sql
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS convites (
    id          SERIAL PRIMARY KEY,
    codigo_hash TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',
    nota        TEXT,
    criado_por  INT REFERENCES usuarios(id) ON DELETE SET NULL,
    usado_por   INT REFERENCES usuarios(id) ON DELETE SET NULL,
    usado_em    TIMESTAMPTZ,
    revogado    BOOLEAN NOT NULL DEFAULT false,
    expira_em   TIMESTAMPTZ NOT NULL,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT convites_role_chk     CHECK (role IN ('admin', 'user')),
    CONSTRAINT convites_codigo_uk    UNIQUE (codigo_hash),
    CONSTRAINT convites_nota_len_chk CHECK (nota IS NULL OR char_length(nota) <= 200)
);

CREATE INDEX IF NOT EXISTS convites_criado_em_idx ON convites (criado_em DESC);

COMMIT;
