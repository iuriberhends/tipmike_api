-- migrations/007_tick_notify_trigger.sql
-- Trigger que dispara NOTIFY a cada INSERT de tick LIVE
-- Usado pelo bot_executor.py pra reagir em tempo real (latencia ~10-50ms)
--
-- IMPORTANTE: usa lock_timeout pra nao ficar travado esperando lock exclusivo.
-- Se nao conseguir o lock em 5s, aborta limpo (sem afetar coletor).
-- Aplicar em horario de menor volume aumenta chance de sucesso na 1a tentativa.

-- Limita espera por lock a 5 segundos (em vez de infinito)
SET lock_timeout = '5s';

CREATE OR REPLACE FUNCTION fn_tick_novo_notify() RETURNS TRIGGER AS $$
DECLARE
    mt TEXT;
BEGIN
    -- Filtros pra reduzir volume de NOTIFY:
    IF NEW.live_time IS NULL THEN
        RETURN NULL;
    END IF;
    IF NEW.odds IS NULL OR NEW.odds < 1.10 OR NEW.odds > 15.0 THEN
        RETURN NULL;
    END IF;

    mt := COALESCE(NEW.mercado_tipo, '');
    IF mt = 'SCORE_UPDATE' OR mt = '' THEN
        RETURN NULL;
    END IF;

    -- Tudo certo, dispara NOTIFY
    BEGIN
        PERFORM pg_notify('tick_novo', NEW.id::text);
    EXCEPTION
        WHEN OTHERS THEN
            -- Nunca quebra o INSERT por problema no NOTIFY
            NULL;
    END;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- DROP + CREATE TRIGGER precisa de ACCESS EXCLUSIVE lock na tabela
-- Se nao conseguir em 5s, falha limpo
DROP TRIGGER IF EXISTS trg_tick_novo_notify ON ticks;

CREATE TRIGGER trg_tick_novo_notify
    AFTER INSERT ON ticks
    FOR EACH ROW
    EXECUTE FUNCTION fn_tick_novo_notify();

COMMENT ON FUNCTION fn_tick_novo_notify() IS 'Dispara NOTIFY tick_novo a cada INSERT de tick live. Usado pelo bot_executor.';

-- Reseta lock_timeout pra default
RESET lock_timeout;
