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
  cards (v3.9)  GET  /mentor/cards/fila?modo=hoje|fechamento   fila do FSRS: reaprendendo/aprendendo vencidos, revisão vencida, novos
                POST /mentor/card/{id}/responder {nota 1..4}   ou {correta, confianca, marcada} nos cards de questão
                POST /mentor/card {frente, verso, assunto_id?, aula_id?}   card livre
                POST /mentor/card/{id}/editar {frente?, verso?, ativo?}
                GET  /mentor/cards/resumo                      estados, vencimentos dos próximos 7 dias, retenção

v3.10 — AS REGRAS DO VALTER: a aula abre por questão (aquecimento → resumo → 5 de concurso; 4/5 fecha SEM vídeo; abaixo disso o
vídeo é o plano B, com fixação e 5 questões novas). Questões sobem numa escada de dificuldade (índice de acerto do Gran) e as
comentadas vêm primeiro. Chute certo vira card. Cards automáticos só em lacuna de lei seca; Gran e lacunas do resumo por botão
(POST /mentor/aula/{id}/gerar-cards). Fila de cards por matéria. Retenção 94% na reta final pra questão e lei seca. O dia sai
em ciclo: 2 matérias (3 nos dias longos), meio a meio, a que ficou mais tempo sem ver primeiro; dia perdido só empurra.
Depois de 3 semanas, matéria em modo questão abaixo de 60% nas revisões ganha reforço no plano.

v3.9 — REVISÃO À LA ANKI: os cards saem do Leitner e entram no FSRS-6 (porte da py-fsrs 6.3.2, sem dependência): quatro notas
(Errei / Difícil / Lembrei / Fácil), Errei volta em 10 min na mesma sessão, intervalo agendado pra 90% de retenção, teto pela data
da prova (config "prova_data"), leech com 6 erros (sai da fila e vira linha do caderno). Cada questão errada vira um "card de
questão" (item), que volta no fechamento do dia, D+1 e daí pelo FSRS; 2 acertos com certeza seguidos aposentam. Geração: erro
com frente/verso propostos, card livre, lacunas (cloze) dos negritos do resumo e dos artigos/prazos da lei seca, flashcards do
Gran só quando o gate veio ≤4/5 ou o assunto é lei seca. Tudo em mentor.card (colunas novas) + mentor.card_rev (histórico).
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
import hashlib
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from database import db

log = logging.getLogger("mentor")
router = APIRouter(prefix="/mentor", tags=["Mentor"])
VERSAO = "3.10-valter"

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
PROVA_DATA_PADRAO = date(2027, 1, 31)          # config "prova_data" sobrepõe (edital novo)
FSRS_PADRAO = {"retencao": 0.9, "retencao_reta": 0.94, "passos_aprendendo_min": [10], "passos_reaprendendo_min": [10], "teto_dia": 60, "novos_dia": 30,
               "leech": 6, "fuzz": True, "margem_prova_dias": 3}
# escada de dificuldade (índice de acerto do Gran): começa nas fáceis; ≥80% em 10+ respostas no assunto libera as médias;
# ≥80% em 25+ libera todas; na reta final, todas
ESCADA = {"facil": 70, "medio": 50, "todas": 0}
REFORCO = {"dias_min": 21, "acerto_max": 60, "n_min": 10, "fatia": 0.10}   # config "fsrs" sobrepõe chave a chave
TIPOS_CARD = ("lei_seca", "literalidade", "contraste", "bizu", "flashcard_gran", "usuario", "questao", "cloze", "erro")
ESTADOS_CARD = ("novo", "aprendendo", "revisao", "reaprendendo", "suspenso", "dominado")
CONTEXTOS_CARD_QUESTAO = ("gate", "reestudo", "R1", "R2", "R3", "R7", "R15", "R30", "M", "semana", "questoes", "simulado", "fixacao")
GRAN_CARDS_MAX, CLOZE_MAX = 12, 8
# =====================================================================
# FSRS-6 (Free Spaced Repetition Scheduler) — porte em Python puro da implementação de referência
# open-spaced-repetition/py-fsrs 6.3.2 (MIT). Sem dependência: as fórmulas estão aqui.
#   R(t,S)  = (1 + FACTOR·t/S)^(-w20)                       retenção prevista t dias depois
#   S0(G)   = w[G-1]                                         estabilidade inicial pela nota
#   D0(G)   = w4 − e^(w5·(G−1)) + 1                          dificuldade inicial
#   D'      = w7·D0(4) + (1−w7)·(D + (10−D)·(−w6·(G−3))/9)   dificuldade (amortecida + reversão à média)
#   S'r     = S·(1 + e^w8·(11−D)·S^−w9·(e^((1−R)·w10) − 1)·hard·easy)   lembrou
#   S'f     = min(w11·D^−w12·((S+1)^w13 − 1)·e^((1−R)·w14), S/e^(w17·w18))  esqueceu
#   S's     = S·max(1, e^(w17·(G−3+w18))·S^−w19) (só o max quando G≥2)      mesmo dia
#   I(r,S)  = S/FACTOR·(r^(1/−w20) − 1)                      intervalo pra retenção alvo r
# Estados: novo → aprendendo (passos curtos) → revisao; errou em revisao → reaprendendo (10 min) → revisao.
# =====================================================================

FSRS_PESOS = (0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835,
              0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542)
FSRS_S_MIN = 0.001
FSRS_D_MIN, FSRS_D_MAX = 1.0, 10.0
FSRS_FUZZ = ({"start": 2.5, "end": 7.0, "factor": 0.15}, {"start": 7.0, "end": 20.0, "factor": 0.1}, {"start": 20.0, "end": math.inf, "factor": 0.05})
NOTA_ERREI, NOTA_DIFICIL, NOTA_LEMBREI, NOTA_FACIL = 1, 2, 3, 4
ESTADOS_FSRS = ("novo", "aprendendo", "revisao", "reaprendendo")


class Fsrs:
    """Agendador. `passos_aprendendo`/`passos_reaprendendo` em segundos. `teto_dias` = intervalo máximo (data da prova)."""

    def __init__(self, pesos=FSRS_PESOS, retencao=0.9, passos_aprendendo=(600,), passos_reaprendendo=(600,), teto_dias=36500, fuzz=True, rnd=None):
        if len(pesos) != 21:
            raise ValueError("FSRS-6 usa 21 pesos")
        self.w = tuple(float(x) for x in pesos)
        self.retencao = float(retencao)
        self.pa = tuple(int(x) for x in passos_aprendendo)
        self.pr = tuple(int(x) for x in passos_reaprendendo)
        self.teto = max(1, int(teto_dias))
        self.fuzz = bool(fuzz)
        self.rnd = rnd or random.random
        self.decay = -self.w[20]
        self.factor = 0.9 ** (1 / self.decay) - 1

    # ---- fórmulas
    def retencao_em(self, estabilidade, dias):
        if not estabilidade:
            return 0.0
        return (1 + self.factor * max(0, dias) / estabilidade) ** self.decay

    def _s0(self, nota):
        return max(self.w[nota - 1], FSRS_S_MIN)

    def _d0(self, nota, limitar=True):
        d = self.w[4] - math.e ** (self.w[5] * (nota - 1)) + 1
        return min(max(d, FSRS_D_MIN), FSRS_D_MAX) if limitar else d

    def _prox_d(self, d, nota):
        delta = -(self.w[6] * (nota - 3))
        d2 = d + (10.0 - d) * delta / 9.0
        d3 = self.w[7] * self._d0(4, limitar=False) + (1 - self.w[7]) * d2
        return min(max(d3, FSRS_D_MIN), FSRS_D_MAX)

    def _s_curto(self, s, nota):
        inc = (math.e ** (self.w[17] * (nota - 3 + self.w[18]))) * (s ** -self.w[19])
        if nota >= NOTA_DIFICIL:
            inc = max(inc, 1.0)
        return max(s * inc, FSRS_S_MIN)

    def _s_lembrou(self, d, s, r, nota):
        hard = self.w[15] if nota == NOTA_DIFICIL else 1
        easy = self.w[16] if nota == NOTA_FACIL else 1
        return s * (1 + (math.e ** self.w[8]) * (11 - d) * (s ** -self.w[9]) * ((math.e ** ((1 - r) * self.w[10])) - 1) * hard * easy)

    def _s_esqueceu(self, d, s, r):
        longo = self.w[11] * (d ** -self.w[12]) * (((s + 1) ** self.w[13]) - 1) * (math.e ** ((1 - r) * self.w[14]))
        curto = s / (math.e ** (self.w[17] * self.w[18]))
        return min(longo, curto)

    def _prox_s(self, d, s, r, nota):
        s2 = self._s_esqueceu(d, s, r) if nota == NOTA_ERREI else self._s_lembrou(d, s, r, nota)
        return max(s2, FSRS_S_MIN)

    def intervalo_dias(self, s):
        i = (s / self.factor) * ((self.retencao ** (1 / self.decay)) - 1)
        return min(max(round(i), 1), self.teto)

    def _fuzz(self, dias):
        if dias < 2.5:
            return dias
        delta = 1.0
        for f in FSRS_FUZZ:
            delta += f["factor"] * max(min(float(dias), f["end"]) - f["start"], 0.0)
        lo, hi = max(2, round(dias - delta)), min(round(dias + delta), self.teto)
        lo = min(lo, hi)
        return min(round(self.rnd() * (hi - lo + 1) + lo), self.teto)

    # ---- o passo
    def responder(self, card, nota, agora):
        """
        card = {"estado", "passo", "estabilidade", "dificuldade", "ultima_rev"}; nota 1..4; agora = datetime tz-aware.
        Devolve dict com estado, passo, estabilidade, dificuldade, ultima_rev, vence_em, intervalo_s, decorrido_dias.
        """
        if nota not in (1, 2, 3, 4):
            raise ValueError("nota 1..4")
        est = card.get("estado") or "novo"
        if est not in ESTADOS_FSRS:
            est = "novo"
        passo = card.get("passo")
        s, d = card.get("estabilidade"), card.get("dificuldade")
        ult = card.get("ultima_rev")
        decorrido = (agora - ult).days if ult else None
        r = self.retencao_em(s, decorrido) if (s and decorrido is not None) else None

        def _rev_pela_s():
            return "revisao", None, timedelta(days=self.intervalo_dias(s))

        if est in ("novo", "aprendendo"):
            passo = 0 if passo is None else passo
            if s is None or d is None:
                s, d = self._s0(nota), self._d0(nota)
            elif decorrido is not None and decorrido < 1:
                s, d = self._s_curto(s, nota), self._prox_d(d, nota)
            else:
                s, d = self._prox_s(d, s, r, nota), self._prox_d(d, nota)
            if len(self.pa) == 0 or (passo >= len(self.pa) and nota >= NOTA_DIFICIL):
                est, passo, iv = _rev_pela_s()
            elif nota == NOTA_ERREI:
                est, passo, iv = "aprendendo", 0, timedelta(seconds=self.pa[0])
            elif nota == NOTA_DIFICIL:
                if passo == 0 and len(self.pa) == 1:
                    iv = timedelta(seconds=self.pa[0] * 1.5)
                elif passo == 0 and len(self.pa) >= 2:
                    iv = timedelta(seconds=(self.pa[0] + self.pa[1]) / 2.0)
                else:
                    iv = timedelta(seconds=self.pa[passo])
                est = "aprendendo"
            elif nota == NOTA_LEMBREI:
                if passo + 1 == len(self.pa):
                    est, passo, iv = _rev_pela_s()
                else:
                    passo += 1
                    est, iv = "aprendendo", timedelta(seconds=self.pa[passo])
            else:
                est, passo, iv = _rev_pela_s()
        elif est == "revisao":
            if decorrido is not None and decorrido < 1:
                s = self._s_curto(s, nota)
            else:
                s = self._prox_s(d, s, r if r is not None else self.retencao_em(s, 0), nota)
            d = self._prox_d(d, nota)
            if nota == NOTA_ERREI:
                if len(self.pr) == 0:
                    est, passo, iv = _rev_pela_s()
                else:
                    est, passo, iv = "reaprendendo", 0, timedelta(seconds=self.pr[0])
            else:
                est, passo, iv = _rev_pela_s()
        else:  # reaprendendo
            passo = 0 if passo is None else passo
            if decorrido is not None and decorrido < 1:
                s, d = self._s_curto(s, nota), self._prox_d(d, nota)
            else:
                s, d = self._prox_s(d, s, r if r is not None else self.retencao_em(s, 0), nota), self._prox_d(d, nota)
            if len(self.pr) == 0 or (passo >= len(self.pr) and nota >= NOTA_DIFICIL):
                est, passo, iv = _rev_pela_s()
            elif nota == NOTA_ERREI:
                est, passo, iv = "reaprendendo", 0, timedelta(seconds=self.pr[0])
            elif nota == NOTA_DIFICIL:
                if passo == 0 and len(self.pr) == 1:
                    iv = timedelta(seconds=self.pr[0] * 1.5)
                elif passo == 0 and len(self.pr) >= 2:
                    iv = timedelta(seconds=(self.pr[0] + self.pr[1]) / 2.0)
                else:
                    iv = timedelta(seconds=self.pr[passo])
                est = "reaprendendo"
            elif nota == NOTA_LEMBREI:
                if passo + 1 == len(self.pr):
                    est, passo, iv = _rev_pela_s()
                else:
                    passo += 1
                    est, iv = "reaprendendo", timedelta(seconds=self.pr[passo])
            else:
                est, passo, iv = _rev_pela_s()

        if self.fuzz and est == "revisao":
            iv = timedelta(days=self._fuzz(iv.days))
        return {"estado": est, "passo": passo, "estabilidade": s, "dificuldade": d, "ultima_rev": agora, "vence_em": agora + iv,
                "intervalo_s": int(iv.total_seconds()), "decorrido_dias": decorrido, "retencao_antes": r}

    def previsao(self, card, agora):
        """Intervalo previsto pra cada nota, sem gravar (pra mostrar nos botões)."""
        out = {}
        for n in (1, 2, 3, 4):
            f = Fsrs(self.w, self.retencao, self.pa, self.pr, self.teto, fuzz=False)
            out[n] = f.responder(dict(card), n, agora)["intervalo_s"]
        return out

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
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v          # driver já decodificou um JSON string (ex.: "2027-01-31")
    return v


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


# =====================================================================
# v3.9 — CARDS NO FSRS: relógio, DDL, geração, fila
# =====================================================================
_TZ = datetime.now().astimezone().tzinfo


def _agora(param=None):
    """Agora, tz-aware (UTC). Só no harness (MENTOR_TESTE=1) o parâmetro `agora` (ISO) sobrepõe o relógio."""
    if param and os.environ.get("MENTOR_TESTE") == "1":
        try:
            dt = datetime.fromisoformat(str(param).strip().replace(" ", "+").replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _dia_local(dt):
    """Dia de estudo (04:00 às 04:00) de um instante — mesma régua de _hoje()."""
    return (dt.astimezone(_TZ).replace(tzinfo=None) - timedelta(hours=4)).date()


def _inicio_dia(d):
    """04:00 local do dia d, tz-aware."""
    return datetime.combine(d, datetime.min.time()).replace(hour=4, tzinfo=_TZ)


_FSRS_OK = False


async def _garantir_fsrs(conn):
    """Colunas/tabela do FSRS + migração única dos cards de Leitner. Idempotente."""
    global _FSRS_OK
    if _FSRS_OK:
        await _migrar_leitner(conn)   # barato quando não há nada a migrar; cobre card gravado por código antigo
        return
    await _garantir_caderno(conn)
    for sql in ("ALTER TABLE mentor.card DROP CONSTRAINT IF EXISTS card_tipo_check",
                "ALTER TABLE mentor.card ADD CONSTRAINT card_tipo_check CHECK (tipo IN ('lei_seca','literalidade','contraste','bizu','flashcard_gran','usuario','questao','cloze','erro'))",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'novo'",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS passo INT",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS estabilidade DOUBLE PRECISION",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS dificuldade DOUBLE PRECISION",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS vence_em TIMESTAMPTZ",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS ultima_rev TIMESTAMPTZ",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS lapsos INT NOT NULL DEFAULT 0",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS reps INT NOT NULL DEFAULT 0",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS certezas_seguidas INT NOT NULL DEFAULT 0",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS questao_id BIGINT REFERENCES mentor.questao(id) ON DELETE CASCADE",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS hash TEXT",
                "ALTER TABLE mentor.card ADD COLUMN IF NOT EXISTS suspenso_em TIMESTAMPTZ",
                "CREATE INDEX IF NOT EXISTS ix_card_vence ON mentor.card(vence_em) WHERE ativo",
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_card_questao ON mentor.card(questao_id) WHERE tipo='questao' AND ativo",
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_card_hash ON mentor.card(hash) WHERE hash IS NOT NULL AND ativo",
                """CREATE TABLE IF NOT EXISTS mentor.card_rev (id BIGSERIAL PRIMARY KEY, card_id BIGINT NOT NULL REFERENCES mentor.card(id) ON DELETE CASCADE,
                   data TIMESTAMPTZ NOT NULL DEFAULT now(), nota SMALLINT NOT NULL, estado_antes TEXT, estado_depois TEXT, estab_antes DOUBLE PRECISION, dif_antes DOUBLE PRECISION,
                   estab_depois DOUBLE PRECISION, dif_depois DOUBLE PRECISION, intervalo_s BIGINT, decorrido_dias INT, retencao_antes DOUBLE PRECISION,
                   sessao_id BIGINT, contexto TEXT)""",
                "CREATE INDEX IF NOT EXISTS ix_card_rev_card ON mentor.card_rev(card_id)",
                "CREATE INDEX IF NOT EXISTS ix_card_rev_data ON mentor.card_rev(data)",
                # v3.x já agendava revisão R2 mas o CHECK de resposta não aceitava o contexto → 500 ao responder a R2. Corrigido aqui.
                "ALTER TABLE mentor.resposta DROP CONSTRAINT IF EXISTS resposta_contexto_check",
                "ALTER TABLE mentor.resposta ADD CONSTRAINT resposta_contexto_check CHECK (contexto IN ('aquecimento','fixacao','gate','reestudo','R1','R2','R3','R7','R15','R30','M','semana','questoes','simulado','bolso','card'))"):
        await conn.execute(sql)
    await _migrar_leitner(conn)
    _FSRS_OK = True


async def _migrar_leitner(conn):
    """Card sem vence_em (Leitner ou gravado por código antigo) → estado/estabilidade/dificuldade/vence_em. Idempotente."""
    f = Fsrs(fuzz=False)
    n = 0
    for c in await conn.fetch("SELECT id, caixa, acertos, erros, proxima FROM mentor.card WHERE ativo AND vence_em IS NULL"):
        visto = (c["caixa"] or 0) > 0 or (c["acertos"] or 0) > 0 or (c["erros"] or 0) > 0
        est = "revisao" if visto else "novo"
        s = float([1, 3, 7, 15, 30][min(max(c["caixa"] or 0, 0), 4)]) if visto else None
        d = min(10.0, f._d0(3) + (c["erros"] or 0)) if visto else None
        vence = _inicio_dia(c["proxima"] or _hoje())
        await conn.execute("UPDATE mentor.card SET estado=$2, estabilidade=$3, dificuldade=$4, vence_em=$5, lapsos=$6, reps=$7 WHERE id=$1",
                           c["id"], est, s, d, vence, int(c["erros"] or 0), int((c["acertos"] or 0) + (c["erros"] or 0)))
        n += 1
    return n


async def _fsrs_cfg(conn, agora=None):
    """Agendador configurado: pesos/retenção/passos da config "fsrs" e teto pela data da prova."""
    cfg = dict(FSRS_PADRAO)
    cfg.update(await _config(conn, "fsrs", {}) or {})
    prova = await _config(conn, "prova_data", None)
    try:
        prova = date.fromisoformat(prova) if prova else PROVA_DATA_PADRAO
    except Exception:
        prova = PROVA_DATA_PADRAO
    hoje = _dia_local(agora or _agora())
    teto = max(1, (prova - hoje).days - int(cfg.get("margem_prova_dias") or 0))
    f = Fsrs(pesos=cfg.get("pesos") or FSRS_PESOS, retencao=float(cfg.get("retencao") or 0.9),
             passos_aprendendo=[int(m) * 60 for m in (cfg.get("passos_aprendendo_min") or [10])],
             passos_reaprendendo=[int(m) * 60 for m in (cfg.get("passos_reaprendendo_min") or [10])],
             teto_dias=teto, fuzz=bool(cfg.get("fuzz", True)))
    cfg["prova_data"] = prova.isoformat()
    cfg["teto_dias"] = teto
    return f, cfg


def _curto_ml(t, n=600):
    """Como _curto, mas mantém as quebras de linha (frente/verso do card)."""
    t = re.sub(r"[ \t]+", " ", str(t or "")).strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def _fsrs_do_card(f, cfg, c, agora):
    """Reta final: questão e lei seca com retenção 94% (cfg retencao_reta); o resto segue a retenção geral."""
    lei = c.get("tipo") in ("questao", "lei_seca") or (c.get("fonte") or "") == "lacuna da lei seca"
    if lei and _dia_local(agora) >= RETA_FINAL:
        r = float(cfg.get("retencao_reta") or 0.94)
        if abs(r - f.retencao) > 1e-9:
            return Fsrs(f.w, r, f.pa, f.pr, f.teto, f.fuzz, f.rnd)
    return f


def _card_hash(tipo, frente, verso):
    base = re.sub(r"\s+", " ", f"{tipo}|{frente}|{verso}".lower()).strip()
    return hashlib.md5(base.encode("utf-8")).hexdigest()


async def _card_criar(conn, *, assunto_id, aula_id, tipo, frente, verso, fonte, questao_id=None, vence_em=None, agora=None):
    """Cria um card (estado novo). Devolve o id, ou None se já existe um igual (hash) ou tipo inválido."""
    if tipo not in TIPOS_CARD or not frente or not verso:
        return None
    frente, verso = _curto_ml(frente, 600), _curto_ml(verso, 1200)
    h = _card_hash(tipo, frente, verso)
    vence = vence_em or (agora or _agora())
    try:
        return await conn.fetchval("""INSERT INTO mentor.card (assunto_id, aula_id, tipo, frente, verso, fonte, proxima, questao_id, hash, estado, vence_em)
                                      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'novo',$10) ON CONFLICT DO NOTHING RETURNING id""",
                                   assunto_id, aula_id, tipo, frente, verso, fonte, _dia_local(vence), questao_id, h, vence)
    except Exception as e:
        log.warning("card_criar: %s", e)
        return None


_RE_NEGRITO = re.compile(r"\*\*(.+?)\*\*")
_RE_ART = re.compile(r"\b(art(?:igo|\.)?\s*\d+[º°]?(?:-[A-Z])?)", re.I)
_RE_PRAZO = re.compile(r"\b(\d+\s*(?:\([a-zç]+\)\s*)?(?:dias?|horas?|meses|anos?|vezes|salários?[- ]mínimos?|%))", re.I)


def _cloze_de_texto(texto, lei_seca=False, maximo=CLOZE_MAX):
    """Lacunas: cada linha com **negrito** vira um card (frente com {{c::…}}, verso = linha inteira). Na lei seca, artigos e prazos também."""
    out = []
    vistos = set()
    for linha in re.split(r"[\r\n]+", texto or ""):
        l = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", linha).strip()
        l = re.sub(r"^#+\s*", "", l)
        if len(l) < 25 or len(l) > 400:
            continue
        alvos = [m.group(1).strip() for m in _RE_NEGRITO.finditer(l)]
        limpa = _RE_NEGRITO.sub(lambda m: m.group(1), l)
        if lei_seca and not alvos:
            alvos = [m.group(1) for m in _RE_ART.finditer(limpa)][:1] + [m.group(1) for m in _RE_PRAZO.finditer(limpa)][:1]
        alvos = [a for a in alvos if 2 <= len(a) <= 80 and len(a) < len(limpa) * 0.7]
        if not alvos:
            continue
        frente = limpa
        for a in alvos[:2]:
            frente = frente.replace(a, "{{c::" + a + "}}", 1)
        if frente == limpa:
            continue
        k = frente.lower()
        if k in vistos:
            continue
        vistos.add(k)
        out.append((frente, limpa))
        if len(out) >= maximo:
            break
    return out


async def _cards_cloze_aula(conn, aula_id, agora=None, vence_em=None):
    a = _r(await conn.fetchrow("""SELECT a.assunto_id, COALESCE(a.resumo_bolso, a.resumo, a.mapa_mental) AS txt, s.modo, a.lei
                                  FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE a.id=$1""", aula_id))
    if not a:
        return 0
    lei = (a.get("modo") == "lei_seca")
    pares = _cloze_de_texto(a.get("txt") or "", lei_seca=lei)
    if lei and a.get("lei"):
        pares += _cloze_de_texto(a["lei"], lei_seca=True, maximo=max(0, CLOZE_MAX - len(pares)))
    n = 0
    amanha = vence_em or _inicio_dia(_dia_local(agora or _agora()) + timedelta(days=1))
    for frente, verso in pares[:CLOZE_MAX]:
        if await _card_criar(conn, assunto_id=a["assunto_id"], aula_id=aula_id, tipo="cloze", frente=frente, verso=verso, fonte="lacuna do resumo" if not lei else "lacuna da lei seca", vence_em=amanha, agora=agora):
            n += 1
    return n


async def _card_questao(conn, questao_id, aula_id=None, assunto_id=None, agora=None, contexto=None, sessao_id=None, nota=1):
    """Questão errada → card de questão (item). Já existe ativo: errou de novo → nota 1 no FSRS. Dominado: reativa."""
    agora = agora or _agora()
    c = _r(await conn.fetchrow("SELECT * FROM mentor.card WHERE questao_id=$1 AND tipo='questao' AND ativo", questao_id))
    if c and c["estado"] == "suspenso":
        return c["id"]
    if c and c["estado"] == "dominado":
        await conn.execute("UPDATE mentor.card SET estado='revisao', certezas_seguidas=0, vence_em=$2 WHERE id=$1", c["id"], agora)
        return c["id"]
    if c:
        if c["estado"] != "novo":
            f, cfg = await _fsrs_cfg(conn, agora)
            await _fsrs_aplicar(conn, c, int(nota or 1), agora, f, cfg, contexto=contexto or "resposta", sessao_id=sessao_id, gravar_resposta=False)
        else:
            await conn.execute("UPDATE mentor.card SET vence_em=LEAST(vence_em, $2) WHERE id=$1", c["id"], agora)
        return c["id"]
    q = _r(await conn.fetchrow("SELECT id, enunciado, alternativas, gabarito, comentario_professor FROM mentor.questao WHERE id=$1", questao_id))
    if not q:
        return None
    if assunto_id is None:
        assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.questao_aula WHERE questao_id=$1 ORDER BY melhor_mat DESC, melhor DESC, forca DESC NULLS LAST LIMIT 1", questao_id)
    if aula_id is None:
        aula_id = await conn.fetchval("SELECT aula_id FROM mentor.questao_aula WHERE questao_id=$1 ORDER BY melhor_mat DESC, melhor DESC, forca DESC NULLS LAST LIMIT 1", questao_id)
    alts = q["alternativas"] or []
    if isinstance(alts, str):
        try:
            alts = json.loads(alts)
        except Exception:
            alts = []
    gab = (q["gabarito"] or "").strip().upper()
    txt_gab = next((x.get("texto") for x in alts if isinstance(x, dict) and (x.get("letra") or "").upper() == gab), None)
    verso = f"Gabarito: {gab}" + (f") {_curto(txt_gab, 300)}" if txt_gab else "")
    com = _comentario(q["comentario_professor"])
    if com:
        verso += "\n" + _curto(com, 500)
    return await conn.fetchval("""INSERT INTO mentor.card (assunto_id, aula_id, tipo, frente, verso, fonte, proxima, questao_id, estado, vence_em)
                                  VALUES ($1,$2,'questao',$3,$4,'questão errada',$5,$6,'novo',$7) ON CONFLICT DO NOTHING RETURNING id""",
                               assunto_id, aula_id, _curto(q["enunciado"], 600), verso, _dia_local(agora), questao_id, agora)


def _nota_da_resposta(correta, confianca):
    """Card de questão: a nota sai da resposta. Errou = 1; certo no chute = 2; certo com dúvida = 3; certo com certeza = 4."""
    if not correta:
        return 1
    return {"chute": 2, "duvida": 3, "certeza": 4}.get((confianca or "duvida"), 3)


async def _fsrs_aplicar(conn, c, nota, agora, f, cfg, *, contexto="card", sessao_id=None, gravar_resposta=False, marcada=None, correta=None, confianca=None):
    """Aplica uma nota a um card: FSRS + histórico + leech + dominado + XP. Devolve o resultado pro painel."""
    antes = {"estado": c["estado"], "passo": c["passo"], "estabilidade": c["estabilidade"], "dificuldade": c["dificuldade"], "ultima_rev": c["ultima_rev"]}
    f = _fsrs_do_card(f, cfg, c, agora)
    r = f.responder(antes, nota, agora)
    estado = r["estado"]
    vence = r["vence_em"]
    if estado == "revisao":
        # revisão é por dia: vence às 04:00 do dia, como o Anki
        dias = max(1, round(r["intervalo_s"] / 86400))
        vence = _inicio_dia(_dia_local(agora) + timedelta(days=dias))
    lapsos = int(c["lapsos"] or 0) + (1 if (nota == 1 and antes["estado"] in ("revisao", "reaprendendo")) else 0)
    certezas = (int(c["certezas_seguidas"] or 0) + 1) if nota == 4 else 0
    leech = False
    dominado = False
    if lapsos >= int(cfg.get("leech") or 6) and nota == 1:
        estado, leech = "suspenso", True
    elif c["tipo"] == "questao" and certezas >= 2 and estado == "revisao":
        estado, dominado = "dominado", True
    await conn.execute("""UPDATE mentor.card SET estado=$2, passo=$3, estabilidade=$4, dificuldade=$5, ultima_rev=$6, vence_em=$7, proxima=$8,
                          lapsos=$9, reps=reps+1, certezas_seguidas=$10, acertos=acertos+$11, erros=erros+$12, suspenso_em=CASE WHEN $13 THEN $6 ELSE suspenso_em END WHERE id=$1""",
                       c["id"], estado, r["passo"], r["estabilidade"], r["dificuldade"], agora, vence, _dia_local(vence), lapsos, certezas,
                       int(nota >= 2), int(nota == 1), leech)
    await conn.execute("""INSERT INTO mentor.card_rev (card_id, data, nota, estado_antes, estado_depois, estab_antes, dif_antes, estab_depois, dif_depois, intervalo_s, decorrido_dias, retencao_antes, sessao_id, contexto)
                          VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
                       c["id"], agora, nota, antes["estado"], estado, antes["estabilidade"], antes["dificuldade"], r["estabilidade"], r["dificuldade"],
                       int((vence - agora).total_seconds()), r["decorrido_dias"], r["retencao_antes"], sessao_id, contexto)
    if gravar_resposta and c.get("questao_id"):
        await conn.execute("""INSERT INTO mentor.resposta (questao_id, sessao_id, aula_id, assunto_id, contexto, marcada, correta, confianca)
                              VALUES ($1,$2,$3,$4,'card',$5,$6,$7)""", c["questao_id"], sessao_id, c.get("aula_id"), c.get("assunto_id"), marcada, correta, confianca)
    xp = None
    if nota >= 3:
        xp = await _add_xp(conn, "card", XP["card"], {"card_id": c["id"], "nota": nota})
    if leech:
        await _garantir_caderno(conn)
        mid = await conn.fetchval("SELECT materia_id FROM mentor.assunto WHERE id=$1", c["assunto_id"]) if c.get("assunto_id") else None
        await conn.execute("""INSERT INTO mentor.caderno (questao_id, aula_id, assunto_id, materia_id, card_id, tipo, linha, enunciado, contexto)
                              VALUES ($1,$2,$3,$4,$5,'regra',$6,$7,'leech')""", c.get("questao_id"), c.get("aula_id"), c.get("assunto_id"), mid, c["id"],
                           _curto("LEECH (errou " + str(lapsos) + "x): reestudar — " + re.sub(r"\{\{c::(.+?)\}\}", r"\1", c["frente"]), 240), _curto(c["verso"], 240))
    return {"card_id": c["id"], "nota": nota, "estado": estado, "estado_antes": antes["estado"], "vence_em": vence.isoformat(), "intervalo_s": int((vence - agora).total_seconds()),
            "estabilidade": round(r["estabilidade"], 3), "dificuldade": round(r["dificuldade"], 2), "lapsos": lapsos, "leech": leech, "dominado": dominado, "xp": xp,
            "volta_hoje": vence <= _inicio_dia(_dia_local(agora) + timedelta(days=1))}


def _card_item(c, f, agora, cfg=None):
    """Linha da fila pro painel: campos do card + intervalo previsto por nota (texto dos botões)."""
    it = {k: c.get(k) for k in ("id", "tipo", "frente", "verso", "fonte", "estado", "passo", "lapsos", "reps", "questao_id", "assunto_id", "aula_id", "materia", "assunto", "aula")}
    it["cloze"] = bool(c.get("tipo") == "cloze" or "{{c::" in (c.get("frente") or ""))
    if cfg is not None:
        f = _fsrs_do_card(f, cfg, c, agora)
    it["vence_em"] = c["vence_em"].isoformat() if c.get("vence_em") else None
    it["retencao"] = round(f.retencao_em(c["estabilidade"], max(0, (agora - c["ultima_rev"]).days)), 3) if c.get("estabilidade") and c.get("ultima_rev") else None
    try:
        it["previsao"] = f.previsao({"estado": c["estado"], "passo": c["passo"], "estabilidade": c["estabilidade"], "dificuldade": c["dificuldade"], "ultima_rev": c["ultima_rev"]}, agora)
    except Exception:
        it["previsao"] = {}
    if c.get("q_id"):
        it["questao"] = _q_proto({"id": c["q_id"], "enunciado": c["q_enunciado"], "alternativas": c["q_alternativas"], "gabarito": c["q_gabarito"], "banca_sigla": c["q_banca"],
                                  "ano": c["q_ano"], "orgao_sigla": c["q_orgao"], "cargo": c["q_cargo"], "indice_acerto": c["q_pct"], "comentario_professor": c["q_comentario"], "soldado_ba": c["q_soldado"]},
                                 c.get("materia"), c.get("assunto"))
    return it


_SQL_CARD_FILA = """SELECT c.*, s.nome AS assunto, m.nome AS materia, a.titulo AS aula, q.id AS q_id, q.enunciado AS q_enunciado, q.alternativas AS q_alternativas, q.gabarito AS q_gabarito,
                           q.banca_sigla AS q_banca, q.ano AS q_ano, q.orgao_sigla AS q_orgao, q.cargo AS q_cargo, q.indice_acerto AS q_pct, q.comentario_professor AS q_comentario, q.soldado_ba AS q_soldado
                    FROM mentor.card c LEFT JOIN mentor.assunto s ON s.id=c.assunto_id LEFT JOIN mentor.materia m ON m.id=s.materia_id LEFT JOIN mentor.aula a ON a.id=c.aula_id
                    LEFT JOIN mentor.questao q ON q.id=c.questao_id AND c.tipo='questao'
                    WHERE c.ativo AND c.estado NOT IN ('suspenso','dominado') """


async def _fila_cards(conn, agora, modo="hoje", f=None, cfg=None, materia_id=None):
    """
    Fila do dia: (1) aprendendo/reaprendendo já vencidos, (2) revisão vencida (mais atrasado primeiro), (3) novos (até novos_dia).
    Teto do dia desconta o que já foi respondido hoje. modo=fechamento: só os erros de hoje e os passos curtos vencidos.
    """
    if f is None or cfg is None:
        f, cfg = await _fsrs_cfg(conn, agora)
    await _migrar_leitner(conn)
    hoje = _dia_local(agora)
    ini, fim = _inicio_dia(hoje), _inicio_dia(hoje + timedelta(days=1))
    feitos_hoje = int(await conn.fetchval("SELECT count(DISTINCT card_id) FROM mentor.card_rev WHERE data >= $1 AND data < $2", ini, fim) or 0)
    novos_hoje = int(await conn.fetchval("SELECT count(DISTINCT card_id) FROM mentor.card_rev WHERE data >= $1 AND data < $2 AND estado_antes='novo'", ini, fim) or 0)
    teto = max(0, int(cfg.get("teto_dia") or 60) - feitos_hoje)
    # (1) passos curtos vencidos, (2) erros de hoje (questão errada ainda nova), (3) revisão vencida, (4) novos
    base_sql = _SQL_CARD_FILA + (f"AND s.materia_id = {int(materia_id)} " if materia_id else "")
    curtos = _rs(await conn.fetch(base_sql + "AND c.estado IN ('aprendendo','reaprendendo') AND c.vence_em <= $1 ORDER BY c.vence_em", agora))
    erros = _rs(await conn.fetch(base_sql + "AND c.estado='novo' AND c.tipo='questao' AND c.vence_em <= $1 ORDER BY c.id LIMIT 200", agora))
    vencidos, novos = [], []
    if modo != "fechamento":
        vencidos = _rs(await conn.fetch(base_sql + "AND c.estado='revisao' AND c.vence_em <= $1 ORDER BY c.vence_em, random() LIMIT $2", agora, max(0, teto - len(curtos) - len(erros))))
        lim = max(0, min(int(cfg.get("novos_dia") or 30) - novos_hoje, teto - len(curtos) - len(erros) - len(vencidos)))
        if lim:
            # novos: os do caderno/erro primeiro, depois lacunas, por fim Gran
            novos = _rs(await conn.fetch(base_sql + """AND c.estado='novo' AND c.tipo <> 'questao' AND c.vence_em <= $1
                                          ORDER BY CASE c.tipo WHEN 'erro' THEN 0 WHEN 'usuario' THEN 0 WHEN 'contraste' THEN 0 WHEN 'cloze' THEN 1 ELSE 2 END, c.vence_em, c.id LIMIT $2""", agora, lim))
    prox = await conn.fetchval("SELECT min(vence_em) FROM mentor.card WHERE ativo AND estado IN ('aprendendo','reaprendendo') AND vence_em > $1", agora)
    tot = _r(await conn.fetchrow("""SELECT count(*) FILTER (WHERE estado IN ('aprendendo','reaprendendo') AND vence_em <= $1) AS curtos,
                                           count(*) FILTER (WHERE estado='revisao' AND vence_em <= $1) AS vencidos,
                                           count(*) FILTER (WHERE estado='novo' AND tipo <> 'questao' AND vence_em <= $1) AS novos,
                                           count(*) FILTER (WHERE estado='novo' AND tipo='questao' AND vence_em <= $1) AS erros_hoje,
                                           count(*) FILTER (WHERE estado IN ('aprendendo','reaprendendo') AND vence_em > $1 AND vence_em < $2) AS voltam_hoje
                                    FROM mentor.card WHERE ativo AND estado NOT IN ('suspenso','dominado')""", agora, fim))
    # baralhos (Valter): os de 10 min na frente; o resto agrupado por matéria, na ordem em que cada matéria aparece
    resto = erros + vencidos + novos
    ordem_mat = {}
    for c in resto:
        ordem_mat.setdefault(c.get("materia") or "—", len(ordem_mat))
    resto.sort(key=lambda c: ordem_mat[c.get("materia") or "—"])
    lista = [_card_item(c, f, agora, cfg) for c in curtos + resto]
    por_mat = _rs(await conn.fetch("""SELECT m.id, m.nome, count(*) AS n FROM mentor.card c JOIN mentor.assunto s ON s.id=c.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                                      WHERE c.ativo AND c.estado NOT IN ('suspenso','dominado') AND c.vence_em <= $1 GROUP BY m.id, m.nome ORDER BY 3 DESC""", agora))
    # sequência de fila zerada: o dia conta quando nada ficou pra agora (10 min, erros de hoje e revisão vencida)
    # conta só se você revisou alguma coisa hoje e não sobrou nada; se aparecer erro novo depois, o dia volta a ficar em aberto
    if modo == "hoje" and not materia_id:
        try:
            dias = set(await _config(conn, "cards_zerados", []) or [])
            zerou = not curtos and not erros and not vencidos and feitos_hoje > 0
            h = hoje.isoformat()
            if zerou != (h in dias):
                (dias.add(h) if zerou else dias.discard(h))
                await conn.execute("INSERT INTO mentor.config (chave, valor) VALUES ('cards_zerados', $1::jsonb) ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor, alterado_em=now()",
                                   json.dumps(sorted(dias)[-400:]))
        except Exception as e:
            log.warning("cards_zerados: %s", e)
    return {"agora": agora.isoformat(), "hoje": hoje.isoformat(), "modo": modo, "lista": lista, "totais": {k: int(v or 0) for k, v in tot.items()},
            "feitos_hoje": feitos_hoje, "novos_hoje": novos_hoje, "teto_dia": int(cfg.get("teto_dia") or 60), "novos_dia": int(cfg.get("novos_dia") or 25),
            "proximo_curto_em_s": int((prox - agora).total_seconds()) if prox else None, "retencao_alvo": f.retencao, "teto_dias": cfg.get("teto_dias"), "prova_data": cfg.get("prova_data"),
            "por_materia": [{"id": r["id"], "nome": r["nome"], "n": int(r["n"])} for r in por_mat], "materia_id": materia_id}


async def _semear_cards_aula(conn, aula_id, certas=None, agora=None, gerar=False):
    """
    Quando a aula fecha (uma vez só): lacunas do resumo/lei seca sempre; flashcards do Gran só se o gate veio ≤4/5 ou o assunto é
    lei seca (aula fechada 5/5 não gera card do Gran). Os do Gran entram ordenados pela semelhança com as questões erradas da aula.
    """
    await _garantir_fsrs(conn)
    agora = agora or _agora()
    n_cloze = 0
    # v3.10 (Valter: card que você não fez não te ensina): automático só a lacuna da lei seca; Gran e lacunas do resumo, por botão
    modo_ass = await conn.fetchval("SELECT s.modo FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE a.id=$1", aula_id)
    if not gerar and modo_ass != "lei_seca":
        return 0
    if not await conn.fetchval("SELECT 1 FROM mentor.card WHERE aula_id=$1 AND tipo='cloze' LIMIT 1", aula_id):
        try:
            n_cloze = await _cards_cloze_aula(conn, aula_id, agora)
        except Exception as e:
            log.warning("cloze aula %s: %s", aula_id, e)
    if await conn.fetchval("SELECT 1 FROM mentor.card WHERE aula_id=$1 AND tipo='flashcard_gran' LIMIT 1", aula_id):
        return n_cloze
    a = _r(await conn.fetchrow("SELECT a.assunto_id, a.flashcards, s.modo FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE a.id=$1", aula_id))
    if not a or not a["flashcards"]:
        return n_cloze
    if certas is None:
        certas = await conn.fetchval("SELECT count(*) FILTER (WHERE correta) FROM (SELECT correta FROM mentor.resposta WHERE aula_id=$1 AND contexto='gate' ORDER BY id DESC LIMIT $2) x", aula_id, GATE_N)
        certas = int(certas) if certas is not None else None
    if not gerar:
        return n_cloze
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
    # palavras das questões erradas da aula: o flashcard que fala delas vem primeiro
    erradas = await conn.fetch("SELECT q.enunciado FROM mentor.resposta r JOIN mentor.questao q ON q.id=r.questao_id WHERE r.aula_id=$1 AND r.correta = FALSE", aula_id)
    chave = set()
    for e in erradas:
        chave.update(w for w in re.findall(r"[a-záéíóúâêôãõç]{5,}", (e["enunciado"] or "").lower()))
    pares = []
    for c in cartas:
        if not isinstance(c, dict):
            continue
        fr = c.get("front") or c.get("question") or c.get("frente") or c.get("pergunta") or c.get("term")
        vs = c.get("back") or c.get("answer") or c.get("verso") or c.get("resposta") or c.get("definition")
        if not fr or not vs:
            continue
        pal = set(re.findall(r"[a-záéíóúâêôãõç]{5,}", (str(fr) + " " + str(vs)).lower()))
        pares.append((-len(pal & chave), len(pares), _curto(fr, 500), _curto(vs, 900)))
    pares.sort()
    n = 0
    amanha = _inicio_dia(_dia_local(agora) + timedelta(days=1))
    for _, _, fr, vs in pares[:GRAN_CARDS_MAX]:
        if await _card_criar(conn, assunto_id=a["assunto_id"], aula_id=aula_id, tipo="flashcard_gran", frente=fr, verso=vs, fonte="flashcard do Gran", vence_em=amanha, agora=agora):
            n += 1
    return n + n_cloze


async def _nivel_assunto(conn, assunto_id, hoje=None):
    """Degrau da escada do assunto: facil → medio → todas (reta final: todas)."""
    hoje = hoje or _hoje()
    if hoje >= RETA_FINAL or not assunto_id:
        return "todas" if hoje >= RETA_FINAL else "facil"
    r = await conn.fetchrow("""SELECT count(*) AS n, count(*) FILTER (WHERE correta) AS c FROM mentor.resposta
                               WHERE assunto_id=$1 AND correta IS NOT NULL AND contexto NOT IN ('aquecimento','fixacao','bolso')""", assunto_id)
    n, c = int(r["n"] or 0), int(r["c"] or 0)
    if n >= 25 and c >= 0.8 * n:
        return "todas"
    if n >= 10 and c >= 0.8 * n:
        return "medio"
    return "facil"


def _o_nivel(q, nivel):
    """0 = questão dentro do degrau (índice de acerto ≥ corte); 1 = fora ou sem índice."""
    corte = ESCADA.get(nivel, 0)
    if not corte:
        return 0
    ia = q.get("indice_acerto")
    try:
        return 0 if ia is not None and float(ia) >= corte else 1
    except Exception:
        return 1


def _o_com(q):
    """Questão com comentário do professor primeiro (sem comentário costuma ser prova pequena e mal feita)."""
    v = q.get("comentario_professor")
    return 0 if v and str(v).strip() not in ("", "{}", "null") else 1


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
    nivel = await _nivel_assunto(conn, await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id))
    lst.sort(key=lambda q: (q["o_melhor"], _o_nivel(q, nivel), _o_com(q), q["o_via"], q["o_kw"], q["o_banca"], q["o_sold"], -(q["ano"] or 0), q["rnd"]))
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
    nivel = await _nivel_assunto(conn, assunto_id)
    lst.sort(key=lambda q: (_o_nivel(q, nivel), _o_com(q), 0 if q["banca_sigla"] in BANCAS_PREF else 1, 0 if q["soldado_ba"] else 1))
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


async def _materias_reforco(conn, hoje=None):
    """Valter: primeiro o critério da prova; a SUA dificuldade só depois de rodar o ciclo algumas vezes. Depois de 3 semanas de estudo,
    matéria em modo questão com acerto < 60% nas revisões/questões (10+ respostas, últimas 3 semanas) ganha reforço."""
    hoje = hoje or _hoje()
    prim = await conn.fetchval("SELECT min(data) FROM mentor.resposta")
    if not prim or (hoje - prim.date()).days < REFORCO["dias_min"]:
        return {}
    rows = await conn.fetch("""SELECT s.materia_id AS mid, count(*) AS n, count(*) FILTER (WHERE r.correta) AS c
                               FROM mentor.resposta r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                               WHERE m.fase='questao' AND r.correta IS NOT NULL AND r.data > now() - interval '21 days'
                                 AND r.contexto IN ('R1','R2','R3','R7','R15','R30','M','questoes','card','semana')
                               GROUP BY 1""")
    out = {}
    for r in rows:
        n, c = int(r["n"]), int(r["c"])
        if n >= REFORCO["n_min"] and 100.0 * c / n < REFORCO["acerto_max"]:
            out[r["mid"]] = round(100.0 * c / n, 1)
    return out


def _plano_trilhas(mats, aulas, seg, cap_h, trilhas, pesos, feitas=None, reforco=None):
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
    # reforço: fatia da semana pras matérias fracas (modo questão), antes das trilhas
    if reforco:
        _, g = encher(sorted(reforco, key=lambda m: reforco[m]), cap_h * REFORCO["fatia"], "questao")
        gasto += g
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
            reforco = await _materias_reforco(conn, hoje)
            novos, g2 = _plano_trilhas(mats, aulas, seg, max(0.0, cap_conteudo - gasto), trilhas, pesos, feitas, reforco=reforco)
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
            "itens": len(itens), "aulas": sum(len(it["aulas"]) for it in itens), "horas_previstas": round(gasto, 1), "pendencias_da_anterior": len(pend_set),
            "reforco": {str(k): v for k, v in (reforco or {}).items()}}


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
        # v3.9: errou → a questão vira card (volta no fechamento do dia, D+1 e daí pelo FSRS). Nunca derruba a resposta.
        card_q = None
        chute_certo = bool(correta) and confianca == "chute"
        if (correta is False or chute_certo) and contexto in CONTEXTOS_CARD_QUESTAO:
            try:
                await _garantir_fsrs(conn)
                card_q = await _card_questao(conn, questao_id, aula_id, assunto_id, _agora(), contexto=contexto, sessao_id=sessao_id, nota=2 if chute_certo else 1)
            except Exception as e:
                log.warning("card de questão %s: %s", questao_id, e)
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
    return {"resposta_id": rid, "correta": correta, "gabarito": q["gabarito"], "comentario_professor": _comentario(q["comentario_professor"]), "combo_xp": combo, "caderno": cad, "card_questao_id": card_q}


async def _agendar_revisao(conn, assunto_id, tipo, dias, hoje=None):
    hoje = hoje or _hoje()
    prevista = hoje + timedelta(days=dias)
    await conn.execute("DELETE FROM mentor.revisao WHERE assunto_id=$1 AND feita IS NULL", assunto_id)
    await conn.execute("""INSERT INTO mentor.revisao (assunto_id, tipo, prevista) VALUES ($1,$2,$3)
                          ON CONFLICT (assunto_id) WHERE feita IS NULL DO UPDATE SET tipo=EXCLUDED.tipo, prevista=EXCLUDED.prevista, adiada_para=NULL""", assunto_id, tipo, prevista)
    return prevista


async def _questoes_plano_b(conn, aula_id, assunto_id, vistas):
    """
    As 5 da 2ª rodada. Tem que ser 5 (o gate lê as últimas 5 respostas): inéditas da aula → inéditas do assunto →
    repete as do aquecimento (não contaram) → repete as que errou no gate (depois do vídeo, responder de novo vale).
    """
    novas = list(await _questoes_da_aula(conn, aula_id, GATE_N, "gate", excluir=vistas))
    esc = {q["id"] for q in novas}
    if len(novas) < GATE_N:
        for q in await _questoes_do_assunto(conn, assunto_id, GATE_N * 2):
            if q["id"] not in esc and q["id"] not in vistas and len(novas) < GATE_N:
                novas.append(q)
                esc.add(q["id"])
    if len(novas) < GATE_N:
        rows = _rs(await conn.fetch("""SELECT DISTINCT ON (q.id) q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo,
                                              q.indice_acerto, q.comentario_professor, q.soldado_ba, r.contexto, r.correta
                                       FROM mentor.resposta r JOIN mentor.questao q ON q.id=r.questao_id
                                       WHERE r.aula_id=$1 AND r.contexto IN ('aquecimento','gate') ORDER BY q.id, r.id DESC""", aula_id))
        rows.sort(key=lambda q: (q["contexto"] != "aquecimento", bool(q["correta"])))
        for q in rows:
            if q["id"] not in esc and len(novas) < GATE_N:
                q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
                q["repeticao"] = True
                novas.append(q)
                esc.add(q["id"])
    return novas[:GATE_N]


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
                await _semear_cards_aula(conn, aula_id, certas)
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
            novas = await _questoes_plano_b(conn, aula_id, a["assunto_id"], vistas) if reestudo_antes == 0 else []
            if reestudo_antes >= 1:
                # reincidente: fica estudado com ressalva, R1 com gate próprio (spec §3.3.2)
                await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE id=$1", aula_id)
                await _semear_cards_aula(conn, aula_id, certas)
                faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula WHERE assunto_id=$1 AND tipo IN ('conteudo','exercicios','propria') AND estado <> 'concluida'", a["assunto_id"])
                if faltam == 0:
                    await conn.execute("UPDATE mentor.assunto SET estado='ressalva', estudado_em=now() WHERE id=$1", a["assunto_id"])
                    await _agendar_revisao(conn, a["assunto_id"], "R1", INTERVALO["R1"])
                return {"aula_id": aula_id, "fechou": True, "com_ressalva": True, "certas": certas, "n": n, "assunto_fechou": faltam == 0}
            # marca o reestudo (conta quantas vezes a aula reprovou) sem inventar resposta
            await conn.execute("UPDATE mentor.aula SET estado='ressalva' WHERE id=$1", aula_id)
            nm = await conn.fetchrow("SELECT s.nome AS assunto, m.nome AS mat FROM mentor.assunto s JOIN mentor.materia m ON m.id=s.materia_id WHERE s.id=$1", a["assunto_id"])
            return {"aula_id": aula_id, "fechou": False, "certas": certas, "n": n, "reestudo": True, "plano_b": True, "resumo_bolso": a["resumo_bolso"],
                    "tem_video": bool(a.get("url") and (a.get("duracao_s") or 0) > 0), "novas": novas,
                    "novas_proto": [_q_proto(q, nm["mat"] if nm else None, nm["assunto"] if nm else None) for q in novas]}


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
                          AND NOT EXISTS (SELECT 1 FROM mentor.resposta r2 WHERE r2.questao_id=r1.questao_id AND r2.correta AND r2.id > r1.id)
                          AND NOT EXISTS (SELECT 1 FROM mentor.card c WHERE c.questao_id=r1.questao_id AND c.tipo='questao' AND c.ativo))
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


CAIXAS = [1, 3, 7, 15, 30]   # só pra migrar o que existia no Leitner


@router.get("/cards/fila")
async def cards_fila(modo: str = "hoje", agora: Optional[str] = None, materia_id: Optional[int] = None):
    """Fila do FSRS. modo=hoje (tudo do dia) | fechamento (só o que venceu: erros de hoje e passos de 10 min). materia_id = um baralho só."""
    if modo not in ("hoje", "fechamento"):
        raise HTTPException(400, "modo inválido")
    ag = _agora(agora)
    async with db() as conn:
        await _garantir_fsrs(conn)
        return await _fila_cards(conn, ag, modo, materia_id=materia_id)


@router.get("/cards/hoje")
async def cards_hoje(limite: int = 20, agora: Optional[str] = None):
    """Compatibilidade com o painel antigo: a lista da fila."""
    ag = _agora(agora)
    async with db() as conn:
        await _garantir_fsrs(conn)
        fila = await _fila_cards(conn, ag, "hoje")
    return fila["lista"][:limite]


@router.post("/card/{card_id}/responder")
async def responder_card(card_id: int, nota: Optional[int] = Body(None), lembrou: Optional[bool] = Body(None), correta: Optional[bool] = Body(None),
                         marcada: Optional[str] = Body(None), confianca: Optional[str] = Body(None), sessao_id: Optional[int] = Body(None), agora: Optional[str] = Body(None)):
    """
    Nota 1..4 (Errei / Difícil / Lembrei / Fácil). Card de questão: manda marcada + confianca (a nota sai da resposta) — ou correta.
    `lembrou` (painel antigo) vira 3/1.
    """
    ag = _agora(agora)
    async with db() as conn:
        await _garantir_fsrs(conn)
        c = _r(await conn.fetchrow("SELECT c.*, q.gabarito AS q_gab FROM mentor.card c LEFT JOIN mentor.questao q ON q.id=c.questao_id WHERE c.id=$1 AND c.ativo", card_id))
        if not c:
            raise HTTPException(404, "card não encontrado")
        if c["estado"] == "suspenso":
            raise HTTPException(409, "card suspenso (leech): reestude a aula e reative em /card/{id}/editar")
        if c["tipo"] == "questao":
            if marcada is not None and c.get("q_gab"):
                correta = marcada.strip().upper() == (c["q_gab"] or "").strip().upper()
            if correta is None:
                raise HTTPException(400, "card de questão: mande marcada (ou correta) e confianca")
            if confianca not in (None, "certeza", "duvida", "chute"):
                raise HTTPException(400, "confianca inválida")
            nota = _nota_da_resposta(bool(correta), confianca)
        elif nota is None:
            if lembrou is None:
                raise HTTPException(400, "mande nota 1..4")
            nota = 3 if lembrou else 1
        if nota not in (1, 2, 3, 4):
            raise HTTPException(400, "nota 1..4")
        f, cfg = await _fsrs_cfg(conn, ag)
        r = await _fsrs_aplicar(conn, c, int(nota), ag, f, cfg, contexto="card", sessao_id=sessao_id, gravar_resposta=(c["tipo"] == "questao"),
                                marcada=marcada, correta=correta, confianca=confianca)
        r["correta"] = correta
        r["gabarito"] = c.get("q_gab")
        # compat com o painel antigo
        r["caixa"] = {"novo": 0, "aprendendo": 0, "reaprendendo": 0, "revisao": min(4, max(0, round(math.log2(max(1, r["intervalo_s"] / 86400)))))}.get(r["estado"], 0)
        r["proxima"] = r["vence_em"][:10]
    return r


@router.post("/card")
async def card_novo(frente: str = Body(...), verso: str = Body(...), assunto_id: Optional[int] = Body(None), aula_id: Optional[int] = Body(None),
                    tipo: str = Body("usuario"), fonte: Optional[str] = Body(None), agora: Optional[str] = Body(None)):
    """Card livre (ou de um tipo específico): entra na fila de hoje como novo."""
    frente, verso = (frente or "").strip(), (verso or "").strip()
    if len(frente) < 3 or len(verso) < 1:
        raise HTTPException(400, "frente (3+) e verso (1+) obrigatórios")
    if tipo not in TIPOS_CARD or tipo == "questao":
        raise HTTPException(400, "tipo inválido")
    ag = _agora(agora)
    async with db() as conn:
        await _garantir_fsrs(conn)
        if assunto_id is None and aula_id:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id)
        cid = await _card_criar(conn, assunto_id=assunto_id, aula_id=aula_id, tipo=tipo, frente=frente, verso=verso, fonte=fonte or "card seu", vence_em=ag, agora=ag)
        if not cid:
            raise HTTPException(409, "já existe um card igual")
    return {"card_id": cid, "estado": "novo"}


@router.post("/card/{card_id}/editar")
async def card_editar(card_id: int, frente: Optional[str] = Body(None), verso: Optional[str] = Body(None), ativo: Optional[bool] = Body(None), reativar: bool = Body(False)):
    """Edita frente/verso, apaga (ativo=false) ou reativa um leech (volta como reaprendendo, agora)."""
    async with db() as conn:
        await _garantir_fsrs(conn)
        c = _r(await conn.fetchrow("SELECT * FROM mentor.card WHERE id=$1", card_id))
        if not c:
            raise HTTPException(404, "card não encontrado")
        if frente is not None or verso is not None:
            fr, vs = (frente if frente is not None else c["frente"]).strip(), (verso if verso is not None else c["verso"]).strip()
            if len(fr) < 3 or len(vs) < 1:
                raise HTTPException(400, "frente (3+) e verso (1+)")
            await conn.execute("UPDATE mentor.card SET frente=$2, verso=$3, hash=$4 WHERE id=$1", card_id, _curto_ml(fr, 600), _curto_ml(vs, 1200), _card_hash(c["tipo"], _curto_ml(fr, 600), _curto_ml(vs, 1200)))
        if ativo is not None:
            await conn.execute("UPDATE mentor.card SET ativo=$2 WHERE id=$1", card_id, ativo)
        if reativar:
            await conn.execute("UPDATE mentor.card SET estado='reaprendendo', passo=0, lapsos=0, vence_em=$2, suspenso_em=NULL, ativo=TRUE WHERE id=$1", card_id, _agora())
        c = _r(await conn.fetchrow("SELECT id, tipo, frente, verso, estado, ativo FROM mentor.card WHERE id=$1", card_id))
    return c


async def _metricas_dia(conn, agora):
    """Valter: a meta é fazer mais questões do que cards criados; e a fila do dia zerada, todo dia."""
    hoje = _dia_local(agora)
    ini, fim = _inicio_dia(hoje), _inicio_dia(hoje + timedelta(days=1))
    q = int(await conn.fetchval("SELECT count(*) FROM mentor.resposta WHERE data >= $1 AND data < $2 AND contexto NOT IN ('aquecimento','bolso')", ini, fim) or 0)
    # cards que VOCÊ criou (do erro e livres); os de questão, do Gran e do resumo são automáticos/por botão e não entram na conta
    cc = int(await conn.fetchval("SELECT count(*) FROM mentor.card WHERE criado_em >= $1 AND criado_em < $2 AND fonte IN ('caderno de erros','card seu')", ini, fim) or 0)
    dias = set(await _config(conn, "cards_zerados", []) or [])
    seq, d = 0, hoje
    if d.isoformat() not in dias:
        d = d - timedelta(days=1)          # hoje ainda em aberto: conta até ontem
    while d.isoformat() in dias:
        seq += 1
        d = d - timedelta(days=1)
    return {"questoes_hoje": q, "cards_criados_hoje": cc, "seq_fila_zerada": seq, "zerou_hoje": hoje.isoformat() in dias}


@router.post("/aula/{aula_id}/gerar-cards")
async def aula_gerar_cards(aula_id: int, agora: Optional[str] = Body(None, embed=True)):
    """Botão "gerar cards desta aula": lacunas do resumo (negritos) + flashcards do Gran (os que falam dos seus erros primeiro). Entram hoje."""
    ag = _agora(agora)
    async with db() as conn:
        await _garantir_fsrs(conn)
        if not await conn.fetchval("SELECT 1 FROM mentor.aula WHERE id=$1", aula_id):
            raise HTTPException(404, "aula não encontrada")
        antes = int(await conn.fetchval("SELECT count(*) FROM mentor.card WHERE aula_id=$1 AND tipo IN ('cloze','flashcard_gran')", aula_id) or 0)
        n_cloze = 0
        if not await conn.fetchval("SELECT 1 FROM mentor.card WHERE aula_id=$1 AND tipo='cloze' LIMIT 1", aula_id):
            n_cloze = await _cards_cloze_aula(conn, aula_id, ag, vence_em=ag)
        n_gran = await _semear_cards_aula(conn, aula_id, agora=ag, gerar=True)
        depois = int(await conn.fetchval("SELECT count(*) FROM mentor.card WHERE aula_id=$1 AND tipo IN ('cloze','flashcard_gran')", aula_id) or 0)
        # o que o semear criou "pra amanhã" entra hoje: foi você que pediu
        await conn.execute("UPDATE mentor.card SET vence_em=LEAST(vence_em, $2) WHERE aula_id=$1 AND estado='novo' AND tipo IN ('cloze','flashcard_gran')", aula_id, ag)
    return {"aula_id": aula_id, "criados": max(0, depois - antes), "total_da_aula": depois}


@router.get("/cards/resumo")
async def cards_resumo(agora: Optional[str] = None):
    """Estados, o que vence nos próximos 7 dias, retenção observada (últimas 200 revisões) e leeches."""
    ag = _agora(agora)
    async with db() as conn:
        await _garantir_fsrs(conn)
        f, cfg = await _fsrs_cfg(conn, ag)
        hoje = _dia_local(ag)
        estados = {r["estado"]: int(r["n"]) for r in await conn.fetch("SELECT estado, count(*) AS n FROM mentor.card WHERE ativo GROUP BY estado")}
        por_tipo = {r["tipo"]: int(r["n"]) for r in await conn.fetch("SELECT tipo, count(*) AS n FROM mentor.card WHERE ativo AND estado NOT IN ('suspenso','dominado') GROUP BY tipo")}
        dias = []
        for k in range(0, 7):
            d = hoje + timedelta(days=k)
            n = await conn.fetchval("SELECT count(*) FROM mentor.card WHERE ativo AND estado NOT IN ('suspenso','dominado') AND vence_em < $1 AND ($2 OR vence_em >= $3)",
                                    _inicio_dia(d + timedelta(days=1)), k == 0, _inicio_dia(d))
            dias.append({"dia": d.isoformat(), "vencem": int(n or 0)})
        ult = await conn.fetch("SELECT nota FROM mentor.card_rev WHERE estado_antes IN ('revisao','reaprendendo') ORDER BY data DESC LIMIT 200")
        ret = round(100.0 * sum(1 for r in ult if r["nota"] >= 2) / len(ult), 1) if ult else None
        hoje_n = await conn.fetchrow("SELECT count(*) AS revs, count(DISTINCT card_id) AS cards, count(*) FILTER (WHERE nota=1) AS errei FROM mentor.card_rev WHERE data >= $1 AND data < $2",
                                     _inicio_dia(hoje), _inicio_dia(hoje + timedelta(days=1)))
        leeches = _rs(await conn.fetch("SELECT c.id, c.frente, c.lapsos, s.nome AS assunto, m.nome AS materia FROM mentor.card c LEFT JOIN mentor.assunto s ON s.id=c.assunto_id LEFT JOIN mentor.materia m ON m.id=s.materia_id WHERE c.ativo AND c.estado='suspenso' ORDER BY c.suspenso_em DESC NULLS LAST LIMIT 20"))
        por_mat = _rs(await conn.fetch("""SELECT m.nome AS materia, count(*) AS total, count(*) FILTER (WHERE c.vence_em <= $1 AND c.estado NOT IN ('suspenso','dominado')) AS vencidos,
                                                 round(avg(c.dificuldade)::numeric, 1) AS dificuldade FROM mentor.card c JOIN mentor.assunto s ON s.id=c.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                                          WHERE c.ativo GROUP BY m.nome ORDER BY 3 DESC, 2 DESC""", ag))
        for r in por_mat:
            if r.get("dificuldade") is not None:
                r["dificuldade"] = float(r["dificuldade"])
        met = await _metricas_dia(conn, ag)
    return {"hoje": hoje.isoformat(), "estados": estados, "por_tipo": por_tipo, "proximos_dias": dias, "retencao_observada": ret, "retencao_alvo": f.retencao, "metricas": met,
            "hoje_feitos": {k: int(v or 0) for k, v in dict(hoje_n).items()}, "leeches": leeches, "por_materia": por_mat, "teto_dia": cfg.get("teto_dia"), "novos_dia": cfg.get("novos_dia"),
            "prova_data": cfg.get("prova_data"), "teto_dias": cfg.get("teto_dias"), "leech": cfg.get("leech")}


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
                       tipo: str = Body("regra"), linha: str = Body(...), enunciado: Optional[str] = Body(None), contexto: Optional[str] = Body(None),
                       frente: Optional[str] = Body(None), verso: Optional[str] = Body(None), cards: Optional[list] = Body(None)):
    """Uma linha por erro. v3.9: `frente`/`verso` (propostos pelo painel a partir da questão) viram o card do erro; sem eles, o card é 'linha → regra'."""
    linha = re.sub(r"\s+", " ", (linha or "")).strip()
    frente = _curto_ml(frente, 600) or None
    verso = _curto_ml(verso, 1200) or None
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
            await _garantir_fsrs(conn)
            ag = _agora()
            amanha = _inicio_dia(_dia_local(ag) + timedelta(days=1))
            pares = []
            for cc in (cards or [])[:4]:
                if isinstance(cc, dict) and str(cc.get("frente") or "").strip() and str(cc.get("verso") or "").strip():
                    pares.append((_curto_ml(cc["frente"], 600), _curto_ml(cc["verso"], 1200), bool(cc.get("com_linha"))))
            if pares:
                # Valter: card de Certo/Errado simula a prova. A linha (por que errei) vai no verso do card da alternativa errada.
                for fr, vs, com_linha in pares:
                    cid_ = await _card_criar(conn, assunto_id=assunto_id, aula_id=aula_id, tipo="erro", frente=fr,
                                             verso=vs + ("\n" + linha if com_linha and linha and linha.lower() not in vs.lower() else ""),
                                             fonte="caderno de erros", questao_id=questao_id, vence_em=amanha, agora=ag)
                    card_id = card_id or cid_
            elif frente and verso:
                card_id = await _card_criar(conn, assunto_id=assunto_id, aula_id=aula_id, tipo="erro", frente=frente, verso=verso + ("\n" + linha if linha and linha.lower() not in verso.lower() else ""),
                                            fonte="caderno de erros", questao_id=questao_id, vence_em=amanha, agora=ag)
            else:
                fr = (enunciado + "\n\nQual é a regra?") if enunciado else "Qual é a regra?"
                card_id = await _card_criar(conn, assunto_id=assunto_id, aula_id=aula_id, tipo="contraste" if tipo == "contraste" else "usuario", frente=fr, verso=linha,
                                            fonte="caderno de erros", questao_id=questao_id, vence_em=amanha, agora=ag)
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
        hoje_ciclo = {"n_materias": 0, "materias": [], "proximas": [], "min_por_materia": 0}
        hoje_rev = {"revisoes": 0, "questoes": 0, "lista": [], "cards": 0, "caderno_abertas": 0, "caderno_por_materia": {}}
        aberta = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE status='aberta' ORDER BY inicio DESC LIMIT 1"))
        if aberta:
            cfg = await _config(conn, "horas_dia", {})
            meta_min = _horas_dia(hoje, cfg) * 60 * PCT_CONTEUDO
            usado = 0
            pend = await conn.fetch("""SELECT a.id, a.titulo, a.url, a.duracao_s, a.estado, a.tipo, a.base, s.modo AS modo_v3, s.id AS assunto_id, s.nome AS assunto, m.nome AS mat, m.id AS materia_id, a.questoes_fixacao, a.flashcards,
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
            # v3.10 — o dia em CICLO (Valter): N matérias por dia pelas horas (até 2h: 1; até 4h30: 2; mais: 3), meio a meio no tempo.
            # A matéria que está há mais tempo sem ser estudada (antes de hoje) vem primeiro; dia perdido só empurra a fila.
            horas_hoje = _horas_dia(hoje, cfg)
            n_mat = 1 if horas_hoje <= 2 else (2 if horas_hoje <= 4.5 else 3)
            por_mat = {}
            for a in pend:
                por_mat.setdefault(a["materia_id"], []).append(a)
            ultimo = {r["mid"]: r["ult"] for r in await conn.fetch("""SELECT s.materia_id AS mid, max(r.data) AS ult FROM mentor.resposta r JOIN mentor.assunto s ON s.id=r.assunto_id
                                                                       WHERE r.contexto IN ('aquecimento','gate','fixacao') AND r.data < $1 GROUP BY 1""", _inicio_dia(hoje))}
            ordem_plano = list(por_mat.keys())
            ciclo = sorted(ordem_plano, key=lambda mid: (ultimo.get(mid) is not None, ultimo.get(mid) or datetime.min.replace(tzinfo=timezone.utc), ordem_plano.index(mid)))
            do_dia = ciclo[:n_mat]
            hoje_ciclo = {"n_materias": n_mat, "materias": [por_mat[m][0]["mat"] for m in do_dia], "proximas": [por_mat[m][0]["mat"] for m in ciclo[n_mat:n_mat + 4]],
                          "min_por_materia": int(meta_min / max(1, len(do_dia))) if do_dia else 0}
            escolhidas = []
            fatia = meta_min / max(1, len(do_dia))
            for mid in do_dia:
                gasto_m = 0.0
                for a in por_mat[mid]:
                    custo = ((a["duracao_s"] or 0) / 60 / 1.5 * 0.5 + 25) if a["base"] else 25   # vídeo é plano B: conta metade
                    if gasto_m and gasto_m + custo > fatia:
                        break
                    gasto_m += custo
                    escolhidas.append(a)
            for a in escolhidas:
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
                                   "tem_video": bool(a["url"] and (a["duracao_s"] or 0) > 0), "n_flash": len(a["flashcards"]) if isinstance(a["flashcards"], list) else 0,
                                   "bolso": (a["bolso"] or "")[:12000], "recalls": recalls,
                                   "pre": [_q_proto(q, a["mat"], a["assunto"]) for q in pre], "gate": [_q_proto(q, a["mat"], a["assunto"]) for q in gate], "fix": fix})
        # ---- revisão de hoje em números (questões + cards + caderno): o painel mostra no Plano de hoje e na aba Revisão
        await _garantir_caderno(conn)
        await _garantir_fsrs(conn)
        try:
            _fila_tot = (await _fila_cards(conn, _agora(), "hoje"))["totais"]
        except Exception as e:
            log.warning("fila de cards: %s", e)
            _fila_tot = {}
        try:
            _metricas_hoje = await _metricas_dia(conn, _agora())
        except Exception as e:
            log.warning("métricas do dia: %s", e)
            _metricas_hoje = {}
        revs_hoje = _rs(await conn.fetch("""SELECT r.tipo, s.nome FROM mentor.revisao r JOIN mentor.assunto s ON s.id=r.assunto_id
                                            WHERE r.feita IS NULL AND COALESCE(r.adiada_para, r.prevista) <= $1 ORDER BY r.prevista""", hoje))
        hoje_rev = {"revisoes": len(revs_hoje), "questoes": sum(REV_N.get(r["tipo"], 4) for r in revs_hoje), "lista": [f'{r["tipo"]} · {r["nome"]}' for r in revs_hoje],
                    "cards": _fila_tot.get("curtos", 0) + _fila_tot.get("vencidos", 0), "cards_novos": _fila_tot.get("novos", 0), "erros_hoje": _fila_tot.get("erros_hoje", 0),
                    "cards_voltam_hoje": _fila_tot.get("voltam_hoje", 0), **_metricas_hoje,
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
        try:
            _ref = await _materias_reforco(conn, hoje)
        except Exception as e:
            log.warning("reforço: %s", e)
            _ref = {}
        for b in base_mat:
            b["reforco"] = _ref.get(b["id"])
        for b in base_mat:
            b["pronta"] = b["fase"] == "base" and b["base_total"] > 0 and b["base_feitas"] >= b["base_total"]
            if b.get("prova_base_em"):
                b["prova_base_em"] = b["prova_base_em"].isoformat()
            if isinstance(b.get("ultima_prova"), str):
                b["ultima_prova"] = json.loads(b["ultima_prova"])
    return {"D": {"materias": D_mat, "orcamento": orc, "calendario": {"fechado": "2027-01-31", "em_dia": [], "atrasado": []}, "questoes": D_q, "projecao": proj, "outras": [], "agenda_sim": AGENDA_SIM, "base": base_mat, "flags_ok": flags_ok},
            "SEMANAS": semanas, "HOJE_AULAS": hoje_aulas, "HOJE": hoje.isoformat(), "ESTADO": est, "progresso": prog, "HOJE_REV": {**hoje_rev, "ciclo": hoje_ciclo}, "VERSAO": VERSAO}


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
