-- ============================================================
-- 020_mentor_schema.sql
-- MIKE MENTOR - mentor pessoal de estudos para Soldado PMBA.
--
-- Vive num schema proprio ("mentor") dentro do mikedb: reaproveita
-- pool, auth e deploy do tipmike_api sem se misturar com as tabelas
-- dos bots de aposta.
--
-- Vocabulario (a spec e o Gran usam "topico" com sentidos diferentes):
--   modulo = o "topico" do Gran = item do edital que agrupa aulas
--            (Penal tem 8: "1. Do crime...", "3. Contravencao"...)
--   topico = a unidade de estudo da spec = UMA aula do Gran (1:1)
--            e o que recebe R1/R7/R30, placar, cards
--
-- Idempotente: rodar duas vezes nao quebra nem duplica.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS mentor;

-- ------------------------------------------------------------
-- Arvore do curso: disciplina -> modulo -> topico(aula)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mentor.disciplina (
    id              BIGINT PRIMARY KEY,          -- id do Gran (ex: 3392)
    nome            TEXT NOT NULL,
    peso_prova      INT,                         -- questoes na prova (spec §1)
    caderno_questoes_url TEXT,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mentor.modulo (
    id              BIGSERIAL PRIMARY KEY,
    disciplina_id   BIGINT NOT NULL REFERENCES mentor.disciplina(id) ON DELETE CASCADE,
    id_gran         INT NOT NULL,                -- o "conteudo_id" do Gran
    ordem           INT,
    titulo          TEXT NOT NULL,
    UNIQUE (disciplina_id, id_gran)
);

CREATE TABLE IF NOT EXISTS mentor.topico (
    id              BIGSERIAL PRIMARY KEY,
    disciplina_id   BIGINT NOT NULL REFERENCES mentor.disciplina(id) ON DELETE CASCADE,
    modulo_id       BIGINT REFERENCES mentor.modulo(id) ON DELETE SET NULL,
    ordem           INT,
    titulo          TEXT NOT NULL,
    -- classificacao SUA, nao vem do Gran; preencher a mao ou por regra
    estrela         BOOLEAN NOT NULL DEFAULT FALSE,
    voo             BOOLEAN NOT NULL DEFAULT FALSE,
    -- o vinculo aula<->questoes: filtro do Gran Questoes ja amarrado
    -- aos assuntos DESTA aula (spec §3.3.1 usa isso como fonte primaria)
    caderno_questoes_url TEXT,
    assunto_ids     BIGINT[],                    -- extraidos da URL acima
    codigo_aula     TEXT UNIQUE,                 -- codigo do Gran (ex: "135779")
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_topico_disciplina ON mentor.topico(disciplina_id);
CREATE INDEX IF NOT EXISTS ix_topico_modulo     ON mentor.topico(modulo_id);
-- GIN pra achar rapido "que topicos cobrem o assunto X"
CREATE INDEX IF NOT EXISTS ix_topico_assuntos   ON mentor.topico USING GIN (assunto_ids);

-- ------------------------------------------------------------
-- Conteudo da aula
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mentor.aula (
    id              BIGSERIAL PRIMARY KEY,
    topico_id       BIGINT NOT NULL UNIQUE REFERENCES mentor.topico(id) ON DELETE CASCADE,
    titulo          TEXT NOT NULL,
    ordem_curso     INT,
    ordem_gran      TEXT,                        -- o nr_ordem cru ("36.00")
    professor       TEXT,
    duracao         TEXT,
    video_id        TEXT,
    url_aula        TEXT,
    transcricao     TEXT,
    resumo          TEXT,
    resumo_bolso    TEXT,
    mapa_mental     TEXT,
    pdf_path        TEXT,
    destilado       TEXT,                        -- gerado pelo Cerebro (§3.3.2)
    destilado_em    TIMESTAMPTZ,
    coletado_em     TIMESTAMPTZ,
    atualizado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- Questoes do Gran Questoes
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mentor.questao (
    id              BIGINT PRIMARY KEY,          -- id do Gran (ex: 4522255)
    enunciado       TEXT,
    alternativas    JSONB NOT NULL,              -- [{letra,texto,id,correta}]
    gabarito        TEXT,                        -- "A".."E"
    comentario      TEXT,                        -- so-demanda (§3.3.1b): nulo na carga
    comentario_em   TIMESTAMPTZ,
    banca           TEXT,
    ano             INT,
    orgao           TEXT,
    prova           TEXT,
    cargo           TEXT,
    origem          TEXT NOT NULL DEFAULT 'gran',
    topico_id       BIGINT REFERENCES mentor.topico(id) ON DELETE SET NULL,
    confianca_classif REAL,
    indice_acerto_gran REAL,                     -- % de acerto (dificuldade real)
    acertos_gran    INT,
    total_gran      INT,
    dificuldade_gran INT,                        -- 1..5, o rotulo do Gran
    formato         TEXT,                        -- simples|exceto|I-II-III|V-F|caso
    desatualizada   BOOLEAN NOT NULL DEFAULT FALSE,
    anulada         BOOLEAN NOT NULL DEFAULT FALSE,
    -- assuntos do Gran. assunto_ids e a chave do vinculo com topico.
    -- ATENCAO: nas 7.247 ja coletadas esta VAZIO (rate limit) - ver
    -- MIKE_MENTOR_STATUS.md. A juncao com topico depende dele.
    assuntos        JSONB,
    assunto_ids     BIGINT[],
    -- da pra mandar essa questao como TEXTO no Telegram? (§3.5)
    -- falso quando o enunciado e imagem ou vem de um grupo com texto comum
    so_texto        BOOLEAN NOT NULL DEFAULT TRUE,
    tem_imagem      BOOLEAN,
    grupo_questao   BIGINT,
    coletado_em     TIMESTAMPTZ,
    atualizado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_questao_topico   ON mentor.questao(topico_id);
CREATE INDEX IF NOT EXISTS ix_questao_assuntos ON mentor.questao USING GIN (assunto_ids);
CREATE INDEX IF NOT EXISTS ix_questao_banca    ON mentor.questao(banca);
CREATE INDEX IF NOT EXISTS ix_questao_ano      ON mentor.questao(ano);
-- a fila da pos-aula ordena por dificuldade real; so questoes usaveis
CREATE INDEX IF NOT EXISTS ix_questao_usaveis
    ON mentor.questao(topico_id, indice_acerto_gran DESC)
    WHERE so_texto AND NOT desatualizada AND NOT anulada AND gabarito IS NOT NULL;

-- Questoes de fixacao (do quiz.json de cada aula): ja vem com gabarito
-- E explicacao por alternativa, entao alimentam "explicar erro" sem API.
CREATE TABLE IF NOT EXISTS mentor.questao_fixacao (
    id              BIGSERIAL PRIMARY KEY,
    topico_id       BIGINT NOT NULL REFERENCES mentor.topico(id) ON DELETE CASCADE,
    id_gran         TEXT,                        -- "q-1", "q-2"...
    enunciado       TEXT NOT NULL,
    alternativas    JSONB NOT NULL,
    gabarito        TEXT,
    explicacao_por_alternativa JSONB,
    UNIQUE (topico_id, id_gran)
);

-- ------------------------------------------------------------
-- Flashcards (SRS proprio, SM-2)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mentor.card (
    id              BIGSERIAL PRIMARY KEY,
    topico_id       BIGINT NOT NULL REFERENCES mentor.topico(id) ON DELETE CASCADE,
    deck            TEXT,                        -- agrupamento do Gran
    frente          TEXT NOT NULL,
    verso           TEXT NOT NULL,
    dica            TEXT,
    origem          TEXT NOT NULL DEFAULT 'gran',-- gran|erro|par|destilado
    id_gran         TEXT,
    -- SM-2
    ef              REAL NOT NULL DEFAULT 2.5,
    intervalo       INT  NOT NULL DEFAULT 0,
    repeticoes      INT  NOT NULL DEFAULT 0,
    proxima         DATE,
    UNIQUE (topico_id, id_gran)
);

CREATE INDEX IF NOT EXISTS ix_card_proxima ON mentor.card(proxima) WHERE proxima IS NOT NULL;

-- ------------------------------------------------------------
-- Estudo, respostas e agenda
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mentor.estudo (
    id              BIGSERIAL PRIMARY KEY,
    topico_id       BIGINT NOT NULL REFERENCES mentor.topico(id) ON DELETE CASCADE,
    data            DATE NOT NULL DEFAULT CURRENT_DATE,
    tipo            TEXT NOT NULL,               -- aula|R1|R7|R30|simulado
    percentual      REAL,
    com_ressalva    BOOLEAN NOT NULL DEFAULT FALSE,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT estudo_tipo_ok CHECK (tipo IN ('aula','R1','R7','R30','simulado','reestudo'))
);

CREATE INDEX IF NOT EXISTS ix_estudo_topico ON mentor.estudo(topico_id, data DESC);

CREATE TABLE IF NOT EXISTS mentor.resposta (
    id              BIGSERIAL PRIMARY KEY,
    questao_id      BIGINT REFERENCES mentor.questao(id) ON DELETE SET NULL,
    questao_fixacao_id BIGINT REFERENCES mentor.questao_fixacao(id) ON DELETE SET NULL,
    topico_id       BIGINT REFERENCES mentor.topico(id) ON DELETE SET NULL,
    data            TIMESTAMPTZ NOT NULL DEFAULT now(),
    marcada         TEXT,
    correta         BOOLEAN,
    contexto        TEXT,                        -- pre|pos_aula|R1|R7|R30|simulado
    confianca       TEXT,                        -- certeza|duvida|chute
    tipo_erro       TEXT,                        -- nao_sabia|trocou_par|leu_errado
    tempo_s         INT,
    CONSTRAINT resposta_confianca_ok CHECK (confianca IS NULL OR confianca IN ('certeza','duvida','chute')),
    CONSTRAINT resposta_tipo_erro_ok CHECK (tipo_erro IS NULL OR tipo_erro IN ('nao_sabia','trocou_par','leu_errado'))
);

CREATE INDEX IF NOT EXISTS ix_resposta_topico ON mentor.resposta(topico_id, data DESC);
CREATE INDEX IF NOT EXISTS ix_resposta_questao ON mentor.resposta(questao_id, data DESC);

CREATE TABLE IF NOT EXISTS mentor.revisao (
    id              BIGSERIAL PRIMARY KEY,
    topico_id       BIGINT NOT NULL REFERENCES mentor.topico(id) ON DELETE CASCADE,
    tipo            TEXT NOT NULL,               -- R1|R7|R30
    prevista        DATE NOT NULL,
    feita           DATE,
    adiada_para     DATE,
    adiamentos      INT NOT NULL DEFAULT 0,
    CONSTRAINT revisao_tipo_ok CHECK (tipo IN ('R1','R7','R30')),
    UNIQUE (topico_id, tipo)
);

-- a fila do dia: o que venceu e ainda nao foi feito
CREATE INDEX IF NOT EXISTS ix_revisao_fila
    ON mentor.revisao(COALESCE(adiada_para, prevista)) WHERE feita IS NULL;

CREATE TABLE IF NOT EXISTS mentor.par_confundido (
    id              BIGSERIAL PRIMARY KEY,
    topico_id       BIGINT NOT NULL REFERENCES mentor.topico(id) ON DELETE CASCADE,
    lado_a          TEXT NOT NULL,
    lado_b          TEXT NOT NULL,
    ocorrencias     INT NOT NULL DEFAULT 1,
    card_id         BIGINT REFERENCES mentor.card(id) ON DELETE SET NULL,
    UNIQUE (topico_id, lado_a, lado_b)
);

CREATE TABLE IF NOT EXISTS mentor.simulado (
    id              BIGSERIAL PRIMARY KEY,
    data            TIMESTAMPTZ NOT NULL DEFAULT now(),
    config          JSONB,
    resultado       JSONB
);

CREATE TABLE IF NOT EXISTS mentor.redacao (
    id              BIGSERIAL PRIMARY KEY,
    data            DATE NOT NULL DEFAULT CURRENT_DATE,
    tema            TEXT,
    texto           TEXT,
    nota_conteudo   REAL,
    nota_estrutura  REAL,
    nota_expressao  REAL,
    correcao        JSONB
);

-- ------------------------------------------------------------
-- Visao de apoio: quais questoes servem pra estudar um topico
-- ------------------------------------------------------------

CREATE OR REPLACE VIEW mentor.v_questao_usavel AS
SELECT q.*
FROM mentor.questao q
WHERE q.so_texto
  AND NOT q.desatualizada
  AND NOT q.anulada
  AND q.gabarito IS NOT NULL
  AND q.enunciado IS NOT NULL
  AND length(btrim(q.enunciado)) > 0;

COMMENT ON VIEW mentor.v_questao_usavel IS
'Questoes que podem ser enviadas como texto no bot e contam placar. '
'Exclui enunciado vazio/imagem (6 casos conhecidos em Penal), anuladas '
'e desatualizadas, conforme spec §3.4.';

COMMENT ON COLUMN mentor.questao.assunto_ids IS
'Ids de assunto do Gran. E a chave que liga a questao ao topico via '
'topico.assunto_ids (spec §3.3.1). VAZIO nas 7.247 ja coletadas - a '
'releitura esbarrou em rate limit. Ver MIKE_MENTOR_STATUS.md.';

COMMENT ON COLUMN mentor.topico.estrela IS
'Classificacao do usuario (tema que mais cai). Nao vem do Gran.';

COMMENT ON COLUMN mentor.topico.voo IS
'Classificacao do usuario (tema leve, de revisao rapida). Nao vem do Gran.';

-- Verificacao
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'mentor'
ORDER BY table_name;
