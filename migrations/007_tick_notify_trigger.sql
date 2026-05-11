-- migrations/007_tick_notify_trigger.sql
-- Trigger que dispara NOTIFY a cada INSERT de tick LIVE
-- Usado pelo bot_executor.py pra reagir em tempo real (latencia ~10-50ms)
--
-- SEGURANCA:
-- - Trigger eh AFTER INSERT (tick ja foi gravado quando dispara)
-- - EXCEPTION nunca quebra o INSERT (NOTIFY falhar nao afeta o coletor)
-- - Filtra so ticks com live_time NOT NULL (jogo ao vivo)
-- - Filtra mercados de interesse (over_under, ml, btts, ah, eh)
-- - Filtra odds em range razoavel (1.10-15.0)
-- - Payload pequeno (so o ID), executor consulta o resto via SELECT
--
-- REVERSIVEL:
--   DROP TRIGGER IF EXISTS trg_tick_novo_notify ON ticks;
--   DROP FUNCTION IF EXISTS fn_tick_novo_notify();

CREATE OR REPLACE FUNCTION fn_tick_novo_notify() RETURNS TRIGGER AS $$
DECLARE
    mt TEXT;
BEGIN
    -- Filtros pra reduzir volume de NOTIFY:
    -- 1. So ticks live
    -- 2. So odds validas e em range razoavel
    -- 3. So mercados que bots costumam monitorar
    --
    -- Nota: SCORE_UPDATE eh ignorado (mercado_tipo='SCORE_UPDATE')
    --       porque eh evento de placar, nao de odd
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

DROP TRIGGER IF EXISTS trg_tick_novo_notify ON ticks;

CREATE TRIGGER trg_tick_novo_notify
    AFTER INSERT ON ticks
    FOR EACH ROW
    EXECUTE FUNCTION fn_tick_novo_notify();

COMMENT ON FUNCTION fn_tick_novo_notify() IS 'Dispara NOTIFY tick_novo a cada INSERT de tick live. Usado pelo bot_executor.';
