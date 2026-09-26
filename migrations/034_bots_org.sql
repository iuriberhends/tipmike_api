-- ============================================================
-- 034_bots_org.sql — v50: FAVORITOS e GRUPOS de bots (por usuário)
-- ============================================================
-- Tabelas NOVAS. A tabela `bots` não muda: executor, backtest e clones
-- seguem iguais. Idempotente: pode rodar de novo sem erro.
-- Apagar um bot limpa sozinho o favorito/grupo dele (ON DELETE CASCADE).
-- Apagar um GRUPO não apaga bot nenhum: os bots voltam pra "Sem grupo".
BEGIN;

CREATE TABLE IF NOT EXISTS bot_grupos (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER      NOT NULL,
    nome       VARCHAR(60)  NOT NULL,
    cor        VARCHAR(7),
    ordem      INTEGER      NOT NULL DEFAULT 0,
    criado_em  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bot_grupos_user_nome
    ON bot_grupos (user_id, lower(nome));

CREATE TABLE IF NOT EXISTS bot_grupo_membros (
    user_id   INTEGER NOT NULL,
    bot_id    INTEGER NOT NULL REFERENCES bots(id)       ON DELETE CASCADE,
    grupo_id  INTEGER NOT NULL REFERENCES bot_grupos(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, bot_id)
);
CREATE INDEX IF NOT EXISTS ix_bot_grupo_membros_grupo ON bot_grupo_membros (grupo_id);
CREATE INDEX IF NOT EXISTS ix_bot_grupo_membros_bot   ON bot_grupo_membros (bot_id);

CREATE TABLE IF NOT EXISTS bot_favoritos (
    user_id    INTEGER     NOT NULL,
    bot_id     INTEGER     NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    criado_em  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, bot_id)
);
CREATE INDEX IF NOT EXISTS ix_bot_favoritos_bot ON bot_favoritos (bot_id);

COMMIT;
