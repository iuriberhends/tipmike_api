-- migrations/018_apostas_supermae_claim.sql
-- Colunas pro BRACO DE SINAIS do supermae (outra VPS) fazer o claim atomico
-- e reportar a execucao em dinheiro real.
--
-- claimed_supermae: NOW() quando o supermae "pegou" a tip pra apostar. O claim
--   e atomico (UPDATE ... WHERE claimed_supermae IS NULL RETURNING) — garante
--   que NOTIFY + poll nunca apostem a mesma tip 2x.
-- supermae_exec_*: auditoria de como o supermae executou a tip na Superbet.
--
-- Todas NULLABLE e sem default alem de NULL: nao afeta em NADA o fluxo atual
-- da MikeDB (o bot_executor continua inserindo apostas normalmente).

ALTER TABLE apostas
    ADD COLUMN IF NOT EXISTS claimed_supermae      TIMESTAMP,
    ADD COLUMN IF NOT EXISTS supermae_exec_ok       BOOLEAN,
    ADD COLUMN IF NOT EXISTS supermae_exec_detalhe  TEXT,
    ADD COLUMN IF NOT EXISTS supermae_exec_em       TIMESTAMP;

-- Index parcial pro poll de seguranca do supermae (pendentes nao-claimadas
-- recentes). Cobre a query do braco sem varrer a tabela inteira.
CREATE INDEX IF NOT EXISTS idx_apostas_supermae_claim
    ON apostas (apostado_em)
    WHERE claimed_supermae IS NULL AND status = 'pendente';

COMMENT ON COLUMN apostas.claimed_supermae IS
'NOW() quando o braco de sinais do supermae pegou esta tip pra apostar (claim atomico anti-duplicata). NULL = ainda nao pega.';
COMMENT ON COLUMN apostas.supermae_exec_ok IS
'True/False de como o supermae executou a tip na Superbet (dinheiro real). NULL = nao executada.';

-- ============================================================
-- Verificacao
-- ============================================================
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'apostas'
  AND column_name IN ('claimed_supermae','supermae_exec_ok','supermae_exec_detalhe','supermae_exec_em')
ORDER BY column_name;
