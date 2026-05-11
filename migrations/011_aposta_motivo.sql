-- ============================================================
-- 011_aposta_motivo.sql
-- Adiciona coluna 'motivo' em apostas: resumo curto do porque
-- da tip ter sido apitada (ex: "WR10=100% · gap=8 · Q2 · L=115.5")
-- ============================================================

ALTER TABLE apostas
ADD COLUMN IF NOT EXISTS motivo TEXT;

COMMENT ON COLUMN apostas.motivo IS
'Resumo curto dos filtros que bateram pra apitar a tip. '
'Preenchido pelo bot_executor no momento do INSERT. '
'Exemplo: "WR10=100% · WR5=100% · gap=8 · Q2 · L=115.5"';

-- Backfill opcional pra apostas antigas: deixa NULL mesmo
-- (a logica de montagem precisa do stats_h2h e do filtros do bot
--  no momento da apita, dificil reconstruir retroativamente).

-- Verificacao
SELECT
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'apostas' AND column_name = 'motivo';