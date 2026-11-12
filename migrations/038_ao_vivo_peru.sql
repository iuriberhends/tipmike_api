-- 038 (11/nov/2026) — ROBÔS AO VIVO NO PERU
--
-- O motor e o executor já leem as casas do Peru (tradução do espanhol, mapa por
-- plataforma, HC e 1º tempo validados, fim de jogo da OBG pelo placar corrigido).
-- (Numerada 038: a 037 e' a apostas_exec do robo de apostas, escrita antes.)
-- Esta migration liga o ao vivo delas sem atrapalhar o Brasil:
--
-- 1) TELEGRAM POR PAÍS. O gatilho da 010 escolhia o canal só pelo esporte
--    ('FIFA'/'NBA'): um bot do Peru postaria no canal do Brasil. Agora casa de
--    outro país procura o canal com o sufixo do país ('FIFA PE', 'NBA PE'). Se
--    ele não existir, o bot fica SEM Telegram (nunca cai no canal do Brasil).
--    Também passa a disparar quando a CASA do bot muda.
--
-- 2) GATILHO tick_novo. Com o ao vivo ligado, casa de outro país só acorda o
--    executor se tiver ao menos um bot ATIVO nela. Sem isso voltariam ~1 milhão
--    de avisos por hora do Peru sem uso. Brasil: igual à 036.
--
-- 3) ao_vivo = TRUE nas casas do Peru.
--
-- Mesmo cuidado das anteriores: lock_timeout curto; erro no cadastro nunca
-- derruba o INSERT do coletor nem o UPDATE do bot.

SET lock_timeout = '5s';

-- 1) Telegram por país ---------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_bot_auto_telegram_canal() RETURNS TRIGGER AS $$
DECLARE
    v_canal_id   INT;
    v_canal_nome TEXT;
    v_pais       TEXT;
BEGIN
    IF NEW.esporte = 'fifa' THEN
        v_canal_nome := 'FIFA';
    ELSIF NEW.esporte = 'nba2k' THEN
        v_canal_nome := 'NBA';
    ELSE
        v_canal_nome := NULL;
    END IF;

    -- 038: casa de outro país -> canal com o sufixo do país
    BEGIN
        SELECT pais INTO v_pais FROM casas WHERE codigo = NEW.casa;
    EXCEPTION WHEN OTHERS THEN
        v_pais := NULL;
    END;
    IF v_canal_nome IS NOT NULL AND v_pais IS NOT NULL AND v_pais <> 'BR' THEN
        v_canal_nome := v_canal_nome || ' ' || v_pais;
    END IF;

    IF v_canal_nome IS NOT NULL THEN
        SELECT id INTO v_canal_id FROM telegram_canais WHERE nome = v_canal_nome LIMIT 1;
    ELSE
        v_canal_id := NULL;
    END IF;

    NEW.telegram_canal_id := v_canal_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bot_auto_telegram_canal ON bots;
CREATE TRIGGER trg_bot_auto_telegram_canal
BEFORE INSERT OR UPDATE OF esporte, casa ON bots
FOR EACH ROW EXECUTE FUNCTION fn_bot_auto_telegram_canal();

-- 2) Gatilho tick_novo -----------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_tick_novo_notify() RETURNS TRIGGER AS $$
DECLARE
    mt         TEXT;
    v_pais     TEXT;
    v_ao_vivo  BOOLEAN;
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
    BEGIN
        SELECT pais, ao_vivo INTO v_pais, v_ao_vivo FROM casas WHERE codigo = NEW.bookmaker;
        IF FOUND THEN
            -- 036: casa com ao vivo desligado não acorda o executor
            IF NOT v_ao_vivo THEN
                RETURN NULL;
            END IF;
            -- 038: casa de outro país só com bot ativo nela
            IF v_pais IS NOT NULL AND v_pais <> 'BR'
               AND NOT EXISTS (SELECT 1 FROM bots WHERE casa = NEW.bookmaker AND status = 'ativo') THEN
                RETURN NULL;
            END IF;
        END IF;
        PERFORM pg_notify('tick_novo', NEW.id::text);
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

-- 3) Ao vivo ligado no Peru ------------------------------------------------------
UPDATE casas SET ao_vivo = TRUE, atualizado = now() WHERE pais = 'PE' AND NOT ao_vivo;
