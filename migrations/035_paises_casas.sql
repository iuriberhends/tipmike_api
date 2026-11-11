-- 035 (11/nov/2026) — CADASTRO DE PAÍSES E CASAS
--
-- Até aqui a "casa" era texto livre (ticks.bookmaker, bots.casa) e cada parte do
-- sistema tinha a sua lista fixa: CASAS_VALIDAS nos bots, COMBINACOES no catálogo,
-- MERCADO_TIPOS_POR_CASA no motor e 4 listas diferentes no site. Com a operação
-- do Peru (e outros países depois), a casa passa a ter dono: um PAÍS.
--
-- Regras:
--   * casas.codigo = ticks.bookmaker = bots.casa = pasta bookmaker= do parquet.
--   * Casa de fora do Brasil leva o sufixo do país no código (betsson_pe). As do
--     Brasil seguem sem sufixo, como sempre foram.
--   * pais NULL = casa GLOBAL (aparece em todos os países). Hoje só a bet365:
--     uma coleta só (JBot), usada pelo Brasil e como referência no Peru.
--   * ao_vivo = os robôs ao vivo podem usar a casa (gatilho tick_novo + executor).
--     Peru nasce DESLIGADO: só dados e backtest até o motor ler o espanhol.
--   * idioma = idioma dos MERCADOS no feed (o motor escolhe o vocabulário por ele).
--   * Seeds com ON CONFLICT DO NOTHING: rodar de novo não desfaz ajuste feito à mão.

CREATE TABLE IF NOT EXISTS paises (
    codigo        TEXT PRIMARY KEY,                 -- ISO 3166-1 alfa-2
    nome          TEXT NOT NULL,
    moeda         TEXT NOT NULL,                    -- ISO 4217
    simbolo_moeda TEXT NOT NULL,
    fuso          TEXT NOT NULL,                    -- IANA
    idioma        TEXT NOT NULL,                    -- locale da interface
    sufixo        TEXT NOT NULL DEFAULT '',         -- sufixo do código das casas
    ativo         BOOLEAN NOT NULL DEFAULT TRUE,
    ordem         SMALLINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS casas (
    codigo     TEXT PRIMARY KEY,
    nome       TEXT NOT NULL,
    pais       TEXT REFERENCES paises(codigo),      -- NULL = global
    plataforma TEXT,                                -- obg, altenar, kambi, kaizen, jbot...
    idioma     TEXT NOT NULL DEFAULT 'pt',          -- pt | es | en (mercados do feed)
    cor        TEXT,                                -- família de cor do site (emerald, blue...)
    ativa      BOOLEAN NOT NULL DEFAULT TRUE,       -- aparece nas listas
    ao_vivo    BOOLEAN NOT NULL DEFAULT TRUE,       -- robôs ao vivo podem usar
    ordem      SMALLINT NOT NULL DEFAULT 0,
    atualizado TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO paises (codigo, nome, moeda, simbolo_moeda, fuso, idioma, sufixo, ordem) VALUES
    ('BR', 'Brasil', 'BRL', 'R$', 'America/Sao_Paulo', 'pt-BR', '',    1),
    ('PE', 'Peru',   'PEN', 'S/', 'America/Lima',      'es-PE', '_pe', 2)
ON CONFLICT (codigo) DO NOTHING;

INSERT INTO casas (codigo, nome, pais, plataforma, idioma, cor, ativa, ao_vivo, ordem) VALUES
    -- global
    ('bet365',           'Bet365',        NULL, 'jbot',      'en', 'emerald', TRUE,  TRUE,  1),
    -- Brasil
    ('superbet',         'Superbet',      'BR', 'superbet',  'pt', 'amber',   TRUE,  TRUE,  2),
    ('betano',           'Betano',        'BR', 'kaizen',    'pt', 'blue',    TRUE,  TRUE,  3),
    ('estrelabet',       'Estrelabet',    'BR', 'altenar',   'pt', 'purple',  TRUE,  TRUE,  4),
    ('kto',              'KTO',           'BR', 'superbet',  'pt', NULL,      TRUE,  TRUE,  5),
    ('apostaganha',      'ApostaGanha',   'BR', NULL,        'pt', NULL,      TRUE,  TRUE,  6),
    ('novibet',          'Novibet',       'BR', NULL,        'pt', NULL,      TRUE,  TRUE,  7),
    ('vupi',             'Vupi',          'BR', NULL,        'pt', NULL,      TRUE,  TRUE,  8),
    ('meridianbet',      'Meridianbet',   'BR', NULL,        'pt', NULL,      TRUE,  TRUE,  9),
    ('betnacional',      'Betnacional',   'BR', NULL,        'pt', NULL,      TRUE,  TRUE, 10),
    ('betmgm',           'BetMGM',        'BR', NULL,        'pt', NULL,      TRUE,  TRUE, 11),
    ('betboom',          'Betboom',       'BR', NULL,        'pt', NULL,      TRUE,  TRUE, 12),
    ('starbet',          'Starbet',       'BR', NULL,        'pt', NULL,      TRUE,  TRUE, 13),
    -- Peru (ao vivo desligado: só dados e backtest)
    ('betsson_pe',       'Betsson',       'PE', 'obg',       'es', NULL,      TRUE,  FALSE, 1),
    ('betsafe_pe',       'Betsafe',       'PE', 'obg',       'es', NULL,      TRUE,  FALSE, 2),
    ('inkabet_pe',       'Inkabet',       'PE', 'obg',       'es', NULL,      TRUE,  FALSE, 3),
    ('jokerbet_pe',      'Jokerbet',      'PE', 'altenar',   'es', NULL,      TRUE,  FALSE, 4),
    ('masterbets365_pe', 'Masterbets365', 'PE', 'altenar',   'es', NULL,      TRUE,  FALSE, 5),
    ('rushbet_pe',       'Rushbet',       'PE', 'kambi',     'es', NULL,      TRUE,  FALSE, 6),
    ('olimpo_pe',        'Olimpo',        'PE', 'kambi',     'es', NULL,      TRUE,  FALSE, 7),
    ('betano_pe',        'Betano',        'PE', 'kaizen',    'es', 'blue',    TRUE,  FALSE, 8)
ON CONFLICT (codigo) DO NOTHING;
