-- ============================================================
-- 013_usuarios_auth.sql
-- Usuários + refresh tokens (Fase 1 do sistema de auth)
-- Idempotente: pode ser executada mais de uma vez sem efeito colateral.
--
-- Rodar:  psql -U postgres -d mikedb -f migrations/013_usuarios_auth.sql
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS usuarios (
    id           SERIAL PRIMARY KEY,
    email        TEXT NOT NULL,
    nome         TEXT NOT NULL,
    senha_hash   TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'user',
    ativo        BOOLEAN NOT NULL DEFAULT true,
    criado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_login TIMESTAMPTZ,
    CONSTRAINT usuarios_role_chk      CHECK (role IN ('admin', 'user')),
    CONSTRAINT usuarios_email_len_chk CHECK (char_length(email) BETWEEN 3 AND 255),
    CONSTRAINT usuarios_nome_len_chk  CHECK (char_length(nome) BETWEEN 2 AND 80)
);

-- Unicidade case-insensitive de e-mail (a aplicação também normaliza pra lower)
CREATE UNIQUE INDEX IF NOT EXISTS usuarios_email_lower_uidx ON usuarios (lower(email));

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          BIGSERIAL PRIMARY KEY,
    usuario_id  INT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,       -- SHA-256 do token; o token cru NUNCA é salvo
    expira_em   TIMESTAMPTZ NOT NULL,
    revogado    BOOLEAN NOT NULL DEFAULT false,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
    criado_ip   TEXT,
    CONSTRAINT refresh_tokens_hash_uk UNIQUE (token_hash)
);

CREATE INDEX IF NOT EXISTS refresh_tokens_usuario_idx ON refresh_tokens (usuario_id);
CREATE INDEX IF NOT EXISTS refresh_tokens_expira_idx  ON refresh_tokens (expira_em);

COMMIT;
