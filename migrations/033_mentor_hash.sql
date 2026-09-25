-- 033_mentor_hash.sql — questões repetidas (mesmo texto, ids diferentes) passam a ter a mesma marca.
-- Idempotente. Não derruba nada.
ALTER TABLE mentor.questao ADD COLUMN IF NOT EXISTS enunciado_hash TEXT
    GENERATED ALWAYS AS (md5(left(lower(coalesce(enunciado,'')), 300))) STORED;
CREATE INDEX IF NOT EXISTS ix_questao_hash ON mentor.questao(enunciado_hash);
