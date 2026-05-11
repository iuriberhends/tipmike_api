-- ============================================================
-- 009_telegram.sql — Integração com Telegram
--
-- Adiciona:
-- - Tabela telegram_canais (FIFA, NBA, etc)
-- - Coluna telegram_canal_id em bots (FK opcional)
-- - Coluna em_treinamento em bots (BOOL, default false)
-- - Trigger fn_aposta_telegram_notify pra disparar NOTIFY
--   nos canais 'aposta_nova' (INSERT) e 'aposta_resolvida' (UPDATE)
-- ============================================================

-- 1. TABELA DE CANAIS
CREATE TABLE IF NOT EXISTS telegram_canais (
    id          SERIAL PRIMARY KEY,
    nome        TEXT NOT NULL,
    chat_id     TEXT NOT NULL UNIQUE,
    descricao   TEXT,
    ativo       BOOLEAN DEFAULT true,
    criado_em   TIMESTAMPTZ DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE telegram_canais IS 'Canais Telegram onde os bots podem enviar tips';
COMMENT ON COLUMN telegram_canais.chat_id IS 'ID do chat/canal (começa com -100 pra canais)';

-- Trigger updated_at automatico
CREATE OR REPLACE FUNCTION fn_telegram_canais_touch() RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_telegram_canais_touch ON telegram_canais;
CREATE TRIGGER trg_telegram_canais_touch
BEFORE UPDATE ON telegram_canais
FOR EACH ROW EXECUTE FUNCTION fn_telegram_canais_touch();

-- 2. CAMPOS NO BOT
ALTER TABLE bots
    ADD COLUMN IF NOT EXISTS telegram_canal_id INT REFERENCES telegram_canais(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS em_treinamento BOOLEAN DEFAULT false;

COMMENT ON COLUMN bots.telegram_canal_id IS 'Canal Telegram onde envia as tips (NULL = não envia)';
COMMENT ON COLUMN bots.em_treinamento IS 'Quando TRUE: bot continua simulando mas não notifica Telegram nem opera real';

CREATE INDEX IF NOT EXISTS idx_bots_telegram_canal ON bots(telegram_canal_id) WHERE telegram_canal_id IS NOT NULL;

-- 3. TRIGGER DE NOTIFY (aposta criada + aposta resolvida)
CREATE OR REPLACE FUNCTION fn_aposta_telegram_notify() RETURNS TRIGGER AS $$
BEGIN
    -- INSERT: aposta nova (status sempre 'pendente' na criação)
    IF TG_OP = 'INSERT' AND NEW.status = 'pendente' THEN
        BEGIN
            PERFORM pg_notify('aposta_nova', NEW.id::text);
        EXCEPTION
            WHEN OTHERS THEN NULL;  -- nunca quebra o INSERT por erro no NOTIFY
        END;
        RETURN NEW;
    END IF;

    -- UPDATE: status mudou de pendente pra resolvido (green/red/void)
    IF TG_OP = 'UPDATE'
       AND COALESCE(OLD.status, '') = 'pendente'
       AND NEW.status IN ('green', 'red', 'void') THEN
        BEGIN
            PERFORM pg_notify('aposta_resolvida', NEW.id::text);
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_aposta_telegram ON apostas;
CREATE TRIGGER trg_aposta_telegram
AFTER INSERT OR UPDATE ON apostas
FOR EACH ROW EXECUTE FUNCTION fn_aposta_telegram_notify();

-- 4. INSERIR OS 2 CANAIS INICIAIS (placeholder — usuario edita o chat_id depois)
-- Use UPDATE pra trocar o chat_id depois:
--   UPDATE telegram_canais SET chat_id = '-1001234567890' WHERE nome = 'FIFA';

INSERT INTO telegram_canais (nome, chat_id, descricao, ativo)
VALUES
    ('FIFA', 'PLACEHOLDER_FIFA', 'Canal para tips de e-Soccer (Fifa)', false),
    ('NBA',  'PLACEHOLDER_NBA',  'Canal para tips de e-Basket (NBA2K)', false)
ON CONFLICT (chat_id) DO NOTHING;

-- ============================================================
-- VERIFICAÇÃO
-- ============================================================
-- SELECT * FROM telegram_canais;
-- \d bots
-- SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'apostas'::regclass;