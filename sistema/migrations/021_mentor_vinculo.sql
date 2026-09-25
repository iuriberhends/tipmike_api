-- ===========================================================================
--  021 - o vinculo questao <-> topico, feito direito
--
--  O QUE ESTAVA ERRADO (achado em 23/09/2026, depois da 1a carga real)
--
--  A 020 liga a questao ao topico por uma coluna so, preenchida assim:
--      UPDATE mentor.questao q SET topico_id = t.id
--      FROM mentor.topico t WHERE q.assunto_ids && t.assunto_ids
--
--  Dois defeitos, os dois silenciosos:
--
--  1. O caderno de TODA aula inclui o assunto RAIZ da disciplina (406223 =
--     "Direito Penal" inteiro), e toda questao de Penal carrega esse mesmo id.
--     Resultado: o && dava verdadeiro para quase todo par (questao, topico).
--     Medido: 78% das questoes "casavam" com mais de um topico, uma delas com
--     53 - entre elas Homicidio, Contravencoes Penais e Conceitos
--     Introdutorios ao mesmo tempo. O numero bonito de 6.894 questoes ligadas
--     era isso: ruido. Com os genericos fora, sao 3.017 de verdade.
--     O mentor_tiers.py ja tinha descoberto e resolvido isso ("82 topicos
--     viraram S com contagens identicas"); a correcao nunca chegou na carga.
--
--  2. Mesmo com os genericos fora, 17% das questoes pertencem legitimamente a
--     mais de um topico (ate 15) - a mesma questao de furto serve as 4 aulas
--     de Furto. Uma coluna so nao expressa isso, e o Postgres escolhia UM ao
--     acaso: a contagem de cada aula virava sorteio.
--
--  O QUE ESTA TABELA FAZ: guarda TODOS os vinculos. A coluna
--  mentor.questao.topico_id continua existindo como o topico PRINCIPAL (pra
--  quem so quer um), mas passa a ser escolhida por regra, nao por acaso.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS mentor.questao_topico (
    questao_id   BIGINT NOT NULL REFERENCES mentor.questao(id) ON DELETE CASCADE,
    topico_id    BIGINT NOT NULL REFERENCES mentor.topico(id)  ON DELETE CASCADE,
    -- quantos assuntos ESPECIFICOS os dois tem em comum. Serve de desempate
    -- pra eleger o topico principal e de medida de quao justo e o vinculo.
    forca        INT NOT NULL DEFAULT 1,
    principal    BOOLEAN NOT NULL DEFAULT FALSE,
    criado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (questao_id, topico_id)
);

CREATE INDEX IF NOT EXISTS ix_qt_topico  ON mentor.questao_topico(topico_id);
CREATE INDEX IF NOT EXISTS ix_qt_questao ON mentor.questao_topico(questao_id);

-- so UM principal por questao
CREATE UNIQUE INDEX IF NOT EXISTS ix_qt_principal
    ON mentor.questao_topico(questao_id) WHERE principal;

-- Os assuntos que nao distinguem nada (a raiz da disciplina e afins). Ficam
-- gravados em vez de recalculados em todo lugar: assim a carga, o tier e o
-- painel usam a MESMA lista, e da pra auditar por que uma questao nao ligou.
CREATE TABLE IF NOT EXISTS mentor.assunto_generico (
    assunto_id     BIGINT PRIMARY KEY,
    disciplina_id  BIGINT REFERENCES mentor.disciplina(id) ON DELETE CASCADE,
    nome           TEXT,
    -- em quantos cadernos ele aparece, e de quantos. E a evidencia da regra
    -- "aparece em mais de um terco dos cadernos, logo nao distingue".
    cadernos       INT,
    cadernos_total INT,
    criado_em      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A visao que o painel deve usar: questao ligada a topico pelo vinculo bom.
--
-- O nome topico_vinculo_id NAO e capricho: mentor.questao ja tem uma coluna
-- topico_id (o topico PRINCIPAL), entao `qt.topico_id, q.*` deixava duas
-- colunas com o mesmo nome e o Postgres recusava a view inteira
-- ("column topico_id specified more than once"). Os dois campos convivem e
-- querem dizer coisas diferentes:
--     topico_id          -> o topico principal daquela questao
--     topico_vinculo_id  -> o topico DESTA linha de vinculo
-- Para listar as questoes de uma aula, filtre por topico_vinculo_id.
--
-- Uso q.* de proposito: listar as colunas a mao faria a view envelhecer calada
-- toda vez que mentor.questao ganhasse campo (ja ganhou tres).
CREATE OR REPLACE VIEW mentor.v_questao_do_topico AS
SELECT q.*,
       qt.topico_id AS topico_vinculo_id,
       qt.forca,
       qt.principal
  FROM mentor.questao_topico qt
  JOIN mentor.questao q ON q.id = qt.questao_id;

DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM mentor.questao_topico;
    RAISE NOTICE 'mentor.questao_topico existe (% vinculos hoje)', n;
END $$;
