-- migrations/006_bots_status.sql
-- Adiciona coluna 'status' na tabela bots pra controlar quais simulam
-- Valores: 'pausado' (default) | 'ativo' | 'arquivado'

ALTER TABLE bots
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pausado',
    ADD COLUMN IF NOT EXISTS ativado_em TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_bots_status ON bots(status) WHERE status = 'ativo';

COMMENT ON COLUMN bots.status IS 'pausado=nao simula | ativo=simulando paper | arquivado=fora de uso';
