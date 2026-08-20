-- ============================================================================
-- smoke_esteira.sql — valida a fundacao da esteira SEM deixar nada gravado
-- ----------------------------------------------------------------------------
-- Roda 9 testes dentro de UMA transacao e da ROLLBACK no fim (so as sequences
-- avancam, o que e irrelevante). Qualquer falha estoura com FALHA: ... e o
-- psql sai com erro (usar -v ON_ERROR_STOP=1).
-- ============================================================================

SET client_encoding = 'UTF8';

BEGIN;

DO $$
DECLARE
    v_job    INTEGER;
    v_item   INTEGER;
    v_ts1    TIMESTAMP;
    v_ts2    TIMESTAMP;
    v_n      INTEGER;
    v_conc   INTEGER;
    v_b      BOOLEAN;
BEGIN
    ------------------------------------------------------------------
    -- 1) cria rodada
    ------------------------------------------------------------------
    INSERT INTO esteira_jobs (nome, origem, origem_ref, params)
    VALUES ('SMOKE_RODADA', 'planilha', 'estrategias.xlsx',
            '{"fonte":"arquivo","upload_id":"smoke_teste"}'::jsonb)
    RETURNING id INTO v_job;
    RAISE NOTICE 'OK 1/9: rodada criada (id %)', v_job;

    ------------------------------------------------------------------
    -- 2) decisao de usuario em params via jsonb_set (nunca no status)
    ------------------------------------------------------------------
    UPDATE esteira_jobs
       SET params = jsonb_set(params, '{confirmado}', 'true'::jsonb, true)
     WHERE id = v_job;
    SELECT (params->>'confirmado')::boolean INTO v_b FROM esteira_jobs WHERE id = v_job;
    IF v_b IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'FALHA: params.confirmado nao gravou via jsonb_set';
    END IF;
    RAISE NOTICE 'OK 2/9: params.confirmado via jsonb_set';

    ------------------------------------------------------------------
    -- 3) trigger de atualizado_em (clock_timestamp avanca na transacao)
    ------------------------------------------------------------------
    SELECT atualizado_em INTO v_ts1 FROM esteira_jobs WHERE id = v_job;
    PERFORM pg_sleep(0.05);
    UPDATE esteira_jobs SET progresso_msg = 'aguardando slot (RAM 5.1 GB livre < 6)' WHERE id = v_job;
    SELECT atualizado_em INTO v_ts2 FROM esteira_jobs WHERE id = v_job;
    IF v_ts2 <= v_ts1 THEN
        RAISE EXCEPTION 'FALHA: trigger touch nao avancou atualizado_em (% -> %)', v_ts1, v_ts2;
    END IF;
    RAISE NOTICE 'OK 3/9: trigger atualizado_em';

    ------------------------------------------------------------------
    -- 4) CHECK de status rejeita valor invalido
    ------------------------------------------------------------------
    BEGIN
        UPDATE esteira_jobs SET status = 'status_que_nao_existe' WHERE id = v_job;
        RAISE EXCEPTION 'FALHA: CHECK de status aceitou valor invalido';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'OK 4/9: CHECK de status rejeitou valor invalido';
    END;

    ------------------------------------------------------------------
    -- 5) itens: sentinela + controle + estrategia com snapshot realista
    ------------------------------------------------------------------
    INSERT INTO esteira_itens (esteira_job_id, ordem, nome, papel, assinatura, snapshot)
    VALUES (v_job, 0, 'CALIBRACAO_ESCANCARADA', 'sentinela', 'sha1_sentinela_smoke',
            '{"casa":"superbet","esporte":"nba2k","mercado":"ah_ft","filtros":{}}'::jsonb);

    INSERT INTO esteira_itens (esteira_job_id, ordem, nome, papel, assinatura, snapshot)
    VALUES (v_job, 1, 'CONTROLE_SEM_EIXO', 'controle', 'sha1_controle_smoke',
            '{"casa":"superbet","esporte":"nba2k","mercado":"ah_ft","filtros":{"linhaMin":9.5}}'::jsonb);

    INSERT INTO esteira_itens (esteira_job_id, ordem, nome, papel, assinatura, snapshot)
    VALUES (v_job, 2, 'todas65_L95', 'estrategia', 'sha1_todas65_smoke',
            '{"casa":"superbet","esporte":"nba2k","mercado":"ah_ft","hc_lado":"+",
              "filtros":{"linhaMin":9.5,"folgaAtivo":true,"folgaMin":3.5,
                         "evitarLinhasSeq":false,
                         "filtros_hist":[{"janela":"all","prob":[65,100],"minPartidas":0,"base":"match"}]}}'::jsonb)
    RETURNING id INTO v_item;
    RAISE NOTICE 'OK 5/9: 3 itens criados (sentinela/controle/estrategia)';

    ------------------------------------------------------------------
    -- 6) so 1 sentinela por rodada (unique parcial)
    ------------------------------------------------------------------
    BEGIN
        INSERT INTO esteira_itens (esteira_job_id, nome, papel, snapshot)
        VALUES (v_job, 'SENTINELA_2', 'sentinela', '{}'::jsonb);
        RAISE EXCEPTION 'FALHA: aceitou 2a sentinela na mesma rodada';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'OK 6/9: apenas 1 sentinela por rodada';
    END;

    ------------------------------------------------------------------
    -- 7) FK do backtest_job_id (so testa se a FK existe neste banco)
    ------------------------------------------------------------------
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_esteira_itens_backtest_job') THEN
        BEGIN
            UPDATE esteira_itens SET backtest_job_id = -999999 WHERE id = v_item;
            RAISE EXCEPTION 'FALHA: FK backtest_job_id aceitou id inexistente';
        EXCEPTION WHEN foreign_key_violation THEN
            RAISE NOTICE 'OK 7/9: FK backtest_job_id ativa e validando';
        END;
    ELSE
        RAISE NOTICE 'OK 7/9 (pulado): FK p/ backtest_jobs nao existe neste banco - coluna livre, ok';
    END IF;

    ------------------------------------------------------------------
    -- 8) view soma certo (1 concluido, 3 no total)
    ------------------------------------------------------------------
    UPDATE esteira_itens
       SET status = 'concluido',
           metricas = '{"ap":346,"greens":253,"reds":93,"wr":73.1,"u":116.2,"roi":33.6,"dd":12.1,"roi_3d":69.5,"z_jogo":4.57,"lucro_dd":9.6}'::jsonb
     WHERE id = v_item;

    SELECT itens_total_real, itens_concluidos_real
      INTO v_n, v_conc
      FROM v_esteira_jobs WHERE id = v_job;
    IF v_n <> 3 OR v_conc <> 1 THEN
        RAISE EXCEPTION 'FALHA: view devolveu total=% concluidos=% (esperado 3 e 1)', v_n, v_conc;
    END IF;
    RAISE NOTICE 'OK 8/9: view v_esteira_jobs contando certo (3 itens, 1 concluido)';

    ------------------------------------------------------------------
    -- 9) cascade: apagar a rodada leva os itens junto
    ------------------------------------------------------------------
    DELETE FROM esteira_jobs WHERE id = v_job;
    SELECT count(*) INTO v_n FROM esteira_itens WHERE esteira_job_id = v_job;
    IF v_n <> 0 THEN
        RAISE EXCEPTION 'FALHA: cascade nao apagou os itens (% sobraram)', v_n;
    END IF;
    RAISE NOTICE 'OK 9/9: ON DELETE CASCADE funcionando';

    RAISE NOTICE '=== SMOKE ESTEIRA: 9/9 TESTES PASSARAM ===';
END $$;

ROLLBACK;  -- nada fica gravado
