# -*- coding: utf-8 -*-
"""
routers/mentor.py — MIKE MENTOR, motor de estudo (schema v3, spec v3 + v3.2)

Tudo que o painel precisa, em rotas:
  painel        GET  /mentor/painel                    início: agora, plano de hoje, KPIs, XP/sequência
  edital        GET  /mentor/edital?materia_id=        árvore: módulo → assunto → aulas (tier, estado, estoque)
  semanas       GET  /mentor/semanas                   lista; POST /mentor/semana/montar (planejador da segunda)
                GET  /mentor/semana/{id}
  sessão        POST /mentor/sessao/iniciar            POST /mentor/sessao/{id}/fechar
  aula          GET  /mentor/aula/{id}                 conteúdo + aquecimento + fixação + gate
                POST /mentor/aula/{id}/assistida
                POST /mentor/aula/{id}/gate            fecha o gate (4 de 5); agenda R1 quando o assunto fecha
  resposta      POST /mentor/resposta                  grava e devolve a correção
  revisões      GET  /mentor/revisoes/hoje             POST /mentor/revisao/{id}/concluir  POST /mentor/revisao/{id}/adiar
  questões      GET  /mentor/questoes                  "Fazer questões" (tudo estudado | escolher)
  simulado      GET  /mentor/simulados  POST /mentor/simulado/montar  GET /mentor/simulado/{id}
                POST /mentor/simulado/{id}/entregar
  mais cai      GET  /mentor/mais-cai
  bizus         GET  /mentor/bizus/aula/{id}
  cards         GET  /mentor/cards/hoje  POST /mentor/card/{id}/responder
  caderno       GET  /mentor/caderno  POST /mentor/caderno  POST /mentor/caderno/{id}   (caderno de erros: uma linha por erro)
  base          GET  /mentor/base                          base por matéria (fase, trilha, aulas feitas, prova)
                POST /mentor/materia/{id}/prova-base/iniciar   20 questões de Soldado da matéria
                POST /mentor/prova-base/{id}/entregar          régua por matéria → modo questão

v3.8 — A BASE no motor (BASE_POR_MATERIA_E_CRONOGRAMA.md): cada aula tem base=true/false (mentor_flags.py), cada assunto um modo
(complemento | lei_seca | questoes | leitura | fora | revisao) e cada matéria uma fase (base → questao). O planejador monta a
semana por 4 trilhas (config "trilhas"), base primeiro; quando a base de uma matéria fecha, a prova de base (20 questões) libera
o modo questão e a matéria passa a entrar por sessões sem vídeo (resumo + fixação + concurso). O aquecimento decide o vídeo
no painel: base pula com 5/5; complemento chama com ≤ 2/5.
  versão        GET  /mentor/versao

v3.7 — O MÉTODO no motor: caderno de erros (uma linha por erro → vira card), recall a cada 10 min do vídeo
(evento "recall"), flashcards do Gran viram cards de Leitner quando a aula fecha, resumo de bolso e contagens
de revisão (questões + cards + caderno) entregues ao painel.

Regras (spec v3.2): gate 4 de 5 por aula; aquecimento não conta; certo/errado entra em tudo menos no
simulado; revisão adaptativa R1→R7→R30→M com intervalos que esticam ou encurtam pelo acerto; teto de
revisão 35% do dia; XP só por resultado (gate, revisão no dia, card, meta) — nunca por velocidade.
"""
import json
import logging
import math
import random
import re
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from database import db

log = logging.getLogger("mentor")
router = APIRouter(prefix="/mentor", tags=["Mentor"])
VERSAO = "3.8-base"

# ----------------------------------------------------------------- constantes
GATE_N, GATE_MIN = 5, 4
AQUEC_N, FIX_N = 5, 5
REV_N = {"R1": 4, "R2": 4, "R3": 4, "R7": 6, "R15": 5, "R30": 4, "M": 4}
INTERVALO = {"R1": 1, "R2": 1, "R3": 2, "R7": 6, "R15": 8, "R30": 23, "M": 30}
XP = {"gate": 50, "gate_reestudo": 25, "revisao": 30, "revisao_no_dia": 15, "card": 3, "meta_dia": 80,
      "meta_semana": 300, "simulado": 200, "simulado_acima_60": 200, "combo": 10, "aula_propria": 30, "prova_base": 150}
TRILHAS_PADRAO = {"A": [1, 2], "B": [10, 7, 9], "C": [3, 4], "D": [8, 11, 12, 6, 5]}
PESOS_TRILHA_PADRAO = {"A": 0.25, "B": 0.37, "C": 0.21, "D": 0.17}
MODOS_QUESTAO = ("complemento", "lei_seca", "questoes", "leitura")
ATUALIDADES_ID, ATUALIDADES_DESDE = 5, date(2026, 11, 2)
RETA_FINAL = date(2027, 1, 18)
PROVA_BASE_N = 20
PATENTES = [(0, "Recruta"), (500, "Soldado"), (1500, "Cabo"), (3000, "3º Sargento"), (5000, "2º Sargento"),
            (8000, "1º Sargento"), (12000, "Subtenente"), (17000, "Aspirante"), (23000, "Tenente"),
            (30000, "Capitão"), (40000, "Major"), (52000, "Tenente-Coronel"), (65000, "Coronel")]
MESES_HORAS = {10: 3.0, 11: 4.0, 12: 5.5, 1: 8.0, 2: 8.0, 9: 3.0}
PCT_CONTEUDO = 0.65
DIST_SIMULADO = [(1, 10), (2, 8), (3, 8), (4, 8), (5, 8), (6, 8), (7, 5), (8, 5), (9, 5), (10, 5), (11, 5), (12, 5)]
BANCAS_PREF = ("FCC", "IBFC")


def _hoje():
    # dia de estudo vai das 04:00 às 04:00 (spec) — fuso do servidor
    return (datetime.now() - timedelta(hours=4)).date()


def _r(row):
    return dict(row) if row is not None else None


def _rs(rows):
    return [dict(r) for r in rows]


def _comentario(v):
    """O comentário vem gravado como JSON {professor, texto, html}; devolve só o texto."""
    if not v:
        return None
    if isinstance(v, str) and v.lstrip().startswith("{"):
        try:
            d = json.loads(v)
            t = d.get("texto") or d.get("html") or ""
            if d.get("professor"):
                t = f"{t}\n\n— {d['professor']}"
            v = t
        except Exception:
            pass
    import re as _re
    v = _re.sub(r"<[^>]+>", " ", str(v))
    v = _re.sub(r"\s{3,}", "\n\n", v).strip()
    return v or None


def _patente(xp):
    p = PATENTES[0][1]
    for lim, nome in PATENTES:
        if xp >= lim:
            p = nome
    return p


async def _add_xp(conn, tipo, pontos, ref=None):
    await conn.execute("INSERT INTO mentor.xp_evento (tipo, pontos, ref) VALUES ($1,$2,$3)", tipo, pontos, json.dumps(ref or {}))
    xp = await conn.fetchval("UPDATE mentor.progresso SET xp_total = xp_total + $1 WHERE id=1 RETURNING xp_total", pontos)
    await conn.execute("UPDATE mentor.progresso SET patente=$1 WHERE id=1", _patente(xp))
    return xp


async def _config(conn, chave, padrao=None):
    v = await conn.fetchval("SELECT valor FROM mentor.config WHERE chave=$1", chave)
    if v is None:
        return padrao
    return json.loads(v) if isinstance(v, str) else v


def _horas_dia(d: date, cfg=None):
    cfg = cfg or {}
    return float(cfg.get(str(d.month), MESES_HORAS.get(d.month, 3.0)))


def _dia_conta(d: date):
    """Outubro: sábado só revisão, domingo livre. De novembro em diante, 7 dias (v3.2 §2)."""
    if d.month in (9, 10) and d.year == 2026:
        return "conteudo" if d.weekday() < 5 else ("revisao" if d.weekday() == 5 else "livre")
    return "conteudo"


# ----------------------------------------------------------------- seleção de questões
SQL_BASE_Q = """
    q.usavel AND q.fora_banca = FALSE AND COALESCE(q.fora_edital, FALSE) = FALSE
    AND (q.prova_real_id IS NULL OR NOT EXISTS (SELECT 1 FROM mentor.prova_real pr WHERE pr.id=q.prova_real_id AND pr.reservada))
"""


def _palavra_chave(titulo):
    """A palavra que distingue a aula das irmãs (Writer × Calc × Impress; Windows × Linux)."""
    import re as _re
    t = _re.sub(r"\b(I{1,3}|IV|V|VI{0,3}|IX|X|\d+)\b", " ", titulo or "")
    t = _re.sub(r"[-–:()\[\]]", " ", t)
    STOP = {"office", "libreoffice", "aula", "parte", "de", "da", "do", "e", "o", "a", "os", "as", "em", "no", "na", "dos", "das", "com", "para", "web", "365", "introdução", "exercícios", "exercicios", "resolução", "questões", "questoes", "teoria", "geral"}
    for w in t.split():
        if len(w) >= 4 and w.lower() not in STOP:
            return w
    return None


_FEEDBACK_OK = False


async def _garantir_feedback(conn):
    """A tabela de feedback é criada pelo afinador; se ainda não existe, cria aqui (uma vez por processo)."""
    global _FEEDBACK_OK
    if _FEEDBACK_OK:
        return
    await conn.execute("""CREATE TABLE IF NOT EXISTS mentor.vinculo_feedback (questao_id BIGINT NOT NULL, aula_id BIGINT NOT NULL, ok BOOLEAN NOT NULL,
                          criado_em TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (questao_id, aula_id))""")
    _FEEDBACK_OK = True


_BASE_OK = False


async def _garantir_base(conn):
    """Colunas/tabela da base (as mesmas do mentor_flags.py). Uma vez por processo; não muda dados."""
    global _BASE_OK
    if _BASE_OK:
        return
    for sql in ("ALTER TABLE mentor.aula ADD COLUMN IF NOT EXISTS base BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE mentor.materia ADD COLUMN IF NOT EXISTS fase TEXT NOT NULL DEFAULT 'base'",
                "ALTER TABLE mentor.materia ADD COLUMN IF NOT EXISTS trilha TEXT",
                "ALTER TABLE mentor.materia ADD COLUMN IF NOT EXISTS trilha_ordem INT",
                "ALTER TABLE mentor.materia ADD COLUMN IF NOT EXISTS regua_base INT NOT NULL DEFAULT 60",
                "ALTER TABLE mentor.materia ADD COLUMN IF NOT EXISTS prova_base_em TIMESTAMPTZ",
                "ALTER TABLE mentor.plano_item ADD COLUMN IF NOT EXISTS modo TEXT",
                "ALTER TABLE mentor.plano_item ADD COLUMN IF NOT EXISTS aulas BIGINT[]",
                """CREATE TABLE IF NOT EXISTS mentor.prova_base (id BIGSERIAL PRIMARY KEY, materia_id INT NOT NULL REFERENCES mentor.materia(id) ON DELETE CASCADE,
                   questoes BIGINT[] NOT NULL, respostas JSONB, certas INT, n INT, acerto NUMERIC(5,2), regua INT, aprovada BOOLEAN, aulas_erradas BIGINT[],
                   sessao_id BIGINT REFERENCES mentor.sessao(id) ON DELETE SET NULL, inicio TIMESTAMPTZ NOT NULL DEFAULT now(), fim TIMESTAMPTZ)"""):
        await conn.execute(sql)
    _BASE_OK = True


_CADERNO_OK = False


async def _garantir_caderno(conn):
    """Caderno de erros: uma linha por erro (regra/contraste/pegadinha/desatenção). Criado aqui na primeira vez."""
    global _CADERNO_OK
    if _CADERNO_OK:
        return
    await conn.execute("""CREATE TABLE IF NOT EXISTS mentor.caderno (
        id          BIGSERIAL PRIMARY KEY,
        questao_id  BIGINT REFERENCES mentor.questao(id) ON DELETE SET NULL,
        aula_id     BIGINT REFERENCES mentor.aula(id) ON DELETE SET NULL,
        assunto_id  BIGINT REFERENCES mentor.assunto(id) ON DELETE SET NULL,
        materia_id  BIGINT REFERENCES mentor.materia(id) ON DELETE SET NULL,
        card_id     BIGINT REFERENCES mentor.card(id) ON DELETE SET NULL,
        tipo        TEXT NOT NULL CHECK (tipo IN ('regra','contraste','pegadinha','desatencao')),
        linha       TEXT NOT NULL,
        enunciado   TEXT,
        contexto    TEXT,
        riscada_em  TIMESTAMPTZ,
        voltou      INT NOT NULL DEFAULT 0,
        criado_em   TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_caderno_questao ON mentor.caderno(questao_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_caderno_aberta ON mentor.caderno(materia_id) WHERE riscada_em IS NULL")
    # aulas fechadas antes da v3.7: os flashcards do Gran delas viram cards agora (uma vez)
    for r in await conn.fetch("SELECT id FROM mentor.aula WHERE estado='concluida' AND flashcards IS NOT NULL AND NOT EXISTS (SELECT 1 FROM mentor.card c WHERE c.aula_id=mentor.aula.id AND c.tipo='flashcard_gran')"):
        try:
            await _semear_cards_aula(conn, r["id"])
        except Exception as e:
            log.warning("semear cards aula %s: %s", r["id"], e)
    _CADERNO_OK = True


def _curto(t, n=220):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


async def _semear_cards_aula(conn, aula_id):
    """Flashcards do Gran da aula viram cards de Leitner (caixa 0, amanhã) quando a aula fecha — uma vez só."""
    if await conn.fetchval("SELECT 1 FROM mentor.card WHERE aula_id=$1 AND tipo='flashcard_gran' LIMIT 1", aula_id):
        return 0
    a = _r(await conn.fetchrow("SELECT assunto_id, flashcards FROM mentor.aula WHERE id=$1", aula_id))
    if not a or not a["flashcards"]:
        return 0
    fc = a["flashcards"]
    if isinstance(fc, str):
        try:
            fc = json.loads(fc)
        except Exception:
            return 0
    cartas = []
    decks = fc if isinstance(fc, list) else (fc.get("decks") or fc.get("cards") or [])
    for d in decks:
        if isinstance(d, dict) and isinstance(d.get("cards") or d.get("flashcards"), list):
            cartas += d.get("cards") or d.get("flashcards")
        elif isinstance(d, dict):
            cartas.append(d)
    n = 0
    hoje = _hoje()
    for c in cartas[:15]:
        if not isinstance(c, dict):
            continue
        fr = c.get("front") or c.get("question") or c.get("frente") or c.get("pergunta") or c.get("term")
        vs = c.get("back") or c.get("answer") or c.get("verso") or c.get("resposta") or c.get("definition")
        if not fr or not vs:
            continue
        await conn.execute("INSERT INTO mentor.card (assunto_id, aula_id, tipo, frente, verso, fonte, proxima) VALUES ($1,$2,'flashcard_gran',$3,$4,'flashcard do Gran',$5)",
                           a["assunto_id"], aula_id, _curto(fr, 500), _curto(vs, 900), hoje + timedelta(days=1))
        n += 1
    return n


async def _questoes_da_aula(conn, aula_id, n, contexto, ineditas=True, so_me=False, excluir=()):
    """
    Questões ligadas à aula. Regras: etiqueta exata antes de etiqueta-pai; nunca duas com o mesmo texto;
    nunca um texto já respondido (mesmo com id diferente); a palavra-chave do título desempata
    (uma aula de Writer prefere questão que fala em Writer).
    """
    excl = list(excluir)
    await _garantir_feedback(conn)
    titulo = await conn.fetchval("SELECT titulo FROM mentor.aula WHERE id=$1", aula_id)
    kw = _palavra_chave(titulo)
    sql = f"""
        SELECT DISTINCT ON (q.enunciado_hash) q.enunciado_hash, q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo,
               q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.via, qa.forca,
               (CASE WHEN qa.melhor_mat THEN 0 WHEN qa.melhor THEN 1 ELSE 2 END) AS o_melhor,
               (CASE WHEN qa.via = 'etiqueta' THEN 0 ELSE 1 END) AS o_via,
               (CASE WHEN $4::text IS NOT NULL AND q.enunciado ILIKE '%' || $4 || '%' THEN 0 ELSE 1 END) AS o_kw,
               (CASE WHEN q.banca_sigla = ANY($3::text[]) THEN 0 ELSE 1 END) AS o_banca,
               (CASE WHEN q.soldado_ba THEN 0 ELSE 1 END) AS o_sold, random() AS rnd
        FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id = qa.questao_id
        WHERE qa.aula_id = $1 AND {SQL_BASE_Q}
          {"AND q.tipo = 'ME'" if so_me else ""}
          AND NOT (q.id = ANY($2::bigint[]))
          AND NOT EXISTS (SELECT 1 FROM mentor.vinculo_feedback f WHERE f.questao_id = q.id AND f.aula_id = $1 AND NOT f.ok)
          {{NIVEL}}
          {"AND NOT EXISTS (SELECT 1 FROM mentor.resposta r JOIN mentor.questao q2 ON q2.id = r.questao_id WHERE q2.enunciado_hash = q.enunciado_hash" + ("" if ineditas else " AND r.data > now() - interval '30 days'") + ")"}
        ORDER BY q.enunciado_hash, o_melhor, o_via, o_kw, o_banca, o_sold, q.ano DESC NULLS LAST
    """
    # nível: fora a questão que tem etiqueta de uma aula POSTERIOR do mesmo assunto ainda não concluída e que esta aula não tem
    # 1) pelo texto (mentor_afinar.py): a questão pertence à aula que mais a explica; se essa aula é uma parte
    #    posterior ainda não concluída, espera. 2) sem afinação, pela etiqueta exclusiva de aula posterior.
    NIVEL = """AND NOT EXISTS (SELECT 1 FROM mentor.questao_aula qb JOIN mentor.aula a2 ON a2.id = qb.aula_id JOIN mentor.aula a1 ON a1.id = $1
                          WHERE qb.questao_id = q.id AND qb.melhor AND a2.assunto_id = a1.assunto_id AND a2.ordem > a1.ordem AND a2.estado <> 'concluida')
               AND NOT EXISTS (SELECT 1 FROM mentor.questao_aula qc JOIN mentor.aula ac ON ac.id = qc.aula_id JOIN mentor.aula a1 ON a1.id = $1
                          WHERE qc.questao_id = q.id AND qc.melhor_mat AND ac.assunto_id <> a1.assunto_id)
               AND (EXISTS (SELECT 1 FROM mentor.questao_aula qb JOIN mentor.aula a1 ON a1.id = $1 JOIN mentor.aula ab ON ab.id = qb.aula_id
                            WHERE qb.questao_id = q.id AND qb.melhor AND ab.assunto_id = a1.assunto_id)
                    OR NOT EXISTS (SELECT 1 FROM mentor.aula a2 JOIN mentor.aula a1 ON a1.id = $1
                          WHERE a2.id <> a1.id AND a2.assunto_id = a1.assunto_id AND a2.ordem > a1.ordem AND a2.estado <> 'concluida'
                            AND EXISTS (SELECT 1 FROM unnest(q.etiquetas) e WHERE e = ANY(a2.etiquetas) AND NOT (e = ANY(a1.etiquetas)))))"""
    rows = await conn.fetch(sql.replace("{NIVEL}", NIVEL), aula_id, excl, list(BANCAS_PREF), kw)
    if len(rows) < n:   # estoque curto no nível: completa sem a regra
        vistos = {r["id"] for r in rows}
        rows = list(rows) + [r for r in await conn.fetch(sql.replace("{NIVEL}", ""), aula_id, excl, list(BANCAS_PREF), kw) if r["id"] not in vistos]
    lst = _rs(rows)
    lst.sort(key=lambda q: (q["o_melhor"], q["o_via"], q["o_kw"], q["o_banca"], q["o_sold"], -(q["ano"] or 0), q["rnd"]))
    # exclui textos já escolhidos nesta rodada (excluir pode trazer ids de textos iguais)
    if excl:
        hashes_excl = {r["h"] for r in await conn.fetch("SELECT enunciado_hash AS h FROM mentor.questao WHERE id = ANY($1::bigint[])", excl)}
        lst = [q for q in lst if q.get("enunciado_hash") not in hashes_excl]
    out = lst[:n]
    for q in out:
        q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
        q["contexto"] = contexto
        q["comentario_professor"] = _comentario(q.get("comentario_professor"))
        for k in ("o_melhor", "o_via", "o_kw", "o_banca", "o_sold", "rnd", "enunciado_hash"):
            q.pop(k, None)
    return out


async def _questoes_do_assunto(conn, assunto_id, n, ineditas=True):
    rows = await conn.fetch(f"""
        SELECT DISTINCT ON (q.id) q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano,
               q.orgao_sigla, q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.aula_id
        FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id = qa.questao_id
        WHERE qa.assunto_id = $1 AND {SQL_BASE_Q}
          {"AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id = q.id)" if ineditas else ""}
    """, assunto_id)
    lst = _rs(rows)
    random.shuffle(lst)
    lst.sort(key=lambda q: (0 if q["banca_sigla"] in BANCAS_PREF else 1, 0 if q["soldado_ba"] else 1))
    out = lst[:n]
    if len(out) < n and ineditas:
        out += await _questoes_do_assunto(conn, assunto_id, n - len(out), ineditas=False)
    vistos, dedup = set(), []
    for q in out:
        h = (q.get("enunciado") or "")[:300].lower()
        if h in vistos:
            continue
        vistos.add(h)
        q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
        q["comentario_professor"] = _comentario(q.get("comentario_professor"))
        dedup.append(q)
    return dedup[:n]


# ----------------------------------------------------------------- painel
@router.get("/painel")
async def painel():
    hoje = _hoje()
    async with db() as conn:
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        semana = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE status='aberta' ORDER BY inicio DESC LIMIT 1"))
        plano = []
        if semana:
            plano = _rs(await conn.fetch("""
                SELECT pi.id, pi.ordem, pi.previsto_h, pi.feito, s.id AS assunto_id, s.nome, s.tier, s.modo, s.estado, m.nome AS materia,
                       (SELECT count(*) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo IN ('conteudo','exercicios','propria')) AS n_aulas,
                       (SELECT count(*) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.estado='concluida') AS n_concluidas
                FROM mentor.plano_item pi JOIN mentor.assunto s ON s.id=pi.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                WHERE pi.semana_id=$1 ORDER BY pi.ordem""", semana["id"]))
        revisoes = _rs(await conn.fetch("""
            SELECT r.id, r.tipo, r.prevista, s.nome, m.nome AS materia, (r.prevista < $1) AS atrasada
            FROM mentor.revisao r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
            WHERE r.feita IS NULL AND COALESCE(r.adiada_para, r.prevista) <= $1 ORDER BY r.prevista""", hoje))
        cards = await conn.fetchval("SELECT count(*) FROM mentor.card WHERE ativo AND proxima <= $1", hoje)
        # próxima ação: revisão atrasada > R1 de ontem > aula pendente do plano
        proxima = None
        if revisoes:
            proxima = {"tipo": "revisao", "revisao_id": revisoes[0]["id"], "titulo": f"{revisoes[0]['tipo']} · {revisoes[0]['nome']}", "materia": revisoes[0]["materia"]}
        else:
            for it in plano:
                if not it["feito"]:
                    aula = _r(await conn.fetchrow("""SELECT id, titulo, duracao_s, estado FROM mentor.aula WHERE assunto_id=$1 AND estado <> 'concluida'
                                                     AND tipo IN ('conteudo','exercicios','propria') ORDER BY ordem LIMIT 1""", it["assunto_id"]))
                    if aula:
                        proxima = {"tipo": "aula", "aula_id": aula["id"], "assunto_id": it["assunto_id"], "titulo": aula["titulo"], "materia": it["materia"], "duracao_s": aula["duracao_s"], "modo": it["modo"]}
                        break
        # KPIs
        horas_hoje = await conn.fetchval("SELECT COALESCE(sum(minutos),0)/60.0 FROM mentor.sessao WHERE fim IS NOT NULL AND (inicio - interval '4 hours')::date = $1", hoje)
        horas_semana = semana["horas_feitas"] if semana else 0
        dias30 = await conn.fetchval("SELECT count(DISTINCT (inicio - interval '4 hours')::date) FROM mentor.sessao WHERE inicio > now() - interval '30 days' AND minutos > 0")
        respondidas = await conn.fetchval("SELECT count(*) FROM mentor.resposta WHERE contexto <> 'aquecimento'")
        acerto = await conn.fetchval("SELECT round(100.0*avg(CASE WHEN correta THEN 1 ELSE 0 END),1) FROM mentor.resposta WHERE contexto IN ('gate','R1','R7','R30','M','questoes','simulado')")
        por_dia = _rs(await conn.fetch("""SELECT (inicio - interval '4 hours')::date AS dia, round(sum(minutos)/60.0,2) AS horas
                                          FROM mentor.sessao WHERE fim IS NOT NULL AND inicio > now() - interval '35 days' GROUP BY 1 ORDER BY 1"""))
        por_materia = _rs(await conn.fetch("""SELECT m.nome AS materia, count(*) AS n, round(100.0*avg(CASE WHEN r.correta THEN 1 ELSE 0 END),0) AS acerto
                                              FROM mentor.resposta r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                                              WHERE r.contexto <> 'aquecimento' GROUP BY 1 ORDER BY 1"""))
        meta_dia = _horas_dia(hoje, await _config(conn, "horas_dia", {}))
    return {"hoje": hoje.isoformat(), "dia": _dia_conta(hoje), "meta_dia_h": meta_dia, "horas_hoje": float(horas_hoje or 0),
            "semana": semana, "plano": plano, "revisoes_pendentes": revisoes, "cards_pendentes": cards, "proxima": proxima,
            "kpi": {"horas_semana": float(horas_semana or 0), "meta_semana": float(semana["meta_horas"]) if semana else None,
                    "dias_30": dias30, "questoes_respondidas": respondidas, "acerto_geral": float(acerto) if acerto is not None else None},
            "horas_por_dia": por_dia, "acerto_por_materia": por_materia, "progresso": prog}


# ----------------------------------------------------------------- edital
@router.get("/materias")
async def materias():
    async with db() as conn:
        rows = await conn.fetch("""
            SELECT m.id, m.nome, m.peso_prova,
                   count(DISTINCT s.id) AS assuntos,
                   count(DISTINCT s.id) FILTER (WHERE s.estado IN ('estudado','dominado')) AS estudados,
                   count(DISTINCT a.id) AS aulas,
                   count(DISTINCT a.id) FILTER (WHERE a.estado='concluida') AS aulas_concluidas,
                   COALESCE(sum(a.duracao_s) FILTER (WHERE s.tier IN ('S','A')),0)/3600.0 AS horas_sa
            FROM mentor.materia m LEFT JOIN mentor.assunto s ON s.materia_id=m.id LEFT JOIN mentor.aula a ON a.assunto_id=s.id
            GROUP BY m.id ORDER BY m.id""")
    return _rs(rows)


@router.get("/edital")
async def edital(materia_id: int = Query(...)):
    async with db() as conn:
        mat = _r(await conn.fetchrow("SELECT * FROM mentor.materia WHERE id=$1", materia_id))
        if not mat:
            raise HTTPException(404, "matéria não encontrada")
        mods = _rs(await conn.fetch("SELECT * FROM mentor.modulo WHERE materia_id=$1 ORDER BY ordem", materia_id))
        ass = _rs(await conn.fetch("""
            SELECT s.*, (SELECT count(DISTINCT qa.questao_id) FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id
                         WHERE qa.assunto_id=s.id AND {base}) AS estoque_usavel,
                   (SELECT count(*) FROM mentor.resposta r WHERE r.assunto_id=s.id AND r.contexto <> 'aquecimento') AS respondidas,
                   (SELECT round(100.0*avg(CASE WHEN r.correta THEN 1 ELSE 0 END),0) FROM mentor.resposta r WHERE r.assunto_id=s.id AND r.contexto IN ('gate','R1','R7','R30','M')) AS acerto
            FROM mentor.assunto s WHERE s.materia_id=$1 ORDER BY s.modulo_id, s.ordem""".format(base=SQL_BASE_Q.replace("q.", "q.")), materia_id))
        aulas = _rs(await conn.fetch("""SELECT id, assunto_id, ordem, tipo, titulo, duracao_s, estado, codigo_aula, video_id
                                        FROM mentor.aula WHERE assunto_id = ANY($1::bigint[]) ORDER BY assunto_id, ordem""", [a["id"] for a in ass]))
    por_ass = {}
    for a in aulas:
        por_ass.setdefault(a["assunto_id"], []).append(a)
    for a in ass:
        a["aulas"] = por_ass.get(a["id"], [])
        a.pop("etiquetas", None)
    for m in mods:
        m["assuntos"] = [a for a in ass if a["modulo_id"] == m["id"]]
    mat.pop("etiquetas_raiz", None)
    mat["modulos"] = mods
    return mat


# ----------------------------------------------------------------- semanas e planejador
def _segunda(d: date):
    return d - timedelta(days=d.weekday())


async def _capacidade_semana(conn, seg: date):
    cfg = await _config(conn, "horas_dia", {})
    total = 0.0
    for i in range(7):
        d = seg + timedelta(days=i)
        tipo = _dia_conta(d)
        if tipo == "conteudo":
            total += _horas_dia(d, cfg)
        elif tipo == "revisao":
            total += 3.0
    return round(total, 1)


def _custo(assunto):
    v = (assunto.get("duracao_total") or 0) / 3600.0
    n = assunto.get("n_aulas") or 0
    t = assunto.get("tier")
    if t == "C":
        return round(n * 0.15 + 0.1, 2)
    if t == "treino":
        return round(n * 0.3, 2)
    if assunto.get("origem") == "propria":
        return 0.75
    return round(v / 1.5 + n * 0.25, 2)


@router.get("/semanas")
async def semanas():
    async with db() as conn:
        rows = await conn.fetch("""SELECT s.*, (SELECT count(*) FROM mentor.plano_item p WHERE p.semana_id=s.id) AS itens,
                                          (SELECT count(*) FROM mentor.plano_item p WHERE p.semana_id=s.id AND p.feito) AS feitos
                                   FROM mentor.semana s ORDER BY inicio""")
    return _rs(rows)


@router.get("/semana/{semana_id}")
async def semana(semana_id: int):
    async with db() as conn:
        s = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE id=$1", semana_id))
        if not s:
            raise HTTPException(404, "semana não encontrada")
        s["itens"] = _rs(await conn.fetch("""
            SELECT pi.*, a.nome, a.tier, a.modo, a.estado, m.nome AS materia,
                   (SELECT count(*) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.tipo IN ('conteudo','exercicios','propria')) AS n_aulas,
                   (SELECT count(*) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.estado='concluida') AS n_concluidas
            FROM mentor.plano_item pi JOIN mentor.assunto a ON a.id=pi.assunto_id JOIN mentor.materia m ON m.id=a.materia_id
            WHERE pi.semana_id=$1 ORDER BY pi.ordem""", semana_id))
        s["revisoes"] = _rs(await conn.fetch("""SELECT r.id, r.tipo, r.prevista, r.feita, r.acerto, a.nome, m.nome AS materia
                                                FROM mentor.revisao r JOIN mentor.assunto a ON a.id=r.assunto_id JOIN mentor.materia m ON m.id=a.materia_id
                                                WHERE COALESCE(r.adiada_para, r.prevista) BETWEEN $1 AND $2 ORDER BY r.prevista""", s["inicio"], s["fim"]))
    return s


async def _trilhas(conn):
    t = await _config(conn, "trilhas", None) or TRILHAS_PADRAO
    p = await _config(conn, "pesos_trilha", None) or PESOS_TRILHA_PADRAO
    return {k: [int(x) for x in v] for k, v in t.items()}, {k: float(v) for k, v in p.items()}


def _custo_aula(a, modo):
    """Horas de estudo de uma aula: base = vídeo em 1,5x + 25 min (aquecimento, bolso, fixação, gate, caderno); sem vídeo = 25 min."""
    if modo == "base":
        return round(((a.get("duracao_s") or 0) / 60 / 1.5 + 25) / 60, 3)
    if modo == "fora":
        return 0.25
    return round(25 / 60, 3)


async def _universo_planejador(conn):
    """Tudo que o planejador precisa, numa passada: matérias (fase/trilha) e aulas pendentes por assunto."""
    await _garantir_base(conn)
    mats = {r["id"]: dict(r) for r in await conn.fetch("SELECT id, nome, fase, trilha, trilha_ordem, regua_base FROM mentor.materia")}
    rows = await conn.fetch("""
        SELECT a.id, a.assunto_id, a.ordem, a.tipo, a.duracao_s, a.base, a.estado, s.materia_id, s.modo, s.tier, s.nome AS assunto, s.ordem AS s_ordem,
               mo.ordem AS mo_ordem, mo.tipo AS mo_tipo
        FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id JOIN mentor.modulo mo ON mo.id=s.modulo_id
        WHERE a.tipo IN ('conteudo','exercicios','propria') AND a.estado <> 'concluida'
        ORDER BY s.materia_id, (mo.tipo='propria'), mo.ordem, s.ordem, a.ordem""")
    aulas = [dict(r) for r in rows]
    return mats, aulas


def _plano_trilhas(mats, aulas, seg, cap_h, trilhas, pesos, feitas=None):
    """
    Monta uma semana: cada trilha recebe cap × peso; dentro da trilha, as matérias em ordem; matéria em fase base entra pelas aulas
    de base (ordem do curso), matéria em modo questão entra pelas aulas de complemento (S → A → B → C, sem vídeo). Aula é a unidade.
    Devolve itens por assunto: {assunto_id, materia_id, modo, aulas:[ids], custo_h}. `feitas` = ids já usados (simulação).
    """
    if feitas is None:
        feitas = set()
    TIER = {"S": 0, "A": 1, "B": 2, "C": 3, "treino": 4}
    pend = [a for a in aulas if a["id"] not in feitas and a["mo_tipo"] != "revisao" and a["modo"] not in ("revisao",)]
    por_mat = {}
    for a in pend:
        por_mat.setdefault(a["materia_id"], []).append(a)

    def fila_da(mid, fase_alvo):
        m = mats.get(mid)
        if not m or m["fase"] != fase_alvo:
            return []
        if mid == ATUALIDADES_ID and seg < ATUALIDADES_DESDE:
            return []
        lst = por_mat.get(mid, [])
        if fase_alvo == "base":
            return [(a, "base") for a in lst if a["base"]]
        out = [(a, a["modo"]) for a in lst if not a["base"] and a["modo"] in MODOS_QUESTAO and a["tier"] != "fora"]
        out.sort(key=lambda x: (TIER.get(x[0]["tier"], 3), x[0]["mo_tipo"] == "propria", x[0]["mo_ordem"], x[0]["s_ordem"], x[0]["ordem"]))
        if seg >= RETA_FINAL:
            out += [(a, "fora") for a in lst if not a["base"] and a["modo"] == "fora"]
        return out

    itens = {}

    def usar(a, modo, custo):
        k = (a["assunto_id"], modo if modo == "base" else "questoes")
        it = itens.setdefault(k, {"assunto_id": a["assunto_id"], "materia_id": a["materia_id"], "modo": k[1], "aulas": [], "aula_modos": [], "custo_h": 0.0})
        it["aulas"].append(a["id"])
        it["aula_modos"].append(modo)
        it["custo_h"] = round(it["custo_h"] + custo, 2)
        feitas.add(a["id"])

    def encher(mids, orc, fase_alvo, limite_aulas=None):
        """Enche as aulas das matérias (na ordem) enquanto couber no orçamento; devolve (orçamento que sobrou, gasto)."""
        g = 0.0
        for mid in mids:
            n = 0
            for a, modo in fila_da(mid, fase_alvo):
                if a["id"] in feitas:
                    continue
                c = _custo_aula(a, modo)
                if c > orc + 0.05:
                    return orc, g
                usar(a, modo, c)
                orc -= c
                g += c
                n += 1
                if limite_aulas and n >= limite_aulas:
                    break
            if orc < 0.2:
                return orc, g
        return orc, g

    gasto = 0.0
    # Atualidades: 1 aula por semana a partir de 02/11, fora das trilhas (resumo do mês + questões)
    if seg >= ATUALIDADES_DESDE:
        _, g = encher([ATUALIDADES_ID], 1.0, "questao", limite_aulas=1)
        gasto += g
    sobra = 0.0
    for t, mids in trilhas.items():
        mids = [m for m in mids if m != ATUALIDADES_ID]
        orc = cap_h * pesos.get(t, 0.25) + sobra
        orc, g = encher(mids, orc, "base")        # 1º a base das matérias da trilha, na ordem
        gasto += g
        orc, g = encher(mids, orc, "questao")     # 2º o complemento das que já fecharam a base
        gasto += g
        sobra = max(0.0, orc)
    # sobra geral: qualquer trilha, base primeiro
    if sobra > 0.3:
        todas = [m for mids in trilhas.values() for m in mids if m != ATUALIDADES_ID]
        sobra, g = encher(todas, sobra, "base")
        gasto += g
        if sobra > 0.3:
            sobra, g = encher(todas, sobra, "questao")
            gasto += g
    return list(itens.values()), round(gasto, 2)


@router.post("/semana/montar")
async def montar_semana(inicio: Optional[str] = Body(None, embed=True), forcar: bool = Body(False, embed=True)):
    """
    O planejador da segunda (v3.8): fecha a semana anterior, abre a nova e escolhe as AULAS pelas trilhas.
    O que sobrou da semana anterior vai na frente. Base primeiro em cada matéria; complemento por questões depois da prova de base.
    """
    hoje = _hoje()
    seg = _segunda(date.fromisoformat(inicio)) if inicio else _segunda(hoje)
    async with db() as conn:
        async with conn.transaction():
            await _garantir_base(conn)
            if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM mentor.aula WHERE base)"):
                raise HTTPException(409, "Nenhuma aula marcada como base: rode mentor_flags.py (edital v3) antes de montar a semana")
            existente = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE inicio=$1", seg))
            if existente and existente["status"] != "bloqueada" and not forcar:
                raise HTTPException(409, f"semana de {seg} já está {existente['status']}; use forcar=true pra refazer")
            await conn.execute("""UPDATE mentor.semana SET status='fechada', fechada_em=now(),
                                  horas_feitas = COALESCE((SELECT sum(minutos)/60.0 FROM mentor.sessao x WHERE x.fim IS NOT NULL
                                                           AND (x.inicio - interval '4 hours')::date BETWEEN semana.inicio AND semana.fim),0)
                                  WHERE status='aberta' AND inicio < $1""", seg)
            cap_total = await _capacidade_semana(conn, seg)
            cap_conteudo = round(cap_total * PCT_CONTEUDO, 1)
            trilhas, pesos = await _trilhas(conn)
            mats, aulas = await _universo_planejador(conn)
            # pendências: aulas planejadas em semanas fechadas e não concluídas entram primeiro, no mesmo modo
            pend_ids = [r["aid"] for r in await conn.fetch("""SELECT DISTINCT unnest(p.aulas) AS aid FROM mentor.plano_item p JOIN mentor.semana s ON s.id=p.semana_id
                                                              WHERE s.inicio < $1 AND NOT p.feito AND s.status='fechada' AND p.aulas IS NOT NULL""", seg)]
            pend_set = set(pend_ids)
            itens, gasto, feitas = [], 0.0, set()
            por_id = {a["id"]: a for a in aulas}
            for aid in pend_ids:
                a = por_id.get(aid)
                if not a:
                    continue
                modo = "base" if a["base"] else (a["modo"] if a["modo"] in MODOS_QUESTAO else "fora")
                c = _custo_aula(a, modo)
                k = next((it for it in itens if it["assunto_id"] == a["assunto_id"] and it["modo"] == ("base" if modo == "base" else "questoes")), None)
                if not k:
                    k = {"assunto_id": a["assunto_id"], "materia_id": a["materia_id"], "modo": "base" if modo == "base" else "questoes", "aulas": [], "aula_modos": [], "custo_h": 0.0}
                    itens.append(k)
                k["aulas"].append(aid)
                k["aula_modos"].append(modo)
                k["custo_h"] = round(k["custo_h"] + c, 2)
                feitas.add(aid)
                gasto += c
            novos, g2 = _plano_trilhas(mats, aulas, seg, max(0.0, cap_conteudo - gasto), trilhas, pesos, feitas)
            itens += novos
            gasto += g2
            fim = seg + timedelta(days=6)
            fase = _fase_semana(seg)
            if existente:
                sid = existente["id"]
                await conn.execute("UPDATE mentor.semana SET meta_horas=$2, status='aberta', fase=$3, montada_em=now() WHERE id=$1", sid, cap_total, fase)
                await conn.execute("DELETE FROM mentor.plano_item WHERE semana_id=$1", sid)
            else:
                sid = await conn.fetchval("INSERT INTO mentor.semana (inicio, fim, meta_horas, status, fase, montada_em) VALUES ($1,$2,$3,'aberta',$4,now()) RETURNING id",
                                          seg, fim, cap_total, fase)
            for i, it in enumerate(itens, 1):
                await conn.execute("""INSERT INTO mentor.plano_item (semana_id, assunto_id, ordem, previsto_h, modo, aulas) VALUES ($1,$2,$3,$4,$5,$6)
                                      ON CONFLICT (semana_id, assunto_id) DO UPDATE SET aulas = mentor.plano_item.aulas || EXCLUDED.aulas, previsto_h = mentor.plano_item.previsto_h + EXCLUDED.previsto_h""",
                                   sid, it["assunto_id"], i, it["custo_h"], it["modo"], it["aulas"])
            prox = seg + timedelta(days=7)
            await conn.execute("INSERT INTO mentor.semana (inicio, fim, meta_horas, status) VALUES ($1,$2,$3,'bloqueada') ON CONFLICT (inicio) DO NOTHING",
                               prox, prox + timedelta(days=6), await _capacidade_semana(conn, prox))
    return {"semana_id": sid, "inicio": seg.isoformat(), "meta_horas": cap_total, "conteudo_h": cap_conteudo, "fase": fase,
            "itens": len(itens), "aulas": sum(len(it["aulas"]) for it in itens), "horas_previstas": round(gasto, 1), "pendencias_da_anterior": len(pend_set)}


# ----------------------------------------------------------------- sessão
@router.post("/sessao/iniciar")
async def iniciar_sessao(tipo: str = Body("estudo", embed=True)):
    if tipo not in ("estudo", "revisao", "questoes", "simulado", "cards", "semana"):
        raise HTTPException(400, "tipo inválido")
    async with db() as conn:
        aberta = await conn.fetchval("SELECT id FROM mentor.sessao WHERE fim IS NULL ORDER BY inicio DESC LIMIT 1")
        if aberta:
            await conn.execute("UPDATE mentor.sessao SET fim=now(), minutos=LEAST(EXTRACT(EPOCH FROM now()-inicio)/60, 240)::int WHERE id=$1", aberta)
        sid = await conn.fetchval("INSERT INTO mentor.sessao (tipo) VALUES ($1) RETURNING id", tipo)
    return {"sessao_id": sid}


@router.post("/sessao/{sessao_id}/fechar")
async def fechar_sessao(sessao_id: int):
    hoje = _hoje()
    async with db() as conn:
        s = _r(await conn.fetchrow("SELECT * FROM mentor.sessao WHERE id=$1", sessao_id))
        if not s:
            raise HTTPException(404, "sessão não encontrada")
        if s["fim"] is None:
            await conn.execute("UPDATE mentor.sessao SET fim=now(), minutos=LEAST(EXTRACT(EPOCH FROM now()-inicio)/60, 240)::int WHERE id=$1", sessao_id)
        resumo = _r(await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE contexto NOT IN ('aquecimento','fixacao')) AS questoes,
                   count(*) FILTER (WHERE correta AND contexto NOT IN ('aquecimento','fixacao')) AS certas,
                   count(DISTINCT aula_id) FILTER (WHERE contexto='gate') AS aulas
            FROM mentor.resposta WHERE sessao_id=$1""", sessao_id))
        xp_sessao = await conn.fetchval("SELECT COALESCE(sum(pontos),0) FROM mentor.xp_evento WHERE (ref->>'sessao_id')::bigint = $1", sessao_id)
        # meta do dia e sequência
        horas_hoje = await conn.fetchval("SELECT COALESCE(sum(minutos),0)/60.0 FROM mentor.sessao WHERE fim IS NOT NULL AND (inicio - interval '4 hours')::date = $1", hoje)
        meta = _horas_dia(hoje, await _config(conn, "horas_dia", {}))
        if _dia_conta(hoje) == "revisao":
            meta = 3.0
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        meta_batida = False
        if float(horas_hoje) >= meta * 0.9 and prog["ultimo_dia_meta"] != hoje:
            meta_batida = True
            ontem = hoje - timedelta(days=1)
            if prog["ultimo_dia_meta"] == ontem or (prog["ultimo_dia_meta"] and (hoje - prog["ultimo_dia_meta"]).days == 2 and prog["congelamentos"] > 0):
                streak = prog["streak_dias"] + 1
                congel = prog["congelamentos"] - (1 if (hoje - prog["ultimo_dia_meta"]).days == 2 else 0)
            else:
                streak, congel = 1, prog["congelamentos"]
            await conn.execute("UPDATE mentor.progresso SET streak_dias=$1, ultimo_dia_meta=$2, congelamentos=$3 WHERE id=1", streak, hoje, congel)
            await _add_xp(conn, "meta_dia", XP["meta_dia"], {"dia": hoje.isoformat(), "sessao_id": sessao_id})
            if hoje.weekday() == 0:
                await conn.execute("UPDATE mentor.progresso SET congelamentos = LEAST(congelamentos + 1, 2) WHERE id=1")
        await conn.execute("UPDATE mentor.sessao SET resumo=$2, xp=$3 WHERE id=$1", sessao_id, json.dumps({**resumo, "meta_batida": meta_batida}), int(xp_sessao))
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        s = _r(await conn.fetchrow("SELECT * FROM mentor.sessao WHERE id=$1", sessao_id))
    return {"sessao": s, "resumo": resumo, "xp_sessao": int(xp_sessao), "horas_hoje": float(horas_hoje), "meta_dia": meta, "meta_batida": meta_batida, "progresso": prog}


# ----------------------------------------------------------------- aula
@router.get("/aula/{aula_id}")
async def aula(aula_id: int, so_conteudo: bool = False):
    async with db() as conn:
        a = _r(await conn.fetchrow("""SELECT a.*, s.nome AS assunto, s.tier, s.modo, s.materia_id, m.nome AS materia
                                      FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id JOIN mentor.materia m ON m.id=s.materia_id WHERE a.id=$1""", aula_id))
        if not a:
            raise HTTPException(404, "aula não encontrada")
        a.pop("transcricao", None)
        for k in ("flashcards", "questoes_fixacao"):
            if isinstance(a.get(k), str):
                try:
                    a[k] = json.loads(a[k])
                except Exception:
                    pass
        a["bizus"] = _rs(await conn.fetch("SELECT id, secao, texto, fonte FROM mentor.bizu WHERE aula_id=$1 AND aprovado ORDER BY secao, id LIMIT 12", aula_id))
        a["estoque"] = await conn.fetchval(f"SELECT count(*) FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id WHERE qa.aula_id=$1 AND {SQL_BASE_Q}", aula_id)
        a["ineditas"] = await conn.fetchval(f"""SELECT count(*) FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id WHERE qa.aula_id=$1 AND {SQL_BASE_Q}
                                             AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id=q.id)""", aula_id)
        if not so_conteudo:
            a["aquecimento"] = await _questoes_da_aula(conn, aula_id, AQUEC_N, "aquecimento")
            fix = a.get("questoes_fixacao") or []
            a["fixacao"] = fix[:FIX_N] if isinstance(fix, list) else []
            a["gate"] = await _questoes_da_aula(conn, aula_id, GATE_N, "gate", excluir=[q["id"] for q in a["aquecimento"]])
        # a aula em andamento marca o assunto
        await conn.execute("UPDATE mentor.assunto SET estado='em_andamento' WHERE id=$1 AND estado='nao_estudado'", a["assunto_id"])
    return a


@router.post("/aula/{aula_id}/assistida")
async def aula_assistida(aula_id: int):
    async with db() as conn:
        ok = await conn.fetchval("UPDATE mentor.aula SET estado='assistida', assistida_em=now() WHERE id=$1 AND estado IN ('pendente','ressalva','pulada') RETURNING id", aula_id)
    return {"aula_id": aula_id, "assistida": bool(ok)}


@router.post("/resposta")
async def responder(questao_id: int = Body(...), contexto: str = Body(...), marcada: Optional[str] = Body(None),
                    aula_id: Optional[int] = Body(None), assunto_id: Optional[int] = Body(None), sessao_id: Optional[int] = Body(None),
                    confianca: Optional[str] = Body(None), tipo_erro: Optional[str] = Body(None), tempo_s: Optional[int] = Body(None),
                    revisao_id: Optional[int] = Body(None)):
    if contexto not in ("aquecimento", "fixacao", "gate", "reestudo", "R1", "R3", "R7", "R15", "R30", "M", "semana", "questoes", "simulado", "bolso", "R2"):
        raise HTTPException(400, "contexto inválido")
    async with db() as conn:
        q = _r(await conn.fetchrow("SELECT id, gabarito, comentario_professor, alternativas, tipo FROM mentor.questao WHERE id=$1", questao_id))
        if not q:
            raise HTTPException(404, "questão não encontrada")
        if assunto_id is None and aula_id is not None:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id)
        correta = (marcada or "").strip().upper() == (q["gabarito"] or "").strip().upper() if marcada else None
        rid = await conn.fetchval("""INSERT INTO mentor.resposta (questao_id, sessao_id, aula_id, assunto_id, contexto, marcada, correta, confianca, tipo_erro, tempo_s)
                                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                                  questao_id, sessao_id, aula_id, assunto_id, contexto, marcada, correta, confianca, tipo_erro, tempo_s)
        # combo de certeza: 3 certezas certas seguidas = XP (v3.2 dopaminérgico)
        combo = None
        if correta and confianca == "certeza" and contexto in ("gate", "R1", "R7", "R30", "M", "questoes"):
            ult = await conn.fetch("SELECT correta, confianca FROM mentor.resposta WHERE sessao_id=$1 AND contexto NOT IN ('aquecimento','fixacao') ORDER BY id DESC LIMIT 3", sessao_id)
            if len(ult) == 3 and all(r["correta"] and r["confianca"] == "certeza" for r in ult):
                await _add_xp(conn, "combo", XP["combo"], {"sessao_id": sessao_id, "resposta_id": rid})
                combo = XP["combo"]
        # caderno de erros: acertar de novo (sem chute) risca a linha; errar de novo faz a linha voltar
        cad = None
        if correta is not None and contexto not in ("aquecimento", "simulado"):
            await _garantir_caderno(conn)
            if correta and confianca != "chute":
                n = await conn.fetchval("WITH u AS (UPDATE mentor.caderno SET riscada_em=now() WHERE questao_id=$1 AND riscada_em IS NULL AND criado_em < now() - interval '2 hours' RETURNING id) SELECT count(*) FROM u", questao_id)
                cad = {"riscadas": int(n or 0)}
            elif not correta or confianca == "chute":
                n = await conn.fetchval("WITH u AS (UPDATE mentor.caderno SET voltou=voltou+1, riscada_em=NULL WHERE questao_id=$1 AND criado_em < now() - interval '2 hours' RETURNING id) SELECT count(*) FROM u", questao_id)
                cad = {"voltaram": int(n or 0)}
            linhas = _rs(await conn.fetch("SELECT id, tipo, linha, riscada_em, voltou FROM mentor.caderno WHERE questao_id=$1 ORDER BY id", questao_id))
            if cad is not None:
                cad["linhas"] = linhas
    return {"resposta_id": rid, "correta": correta, "gabarito": q["gabarito"], "comentario_professor": _comentario(q["comentario_professor"]), "combo_xp": combo, "caderno": cad}


async def _agendar_revisao(conn, assunto_id, tipo, dias, hoje=None):
    hoje = hoje or _hoje()
    prevista = hoje + timedelta(days=dias)
    await conn.execute("DELETE FROM mentor.revisao WHERE assunto_id=$1 AND feita IS NULL", assunto_id)
    await conn.execute("INSERT INTO mentor.revisao (assunto_id, tipo, prevista) VALUES ($1,$2,$3)", assunto_id, tipo, prevista)
    return prevista


@router.post("/aula/{aula_id}/gate")
async def fechar_gate(aula_id: int, sessao_id: Optional[int] = Body(None, embed=True)):
    """Lê as últimas 5 respostas de gate desta aula. 4+ = concluída. Se todas as aulas do assunto fecharam, R1 amanhã."""
    async with db() as conn:
        async with conn.transaction():
            a = _r(await conn.fetchrow("SELECT a.*, s.tier FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE a.id=$1", aula_id))
            if not a:
                raise HTTPException(404, "aula não encontrada")
            if a["estado"] == "concluida":
                return {"aula_id": aula_id, "fechou": True, "ja_concluida": True}
            ult = await conn.fetch("SELECT correta FROM mentor.resposta WHERE aula_id=$1 AND contexto='gate' ORDER BY id DESC LIMIT $2", aula_id, GATE_N)
            certas = sum(1 for r in ult if r["correta"])
            n = len(ult)
            if n < GATE_N:
                return {"aula_id": aula_id, "fechou": False, "certas": certas, "n": n, "faltam": GATE_N - n}
            total_gate = await conn.fetchval("SELECT count(*) FROM mentor.resposta WHERE aula_id=$1 AND contexto='gate'", aula_id)
            reestudo_antes = max(0, (total_gate - 1) // GATE_N)   # 0 na 1ª rodada, 1 na 2ª...
            if certas >= GATE_MIN:
                await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE id=$1", aula_id)
                await _semear_cards_aula(conn, aula_id)
                xp = await _add_xp(conn, "gate", XP["gate"] if not reestudo_antes else XP["gate_reestudo"], {"aula_id": aula_id, "sessao_id": sessao_id})
                await conn.execute("UPDATE mentor.plano_item pi SET feito=TRUE FROM mentor.semana s WHERE pi.semana_id=s.id AND s.status='aberta' AND pi.assunto_id=$1 AND NOT EXISTS (SELECT 1 FROM mentor.aula x WHERE x.assunto_id=$1 AND x.tipo IN ('conteudo','exercicios','propria') AND x.estado <> 'concluida')", a["assunto_id"])
                faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula WHERE assunto_id=$1 AND tipo IN ('conteudo','exercicios','propria') AND estado <> 'concluida'", a["assunto_id"])
                r1 = None
                if faltam == 0:
                    await conn.execute("UPDATE mentor.assunto SET estado='estudado', estudado_em=now() WHERE id=$1", a["assunto_id"])
                    r1 = await _agendar_revisao(conn, a["assunto_id"], "R1", INTERVALO["R1"])
                base_fechada = await _base_fechou(conn, aula_id)
                return {"aula_id": aula_id, "fechou": True, "certas": certas, "n": n, "xp": xp, "assunto_fechou": faltam == 0, "r1_em": r1.isoformat() if r1 else None, "base_fechada": base_fechada}
            # reprovou: reestudo + 5 novas (não repete as do gate)
            await conn.execute("UPDATE mentor.aula SET estado='ressalva' WHERE id=$1", aula_id)
            vistas = [r["questao_id"] for r in await conn.fetch("SELECT questao_id FROM mentor.resposta WHERE aula_id=$1", aula_id)]
            novas = await _questoes_da_aula(conn, aula_id, GATE_N, "gate", excluir=vistas)
            if reestudo_antes >= 1:
                # reincidente: fica estudado com ressalva, R1 com gate próprio (spec §3.3.2)
                await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE id=$1", aula_id)
                await _semear_cards_aula(conn, aula_id)
                faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula WHERE assunto_id=$1 AND tipo IN ('conteudo','exercicios','propria') AND estado <> 'concluida'", a["assunto_id"])
                if faltam == 0:
                    await conn.execute("UPDATE mentor.assunto SET estado='ressalva', estudado_em=now() WHERE id=$1", a["assunto_id"])
                    await _agendar_revisao(conn, a["assunto_id"], "R1", INTERVALO["R1"])
                return {"aula_id": aula_id, "fechou": True, "com_ressalva": True, "certas": certas, "n": n, "assunto_fechou": faltam == 0}
            # marca o reestudo (conta quantas vezes a aula reprovou) sem inventar resposta
            await conn.execute("UPDATE mentor.aula SET estado='ressalva' WHERE id=$1", aula_id)
            return {"aula_id": aula_id, "fechou": False, "certas": certas, "n": n, "reestudo": True, "resumo_bolso": a["resumo_bolso"], "novas": novas}


# ----------------------------------------------------------------- revisões (curva do esquecimento)
async def _base_fechou(conn, aula_id):
    """Depois de concluir uma aula de base: se não sobrou aula de base pendente na matéria, devolve o nome dela (prova de base liberada)."""
    await _garantir_base(conn)
    m = _r(await conn.fetchrow("""SELECT m.id, m.nome, m.fase, a.base FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id JOIN mentor.materia m ON m.id=s.materia_id WHERE a.id=$1""", aula_id))
    if not m or not m["base"] or m["fase"] != "base":
        return None
    faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=$1 AND a.base AND a.estado <> 'concluida'", m["id"])
    return m["nome"] if faltam == 0 else None


def _proxima_revisao(tipo, acerto):
    """Intervalo adaptativo (v3.2 §3). Devolve (tipo_seguinte, dias) ou ('reestudo', 0)."""
    if tipo == "R1":
        if acerto >= 75: return "R7", INTERVALO["R7"]
        if acerto >= 50: return "R3", INTERVALO["R3"]
        return "R2", INTERVALO["R2"]
    if tipo in ("R2", "R3"):
        return ("R7", INTERVALO["R7"]) if acerto >= 60 else ("R3", INTERVALO["R3"])
    if tipo == "R7":
        if acerto >= 80: return "R30", INTERVALO["R30"]
        if acerto >= 60: return "R15", INTERVALO["R15"]
        return "R3", INTERVALO["R3"]
    if tipo == "R15":
        return ("R30", INTERVALO["R30"]) if acerto >= 60 else ("R7", INTERVALO["R7"])
    # R30 e M
    if acerto >= 60: return "M", INTERVALO["M"]
    return "R7", INTERVALO["R7"]


@router.get("/revisoes/hoje")
async def revisoes_hoje(com_questoes: bool = True):
    hoje = _hoje()
    async with db() as conn:
        revs = _rs(await conn.fetch("""
            SELECT r.id, r.tipo, r.prevista, r.adiada_para, r.n_adiamentos, s.id AS assunto_id, s.nome, s.tier, m.nome AS materia, m.id AS materia_id,
                   (r.prevista < $1) AS atrasada
            FROM mentor.revisao r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
            WHERE r.feita IS NULL AND COALESCE(r.adiada_para, r.prevista) <= $1
            ORDER BY r.prevista, m.id""", hoje))
        # intercalar: alterna matérias
        por_mat = {}
        for r in revs:
            por_mat.setdefault(r["materia_id"], []).append(r)
        ordenadas = []
        while any(por_mat.values()):
            for mid in list(por_mat):
                if por_mat[mid]:
                    ordenadas.append(por_mat[mid].pop(0))
        if com_questoes:
            for r in ordenadas:
                r["questoes"] = await _questoes_do_assunto(conn, r["assunto_id"], REV_N.get(r["tipo"], 4))
                # erro pendente volta dentro da revisão (v3.2 §3)
                erradas = await conn.fetch(f"""
                    SELECT q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.indice_acerto, q.comentario_professor, q.soldado_ba
                    FROM mentor.questao q WHERE q.id IN (
                        SELECT r1.questao_id FROM mentor.resposta r1 WHERE r1.assunto_id=$1 AND r1.correta = FALSE AND r1.contexto <> 'aquecimento'
                          AND NOT EXISTS (SELECT 1 FROM mentor.resposta r2 WHERE r2.questao_id=r1.questao_id AND r2.correta AND r2.id > r1.id))
                    AND {SQL_BASE_Q} ORDER BY random() LIMIT 2""", r["assunto_id"])
                for e in erradas:
                    e = dict(e)
                    e["alternativas"] = json.loads(e["alternativas"]) if isinstance(e["alternativas"], str) else e["alternativas"]
                    e["repeticao"] = True
                    if e["id"] not in [x["id"] for x in r["questoes"]]:
                        r["questoes"].append(e)
        teto_min = int(_horas_dia(hoje, await _config(conn, "horas_dia", {})) * 60 * 0.35)
    return {"hoje": hoje.isoformat(), "revisoes": ordenadas, "teto_minutos": teto_min}


@router.post("/revisao/{revisao_id}/concluir")
async def concluir_revisao(revisao_id: int, sessao_id: Optional[int] = Body(None, embed=True)):
    hoje = _hoje()
    async with db() as conn:
        async with conn.transaction():
            r = _r(await conn.fetchrow("SELECT * FROM mentor.revisao WHERE id=$1", revisao_id))
            if not r:
                raise HTTPException(404, "revisão não encontrada")
            if r["feita"]:
                raise HTTPException(409, "revisão já feita")
            resp = await conn.fetch("SELECT correta FROM mentor.resposta WHERE assunto_id=$1 AND contexto=$2 AND data > $3 ORDER BY id DESC LIMIT $4",
                                    r["assunto_id"], r["tipo"], datetime.combine(hoje, datetime.min.time()) - timedelta(days=1), REV_N.get(r["tipo"], 4) + 2)
            n = len(resp)
            if n == 0:
                raise HTTPException(409, "nenhuma resposta desta revisão gravada ainda")
            acerto = round(100.0 * sum(1 for x in resp if x["correta"]) / n, 1)
            await conn.execute("UPDATE mentor.revisao SET feita=now(), acerto=$2, n_questoes=$3, sessao_id=$4 WHERE id=$1", revisao_id, acerto, n, sessao_id)
            prox, dias = _proxima_revisao(r["tipo"], acerto)
            prevista = await _agendar_revisao(conn, r["assunto_id"], prox, dias, hoje)
            no_dia = (r["adiada_para"] or r["prevista"]) >= hoje
            xp = await _add_xp(conn, "revisao", XP["revisao"] + (XP["revisao_no_dia"] if no_dia else 0), {"revisao_id": revisao_id, "sessao_id": sessao_id, "acerto": acerto})
            reestudo = acerto < 50 and r["tipo"] in ("R1", "R7")
            if reestudo:
                await conn.execute("UPDATE mentor.assunto SET estado='ressalva' WHERE id=$1", r["assunto_id"])
    return {"revisao_id": revisao_id, "acerto": acerto, "n": n, "proxima": prox, "prevista": prevista.isoformat(), "xp": xp, "reestudo": reestudo}


@router.post("/revisao/{revisao_id}/adiar")
async def adiar_revisao(revisao_id: int, dias: int = Body(1, embed=True)):
    if dias < 1 or dias > 3:
        raise HTTPException(400, "adiar de 1 a 3 dias")
    async with db() as conn:
        ok = await conn.fetchval("UPDATE mentor.revisao SET adiada_para = COALESCE(adiada_para, prevista) + $2, n_adiamentos = n_adiamentos + 1 WHERE id=$1 AND feita IS NULL RETURNING adiada_para", revisao_id, dias)
        if not ok:
            raise HTTPException(404, "revisão não encontrada ou já feita")
    return {"revisao_id": revisao_id, "adiada_para": ok.isoformat()}


# ----------------------------------------------------------------- questões (aba)
@router.get("/questoes")
async def questoes(modo: str = "estudado", materias: Optional[str] = None, assuntos: Optional[str] = None, n: int = 10, so_me: bool = False):
    n = max(5, min(n, 30))
    async with db() as conn:
        if modo == "estudado":
            ids = [r["id"] for r in await conn.fetch("SELECT id FROM mentor.assunto WHERE estado IN ('estudado','ressalva','dominado','em_andamento')")]
        else:
            ids = [int(x) for x in (assuntos or "").split(",") if x.strip().isdigit()]
            if materias and not ids:
                ids = [r["id"] for r in await conn.fetch("SELECT id FROM mentor.assunto WHERE materia_id = ANY($1::int[])", [int(x) for x in materias.split(",") if x.strip().isdigit()])]
        if not ids:
            return {"questoes": [], "aviso": "nenhum assunto selecionado ou estudado ainda"}
        rows = await conn.fetch(f"""
            SELECT DISTINCT ON (q.id) q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla,
                   q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.assunto_id, qa.aula_id
            FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id
            WHERE qa.assunto_id = ANY($1::bigint[]) AND {SQL_BASE_Q} {"AND q.tipo='ME'" if so_me else ""}
              AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id=q.id)
        """, ids)
        lst = _rs(rows)
        random.shuffle(lst)
        # mistura matérias/assuntos: no máximo 3 por assunto
        out, por = [], {}
        teto = max(3, math.ceil(n / max(1, len(ids))))
        for q in lst:
            if por.get(q["assunto_id"], 0) >= teto:
                continue
            por[q["assunto_id"]] = por.get(q["assunto_id"], 0) + 1
            out.append(q)
            if len(out) >= n:
                break
        for q in out:
            q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
    return {"questoes": out, "disponiveis": len(lst)}


# ----------------------------------------------------------------- simulados
@router.get("/simulados")
async def simulados():
    async with db() as conn:
        rows = await conn.fetch("SELECT s.id, s.numero, s.data, s.tipo, s.status, s.certas, s.nota, s.por_materia, s.inicio, s.fim, array_length(s.questoes,1) AS n, p.nome AS prova FROM mentor.simulado s LEFT JOIN mentor.prova_real p ON p.id=s.prova_real_id ORDER BY s.data")
        reais = await conn.fetch("SELECT id, nome, banca, ano, n_questoes, reservada FROM mentor.prova_real WHERE soldado_ba ORDER BY ano")
    return {"simulados": _rs(rows), "provas_reais": _rs(reais)}


@router.post("/simulado/montar")
async def montar_simulado(data: Optional[str] = Body(None, embed=True), prova_real_id: Optional[int] = Body(None, embed=True), n_total: int = Body(80, embed=True)):
    """Montado: 80 ME inéditas, distribuição do edital 2022, FCC de preferência, sem C/E. Real: a prova reservada inteira."""
    d = date.fromisoformat(data) if data else _hoje()
    async with db() as conn:
        async with conn.transaction():
            if prova_real_id:
                ids = [r["id"] for r in await conn.fetch("SELECT id FROM mentor.questao WHERE prova_real_id=$1 AND usavel ORDER BY id", prova_real_id)]
                if not ids:
                    raise HTTPException(404, "prova real sem questões usáveis")
                tipo = "real"
            else:
                ids = []
                fator = n_total / 80.0
                for mid, n in DIST_SIMULADO:
                    k = max(1, round(n * fator))
                    rows = await conn.fetch(f"""
                        SELECT DISTINCT ON (q.id) q.id, (q.banca_sigla = ANY($3::text[])) AS pref, q.ano
                        FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id JOIN mentor.assunto s ON s.id=qa.assunto_id
                        WHERE s.materia_id=$1 AND q.tipo='ME' AND {SQL_BASE_Q}
                          AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id=q.id)
                          AND NOT EXISTS (SELECT 1 FROM mentor.simulado sm WHERE q.id = ANY(sm.questoes))
                        ORDER BY q.id, random() LIMIT $2""", mid, k * 6, list(BANCAS_PREF))
                    lst = _rs(rows)
                    random.shuffle(lst)
                    lst.sort(key=lambda r: (not r["pref"], -(r["ano"] or 0)))
                    ids += [r["id"] for r in lst[:k]]
                tipo = "montado"
            numero = (await conn.fetchval("SELECT COALESCE(max(numero),0) FROM mentor.simulado")) + 1
            sid = await conn.fetchval("INSERT INTO mentor.simulado (numero, data, tipo, prova_real_id, questoes) VALUES ($1,$2,$3,$4,$5) RETURNING id", numero, d, tipo, prova_real_id, ids)
    return {"simulado_id": sid, "numero": numero, "tipo": tipo, "n": len(ids), "data": d.isoformat()}


@router.get("/simulado/{simulado_id}")
async def simulado(simulado_id: int):
    async with db() as conn:
        s = _r(await conn.fetchrow("SELECT * FROM mentor.simulado WHERE id=$1", simulado_id))
        if not s:
            raise HTTPException(404, "simulado não encontrado")
        qs = _rs(await conn.fetch("""SELECT q.id, q.enunciado, q.alternativas, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, m.nome AS materia
                                     FROM mentor.questao q LEFT JOIN LATERAL (SELECT s.materia_id FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id WHERE qa.questao_id=q.id LIMIT 1) x ON TRUE
                                     LEFT JOIN mentor.materia m ON m.id=x.materia_id WHERE q.id = ANY($1::bigint[])""", s["questoes"]))
        ordem = {qid: i for i, qid in enumerate(s["questoes"])}
        qs.sort(key=lambda q: ordem.get(q["id"], 0))
        for q in qs:
            q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
        if s["status"] == "agendado":
            await conn.execute("UPDATE mentor.simulado SET status='em_andamento', inicio=now() WHERE id=$1", simulado_id)
        s["questoes"] = qs
        s["por_materia"] = json.loads(s["por_materia"]) if isinstance(s["por_materia"], str) else s["por_materia"]
    return s


@router.post("/simulado/{simulado_id}/entregar")
async def entregar_simulado(simulado_id: int, respostas: dict = Body(..., embed=True), sessao_id: Optional[int] = Body(None, embed=True)):
    cfg_pontos = 1.25
    async with db() as conn:
        async with conn.transaction():
            s = _r(await conn.fetchrow("SELECT * FROM mentor.simulado WHERE id=$1", simulado_id))
            if not s or s["status"] == "entregue":
                raise HTTPException(409, "simulado inexistente ou já entregue")
            gab = {r["id"]: (r["gabarito"], r["materia"]) for r in await conn.fetch("""
                SELECT q.id, q.gabarito, m.nome AS materia FROM mentor.questao q
                LEFT JOIN LATERAL (SELECT s.materia_id FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id WHERE qa.questao_id=q.id LIMIT 1) x ON TRUE
                LEFT JOIN mentor.materia m ON m.id=x.materia_id WHERE q.id = ANY($1::bigint[])""", s["questoes"])}
            certas, por = 0, {}
            for qid in s["questoes"]:
                marcada = (respostas.get(str(qid)) or respostas.get(qid) or "").strip().upper() or None
                g, mat = gab.get(qid, (None, None))
                ok = bool(marcada and g and marcada == g.strip().upper())
                certas += ok
                por.setdefault(mat or "?", {"n": 0, "certas": 0})
                por[mat or "?"]["n"] += 1
                por[mat or "?"]["certas"] += ok
                await conn.execute("INSERT INTO mentor.resposta (questao_id, sessao_id, contexto, marcada, correta) VALUES ($1,$2,'simulado',$3,$4)", qid, sessao_id, marcada, ok if marcada else None)
            n = len(s["questoes"]) or 1
            nota = round(certas * (100.0 / n), 2) if n != 80 else round(certas * cfg_pontos, 2)
            await conn.execute("UPDATE mentor.simulado SET status='entregue', fim=now(), certas=$2, nota=$3, por_materia=$4, respostas=$5 WHERE id=$1",
                               simulado_id, certas, nota, json.dumps(por), json.dumps({str(k): v for k, v in respostas.items()}))
            xp = await _add_xp(conn, "simulado", XP["simulado"] + (XP["simulado_acima_60"] if nota >= 60 else 0), {"simulado_id": simulado_id, "nota": nota, "sessao_id": sessao_id})
    return {"simulado_id": simulado_id, "certas": certas, "n": n, "nota": nota, "por_materia": por, "xp": xp}


# ----------------------------------------------------------------- o que mais cai · bizus · cards
@router.get("/mais-cai")
async def mais_cai(materia_id: Optional[int] = None, limite: int = 30):
    async with db() as conn:
        rows = await conn.fetch("""
            SELECT e.id, e.nome, m.nome AS materia, count(DISTINCT q.id) AS questoes, count(DISTINCT q.prova_real_id) AS provas
            FROM mentor.questao q CROSS JOIN LATERAL unnest(q.etiquetas) AS et(id)
            JOIN mentor.etiqueta e ON e.id = et.id
            LEFT JOIN mentor.materia m ON e.raiz = ANY(m.etiquetas_raiz)
            WHERE q.soldado_ba AND e.id <> e.raiz AND ($1::int IS NULL OR m.id = $1)
            GROUP BY e.id, e.nome, m.nome ORDER BY questoes DESC, provas DESC LIMIT $2""", materia_id, limite)
        total = await conn.fetchval("SELECT count(*) FROM mentor.questao WHERE soldado_ba")
        provas = await conn.fetchval("SELECT count(*) FROM mentor.prova_real WHERE soldado_ba")
    return {"total_questoes": total, "provas": provas, "etiquetas": _rs(rows)}


@router.get("/bizus/aula/{aula_id}")
async def bizus_aula(aula_id: int):
    async with db() as conn:
        rows = await conn.fetch("SELECT id, secao, texto, fonte, aprovado FROM mentor.bizu WHERE aula_id=$1 ORDER BY secao, id", aula_id)
    return _rs(rows)


@router.post("/bizu")
async def novo_bizu(aula_id: Optional[int] = Body(None), assunto_id: Optional[int] = Body(None), texto: str = Body(...)):
    texto = (texto or "").strip()
    if len(texto) < 5 or len(texto) > 500:
        raise HTTPException(400, "bizu de 5 a 500 caracteres")
    async with db() as conn:
        if assunto_id is None and aula_id:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id)
        bid = await conn.fetchval("INSERT INTO mentor.bizu (aula_id, assunto_id, fonte, texto) VALUES ($1,$2,'usuario',$3) RETURNING id", aula_id, assunto_id, texto)
        await conn.execute("INSERT INTO mentor.card (assunto_id, aula_id, tipo, frente, verso, fonte, proxima) VALUES ($1,$2,'usuario',$3,$3,'meu bizu',$4)", assunto_id, aula_id, texto, _hoje() + timedelta(days=1))
    return {"bizu_id": bid}


CAIXAS = [1, 3, 7, 15, 30]


@router.get("/cards/hoje")
async def cards_hoje(limite: int = 20):
    hoje = _hoje()
    async with db() as conn:
        rows = await conn.fetch("""SELECT c.id, c.tipo, c.frente, c.verso, c.caixa, c.proxima, c.fonte, s.nome AS assunto, m.nome AS materia, a.titulo AS aula
                                   FROM mentor.card c LEFT JOIN mentor.assunto s ON s.id=c.assunto_id LEFT JOIN mentor.materia m ON m.id=s.materia_id LEFT JOIN mentor.aula a ON a.id=c.aula_id
                                   WHERE c.ativo AND (c.proxima IS NULL OR c.proxima <= $1) ORDER BY c.proxima NULLS FIRST, random() LIMIT $2""", hoje, limite)
    out = _rs(rows)
    for r in out:
        if r.get("proxima"):
            r["proxima"] = r["proxima"].isoformat()
    return out


@router.post("/card/{card_id}/responder")
async def responder_card(card_id: int, lembrou: bool = Body(..., embed=True)):
    hoje = _hoje()
    async with db() as conn:
        c = _r(await conn.fetchrow("SELECT * FROM mentor.card WHERE id=$1 AND ativo", card_id))
        if not c:
            raise HTTPException(404, "card não encontrado")
        caixa = min(c["caixa"] + 1, len(CAIXAS) - 1) if lembrou else 0
        prox = hoje + timedelta(days=CAIXAS[caixa])
        await conn.execute("UPDATE mentor.card SET caixa=$2, proxima=$3, acertos=acertos+$4, erros=erros+$5 WHERE id=$1", card_id, caixa, prox, int(lembrou), int(not lembrou))
        xp = await _add_xp(conn, "card", XP["card"], {"card_id": card_id}) if lembrou else None
    return {"card_id": card_id, "caixa": caixa, "proxima": prox.isoformat(), "xp": xp}


# =====================================================================
# BASE POR MATÉRIA e PROVA DE BASE (v3.8)
# =====================================================================
@router.get("/base")
async def base_por_materia():
    async with db() as conn:
        await _garantir_base(conn)
        rows = _rs(await conn.fetch("""SELECT m.id, m.nome, m.fase, m.trilha, m.trilha_ordem, m.regua_base, m.prova_base_em,
               (SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND a.base) AS base_total,
               (SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND a.base AND a.estado='concluida') AS base_feitas
               FROM mentor.materia m ORDER BY m.trilha, m.trilha_ordem"""))
        for r in rows:
            r["pronta"] = r["fase"] == "base" and r["base_total"] > 0 and r["base_feitas"] >= r["base_total"]
            if r.get("prova_base_em"):
                r["prova_base_em"] = r["prova_base_em"].isoformat()
            r["provas"] = _rs(await conn.fetch("SELECT id, certas, n, acerto, regua, aprovada, aulas_erradas, inicio, fim FROM mentor.prova_base WHERE materia_id=$1 ORDER BY inicio DESC LIMIT 5", r["id"]))
            for p in r["provas"]:
                for k in ("inicio", "fim"):
                    if p.get(k):
                        p[k] = p[k].isoformat()
                if p.get("acerto") is not None:
                    p["acerto"] = float(p["acerto"])
    return rows


@router.post("/materia/{materia_id}/prova-base/iniciar")
async def prova_base_iniciar(materia_id: int, sessao_id: Optional[int] = Body(None, embed=True)):
    """20 questões de Soldado da matéria (inéditas, uma por texto; PM-BA/CBM-BA primeiro, depois as bancas preferidas)."""
    async with db() as conn:
        await _garantir_base(conn)
        m = _r(await conn.fetchrow("SELECT id, nome, fase, regua_base FROM mentor.materia WHERE id=$1", materia_id))
        if not m:
            raise HTTPException(404, "matéria não encontrada")
        aberta = _r(await conn.fetchrow("SELECT id, questoes FROM mentor.prova_base WHERE materia_id=$1 AND fim IS NULL ORDER BY inicio DESC LIMIT 1", materia_id))
        if aberta:
            qs = _rs(await conn.fetch(f"""SELECT q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo, q.indice_acerto, q.comentario_professor, q.soldado_ba
                                          FROM mentor.questao q WHERE q.id = ANY($1::bigint[])""", list(aberta["questoes"])))
            ordem = {qid: i for i, qid in enumerate(aberta["questoes"])}
            qs.sort(key=lambda q: ordem.get(q["id"], 999))
            return {"prova_id": aberta["id"], "materia": m["nome"], "regua": m["regua_base"], "questoes": [_q_proto(q, m["nome"], None) for q in qs], "retomada": True}
        qs = _rs(await conn.fetch(f"""
            SELECT DISTINCT ON (q.enunciado_hash) q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo, q.indice_acerto, q.comentario_professor, q.soldado_ba,
                   random() AS rnd
            FROM mentor.questao q JOIN mentor.questao_aula qa ON qa.questao_id=q.id JOIN mentor.assunto s ON s.id=qa.assunto_id
            WHERE s.materia_id=$1 AND q.tipo='ME' AND {SQL_BASE_Q} AND COALESCE(qa.melhor_mat, TRUE)
              AND NOT EXISTS (SELECT 1 FROM mentor.resposta r JOIN mentor.questao q2 ON q2.id=r.questao_id WHERE q2.enunciado_hash=q.enunciado_hash)
            ORDER BY q.enunciado_hash, q.soldado_ba DESC, (q.banca_sigla = ANY($2::text[])) DESC, random()""", materia_id, list(BANCAS_PREF)))
        if len(qs) < 5:
            raise HTTPException(409, "estoque insuficiente de questões inéditas nesta matéria")
        qs.sort(key=lambda q: (not q["soldado_ba"], q["banca_sigla"] not in BANCAS_PREF, q["rnd"]))
        qs = qs[:PROVA_BASE_N]
        random.shuffle(qs)
        pid = await conn.fetchval("INSERT INTO mentor.prova_base (materia_id, questoes, n, regua, sessao_id) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                                  materia_id, [q["id"] for q in qs], len(qs), m["regua_base"], sessao_id)
    return {"prova_id": pid, "materia": m["nome"], "regua": m["regua_base"], "questoes": [_q_proto(q, m["nome"], None) for q in qs], "retomada": False}


@router.post("/prova-base/{prova_id}/entregar")
async def prova_base_entregar(prova_id: int, respostas: dict = Body(default_factory=dict), confiancas: dict = Body(default_factory=dict), sessao_id: Optional[int] = Body(None)):
    """Corrige, grava as respostas, aplica a régua da matéria; aprovada → fase 'questao'. Reprovada → aulas das questões erradas (reler o resumo de bolso)."""
    async with db() as conn:
        async with conn.transaction():
            await _garantir_base(conn)
            p = _r(await conn.fetchrow("SELECT * FROM mentor.prova_base WHERE id=$1", prova_id))
            if not p:
                raise HTTPException(404, "prova não encontrada")
            if p["fim"]:
                raise HTTPException(409, "prova já entregue")
            m = _r(await conn.fetchrow("SELECT id, nome, fase, regua_base FROM mentor.materia WHERE id=$1", p["materia_id"]))
            certas, erradas_q, detalhe = 0, [], []
            for qid in p["questoes"]:
                q = _r(await conn.fetchrow("SELECT id, gabarito, comentario_professor FROM mentor.questao WHERE id=$1", qid))
                marc = (respostas.get(str(qid)) or respostas.get(qid) or "").strip().upper() or None
                conf = confiancas.get(str(qid)) or confiancas.get(qid)
                gab = (q["gabarito"] or "").strip().upper()
                ok = bool(marc) and marc == gab and conf != "chute"
                aula_id = await conn.fetchval("SELECT aula_id FROM mentor.questao_aula WHERE questao_id=$1 ORDER BY melhor DESC, melhor_mat DESC, forca DESC LIMIT 1", qid)
                assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id) if aula_id else None
                await conn.execute("""INSERT INTO mentor.resposta (questao_id, sessao_id, aula_id, assunto_id, contexto, marcada, correta, confianca)
                                      VALUES ($1,$2,$3,$4,'questoes',$5,$6,$7)""", qid, sessao_id or p["sessao_id"], aula_id, assunto_id, marc, bool(marc) and marc == gab, conf if conf in ("certeza", "duvida", "chute") else None)
                if ok:
                    certas += 1
                else:
                    if aula_id:
                        erradas_q.append(aula_id)
                detalhe.append({"questao_id": qid, "gabarito": gab, "marcada": marc, "correta": ok, "aula_id": aula_id, "comentario_professor": _comentario(q["comentario_professor"])})
            n = len(p["questoes"]) or 1
            acerto = round(100.0 * certas / n, 1)
            aprovada = acerto >= (p["regua"] or m["regua_base"])
            aulas_err = sorted(set(erradas_q))
            await conn.execute("UPDATE mentor.prova_base SET respostas=$2, certas=$3, acerto=$4, aprovada=$5, aulas_erradas=$6, fim=now() WHERE id=$1",
                               prova_id, json.dumps(respostas), certas, acerto, aprovada, aulas_err)
            xp = None
            if aprovada and m["fase"] == "base":
                await conn.execute("UPDATE mentor.materia SET fase='questao', prova_base_em=now() WHERE id=$1", m["id"])
                xp = await _add_xp(conn, "prova_base", XP["prova_base"], {"materia_id": m["id"], "prova_id": prova_id})
            aulas = _rs(await conn.fetch("""SELECT a.id, a.titulo, s.nome AS assunto, count(*) AS erros FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id
                                            JOIN unnest($1::bigint[]) e(id) ON e.id=a.id GROUP BY a.id, a.titulo, s.nome ORDER BY erros DESC, a.ordem""", erradas_q)) if erradas_q else []
    return {"prova_id": prova_id, "materia": m["nome"], "certas": certas, "n": n, "acerto": acerto, "regua": p["regua"] or m["regua_base"], "aprovada": aprovada,
            "fase": "questao" if (aprovada or m["fase"] == "questao") else "base", "xp": xp, "aulas_erradas": aulas, "detalhe": detalhe}


# =====================================================================
# CADERNO DE ERROS — uma linha por erro (regra / contraste / pegadinha / desatenção)
# A linha vira card de Leitner (menos desatenção) e entra na aba Revisão.
# =====================================================================
TIPOS_CADERNO = ("regra", "contraste", "pegadinha", "desatencao")


@router.get("/versao")
async def versao():
    return {"versao": VERSAO}


@router.get("/caderno")
async def caderno_listar(materia_id: Optional[int] = None, materia: Optional[str] = None, abertas: bool = False, limite: int = 500):
    async with db() as conn:
        await _garantir_caderno(conn)
        cond = ["TRUE"]
        args = []
        if materia_id:
            args.append(materia_id)
            cond.append(f"c.materia_id=${len(args)}")
        if materia:
            args.append(materia)
            cond.append(f"m.nome=${len(args)}")
        if abertas:
            cond.append("c.riscada_em IS NULL")
        args.append(limite)
        rows = _rs(await conn.fetch(f"""SELECT c.id, c.questao_id, c.aula_id, c.assunto_id, c.materia_id, c.card_id, c.tipo, c.linha, c.enunciado, c.contexto,
                                              c.riscada_em, c.voltou, c.criado_em, m.nome AS materia, s.nome AS assunto, a.titulo AS aula
                                       FROM mentor.caderno c LEFT JOIN mentor.materia m ON m.id=c.materia_id LEFT JOIN mentor.assunto s ON s.id=c.assunto_id LEFT JOIN mentor.aula a ON a.id=c.aula_id
                                       WHERE {' AND '.join(cond)} ORDER BY (c.riscada_em IS NULL) DESC, c.voltou DESC, c.id DESC LIMIT ${len(args)}""", *args))
        tot = _r(await conn.fetchrow("SELECT count(*) FILTER (WHERE riscada_em IS NULL) AS abertas, count(*) FILTER (WHERE riscada_em IS NOT NULL) AS riscadas, count(*) FILTER (WHERE voltou > 0) AS voltaram FROM mentor.caderno"))
    for r in rows:
        for k in ("riscada_em", "criado_em"):
            if r.get(k):
                r[k] = r[k].isoformat()
    return {"linhas": rows, "abertas": int(tot["abertas"] or 0), "riscadas": int(tot["riscadas"] or 0), "voltaram": int(tot["voltaram"] or 0)}


@router.post("/caderno")
async def caderno_nova(questao_id: Optional[int] = Body(None), aula_id: Optional[int] = Body(None), assunto_id: Optional[int] = Body(None),
                       tipo: str = Body("regra"), linha: str = Body(...), enunciado: Optional[str] = Body(None), contexto: Optional[str] = Body(None)):
    linha = re.sub(r"\s+", " ", (linha or "")).strip()
    if tipo not in TIPOS_CADERNO:
        raise HTTPException(400, "tipo inválido")
    if len(linha) < 3 or len(linha) > 240:
        raise HTTPException(400, "a linha do caderno tem de 3 a 240 caracteres")
    hoje = _hoje()
    async with db() as conn:
        await _garantir_caderno(conn)
        if assunto_id is None and aula_id:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id)
        if assunto_id is None and questao_id:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.questao_aula WHERE questao_id=$1 ORDER BY forca DESC NULLS LAST LIMIT 1", questao_id)
        materia_id = await conn.fetchval("SELECT materia_id FROM mentor.assunto WHERE id=$1", assunto_id) if assunto_id else None
        if not enunciado and questao_id:
            enunciado = await conn.fetchval("SELECT enunciado FROM mentor.questao WHERE id=$1", questao_id)
        enunciado = _curto(enunciado, 240) if enunciado else None
        card_id = None
        if tipo != "desatencao":
            frente = (enunciado + "\n\nQual é a regra?") if enunciado else "Qual é a regra?"
            card_id = await conn.fetchval("INSERT INTO mentor.card (assunto_id, aula_id, tipo, frente, verso, fonte, proxima) VALUES ($1,$2,$3,$4,$5,'caderno de erros',$6) RETURNING id",
                                          assunto_id, aula_id, "contraste" if tipo == "contraste" else "usuario", frente, linha, hoje + timedelta(days=1))
        cid = await conn.fetchval("""INSERT INTO mentor.caderno (questao_id, aula_id, assunto_id, materia_id, card_id, tipo, linha, enunciado, contexto)
                                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""", questao_id, aula_id, assunto_id, materia_id, card_id, tipo, linha, enunciado, contexto)
        abertas = await conn.fetchval("SELECT count(*) FROM mentor.caderno WHERE riscada_em IS NULL AND materia_id=$1", materia_id) if materia_id else None
    return {"id": cid, "card_id": card_id, "abertas_materia": abertas}


@router.post("/caderno/{caderno_id}")
async def caderno_editar(caderno_id: int, linha: Optional[str] = Body(None), tipo: Optional[str] = Body(None), riscada: Optional[bool] = Body(None), apagar: bool = Body(False)):
    async with db() as conn:
        await _garantir_caderno(conn)
        c = _r(await conn.fetchrow("SELECT * FROM mentor.caderno WHERE id=$1", caderno_id))
        if not c:
            raise HTTPException(404, "linha não encontrada")
        if apagar:
            await conn.execute("DELETE FROM mentor.caderno WHERE id=$1", caderno_id)
            if c["card_id"]:
                await conn.execute("UPDATE mentor.card SET ativo=FALSE WHERE id=$1", c["card_id"])
            return {"ok": True, "apagada": True}
        if linha is not None:
            linha = re.sub(r"\s+", " ", linha).strip()
            if len(linha) < 3 or len(linha) > 240:
                raise HTTPException(400, "a linha do caderno tem de 3 a 240 caracteres")
            await conn.execute("UPDATE mentor.caderno SET linha=$2 WHERE id=$1", caderno_id, linha)
            if c["card_id"]:
                await conn.execute("UPDATE mentor.card SET verso=$2 WHERE id=$1", c["card_id"], linha)
        if tipo is not None:
            if tipo not in TIPOS_CADERNO:
                raise HTTPException(400, "tipo inválido")
            await conn.execute("UPDATE mentor.caderno SET tipo=$2 WHERE id=$1", caderno_id, tipo)
        if riscada is not None:
            await conn.execute("UPDATE mentor.caderno SET riscada_em=CASE WHEN $2 THEN now() ELSE NULL END WHERE id=$1", caderno_id, riscada)
        c = _r(await conn.fetchrow("SELECT id, tipo, linha, riscada_em, voltou, card_id FROM mentor.caderno WHERE id=$1", caderno_id))
        if c.get("riscada_em"):
            c["riscada_em"] = c["riscada_em"].isoformat()
    return c


# =====================================================================
# PROTÓTIPO LIGADO NA API — dados no formato do protótipo aprovado
# (D, SEMANAS_DET, HOJE_AULAS), estado persistido e eventos → motor.
# =====================================================================
MODO_PROTO = {"S": "completo", "A": "completo", "B": "expresso", "C": "expresso", "treino": "expresso", "fora": "fora", "revisao": "expresso"}
REVISAO_TIPOS = ("R1", "R2", "R3", "R7", "R15", "R30", "M")
AGENDA_SIM = [["2026-12-04", "montado", "Simulado 1 · montado"], ["2026-12-18", "montado", "Simulado 2 · montado"],
              ["2027-01-08", "real", "Simulado 3 · prova real IBFC 2020 (reservada)"], ["2027-01-22", "montado", "Simulado 4 · montado"],
              ["2027-02-05", "real", "Simulado 5 · prova real FCC 2012 (reservada)"], ["2027-02-19", "montado", "Simulado 6 · montado"]]


def _q_proto(q, mat=None, assunto=None):
    alts = q.get("alternativas") or []
    if isinstance(alts, str):
        alts = json.loads(alts)
    com = _comentario(q.get("comentario_professor"))
    return {"id": q["id"], "ano": q.get("ano"), "banca": q.get("banca_sigla") or "", "banca_curta": q.get("banca_sigla") or "",
            "orgao": q.get("orgao_sigla") or "", "cargo": q.get("cargo") or "", "enunciado": q.get("enunciado") or "",
            "alts": [[a.get("letra"), a.get("texto")] for a in alts], "gab": (q.get("gabarito") or "").strip().upper(),
            "pct": float(q["indice_acerto"]) if q.get("indice_acerto") is not None else None, "mat": mat, "assunto": assunto,
            "explicacao": com or "", "resolucao": ({"fonte": "professor", "texto": com} if com else {"fonte": "mentor", "texto": ""}),
            "soldado": bool(q.get("soldado_ba")), "aula_id": q.get("aula_id"), "assunto_id": q.get("assunto_id")}


def _dur_txt(s):
    s = int(s or 0)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _curta(d):
    MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    SEM = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    return f"{SEM[d.weekday()]} {d.day:02d}/{MESES[d.month - 1]}"


async def _assuntos_proto(conn, ids=None, materia_id=None):
    where = "TRUE"
    args = []
    if ids is not None:
        where = "s.id = ANY($1::bigint[])"
        args = [ids]
    elif materia_id:
        where = "s.materia_id = $1"
        args = [materia_id]
    rows = await conn.fetch(f"""
        SELECT s.id, s.nome, s.tier, s.modo, s.base, s.origem, s.soldado, s.estado, s.ordem, s.materia_id, s.modulo_id, m.nome AS materia,
               mo.nome AS modulo, mo.ordem AS modulo_ordem, mo.tipo AS modulo_tipo,
               COALESCE((SELECT sum(a.duracao_s) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo<>'fora'),0)/60 AS video,
               (SELECT array_agg(a.titulo ORDER BY a.ordem) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo<>'fora') AS aulas,
               (SELECT array_agg(a.id ORDER BY a.ordem) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo<>'fora') AS aula_ids,
               (SELECT array_agg(a.estado ORDER BY a.ordem) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo<>'fora') AS aula_estados,
               (SELECT array_agg(a.base ORDER BY a.ordem) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo<>'fora') AS aula_base,
               (SELECT count(*) FROM mentor.questao_aula qa WHERE qa.assunto_id=s.id) AS questoes
        FROM mentor.assunto s JOIN mentor.materia m ON m.id=s.materia_id JOIN mentor.modulo mo ON mo.id=s.modulo_id
        WHERE {where} ORDER BY s.materia_id, (mo.tipo='propria'), mo.ordem, s.ordem""", *args)
    out = []
    for r in rows:
        r = dict(r)
        tipo = "conteudo"
        if r["tier"] in ("treino",):
            tipo = "exercicios"
        ab = list(r["aula_base"] or [])
        modo = "leitura" if r["origem"] == "propria" else ("completo" if any(ab) else "expresso")
        out.append({"id": r["id"], "nome": r["nome"], "tipo": tipo, "base": any(ab), "tier": r["tier"] if r["tier"] in ("S", "A", "B", "C") else "C",
                    "modo": modo, "modo_v3": r["modo"], "soldado": r["soldado"] or 0, "video": int(r["video"] or 0), "aulas": list(r["aulas"] or []),
                    "aula_ids": list(r["aula_ids"] or []), "aula_estados": list(r["aula_estados"] or []), "aula_base": ab, "questoes": r["questoes"],
                    "propria": r["origem"] == "propria", "ordem": r["ordem"], "materia": r["materia"], "materia_id": r["materia_id"],
                    "modulo": r["modulo"], "modulo_ordem": r["modulo_ordem"], "modulo_tipo": r["modulo_tipo"], "estado": r["estado"]})
    return out


def _custo_h(a):
    if a["propria"]:
        return 0.75
    n = max(1, len(a["aulas"]))
    if a["tier"] == "C":
        return round(n * 0.15 + 0.1, 2)
    return round(a["video"] / 60 / 1.5 + n * 0.25, 2)


def _fase_semana(seg):
    return "A" if seg < date(2026, 12, 14) else ("B" if seg < date(2027, 1, 4) else ("C" if seg < date(2027, 1, 18) else "D"))


def _titulo_semana_v3(seg, por):
    """Base · matérias / Base + modo questão · matérias / Modo questão · matérias / Reta final."""
    if seg >= RETA_FINAL:
        return "Reta final: fora do edital, cards, simulados e questões dos assuntos fracos"
    n_base = n_comp = 0
    for l in por.values():
        for a in l:
            for mo in (a.get("aula_modos") or []):
                if mo == "base":
                    n_base += 1
                else:
                    n_comp += 1
    mats = list(por.keys())
    if n_base and not n_comp:
        cab = "Base (vídeo + questões)"
    elif n_base and n_comp:
        cab = "Base + modo questão"
    elif n_comp:
        cab = "Modo questão (sem vídeo)"
    else:
        cab = "Semana"
    return cab + (" · " + ", ".join(mats[:4]) if mats else "")


def _titulo_semana(seg, mats):
    f = _fase_semana(seg)
    if f == "D":
        return "Reta final: passada M, cards, simulados e questões dos assuntos fracos"
    base = {"A": "O que a prova cobra sempre (tier S)", "B": "O que cobra bastante (tier A)", "C": "Completar: B com evidência e resumos de C"}[f]
    return base + (" · " + ", ".join(mats[:4]) if mats else "")


async def _previsao_semanas(conn, a_partir, ate=date(2027, 2, 21)):
    """Simula as semanas futuras com o planejador de trilhas (sem gravar). A base de uma matéria fecha quando as aulas de base acabam."""
    trilhas, pesos = await _trilhas(conn)
    mats, aulas = await _universo_planejador(conn)
    mats = {k: dict(v) for k, v in mats.items()}
    todos = {a["id"]: a for a in await _assuntos_proto(conn)}
    feitas = set()
    semanas = []
    seg = a_partir
    while seg <= ate:
        cap_total = await _capacidade_semana(conn, seg)
        itens, _ = _plano_trilhas(mats, aulas, seg, cap_total * PCT_CONTEUDO, trilhas, pesos, feitas)
        # fase vira "questao" quando não sobra aula de base
        for mid, m in mats.items():
            if m["fase"] == "base" and not any(a["base"] and a["id"] not in feitas for a in aulas if a["materia_id"] == mid):
                m["fase"] = "questao"
        semanas.append((seg, cap_total, [_item_proto(it, todos, aulas) for it in itens if it["assunto_id"] in todos]))
        seg += timedelta(days=7)
        if not itens and all(m["fase"] == "questao" for m in mats.values()) and not any(a["id"] not in feitas for a in aulas if a["modo"] in MODOS_QUESTAO):
            break
    return semanas


def _item_proto(it, todos, aulas):
    """Assunto do protótipo recortado às aulas planejadas nesta semana, com o modo de cada aula."""
    a = dict(todos[it["assunto_id"]])
    por_id = {x["id"]: x for x in aulas}
    ids = it["aulas"]
    pos = {aid: i for i, aid in enumerate(a.get("aula_ids") or [])}
    a["aulas"] = [a["aulas"][pos[i]] if i in pos else (por_id.get(i, {}).get("titulo") or "") for i in ids]
    a["aula_ids"] = ids
    a["aula_estados"] = [a["aula_estados"][pos[i]] if i in pos else "pendente" for i in ids]
    a["aula_modos"] = it["aula_modos"]
    a["modo_item"] = it["modo"]
    a["modo"] = "completo" if it["modo"] == "base" else "expresso"
    return a


def _semana_proto(n, seg, meta, itens, revisoes):
    por = {}
    for a in itens:
        por.setdefault(a["materia"], []).append({"id": a["id"], "nome": a["nome"], "base": a["base"], "tier": a["tier"], "modo": a["modo"], "propria": a["propria"],
                                                 "aulas": a["aulas"], "aula_ids": a.get("aula_ids", []), "aula_estados": a.get("aula_estados", []),
                                                 "aula_modos": a.get("aula_modos") or [("base" if b else "complemento") for b in (a.get("aula_base") or [])],
                                                 "video": a["video"], "soldado": a["soldado"], "estado": a.get("estado")})
    return {"n": n, "ini": seg.isoformat(), "fim": (seg + timedelta(days=6)).isoformat(), "titulo": _titulo_semana_v3(seg, por), "meta": float(meta),
            "materias": [[m, l] for m, l in por.items()], "revisoes": revisoes, "reta": _fase_semana(seg) == "D"}


@router.get("/proto/dados")
async def proto_dados():
    hoje = _hoje()
    async with db() as conn:
        # ---- D.materias
        todos = await _assuntos_proto(conn)
        mats = _rs(await conn.fetch("SELECT id, nome, peso_prova FROM mentor.materia ORDER BY id"))
        D_mat = []
        for m in mats:
            mods = {}
            for a in todos:
                if a["materia_id"] != m["id"]:
                    continue
                mods.setdefault((a["modulo_ordem"], a["modulo"]), []).append(a)
            D_mat.append({"id": m["id"], "nome": m["nome"], "peso": m["peso_prova"],
                          "modulos": [{"ordem": k[0], "nome": k[1], "assuntos": [{kk: a[kk] for kk in ("id", "nome", "tipo", "base", "tier", "modo", "modo_v3", "soldado", "video", "aulas", "aula_ids", "aula_estados", "aula_base", "questoes", "propria", "ordem", "estado")} for a in l]}
                                      for k, l in sorted(mods.items(), key=lambda x: (x[0][0] == 0, x[0][0]))]})
        # ---- orçamento
        orc = []
        for m in mats:
            am = [a for a in todos if a["materia_id"] == m["id"]]
            base = sum(_custo_h(a) for a in am if a["tier"] == "S")
            orcam = sum(_custo_h(a) for a in am if a["tier"] in ("S", "A"))
            usado = sum(_custo_h(a) for a in am if a["estado"] in ("estudado", "dominado", "ressalva"))
            orc.append({"materia": m["nome"], "peso": m["peso_prova"], "orcamento": round(orcam, 1), "usado": round(usado, 1), "base": round(base, 1)})
        # ---- semanas: reais + previsão
        reais = _rs(await conn.fetch("SELECT * FROM mentor.semana WHERE status <> 'bloqueada' ORDER BY inicio"))
        semanas = []
        n = 1
        ultimo_fim = None
        await _garantir_base(conn)
        todos_por_id = {a["id"]: a for a in todos}
        aulas_univ = [dict(r) for r in await conn.fetch("SELECT id, titulo, base FROM mentor.aula")]
        for s in reais:
            pis = _rs(await conn.fetch("SELECT assunto_id, modo, aulas FROM mentor.plano_item WHERE semana_id=$1 ORDER BY ordem", s["id"]))
            itens = []
            for pi in pis:
                if pi["assunto_id"] not in todos_por_id:
                    continue
                if pi.get("aulas"):
                    itens.append(_item_proto({"assunto_id": pi["assunto_id"], "aulas": list(pi["aulas"]), "modo": pi.get("modo") or "base",
                                              "aula_modos": [("base" if next((x["base"] for x in aulas_univ if x["id"] == i), False) else (todos_por_id[pi["assunto_id"]].get("modo_v3") or "complemento")) for i in pi["aulas"]]},
                                             todos_por_id, aulas_univ))
                else:
                    itens.append(todos_por_id[pi["assunto_id"]])
            revs = [[_curta(r["prevista"]), r["tipo"], r["nome"], REV_N.get(r["tipo"], 4), r["id"], bool(r["feita"]), r["acerto"] and float(r["acerto"])]
                    for r in await conn.fetch("""SELECT r.id, r.tipo, COALESCE(r.adiada_para, r.prevista) AS prevista, r.feita, r.acerto, a.nome FROM mentor.revisao r JOIN mentor.assunto a ON a.id=r.assunto_id
                                                 WHERE COALESCE(r.adiada_para, r.prevista) BETWEEN $1 AND $2 ORDER BY 3""", s["inicio"], s["fim"])]
            sp = _semana_proto(n, s["inicio"], s["meta_horas"], itens, revs)
            sp["id"] = s["id"]
            sp["status"] = s["status"]
            semanas.append(sp)
            ultimo_fim = s["fim"]
            n += 1
        inicio_prev = (ultimo_fim + timedelta(days=1)) if ultimo_fim else _segunda(hoje)
        for seg, cap, itens in await _previsao_semanas(conn, inicio_prev):
            # revisões previstas: R1/R7/R30 dos assuntos das semanas anteriores (estimativa)
            sp = _semana_proto(n, seg, cap, itens, [])
            sp["status"] = "prevista"
            semanas.append(sp)
            n += 1
        # ---- hoje: aulas pendentes do plano aberto, até a meta do dia
        hoje_aulas = []
        hoje_rev = {"revisoes": 0, "questoes": 0, "lista": [], "cards": 0, "caderno_abertas": 0, "caderno_por_materia": {}}
        aberta = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE status='aberta' ORDER BY inicio DESC LIMIT 1"))
        if aberta:
            cfg = await _config(conn, "horas_dia", {})
            meta_min = _horas_dia(hoje, cfg) * 60 * PCT_CONTEUDO
            usado = 0
            pend = await conn.fetch("""SELECT a.id, a.titulo, a.url, a.duracao_s, a.estado, a.tipo, a.base, s.modo AS modo_v3, s.id AS assunto_id, s.nome AS assunto, m.nome AS mat, m.id AS materia_id, a.questoes_fixacao,
                                              COALESCE(a.resumo_bolso, a.resumo, a.mapa_mental) AS bolso, pi.aulas AS planejadas
                                       FROM mentor.plano_item pi JOIN mentor.assunto s ON s.id=pi.assunto_id JOIN mentor.materia m ON m.id=s.materia_id JOIN mentor.aula a ON a.assunto_id=s.id
                                       WHERE pi.semana_id=$1 AND NOT pi.feito AND a.estado <> 'concluida' AND a.tipo IN ('conteudo','exercicios','propria')
                                         AND (pi.aulas IS NULL OR a.id = ANY(pi.aulas)) ORDER BY pi.ordem, a.ordem""", aberta["id"])
            # prova de base: matéria em fase base com todas as aulas de base concluídas entra na frente do dia
            for pb in await conn.fetch("""SELECT m.id, m.nome, m.regua_base FROM mentor.materia m WHERE m.fase='base' AND EXISTS (SELECT 1 FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND a.base)
                                          AND NOT EXISTS (SELECT 1 FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND a.base AND a.estado <> 'concluida')
                                          ORDER BY m.trilha, m.trilha_ordem"""):
                hoje_aulas.append({"tipo": "prova_base", "mat": pb["nome"], "materia_id": pb["id"], "assunto": f"Prova de base · {pb['nome']}", "titulo": f"{PROVA_BASE_N} questões de Soldado · {pb['regua_base']}% libera o modo questão",
                                   "codigo": f"PB{pb['id']}", "aula_id": None, "assunto_id": None, "dur": "40 min", "n": PROVA_BASE_N, "regua": pb["regua_base"], "modo": "prova_base", "pre": [], "gate": [], "fix": [], "bolso": "", "recalls": []})
            # intercalar: no máximo 2 aulas seguidas do mesmo assunto; a próxima vem de outra matéria (spec v3.2 §3)
            por_assunto = {}
            for a in pend:
                por_assunto.setdefault(a["assunto_id"], []).append(a)
            ordem_ass = list(por_assunto.keys())
            escolhidas, mat_ultima, i = [], None, 0
            while any(por_assunto.values()) and len(escolhidas) < 12:
                # escolhe o próximo assunto de matéria diferente da última, na ordem do plano
                cand = [k for k in ordem_ass if por_assunto[k]]
                prox = next((k for k in cand if por_assunto[k][0]["mat"] != mat_ultima), cand[0])
                lote = por_assunto[prox][:2]
                por_assunto[prox] = por_assunto[prox][2:]
                escolhidas += lote
                mat_ultima = lote[0]["mat"]
            for a in escolhidas:
                custo = ((a["duracao_s"] or 0) / 60 / 1.5 + 25) if a["base"] else 25
                if any(x.get("tipo") == "aula" for x in hoje_aulas) and usado + custo > meta_min:
                    break
                usado += custo
                pre = await _questoes_da_aula(conn, a["id"], AQUEC_N, "aquecimento")
                gate = await _questoes_da_aula(conn, a["id"], GATE_N, "gate", excluir=[q["id"] for q in pre])
                fx = a["questoes_fixacao"]
                if isinstance(fx, str):
                    try:
                        fx = json.loads(fx)
                    except Exception:
                        fx = []
                fix = []
                for q in (fx or [])[:FIX_N]:
                    alts = q.get("alternatives") or q.get("alternativas") or []
                    # formato do protótipo: [letra, texto, correta, explicação]
                    fix.append({"enunciado": q.get("question") or q.get("enunciado") or "",
                                "alts": [[(x.get("id") or x.get("letra") or "").upper(), x.get("text") or x.get("texto") or "", bool(x.get("correct") or x.get("correta")), x.get("explanation") or x.get("explicacao") or ""] for x in alts],
                                "gab": next(((x.get("id") or x.get("letra") or "").upper() for x in alts if x.get("correct")), None),
                                "explicacoes": {(x.get("id") or x.get("letra") or "").upper(): x.get("explanation") or x.get("explicacao") or "" for x in alts}})
                recalls = [r["texto"] for r in await conn.fetch("SELECT texto FROM mentor.bizu WHERE aula_id=$1 AND fonte='usuario' AND secao='recall' ORDER BY id", a["id"])]
                modo = "base" if a["base"] else (a["modo_v3"] if a["modo_v3"] in MODOS_QUESTAO else ("fora" if a["modo_v3"] == "fora" else "complemento"))
                if a["tipo"] == "exercicios" and modo != "base":
                    modo = "questoes"
                hoje_aulas.append({"tipo": "aula", "mat": a["mat"], "assunto": a["assunto"], "codigo": str(a["id"]), "aula_id": a["id"], "assunto_id": a["assunto_id"], "materia_id": a["materia_id"], "titulo": a["titulo"],
                                   "url": a["url"], "dur": _dur_txt(a["duracao_s"]), "propria": a["tipo"] == "propria", "modo": modo,
                                   "bolso": (a["bolso"] or "")[:12000], "recalls": recalls,
                                   "pre": [_q_proto(q, a["mat"], a["assunto"]) for q in pre], "gate": [_q_proto(q, a["mat"], a["assunto"]) for q in gate], "fix": fix})
        # ---- revisão de hoje em números (questões + cards + caderno): o painel mostra no Plano de hoje e na aba Revisão
        await _garantir_caderno(conn)
        revs_hoje = _rs(await conn.fetch("""SELECT r.tipo, s.nome FROM mentor.revisao r JOIN mentor.assunto s ON s.id=r.assunto_id
                                            WHERE r.feita IS NULL AND COALESCE(r.adiada_para, r.prevista) <= $1 ORDER BY r.prevista""", hoje))
        hoje_rev = {"revisoes": len(revs_hoje), "questoes": sum(REV_N.get(r["tipo"], 4) for r in revs_hoje), "lista": [f'{r["tipo"]} · {r["nome"]}' for r in revs_hoje],
                    "cards": int(await conn.fetchval("SELECT count(*) FROM mentor.card WHERE ativo AND (proxima IS NULL OR proxima <= $1)", hoje) or 0),
                    "caderno_abertas": int(await conn.fetchval("SELECT count(*) FROM mentor.caderno WHERE riscada_em IS NULL") or 0),
                    "caderno_por_materia": {r["nome"]: int(r["n"]) for r in await conn.fetch("SELECT m.nome, count(*) AS n FROM mentor.caderno c JOIN mentor.materia m ON m.id=c.materia_id WHERE c.riscada_em IS NULL GROUP BY m.nome")}}
        # ---- pool de questões (aba Questões e simulado): ME inéditas, uma por texto, dos assuntos estudados + amostra da prova
        pool = _rs(await conn.fetch(f"""
            SELECT DISTINCT ON (q.enunciado_hash) q.id, q.enunciado, q.alternativas, q.gabarito, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo, q.indice_acerto, q.comentario_professor, q.soldado_ba,
                   qa.assunto_id, qa.aula_id, s.nome AS assunto, m.nome AS mat, random() AS rnd
            FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id JOIN mentor.assunto s ON s.id=qa.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
            WHERE q.tipo='ME' AND {SQL_BASE_Q} AND s.estado IN ('estudado','ressalva','dominado','em_andamento')
              AND NOT EXISTS (SELECT 1 FROM mentor.resposta r JOIN mentor.questao q2 ON q2.id=r.questao_id WHERE q2.enunciado_hash=q.enunciado_hash)
            ORDER BY q.enunciado_hash, (q.banca_sigla = ANY($1::text[])) DESC, q.ano DESC NULLS LAST LIMIT 600""", list(BANCAS_PREF)))
        random.shuffle(pool)
        D_q = [_q_proto(q, q["mat"], q["assunto"]) for q in pool[:400]]
        # ---- estado persistido do protótipo
        est = {r["chave"][6:]: (json.loads(r["valor"]) if isinstance(r["valor"], str) else r["valor"]) for r in await conn.fetch("SELECT chave, valor FROM mentor.config WHERE chave LIKE 'proto:%'")}
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        horas = {str(r["dia"]): int(r["m"]) for r in await conn.fetch("SELECT (inicio - interval '4 hours')::date AS dia, sum(minutos) AS m FROM mentor.sessao WHERE fim IS NOT NULL GROUP BY 1")}
        est["mm3_horas"] = horas
        # projeção (meses) a partir da previsão
        meses = ["out", "nov", "dez", "jan", "fev"]
        proj = {"meses": meses, "revisao": [], "conteudo": [], "acumulado": []}
        acum = 0
        for i, mes in enumerate([10, 11, 12, 1, 2]):
            sems = [s for s in semanas if dt_mes(s["ini"]) == mes]
            cont = sum(len(a) for s in sems for _, a in s["materias"])
            acum += cont
            proj["conteudo"].append(cont)
            proj["revisao"].append(int(sum(s["meta"] for s in sems) * 0.35))
            proj["acumulado"].append(acum)
        base_mat = _rs(await conn.fetch("""SELECT m.id, m.nome, m.fase, m.trilha, m.trilha_ordem, m.regua_base, m.prova_base_em,
                   (SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND a.base) AS base_total,
                   (SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND a.base AND a.estado='concluida') AS base_feitas,
                   (SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND NOT a.base AND s.modo IN ('complemento','lei_seca','questoes','leitura') AND a.tipo IN ('conteudo','exercicios','propria')) AS comp_total,
                   (SELECT count(*) FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=m.id AND NOT a.base AND s.modo IN ('complemento','lei_seca','questoes','leitura') AND a.tipo IN ('conteudo','exercicios','propria') AND a.estado='concluida') AS comp_feitas,
                   (SELECT row_to_json(p) FROM (SELECT id, acerto, aprovada, fim FROM mentor.prova_base pb WHERE pb.materia_id=m.id ORDER BY inicio DESC LIMIT 1) p) AS ultima_prova
                   FROM mentor.materia m ORDER BY m.trilha, m.trilha_ordem"""))
        flags_ok = any(b["base_total"] > 0 for b in base_mat)
        for b in base_mat:
            b["pronta"] = b["fase"] == "base" and b["base_total"] > 0 and b["base_feitas"] >= b["base_total"]
            if b.get("prova_base_em"):
                b["prova_base_em"] = b["prova_base_em"].isoformat()
            if isinstance(b.get("ultima_prova"), str):
                b["ultima_prova"] = json.loads(b["ultima_prova"])
    return {"D": {"materias": D_mat, "orcamento": orc, "calendario": {"fechado": "2027-01-31", "em_dia": [], "atrasado": []}, "questoes": D_q, "projecao": proj, "outras": [], "agenda_sim": AGENDA_SIM, "base": base_mat, "flags_ok": flags_ok},
            "SEMANAS": semanas, "HOJE_AULAS": hoje_aulas, "HOJE": hoje.isoformat(), "ESTADO": est, "progresso": prog, "HOJE_REV": hoje_rev, "VERSAO": VERSAO}


def dt_mes(iso):
    return int(str(iso)[5:7])


@router.post("/proto/estado")
async def proto_estado(chave: str = Body(...), valor: object = Body(None)):
    if not chave.startswith("mm3_") or len(chave) > 40:
        raise HTTPException(400, "chave inválida")
    async with db() as conn:
        await conn.execute("INSERT INTO mentor.config (chave, valor) VALUES ($1,$2) ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor, alterado_em=now()", "proto:" + chave, json.dumps(valor))
    return {"ok": True}


@router.post("/proto/evento")
async def proto_evento(tipo: str = Body(...), dados: dict = Body(default_factory=dict)):
    """Ganchos do protótipo → motor real."""
    hoje = _hoje()
    async with db() as conn:
        if tipo == "resposta":
            r = await responder(questao_id=int(dados["questao_id"]), contexto=dados.get("contexto") or "questoes", marcada=dados.get("marcada"), aula_id=dados.get("aula_id"),
                                assunto_id=dados.get("assunto_id"), sessao_id=dados.get("sessao_id"), confianca=dados.get("confianca"), tipo_erro=None, tempo_s=dados.get("tempo_s"), revisao_id=None)
            return r
        if tipo == "gate":
            return await fechar_gate(int(dados["aula_id"]), sessao_id=dados.get("sessao_id"))
        if tipo == "horas":
            dia = date.fromisoformat(dados["dia"])
            minutos = int(dados["minutos"])
            sid = await conn.fetchval("SELECT id FROM mentor.sessao WHERE tipo='estudo' AND resumo->>'origem'='crono' AND (inicio - interval '4 hours')::date=$1", dia)
            if sid:
                await conn.execute("UPDATE mentor.sessao SET minutos=$2, fim=now() WHERE id=$1", sid, minutos)
            else:
                await conn.execute("INSERT INTO mentor.sessao (tipo, inicio, fim, minutos, resumo) VALUES ('estudo', $1::date + interval '8 hours', now(), $2, '{\"origem\":\"crono\"}')", dia, minutos)
            return {"ok": True}
        if tipo == "estudei":
            aid = int(dados["assunto_id"])
            if dados.get("estudado", True):
                await conn.execute("UPDATE mentor.assunto SET estado='estudado', estudado_em=now() WHERE id=$1 AND estado <> 'estudado'", aid)
                await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE assunto_id=$1 AND estado <> 'concluida' AND tipo IN ('conteudo','exercicios','propria')", aid)
                if not await conn.fetchval("SELECT 1 FROM mentor.revisao WHERE assunto_id=$1 AND feita IS NULL", aid):
                    await _agendar_revisao(conn, aid, "R1", INTERVALO["R1"], hoje)
                await conn.execute("UPDATE mentor.plano_item pi SET feito=TRUE FROM mentor.semana s WHERE pi.semana_id=s.id AND s.status='aberta' AND pi.assunto_id=$1", aid)
            else:
                await conn.execute("UPDATE mentor.assunto SET estado='nao_estudado', estudado_em=NULL WHERE id=$1", aid)
                await conn.execute("UPDATE mentor.aula SET estado='pendente' WHERE assunto_id=$1", aid)
                await conn.execute("DELETE FROM mentor.revisao WHERE assunto_id=$1 AND feita IS NULL", aid)
            return {"ok": True}
        if tipo == "recall":
            # "o que eu lembro" escrito na pausa do vídeo: guardado como bizu do aluno (secao=recall), comparado ao resumo de bolso no painel
            aid = int(dados["aula_id"])
            texto = re.sub(r"[ \t]+", " ", str(dados.get("texto") or "")).strip()
            if len(texto) < 3:
                raise HTTPException(400, "escreva pelo menos uma linha")
            sid = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aid)
            await conn.execute("INSERT INTO mentor.bizu (aula_id, assunto_id, fonte, secao, texto, aprovado) VALUES ($1,$2,'usuario','recall',$3,FALSE)", aid, sid, texto[:2000])
            n = await conn.fetchval("SELECT count(*) FROM mentor.bizu WHERE aula_id=$1 AND secao='recall'", aid)
            return {"ok": True, "n": int(n)}
        if tipo == "aula_concluida":
            aid = int(dados["aula_id"])
            await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE id=$1 AND estado <> 'concluida'", aid)
            await _semear_cards_aula(conn, aid)
            sid = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aid)
            faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula WHERE assunto_id=$1 AND tipo IN ('conteudo','exercicios','propria') AND estado <> 'concluida'", sid)
            if faltam == 0:
                await conn.execute("UPDATE mentor.assunto SET estado='estudado', estudado_em=now() WHERE id=$1 AND estado NOT IN ('estudado','dominado')", sid)
                await conn.execute("UPDATE mentor.plano_item pi SET feito=TRUE FROM mentor.semana s WHERE pi.semana_id=s.id AND s.status='aberta' AND pi.assunto_id=$1", sid)
                if not await conn.fetchval("SELECT 1 FROM mentor.revisao WHERE assunto_id=$1 AND feita IS NULL", sid):
                    await _agendar_revisao(conn, sid, "R1", INTERVALO["R1"], hoje)
            return {"ok": True, "assunto_fechou": faltam == 0}
        if tipo == "revisao_feita":
            rid = int(dados["revisao_id"])
            r = _r(await conn.fetchrow("SELECT * FROM mentor.revisao WHERE id=$1", rid))
            if not r or r["feita"]:
                return {"ok": False}
            acerto = float(dados.get("acerto") if dados.get("acerto") is not None else 100)
            await conn.execute("UPDATE mentor.revisao SET feita=now(), acerto=$2 WHERE id=$1", rid, acerto)
            prox, dias = _proxima_revisao(r["tipo"], acerto)
            await _agendar_revisao(conn, r["assunto_id"], prox, dias, hoje)
            await _add_xp(conn, "revisao", XP["revisao"], {"revisao_id": rid})
            return {"ok": True, "proxima": prox}
        if tipo == "simulado":
            ids = [int(x) for x in dados.get("questoes") or []]
            certas = int(dados.get("certas") or 0)
            n = len(ids) or int(dados.get("n") or 0) or 1
            nota = float(dados.get("pontos") or 0)
            sid = await conn.fetchval("INSERT INTO mentor.simulado (numero, data, tipo, questoes, respostas, inicio, fim, certas, nota, por_materia, status) VALUES ((SELECT COALESCE(max(numero),0)+1 FROM mentor.simulado), $1, 'montado', $2, $3, now(), now(), $4, $5, $6, 'entregue') RETURNING id",
                                      hoje, ids, json.dumps(dados.get("respostas") or {}), certas, nota, json.dumps(dados.get("por_materia") or {}))
            for qid, marc in (dados.get("respostas") or {}).items():
                try:
                    g = await conn.fetchval("SELECT gabarito FROM mentor.questao WHERE id=$1", int(qid))
                    await conn.execute("INSERT INTO mentor.resposta (questao_id, contexto, marcada, correta) VALUES ($1,'simulado',$2,$3)", int(qid), marc, (marc or "").upper() == (g or "").upper())
                except Exception:
                    pass
            await _add_xp(conn, "simulado", XP["simulado"] + (XP["simulado_acima_60"] if nota >= 60 else 0), {"simulado_id": sid, "nota": nota})
            return {"ok": True, "simulado_id": sid}
    raise HTTPException(400, "evento desconhecido")


# =====================================================================
# "Não é desta aula" — o aluno corrige o vínculo; vale na hora e no afinador
# =====================================================================
@router.post("/vinculo/feedback")
async def vinculo_feedback(questao_id: int = Body(...), aula_id: int = Body(...), ok: bool = Body(False), contexto: str = Body("gate"),
                           excluir: list = Body(default_factory=list)):
    async with db() as conn:
        async with conn.transaction():
            await conn.execute("""CREATE TABLE IF NOT EXISTS mentor.vinculo_feedback (questao_id BIGINT NOT NULL, aula_id BIGINT NOT NULL, ok BOOLEAN NOT NULL,
                                  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (questao_id, aula_id))""")
            await conn.execute("""INSERT INTO mentor.vinculo_feedback (questao_id, aula_id, ok) VALUES ($1,$2,$3)
                                  ON CONFLICT (questao_id, aula_id) DO UPDATE SET ok=EXCLUDED.ok, criado_em=now()""", questao_id, aula_id, ok)
            # efeito imediato: a questão deixa de ser "desta aula" (ou volta a ser)
            tem_col = await conn.fetchval("SELECT 1 FROM information_schema.columns WHERE table_schema='mentor' AND table_name='questao_aula' AND column_name='melhor_mat'")
            if tem_col:
                await conn.execute("UPDATE mentor.questao_aula SET melhor=$3, melhor_mat=$3 WHERE questao_id=$1 AND aula_id=$2", questao_id, aula_id, ok)
            if not ok:
                await conn.execute("UPDATE mentor.questao_aula SET forca = LEAST(forca, 0.1) WHERE questao_id=$1 AND aula_id=$2", questao_id, aula_id)
        reposicao = None
        if not ok:
            novas = await _questoes_da_aula(conn, aula_id, 1, contexto, excluir=[int(x) for x in (excluir or []) if str(x).lstrip('-').isdigit()] + [questao_id])
            reposicao = novas[0] if novas else None
    return {"ok": True, "reposicao": reposicao}
