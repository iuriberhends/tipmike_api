-- ============================================================
-- 010_telegram_auto_canal.sql
-- Auto-set telegram_canal_id baseado no esporte do bot
--
-- Regras:
--   esporte = 'fifa'   -> canal FIFA
--   esporte = 'nba2k'  -> canal NBA
--   outros             -> NULL (nao envia)
--
-- Trigger BEFORE INSERT OR UPDATE em bots:
--   Se esporte mudou OU bot recem-criado, recalcula telegram_canal_id
-- ============================================================

CREATE OR REPLACE FUNCTION fn_bot_auto_telegram_canal() RETURNS TRIGGER AS $$
DECLARE
    v_canal_id INT;
    v_canal_nome TEXT;
BEGIN
    -- Define o nome do canal baseado no esporte
    IF NEW.esporte = 'fifa' THEN
        v_canal_nome := 'FIFA';
    ELSIF NEW.esporte = 'nba2k' THEN
        v_canal_nome := 'NBA';
    ELSE
        v_canal_nome := NULL;
    END IF;

    -- Busca o id do canal (so se tiver nome)
    IF v_canal_nome IS NOT NULL THEN
        SELECT id INTO v_canal_id FROM telegram_canais WHERE nome = v_canal_nome LIMIT 1;
    ELSE
        v_canal_id := NULL;
    END IF;

    -- Atribui (sobrescreve sempre, garantindo consistencia esporte<->canal)
    NEW.telegram_canal_id := v_canal_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bot_auto_telegram_canal ON bots;
CREATE TRIGGER trg_bot_auto_telegram_canal
BEFORE INSERT OR UPDATE OF esporte ON bots
FOR EACH ROW EXECUTE FUNCTION fn_bot_auto_telegram_canal();

-- ============================================================
-- BACKFILL: aplica regra em todos os bots existentes
-- ============================================================
UPDATE bots
SET telegram_canal_id = CASE
    WHEN esporte = 'fifa'  THEN (SELECT id FROM telegram_canais WHERE nome = 'FIFA' LIMIT 1)
    WHEN esporte = 'nba2k' THEN (SELECT id FROM telegram_canais WHERE nome = 'NBA' LIMIT 1)
    ELSE NULL
END;

-- ============================================================
-- VERIFICACAO
-- ============================================================
-- SELECT id, nome, esporte, telegram_canal_id, em_treinamento FROM bots ORDER BY id;
-- SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'bots'::regclass;