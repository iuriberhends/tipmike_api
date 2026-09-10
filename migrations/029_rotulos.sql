-- ============================================================
-- Migration 029: ROTULOS (apelidos) — so' pra ORGANIZAR
--
-- POR QUE: hoje, pra criar varredura/esteira/backtest, e' preciso saber o
-- nome do parquet (hash + casa + liga + datas). Esta tabela guarda um
-- APELIDO livre por objeto. NADA no pipeline le' daqui: some a tabela e o
-- sistema roda igual. E' rotulo de tela.
--
--   escopo: 'parquet' | 'backtest' | 'varredura' | 'esteira'
--   chave : upload_id/caminho do parquet, ou o id do job (como texto)
--
-- Aplicar:
--   "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb ^
--        -f migrations\029_rotulos.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS rotulos (
    id            SERIAL PRIMARY KEY,
    escopo        TEXT NOT NULL,
    chave         TEXT NOT NULL,
    nome          TEXT NOT NULL,
    user_id       INTEGER,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rotulos_escopo_chk
        CHECK (escopo IN ('parquet', 'backtest', 'varredura', 'esteira'))
);

-- um apelido por objeto (o PUT sobrescreve)
CREATE UNIQUE INDEX IF NOT EXISTS idx_rotulos_escopo_chave
    ON rotulos (escopo, chave);

COMMENT ON TABLE rotulos IS
 'v029: apelidos de tela (parquet/backtest/varredura/esteira). Nenhum worker '
 'le desta tabela — e so organizacao pro usuario.';
