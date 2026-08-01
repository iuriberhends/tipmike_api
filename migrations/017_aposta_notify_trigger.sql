-- migrations/017_aposta_notify_trigger.sql
-- Trigger que dispara NOTIFY a cada INSERT de aposta apitada por um bot.
-- Usado pelo BRACO DE SINAIS do supermae (outra VPS) pra reagir em tempo
-- real (latencia ~10-50ms + latencia de rede entre as VPS) e executar a
-- tip com dinheiro real na Superbet.
--
-- Espelha a migration 007 (tick_novo): mesmo cuidado com lock_timeout, e o
-- NOTIFY NUNCA quebra o INSERT da aposta (se falhar, engole a excecao).
--
-- IMPORTANTE (seguranca): o payload leva o id da linha em `apostas`; o
-- supermae faz o claim/filtro de bot autorizado no SELECT (defesa em
-- profundidade). Este trigger ja pre-filtra pra reduzir ruido na rede:
-- so notifica apostas que sao candidatas a execucao real.

SET lock_timeout = '5s';

CREATE OR REPLACE FUNCTION fn_aposta_nova_notify() RETURNS TRIGGER AS $$
BEGIN
    -- Pre-filtro pra reduzir volume de NOTIFY que cruza a rede entre VPS.
    -- So interessa aposta recem-apitada (status pendente) com odd valida.
    -- O supermae ainda re-filtra por bot_id autorizado no claim (nao confiar
    -- so no trigger — defesa em profundidade).
    IF COALESCE(NEW.status, '') <> 'pendente' THEN
        RETURN NULL;
    END IF;
    IF NEW.odd IS NULL OR NEW.odd < 1.01 THEN
        RETURN NULL;
    END IF;

    BEGIN
        PERFORM pg_notify('aposta_nova', NEW.id::text);
    EXCEPTION
        WHEN OTHERS THEN
            -- Nunca quebra o INSERT da aposta por problema no NOTIFY.
            NULL;
    END;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- DROP + CREATE TRIGGER precisa de ACCESS EXCLUSIVE lock na tabela `apostas`.
-- Se nao conseguir em 5s, falha limpo (rode em horario de menor volume).
DROP TRIGGER IF EXISTS trg_aposta_nova_notify ON apostas;

CREATE TRIGGER trg_aposta_nova_notify
    AFTER INSERT ON apostas
    FOR EACH ROW
    EXECUTE FUNCTION fn_aposta_nova_notify();

COMMENT ON FUNCTION fn_aposta_nova_notify() IS
'Dispara NOTIFY aposta_nova a cada INSERT de aposta pendente. Consumido pelo braco de sinais do supermae (outra VPS) pra execucao em dinheiro real.';

RESET lock_timeout;

-- ============================================================
-- Verificacao
-- ============================================================
SELECT tgname, tgrelid::regclass AS tabela, tgenabled
FROM pg_trigger
WHERE tgname = 'trg_aposta_nova_notify';
