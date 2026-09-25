-- ============================================================
-- 032_mentor_v3.sql  —  MIKE MENTOR, schema v3 (spec v3 + v3.2)
--
-- Substitui o protótipo 020/021 inteiro. Vive no schema "mentor" do
-- mikedb, ao lado das tabelas dos bots, sem tocar nelas.
--
-- Vocabulário (spec §3.0): MATÉRIA → MÓDULO → ASSUNTO → AULA.
--   assunto = unidade de estudo e de revisão (tier, modo, gate, R1/R7/R30)
--   aula    = uma videoaula do Gran (ou aula própria); gate por aula
--   etiqueta = o "assunto" do Gran Questões (id do Gran), com pai e raiz
--
-- ATENÇÃO: este arquivo DERRUBA o schema mentor e recria do zero.
-- Só rodar antes de haver estudo gravado. Depois disso, migrar por ALTER.
-- ============================================================

DROP SCHEMA IF EXISTS mentor CASCADE;
CREATE SCHEMA mentor;

-- ---------------------------------------------------------------
-- Árvore do edital verticalizado (vem do edital_verticalizado_v2)
-- ---------------------------------------------------------------
CREATE TABLE mentor.materia (
    id                      INT PRIMARY KEY,           -- id do yaml (1..12)
    nome                    TEXT NOT NULL UNIQUE,
    peso_prova              INT NOT NULL,              -- questões na prova (edital 2022)
    gran_curso_id           TEXT,
    gran_disciplina_curso_id TEXT,
    etiquetas_raiz          BIGINT[] NOT NULL DEFAULT '{}',
    obs                     TEXT
);

CREATE TABLE mentor.modulo (
    id              BIGSERIAL PRIMARY KEY,
    materia_id      INT NOT NULL REFERENCES mentor.materia(id) ON DELETE CASCADE,
    ordem           INT NOT NULL,
    nome            TEXT NOT NULL,
    gran_topico_id  TEXT,
    tipo            TEXT NOT NULL DEFAULT 'conteudo'
                    CHECK (tipo IN ('conteudo','revisao','propria')),
    UNIQUE (materia_id, ordem)
);

CREATE TABLE mentor.assunto (
    id              BIGSERIAL PRIMARY KEY,
    materia_id      INT NOT NULL REFERENCES mentor.materia(id) ON DELETE CASCADE,
    modulo_id       BIGINT NOT NULL REFERENCES mentor.modulo(id) ON DELETE CASCADE,
    ordem           INT NOT NULL,
    nome            TEXT NOT NULL,
    tier            TEXT NOT NULL DEFAULT 'C'
                    CHECK (tier IN ('S','A','B','C','treino','fora','revisao')),
    modo            TEXT NOT NULL DEFAULT 'resumo_e_questoes',
    base            BOOLEAN NOT NULL DEFAULT FALSE,
    origem          TEXT NOT NULL DEFAULT 'gran' CHECK (origem IN ('gran','propria')),
    descricao       TEXT,                              -- aulas próprias/resumos: o que é
    itens_edital    TEXT[] NOT NULL DEFAULT '{}',
    etiquetas       BIGINT[] NOT NULL DEFAULT '{}',    -- união das etiquetas das aulas
    soldado         INT,                               -- evidência: questões de Soldado BA
    estoque_me      INT,                               -- questões ME no acervo
    requer          BIGINT[] NOT NULL DEFAULT '{}',    -- assuntos pré-requisito (ids daqui)
    -- estado de estudo
    estado          TEXT NOT NULL DEFAULT 'nao_estudado'
                    CHECK (estado IN ('nao_estudado','em_andamento','estudado','ressalva','dominado')),
    estudado_em     TIMESTAMPTZ,
    UNIQUE (modulo_id, ordem)
);
CREATE INDEX ix_assunto_materia ON mentor.assunto(materia_id, tier);
CREATE INDEX ix_assunto_etiquetas ON mentor.assunto USING GIN (etiquetas);

CREATE TABLE mentor.aula (
    id              BIGSERIAL PRIMARY KEY,
    assunto_id      BIGINT NOT NULL REFERENCES mentor.assunto(id) ON DELETE CASCADE,
    ordem           INT NOT NULL,                      -- ordem dentro do módulo do Gran
    tipo            TEXT NOT NULL DEFAULT 'conteudo'
                    CHECK (tipo IN ('conteudo','exercicios','revisao','fora','propria')),
    titulo          TEXT NOT NULL,
    codigo_aula     TEXT,                              -- código do Gran
    video_id        TEXT,
    url             TEXT,
    professor       TEXT,
    duracao_s       INT,
    etiquetas       BIGINT[] NOT NULL DEFAULT '{}',    -- do caderno da aula (sem a raiz)
    caderno_url     TEXT,
    transcricao     TEXT,
    resumo          TEXT,
    resumo_bolso    TEXT,
    mapa_mental     TEXT,
    flashcards      JSONB,
    questoes_fixacao JSONB,
    pdf_path        TEXT,
    lei             TEXT,                              -- aula própria: fonte (ex.: "CP arts. 1º a 12")
    arquivo_origem  TEXT,
    -- estado
    estado          TEXT NOT NULL DEFAULT 'pendente'
                    CHECK (estado IN ('pendente','assistida','concluida','ressalva','pulada')),
    assistida_em    TIMESTAMPTZ,
    concluida_em    TIMESTAMPTZ
);
CREATE UNIQUE INDEX ix_aula_codigo ON mentor.aula(codigo_aula) WHERE codigo_aula IS NOT NULL;
CREATE INDEX ix_aula_assunto ON mentor.aula(assunto_id, ordem);
CREATE INDEX ix_aula_etiquetas ON mentor.aula USING GIN (etiquetas);

-- ---------------------------------------------------------------
-- Etiquetas do Gran (árvore) e questões
-- ---------------------------------------------------------------
CREATE TABLE mentor.etiqueta (
    id      BIGINT PRIMARY KEY,
    nome    TEXT,
    pai     BIGINT,
    raiz    BIGINT
);
CREATE INDEX ix_etiqueta_pai ON mentor.etiqueta(pai);
CREATE INDEX ix_etiqueta_raiz ON mentor.etiqueta(raiz);

CREATE TABLE mentor.prova_real (
    id          BIGSERIAL PRIMARY KEY,
    nome        TEXT NOT NULL UNIQUE,                  -- como o Gran nomeia a prova
    banca       TEXT,
    orgao_sigla TEXT,
    cargo       TEXT,
    ano         INT,
    soldado_ba  BOOLEAN NOT NULL DEFAULT FALSE,        -- Soldado PM-BA / CBM-BA
    reservada   BOOLEAN NOT NULL DEFAULT FALSE,        -- guardada pra simulado
    n_questoes  INT NOT NULL DEFAULT 0
);

CREATE TABLE mentor.questao (
    id                  BIGINT PRIMARY KEY,            -- id do Gran
    enunciado           TEXT,
    alternativas        JSONB NOT NULL,                -- [{"letra":"A","texto":"..."}]
    gabarito            TEXT,
    n_alt               INT NOT NULL DEFAULT 0,
    tipo                TEXT NOT NULL DEFAULT 'ME' CHECK (tipo IN ('ME','CE','outro')),
    banca               TEXT,
    banca_id            BIGINT,
    banca_sigla         TEXT,
    ano                 INT,
    orgao               TEXT,
    orgao_sigla         TEXT,
    orgao_uf            TEXT,
    cargo               TEXT,
    prova               TEXT,
    prova_real_id       BIGINT REFERENCES mentor.prova_real(id),
    etiquetas           BIGINT[] NOT NULL DEFAULT '{}',
    indice_acerto       NUMERIC(5,2),                  -- % de acerto no Gran (NULL = pendente)
    dificuldade         TEXT,
    anulada             BOOLEAN NOT NULL DEFAULT FALSE,
    desatualizada       BOOLEAN NOT NULL DEFAULT FALSE,
    tem_imagem          BOOLEAN NOT NULL DEFAULT FALSE,
    so_texto            BOOLEAN NOT NULL DEFAULT TRUE,
    tem_comentario_professor BOOLEAN,
    comentario_professor TEXT,
    comentarios_alunos  JSONB,
    -- classificação (spec v3.2 §1)
    soldado_ba          BOOLEAN NOT NULL DEFAULT FALSE,
    fora_edital         BOOLEAN,                       -- NULL = indeterminado
    fora_banca          BOOLEAN NOT NULL DEFAULT FALSE,
    usavel              BOOLEAN GENERATED ALWAYS AS
                        (NOT anulada AND NOT desatualizada AND so_texto AND enunciado IS NOT NULL) STORED,
    arquivo_origem      TEXT,
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_questao_etiquetas ON mentor.questao USING GIN (etiquetas);
CREATE INDEX ix_questao_banca ON mentor.questao(banca_sigla);
CREATE INDEX ix_questao_ano ON mentor.questao(ano);
CREATE INDEX ix_questao_prova ON mentor.questao(prova_real_id) WHERE prova_real_id IS NOT NULL;
CREATE INDEX ix_questao_soldado ON mentor.questao(soldado_ba) WHERE soldado_ba;
CREATE INDEX ix_questao_uso ON mentor.questao(fora_edital, fora_banca) WHERE usavel;

-- vínculo questão ↔ aula (requisito por aula, spec §3.3.1)
CREATE TABLE mentor.questao_aula (
    questao_id  BIGINT NOT NULL REFERENCES mentor.questao(id) ON DELETE CASCADE,
    aula_id     BIGINT NOT NULL REFERENCES mentor.aula(id) ON DELETE CASCADE,
    assunto_id  BIGINT NOT NULL REFERENCES mentor.assunto(id) ON DELETE CASCADE,
    via         TEXT NOT NULL CHECK (via IN ('etiqueta','pai','semelhanca','manual')),
    forca       NUMERIC(5,3) NOT NULL DEFAULT 1,       -- 1 = etiqueta exata; semelhança = cosseno
    PRIMARY KEY (questao_id, aula_id)
);
CREATE INDEX ix_qa_aula ON mentor.questao_aula(aula_id);
CREATE INDEX ix_qa_assunto ON mentor.questao_aula(assunto_id);

-- ---------------------------------------------------------------
-- Cards, bizus
-- ---------------------------------------------------------------
CREATE TABLE mentor.card (
    id          BIGSERIAL PRIMARY KEY,
    assunto_id  BIGINT REFERENCES mentor.assunto(id) ON DELETE CASCADE,
    aula_id     BIGINT REFERENCES mentor.aula(id) ON DELETE SET NULL,
    tipo        TEXT NOT NULL CHECK (tipo IN ('lei_seca','literalidade','contraste','bizu','flashcard_gran','usuario')),
    frente      TEXT NOT NULL,
    verso       TEXT NOT NULL,
    fonte       TEXT,
    -- caixa de Leitner 1-3-7-15-30 (spec v3.2 §6)
    caixa       INT NOT NULL DEFAULT 0,
    proxima     DATE,
    acertos     INT NOT NULL DEFAULT 0,
    erros       INT NOT NULL DEFAULT 0,
    ativo       BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_card_proxima ON mentor.card(proxima) WHERE ativo;
CREATE INDEX ix_card_assunto ON mentor.card(assunto_id);

CREATE TABLE mentor.bizu (
    id          BIGSERIAL PRIMARY KEY,
    aula_id     BIGINT REFERENCES mentor.aula(id) ON DELETE CASCADE,
    assunto_id  BIGINT REFERENCES mentor.assunto(id) ON DELETE CASCADE,
    fonte       TEXT NOT NULL CHECK (fonte IN ('resumo_bolso','transcricao','professor','aluno','acerto_baixo','intensivo','usuario','externo')),
    secao       TEXT,                                  -- ex.: "Pegadinhas Comuns"
    texto       TEXT NOT NULL,
    aprovado    BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_bizu_aula ON mentor.bizu(aula_id);

-- ---------------------------------------------------------------
-- Estudo: sessões, respostas, revisões, semanas, simulados
-- ---------------------------------------------------------------
CREATE TABLE mentor.sessao (
    id          BIGSERIAL PRIMARY KEY,
    tipo        TEXT NOT NULL CHECK (tipo IN ('estudo','revisao','questoes','simulado','cards','semana')),
    inicio      TIMESTAMPTZ NOT NULL DEFAULT now(),
    fim         TIMESTAMPTZ,
    minutos     INT,
    xp          INT NOT NULL DEFAULT 0,
    resumo      JSONB                                  -- fim de sessão (spec v3.2 §7 do dopaminérgico)
);

CREATE TABLE mentor.resposta (
    id          BIGSERIAL PRIMARY KEY,
    questao_id  BIGINT NOT NULL REFERENCES mentor.questao(id),
    sessao_id   BIGINT REFERENCES mentor.sessao(id) ON DELETE SET NULL,
    aula_id     BIGINT REFERENCES mentor.aula(id) ON DELETE SET NULL,
    assunto_id  BIGINT REFERENCES mentor.assunto(id) ON DELETE SET NULL,
    contexto    TEXT NOT NULL CHECK (contexto IN ('aquecimento','fixacao','gate','reestudo','R1','R3','R7','R15','R30','M','semana','questoes','simulado','bolso')),
    marcada     TEXT,
    correta     BOOLEAN,
    confianca   TEXT CHECK (confianca IN ('certeza','duvida','chute')),
    tipo_erro   TEXT CHECK (tipo_erro IN ('nao_sabia','trocou_par','leu_errado','desatencao')),
    tempo_s     INT,
    data        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_resposta_assunto ON mentor.resposta(assunto_id, data DESC);
CREATE INDEX ix_resposta_questao ON mentor.resposta(questao_id, data DESC);
CREATE INDEX ix_resposta_contexto ON mentor.resposta(contexto, data DESC);

CREATE TABLE mentor.revisao (
    id          BIGSERIAL PRIMARY KEY,
    assunto_id  BIGINT NOT NULL REFERENCES mentor.assunto(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL CHECK (tipo IN ('R1','R2','R3','R7','R15','R30','M')),
    prevista    DATE NOT NULL,
    feita       TIMESTAMPTZ,
    sessao_id   BIGINT REFERENCES mentor.sessao(id) ON DELETE SET NULL,
    acerto      NUMERIC(5,2),
    n_questoes  INT,
    adiada_para DATE,
    n_adiamentos INT NOT NULL DEFAULT 0,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- uma revisão pendente por assunto
CREATE UNIQUE INDEX ix_revisao_pendente ON mentor.revisao(assunto_id) WHERE feita IS NULL;
CREATE INDEX ix_revisao_fila ON mentor.revisao(prevista) WHERE feita IS NULL;

CREATE TABLE mentor.semana (
    id          BIGSERIAL PRIMARY KEY,
    inicio      DATE NOT NULL UNIQUE,                  -- segunda
    fim         DATE NOT NULL,
    meta_horas  NUMERIC(5,1) NOT NULL,
    horas_feitas NUMERIC(5,1) NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'bloqueada' CHECK (status IN ('bloqueada','aberta','fechada')),
    fase        TEXT,
    montada_em  TIMESTAMPTZ,
    fechada_em  TIMESTAMPTZ
);

CREATE TABLE mentor.plano_item (
    id          BIGSERIAL PRIMARY KEY,
    semana_id   BIGINT NOT NULL REFERENCES mentor.semana(id) ON DELETE CASCADE,
    assunto_id  BIGINT NOT NULL REFERENCES mentor.assunto(id) ON DELETE CASCADE,
    ordem       INT NOT NULL,
    previsto_h  NUMERIC(5,2) NOT NULL,
    dia         DATE,
    feito       BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (semana_id, assunto_id)
);

CREATE TABLE mentor.simulado (
    id          BIGSERIAL PRIMARY KEY,
    numero      INT,
    data        DATE NOT NULL,
    tipo        TEXT NOT NULL CHECK (tipo IN ('montado','real','demo')),
    prova_real_id BIGINT REFERENCES mentor.prova_real(id),
    questoes    BIGINT[] NOT NULL DEFAULT '{}',
    respostas   JSONB,
    inicio      TIMESTAMPTZ,
    fim         TIMESTAMPTZ,
    certas      INT,
    nota        NUMERIC(5,2),
    por_materia JSONB,
    status      TEXT NOT NULL DEFAULT 'agendado' CHECK (status IN ('agendado','em_andamento','entregue'))
);

-- ---------------------------------------------------------------
-- Motivação (spec v3.2, ideia 1): XP, sequências, patentes
-- ---------------------------------------------------------------
CREATE TABLE mentor.xp_evento (
    id          BIGSERIAL PRIMARY KEY,
    data        TIMESTAMPTZ NOT NULL DEFAULT now(),
    tipo        TEXT NOT NULL,                         -- gate|revisao_no_dia|card|meta_dia|meta_semana|simulado|combo|bonus
    pontos      INT NOT NULL,
    ref         JSONB
);
CREATE INDEX ix_xp_data ON mentor.xp_evento(data DESC);

CREATE TABLE mentor.progresso (
    id                  INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    xp_total            INT NOT NULL DEFAULT 0,
    patente             TEXT NOT NULL DEFAULT 'Recruta',
    streak_dias         INT NOT NULL DEFAULT 0,
    streak_semanas      INT NOT NULL DEFAULT 0,
    congelamentos       INT NOT NULL DEFAULT 1,
    ultimo_dia_meta     DATE,
    emblemas            JSONB NOT NULL DEFAULT '[]'
);
INSERT INTO mentor.progresso (id) VALUES (1);

CREATE TABLE mentor.config (
    chave   TEXT PRIMARY KEY,
    valor   JSONB NOT NULL,
    alterado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO mentor.config (chave, valor) VALUES
 ('gate', '{"questoes": 5, "minimo": 4}'),
 ('revisao', '{"R1": 4, "R7": 6, "R30": 4, "M": 4, "teto_pct_dia": 35}'),
 ('intervalos', '{"R1": 1, "R3": 2, "R7": 6, "R15": 8, "R30": 23, "M": 30}'),
 ('horas_dia', '{"10": 3, "11": 4, "12": 5.5, "1": 8, "2": 8}'),
 ('fim_conteudo', '"2027-01-31"'),
 ('bancas', '["FCC","IBFC","FGV","VUNESP","IDECAN","AOCP","SELECON","IBADE","CONSULPLAN","UNEB","CONSULTEC","CEBRASPE"]'),
 ('simulado', '{"questoes": 80, "minutos": 270, "minimo": 60, "ponto": 1.25}');
