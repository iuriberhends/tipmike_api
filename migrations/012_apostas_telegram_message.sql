-- ============================================================
-- 012_apostas_telegram_message.sql
-- Adiciona tracking da mensagem do Telegram em apostas:
--   telegram_message_id   -> id da mensagem retornada pelo sendMessage
--   telegram_chat_id      -> chat_id usado no envio (snapshot)
--   telegram_message_text -> texto integral (HTML) da mensagem original
--
-- Uso: telegram_notifier v8+ usa esses campos pra EDITAR a mensagem
-- original quando a aposta resolve (anti-flood), em vez de mandar
-- uma mensagem nova de GREEN/RED/DEVOLVIDA.
--
-- Apostas antigas (sem telegram_message_id) caem no fallback de
-- mandar mensagem nova, comportamento atual.
-- ============================================================

ALTER TABLE apostas
    ADD COLUMN IF NOT EXISTS telegram_message_id   BIGINT,
    ADD COLUMN IF NOT EXISTS telegram_chat_id      TEXT,
    ADD COLUMN IF NOT EXISTS telegram_message_text TEXT;

COMMENT ON COLUMN apostas.telegram_message_id IS
'ID da mensagem retornada pelo Telegram sendMessage. '
'Usado pelo telegram_notifier para editar a mensagem ao resolver a aposta.';

COMMENT ON COLUMN apostas.telegram_chat_id IS
'chat_id usado no envio (snapshot, caso o canal do bot mude depois).';

COMMENT ON COLUMN apostas.telegram_message_text IS
'Texto integral (HTML) da mensagem original enviada. '
'Usado como base para editMessageText quando a aposta resolve.';

-- Verificacao
SELECT
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'apostas'
  AND column_name IN ('telegram_message_id', 'telegram_chat_id', 'telegram_message_text')
ORDER BY column_name;