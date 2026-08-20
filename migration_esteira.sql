-- ============================================================================
-- migration_esteira.sql — ESTEIRA NO PAINEL, PASSO 1 (fundacao no banco)
-- ----------------------------------------------------------------------------
-- Cria:   esteira_jobs    (a rodada)
--         esteira_itens   (uma linha por estrategia: snapshot exato + job clicavel)
--         v_esteira_jobs  (resumo p/ painel com contagens REAIS dos itens)
--         trigger de atualizado_em (clock_timestamp, avanca dentro da transacao)
--
-- 100% ADITIVO: nao altera nenhuma tabela existente (a unica interacao com o
-- que ja existe e uma FK OPCIONAL para backtest_jobs, criada num bloco
-- protegido — se falhar, avisa e segue). Idempotente: pode rodar mais de uma
-- vez sem estrago. O esteira_state.json MORRE aqui.
--
-- Timestamps NAIVE (padrao do projeto: relogio ja em BRT, nunca converter).
-- Mensagens/comentarios sem acento de proposito (console cmd da VPS).
--
-- CONTRATO DE STATUS (esteira_jobs) — licoes do varredor embutidas:
--   pendente    = criado pelo router ou na mao; aguardando slot
--   preparando  = daemon reservou o slot e subiu o worker. O worker DEVE
--                 aceitar 'pendente' E 'preparando' como ponto de partida
--                 (no varredor, daemon e worker discordarem sobre o status
--                 deixou job parado calado — nunca repetir)
--   rodando     = worker carimbou h2h, montou itens, sentinela passou;
--                 itens em execucao
--   concluido | erro | cancelado = finais
--   DECISAO DE USUARIO (ex.: confirmado) NUNCA vai no status, que e volatil.
--   Vai em params via jsonb_set — sobrevive a troca de status.
--
-- params (jsonb) esperados pela rodada (contrato documentado, nao travado):
--   confirmado    bool   — decisao do usuario
--   fonte         'arquivo' | 'banco'
--   upload_id     text   — quando fonte=arquivo (parquet ja no UPLOAD_DIR)
--   casa/esporte/data_inicio/data_fim — quando fonte=banco
--   (o que mais o worker precisar; jsonb nao trava evolucao)
--
-- SUSPEITA: h2h_ts_inicio/h2h_ts_fim carimbam MAX(inserted_at) do
--   h2h_historico no inicio e no fim da rodada. Se mudou no meio (backfill
--   durante a rodada), o worker marca suspeita=true + motivo — a rodada nao
--   e comparavel com re-runs (chip H2H le o estado ATUAL do banco).
-- ============================================================================

SET client_encoding = 'UTF8';

BEGIN;

-- ----------------------------------------------------------------------------
-- 1) esteira_jobs — a rodada
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS esteira_jobs (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,                          -- ownership (padrao do projeto); router preenche
    nome            TEXT    NOT NULL DEFAULT '',
    origem          TEXT    NOT NULL DEFAULT 'planilha'
                        CONSTRAINT chk_esteira_jobs_origem
                        CHECK (origem IN ('planilha','varredura','manual')),
    origem_ref      TEXT,                             -- ex.: nome do xlsx, id do garimpo (varredura_jobs)
    params          JSONB   NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT    NOT NULL DEFAULT 'pendente'
                        CONSTRAINT chk_esteira_jobs_status
                        CHECK (status IN ('pendente','preparando','rodando',
                                          'concluido','erro','cancelado')),
    erro            TEXT,
    progresso_msg   TEXT,                             -- inclui "por que nao subiu" (RAM/slots) — o daemon escreve aqui
    total_itens     INTEGER NOT NULL DEFAULT 0,
    itens_prontos   INTEGER NOT NULL DEFAULT 0,
    h2h_ts_inicio   TIMESTAMP,                        -- MAX(inserted_at) do h2h_historico no inicio
    h2h_ts_fim      TIMESTAMP,                        -- idem no fim; diferente => suspeita
    suspeita        BOOLEAN NOT NULL DEFAULT FALSE,
    suspeita_motivo TEXT,
    sentinela_ok    BOOLEAN,                          -- NULL = ainda nao rodou a calibracao
    baseline        JSONB,                            -- baseline do mercado (o numero que da sentido aos outros)
    alertas         JSONB,                            -- resumo dos 4 alertas ceticos da rodada
    pid             INTEGER,                          -- processo worker (faxina de orfao checa _pid_vivo antes de matar no banco)
    criado_em       TIMESTAMP NOT NULL DEFAULT now(), -- (backtest_jobs nao tem criado_em; aqui tem, de proposito)
    iniciado_em     TIMESTAMP,
    finalizado_em   TIMESTAMP,
    atualizado_em   TIMESTAMP NOT NULL DEFAULT now()
);

COMMENT ON TABLE  esteira_jobs               IS 'Rodada da esteira (substitui o esteira_state.json)';
COMMENT ON COLUMN esteira_jobs.params        IS 'Config da rodada. Decisao de usuario (confirmado) vive AQUI, nunca no status';
COMMENT ON COLUMN esteira_jobs.h2h_ts_inicio IS 'Carimbo MAX(inserted_at) do h2h_historico no inicio da rodada';
COMMENT ON COLUMN esteira_jobs.h2h_ts_fim    IS 'Carimbo no fim; se != inicio, worker marca suspeita=true';
COMMENT ON COLUMN esteira_jobs.suspeita      IS 'true = h2h mudou no meio da rodada; numeros nao comparaveis com re-run';
COMMENT ON COLUMN esteira_jobs.progresso_msg IS 'Progresso humano, inclusive motivo de estar aguardando (RAM/slots)';

-- ----------------------------------------------------------------------------
-- 2) esteira_itens — uma linha por estrategia
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS esteira_itens (
    id              SERIAL PRIMARY KEY,
    esteira_job_id  INTEGER NOT NULL
                        REFERENCES esteira_jobs(id) ON DELETE CASCADE,
    ordem           INTEGER NOT NULL DEFAULT 0,
    nome            TEXT    NOT NULL,
    papel           TEXT    NOT NULL DEFAULT 'estrategia'
                        CONSTRAINT chk_esteira_itens_papel
                        CHECK (papel IN ('sentinela','controle','estrategia','variacao')),
    pai_item_id     INTEGER REFERENCES esteira_itens(id) ON DELETE SET NULL,  -- variacao aponta a mae (hill-climb)
    assinatura      TEXT,                             -- sha1 do snapshot: liga itens iguais ENTRE rodadas (aba EVOLUCAO)
    snapshot        JSONB   NOT NULL,                 -- o snapshot EXATO que foi pro motor (pre-compromisso: gravado ANTES de rodar)
    status          TEXT    NOT NULL DEFAULT 'pendente'
                        CONSTRAINT chk_esteira_itens_status
                        CHECK (status IN ('pendente','rodando','concluido',
                                          'erro','pulado','cancelado')),
    backtest_job_id INTEGER,                          -- clicavel no painel; FK opcional adicionada abaixo
    metricas        JSONB,                            -- ap, greens, reds, wr, u, roi, dd, roi_3d, z_jogo, cego, lucro_dd...
    alertas         JSONB,                            -- alertas ceticos por item
    erro            TEXT,
    iniciado_em     TIMESTAMP,
    finalizado_em   TIMESTAMP
);

COMMENT ON TABLE  esteira_itens            IS 'Uma linha por estrategia da rodada; snapshot exato + resultado';
COMMENT ON COLUMN esteira_itens.papel      IS 'sentinela = calibracao obrigatoria; controle = melhor SEM o eixo principal; variacao = filha de hill-climb';
COMMENT ON COLUMN esteira_itens.snapshot   IS 'bot_snapshot avulso exato enviado ao motor. Gravar ANTES de rodar (pre-compromisso)';
COMMENT ON COLUMN esteira_itens.assinatura IS 'sha1 do snapshot normalizado — comparar a mesma config entre rodadas';

-- ----------------------------------------------------------------------------
-- 3) FK opcional para backtest_jobs (protegida: se falhar, avisa e segue)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.backtest_jobs') IS NULL THEN
        RAISE NOTICE 'AVISO: tabela backtest_jobs nao encontrada - FK pulada; backtest_job_id segue funcionando sem FK.';
    ELSIF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_esteira_itens_backtest_job') THEN
        RAISE NOTICE 'FK fk_esteira_itens_backtest_job ja existe - ok.';
    ELSE
        BEGIN
            ALTER TABLE esteira_itens
                ADD CONSTRAINT fk_esteira_itens_backtest_job
                FOREIGN KEY (backtest_job_id) REFERENCES backtest_jobs(id)
                ON DELETE SET NULL;
            RAISE NOTICE 'FK esteira_itens.backtest_job_id -> backtest_jobs(id) criada.';
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'AVISO: FK para backtest_jobs NAO criada (%). Coluna segue sem FK.', SQLERRM;
        END;
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 4) Indices
-- ----------------------------------------------------------------------------
-- fila do daemon: so os estados vivos
CREATE INDEX IF NOT EXISTS idx_esteira_jobs_ativos
    ON esteira_jobs (status, id)
    WHERE status IN ('pendente','preparando','rodando');

CREATE INDEX IF NOT EXISTS idx_esteira_itens_job
    ON esteira_itens (esteira_job_id, status);

CREATE INDEX IF NOT EXISTS idx_esteira_itens_assinatura
    ON esteira_itens (assinatura)
    WHERE assinatura IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_esteira_itens_backtest_job
    ON esteira_itens (backtest_job_id)
    WHERE backtest_job_id IS NOT NULL;

-- 1 sentinela por rodada (a regra "sentinela obrigatoria" vira estrutura)
CREATE UNIQUE INDEX IF NOT EXISTS uq_esteira_itens_sentinela
    ON esteira_itens (esteira_job_id)
    WHERE papel = 'sentinela';

-- ----------------------------------------------------------------------------
-- 5) Trigger de atualizado_em
--    clock_timestamp() de proposito: now() e fixo dentro da transacao e o
--    worker atualiza o mesmo job varias vezes na mesma conexao.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION esteira_touch() RETURNS trigger AS $$
BEGIN
    NEW.atualizado_em := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_esteira_jobs_touch ON esteira_jobs;
CREATE TRIGGER trg_esteira_jobs_touch
    BEFORE UPDATE ON esteira_jobs
    FOR EACH ROW EXECUTE FUNCTION esteira_touch();

-- ----------------------------------------------------------------------------
-- 6) View de resumo p/ painel — contagens REAIS dos itens ao lado dos
--    contadores mantidos pelo worker (auditar drift: melhor faltar que mentir)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_esteira_jobs AS
SELECT
    j.id,
    j.nome,
    j.origem,
    j.origem_ref,
    j.status,
    j.suspeita,
    j.sentinela_ok,
    j.total_itens,
    j.itens_prontos,
    COUNT(i.id)                                            AS itens_total_real,
    COUNT(i.id) FILTER (WHERE i.status = 'concluido')      AS itens_concluidos_real,
    COUNT(i.id) FILTER (WHERE i.status = 'erro')           AS itens_erro,
    COUNT(i.id) FILTER (WHERE i.status = 'pulado')         AS itens_pulados,
    j.progresso_msg,
    j.erro,
    j.h2h_ts_inicio,
    j.h2h_ts_fim,
    j.pid,
    j.criado_em,
    j.iniciado_em,
    j.finalizado_em,
    j.atualizado_em
FROM esteira_jobs j
LEFT JOIN esteira_itens i ON i.esteira_job_id = j.id
GROUP BY j.id;

COMMIT;

-- fim — proximo passo: workers/esteira_job.py (o ciclo) + run_esteira.py (subprocesso)
