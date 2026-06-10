-- ============================================================================
-- CATALOGO DE TORNEIOS (cache persistente) — leitura instantanea pro frontend
-- Pre-computa torneios + grades + jogadores + times por casa/esporte.
-- O endpoint le SO daqui (rapido). Atualizado por atualizar_catalogo.py.
-- ============================================================================
CREATE TABLE IF NOT EXISTS catalogo_torneios (
    casa         TEXT NOT NULL,
    esporte      TEXT NOT NULL,
    payload      JSONB NOT NULL,          -- resultado pronto (torneios/grades/jogadores/times)
    atualizado   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (casa, esporte)
);

-- tabela de "visto recentemente" pro incremental saber o que ja conhece
CREATE TABLE IF NOT EXISTS catalogo_visto (
    casa      TEXT NOT NULL,
    esporte   TEXT NOT NULL,
    liga      TEXT NOT NULL,
    PRIMARY KEY (casa, esporte, liga)
);

-- marca de quando foi a ultima atualizacao incremental (pra olhar so o novo)
CREATE TABLE IF NOT EXISTS catalogo_meta (
    chave   TEXT PRIMARY KEY,
    valor   TEXT
);
