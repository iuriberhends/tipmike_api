-- 036 (11/nov/2026) — GATILHO tick_novo SÓ PARA CASA COM AO VIVO LIGADO
--
-- O gatilho da 007 avisava o bot_executor de TODO tick ao vivo (odd 1.10–15,
-- mercado != SCORE_UPDATE), de qualquer casa. Com o Peru gravando na mesma
-- tabela, isso virou ~1 milhão de avisos por hora (12x a Superbet) para um
-- executor que não tem nenhum bot nessas casas: cada aviso ainda custa um SELECT
-- do tick e uma passada por todos os bots ativos.
--
-- Agora o tick só gera aviso se a casa NÃO estiver marcada como ao_vivo = false
-- no cadastro (035). Casa fora do cadastro continua avisando como antes, para
-- nenhuma casa do Brasil parar por esquecimento. A consulta extra só acontece
-- para o tick que já passou nos filtros baratos, e é por chave primária numa
-- tabela de ~20 linhas.
--
-- Mesmo cuidado da 007: lock_timeout para não travar o coletor; a função é
-- trocada sem recriar o gatilho.

SET lock_timeout = '5s';

CREATE OR REPLACE FUNCTION fn_tick_novo_notify() RETURNS TRIGGER AS $$
DECLARE
    mt TEXT;
BEGIN
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
    -- 036: casa com ao vivo desligado (ex.: Peru) não acorda o executor.
    -- Tudo dentro do bloco: se o cadastro falhar (tabela fora do ar etc.), o
    -- gatilho avisa como antes e NUNCA derruba o INSERT do coletor.
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM casas WHERE codigo = NEW.bookmaker AND NOT ao_vivo) THEN
            PERFORM pg_notify('tick_novo', NEW.id::text);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        BEGIN
            PERFORM pg_notify('tick_novo', NEW.id::text);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
