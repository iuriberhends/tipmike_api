# -*- coding: utf-8 -*-
r"""
auditoria_sentinelas.py v1.0 — ETAPA 4 (auditoria apito -> tick)

Confere CADA aposta simulada dos bots SENTINELA-* contra o TICK que a gerou
(join apostas.tick_id -> ticks.id) e contra um GABARITO INDEPENDENTE de
mercado (derivado do censo da etapa 1 — de propósito NAO usa o
_matches_mercado do motor: auditoria com implementação separada).

O que valida por aposta:
  1. casa/bookmaker e esporte batem com o bot
  2. liga da aposta esta na whitelist de torneios do bot (se houver)
  3. o tick original existe e mercado_tipo/selecao/odd conferem com a aposta
  4. o MERCADO do tick e o certo pro mercado_bot (gabarito independente)
  5. odd > 1.0 (nunca aposta em mercado suspenso)
  6. lado da selecao coerente (over/under p/ OU; valor de HC extraivel p/ ah)
Extras:
  - controle NEGATIVO (nome com 'SENTINELA-NEG'): precisa ter ZERO apostas
  - cobertura: eventos apitados vs eventos elegiveis na celula na janela

Modos:
  --seco    autovalida o gabarito do auditor com apostas sinteticas embutidas
            (casos certos tem que passar, casos errados tem que ser acusados)
  (padrao)  audita o banco: bots com nome ILIKE 'SENTINELA-%%' e as apostas
            simuladas das ultimas --horas horas.

ATENCAO retencao: os ticks ficam ~3 dias no banco — rode a auditoria logo
apos a janela dos sentinelas, senao o join aposta->tick perde a fonte.

Onde rodar: RAIZ do projeto tipmike_api na VPS (so precisa de psycopg2):
    python auditoria_sentinelas.py --seco
    python auditoria_sentinelas.py --horas 24

Saidas na pasta do script: auditoria_log.txt, auditoria_resultado.csv
(1 linha por aposta) e auditoria_resumo.csv (1 linha por sentinela).
Exit 0 somente se: todo positivo tem aposta, negativo zerado e ZERO apostas
reprovadas no gabarito.
"""

import argparse
import csv
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime

PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DSN_DEFAULT = os.environ.get(
    "MIKEDB_DSN", "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
)

ESPORTE_UI_PARA_BANCO = {
    'fifa': 'E-Football',
    'nba2k': 'E-Basketball',
    'ehockey': 'E-Hockey',
    'etennis': 'E-Tennis',
}


# ----------------------------------------------------------------------------
# Normalizacao propria (independente do motor)
# ----------------------------------------------------------------------------
def _norm(s) -> str:
    if s is None:
        return ''
    try:
        txt = s if isinstance(s, str) else str(s)
        # NFKD decompoe tambem os ordinais º/ª -> o/a (o auditor nao pode
        # tropecar no 'ª' que derrubou o motor na bancada E5b)
        txt = unicodedata.normalize('NFKD', txt)
        txt = ''.join(c for c in txt if unicodedata.category(c) != 'Mn')
        return txt.lower()
    except Exception:
        return ''


_RE_1T = re.compile(r'\b1\s*[aoº°ª]{0,2}\s*tempo\b|1st half|first half|primeiro tempo')
_RE_2T = re.compile(r'\b2\s*[aoº°ª]{0,2}\s*tempo\b|2nd half|second half|segundo tempo')
_RE_QUARTO = re.compile(r'\bquart|quarter')


def _eh_ht(nome_n: str) -> bool:
    return bool(_RE_1T.search(nome_n))


def _eh_periodo_qualquer(nome_n: str) -> bool:
    return bool(_RE_1T.search(nome_n) or _RE_2T.search(nome_n)
                or _RE_QUARTO.search(nome_n))


def _lado_ou(selecao) -> str:
    s = _norm(selecao)
    if re.search(r'\b(mais|over|acima)\b', s):
        return 'over'
    if re.search(r'\b(menos|under|abaixo)\b', s):
        return 'under'
    return ''


def _valor_hc(selecao):
    s = str(selecao or '').strip()
    m = re.search(r'\(([+-]?\d+(?:\.\d+)?)\)\s*$', s)
    if not m:
        m = re.search(r'([+-]?\d+(?:\.\d+)?)\s*$', s)
    try:
        return float(m.group(1)) if m else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# GABARITO INDEPENDENTE por (casa, mercado_bot) — derivado do censo etapa 1
# Retorna (ok: bool, motivo: str)
# ----------------------------------------------------------------------------
def gabarito_mercado(casa: str, mercado_bot: str, tick_mercado, tick_tipo) -> tuple:
    c = (casa or '').strip().lower()
    mb = (mercado_bot or '').strip().lower()
    nome = _norm(tick_mercado)
    tipo = str(tick_tipo or '').strip()

    if c == 'superbet':
        if mb == 'ah_ft':
            if tipo != 'HANDICAP':
                return False, f'tipo_{tipo}_nao_HANDICAP'
            if _eh_periodo_qualquer(nome):
                return False, 'hc_de_periodo'
            if '3-way' in nome or '3 way' in nome or 'europeu' in nome:
                return False, 'hc_3way_europeu'
            return True, ''
        if mb == 'over_under_ft':
            if tipo != 'OVER_UNDER':
                return False, f'tipo_{tipo}_nao_OVER_UNDER'
            if _eh_periodo_qualquer(nome):
                return False, 'ou_de_periodo'
            if 'asiatic' in nome or 'asian' in nome:
                return False, 'ou_asiatico'
            return True, ''
        if mb == 'over_under_ht':
            if tipo != 'PERIOD_TOTAL':
                return False, f'tipo_{tipo}_nao_PERIOD_TOTAL'
            if not _eh_ht(nome):
                return False, 'nao_eh_1o_tempo'
            if _RE_QUARTO.search(nome):
                return False, 'eh_quarto'
            return True, ''
        return False, f'mercado_bot_{mb}_sem_gabarito_superbet'

    if c == 'estrelabet':
        esperado = {'ah_ft': {'16'}, 'ah_ht': {'66'},
                    'over_under_ft': {'18'}, 'over_under_ht': {'68'}}.get(mb)
        if esperado is None:
            return False, f'mercado_bot_{mb}_sem_gabarito_estrelabet'
        if tipo not in esperado:
            return False, f'tipo_{tipo}_esperado_{"/".join(sorted(esperado))}'
        return True, ''

    if c == 'betano':
        if mb == 'ah_ft':
            return (tipo == '156', '' if tipo == '156' else f'tipo_{tipo}_esperado_156')
        if mb == 'over_under_ft':
            if tipo not in ('13', '157'):
                return False, f'tipo_{tipo}_esperado_13/157'
            if 'asiatic' in nome or 'asian' in nome or '(esports)' in nome:
                return False, 'ou_variante_errada'
            if _eh_periodo_qualquer(nome):
                return False, 'ou_de_periodo'
            return True, ''
        if mb == 'over_under_ht':
            return (tipo == '14', '' if tipo == '14' else f'tipo_{tipo}_esperado_14')
        return False, f'mercado_bot_{mb}_sem_gabarito_betano'

    return False, f'casa_{c}_sem_gabarito'


def auditar_aposta(ap: dict) -> tuple:
    """Valida UMA aposta (dict com campos da aposta + do tick). (ok, motivos[])."""
    erros = []
    casa_bot = (ap.get('bot_casa') or '').lower()
    # 1) casa
    if (ap.get('bookmaker') or '').lower() != casa_bot:
        erros.append(f"casa_diverge({ap.get('bookmaker')}!={casa_bot})")
    # 2) esporte
    esp_banco = ESPORTE_UI_PARA_BANCO.get(ap.get('bot_esporte'), ap.get('bot_esporte'))
    if ap.get('tick_sport') and esp_banco and ap['tick_sport'] != esp_banco:
        erros.append(f"esporte_diverge({ap['tick_sport']}!={esp_banco})")
    # 3) torneio na whitelist
    torneios = ap.get('bot_torneios') or []
    if torneios:
        liga_n = _norm(ap.get('liga'))
        if not any(_norm(t) in liga_n for t in torneios if t):
            erros.append(f"liga_fora_da_whitelist({ap.get('liga')})")
    # 4) tick fonte
    if ap.get('tick_id') is None:
        erros.append('sem_tick_id')
    elif not ap.get('tick_existe'):
        erros.append('tick_fonte_nao_encontrado(retencao?)')
    else:
        if str(ap.get('mercado_tipo') or '') != str(ap.get('tick_mercado_tipo') or ''):
            erros.append('mercado_tipo_diverge_do_tick')
        if (ap.get('selecao') or '') != (ap.get('tick_selecao') or ''):
            erros.append('selecao_diverge_do_tick')
        try:
            if ap.get('odd') is not None and ap.get('tick_odds') is not None:
                if abs(float(ap['odd']) - float(ap['tick_odds'])) > 1e-6:
                    erros.append(f"odd_diverge({ap['odd']}!={ap['tick_odds']})")
        except (TypeError, ValueError):
            erros.append('odd_ilegivel')
    # 5) gabarito de mercado (usa o mercado do TICK quando existe)
    nome_mercado = ap.get('tick_mercado') if ap.get('tick_existe') else ap.get('ap_mercado_nome')
    ok_m, motivo_m = gabarito_mercado(casa_bot, ap.get('bot_mercado'),
                                      nome_mercado, ap.get('mercado_tipo'))
    if not ok_m:
        erros.append(f'gabarito_mercado:{motivo_m}')
    # 6) odd apostavel
    try:
        if ap.get('odd') is None or float(ap['odd']) <= 1.0:
            erros.append(f"odd_nao_apostavel({ap.get('odd')})")
    except (TypeError, ValueError):
        erros.append('odd_invalida')
    # 7) coerencia do lado
    mb = (ap.get('bot_mercado') or '').lower()
    if mb.startswith('over_under'):
        if _lado_ou(ap.get('selecao')) == '':
            erros.append('selecao_ou_sem_lado')
    if mb.startswith('ah_'):
        if _valor_hc(ap.get('selecao')) is None:
            erros.append('selecao_hc_sem_valor')
    return (len(erros) == 0), erros


# ----------------------------------------------------------------------------
# Log
# ----------------------------------------------------------------------------
class Log:
    def __init__(self, caminho):
        try:
            self._fh = open(caminho, 'w', encoding='utf-8')
        except OSError as e:
            self._fh = None
            print(f'[AVISO] sem log em arquivo ({e})')

    def msg(self, texto=''):
        linha = f"[{datetime.now().strftime('%H:%M:%S')}] {texto}" if texto else ''
        print(linha)
        if self._fh:
            try:
                self._fh.write(linha + '\n')
                self._fh.flush()
            except OSError:
                pass

    def fechar(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass


# ----------------------------------------------------------------------------
# MODO SECO: autovalidacao do gabarito do auditor
# Cada caso: (descricao, aposta_dict, deve_passar)
# ----------------------------------------------------------------------------
def _ap(casa, esporte, mercado_bot, tick_mercado, tick_tipo, selecao, odd=1.85,
        liga='X', torneios=None, tick_existe=True):
    return {
        'bot_casa': casa, 'bot_esporte': esporte, 'bot_mercado': mercado_bot,
        'bot_torneios': torneios or [], 'bookmaker': casa,
        'tick_sport': ESPORTE_UI_PARA_BANCO.get(esporte, esporte),
        'liga': liga, 'tick_id': 1, 'tick_existe': tick_existe,
        'mercado_tipo': tick_tipo, 'tick_mercado_tipo': tick_tipo,
        'tick_mercado': tick_mercado, 'ap_mercado_nome': tick_mercado,
        'selecao': selecao, 'tick_selecao': selecao,
        'odd': odd, 'tick_odds': odd,
    }


CASOS_SECO = [
    ("superbet ah_ft aceita 'Handicap (Inc. prorrogação)'",
     _ap('superbet', 'nba2k', 'ah_ft', 'Handicap (Inc. prorrogação)', 'HANDICAP',
         'Boston Celtics (Berlin) (-13.5)'), True),
    ("superbet ah_ft ACUSA HC de 1º tempo",
     _ap('superbet', 'nba2k', 'ah_ft', '1º Tempo - Handicap', 'HANDICAP',
         'Boston Celtics (Berlin) (-4.5)'), False),
    ("superbet ah_ft ACUSA 'Handicap 3-Way'",
     _ap('superbet', 'fifa', 'ah_ft', 'Handicap 3-Way', 'HANDICAP', '1 (-1.5)'), False),
    ("superbet ah_ft ACUSA '1º Tempo - Handicap asiático' (caso S9b)",
     _ap('superbet', 'fifa', 'ah_ft', '1º Tempo - Handicap asiático', 'HANDICAP',
         'Bayern de Munique (KINGSLAYER) (-0.5)'), False),
    ("superbet over_under_ft aceita 'Total de Pontos (Inc. prorrogação)'",
     _ap('superbet', 'nba2k', 'over_under_ft', 'Total de Pontos (Inc. prorrogação)',
         'OVER_UNDER', 'Mais de 105.5'), True),
    ("superbet over_under_ft ACUSA 'Total de Gols Asiático'",
     _ap('superbet', 'fifa', 'over_under_ft', 'Total de Gols Asiático',
         'OVER_UNDER', 'Mais de 4.75'), False),
    ("superbet over_under_ht aceita '1º Tempo - Total de Pontos' (PERIOD_TOTAL)",
     _ap('superbet', 'nba2k', 'over_under_ht', '1º Tempo - Total de Pontos',
         'PERIOD_TOTAL', 'Mais de 38.5'), True),
    ("estrelabet over_under_ht aceita tipo 68 com 'ª' feminino (caso E5b)",
     _ap('estrelabet', 'fifa', 'over_under_ht', '1ª tempo - Total de gols', '68',
         'Mais de 2.5'), True),
    ("estrelabet ah_ft ACUSA tipo 303 (quarto)",
     _ap('estrelabet', 'nba2k', 'ah_ft', '2º quarto - handicap', '303',
         'Denver Nuggets (Polub) (-1.5)'), False),
    ("betano ah_ft aceita tipo 156 (seleção sem parênteses)",
     _ap('betano', 'nba2k', 'ah_ft', 'Handicap', '156', 'Partizan (tapachan) -15.5'), True),
    ("betano over_under_ft ACUSA tipo 189 (asiático)",
     _ap('betano', 'fifa', 'over_under_ft', 'Asiático (Mais/Menos) Total de Gols',
         '189', 'Mais de 4.75'), False),
    ("qualquer: ACUSA odd suspensa (<=1)",
     _ap('betano', 'nba2k', 'ah_ft', 'Handicap', '156', 'Partizan (tapachan) -15.5',
         odd=1.0), False),
    ("qualquer: ACUSA liga fora da whitelist",
     _ap('superbet', 'nba2k', 'ah_ft', 'Handicap (Inc. prorrogação)', 'HANDICAP',
         'Boston Celtics (Berlin) (-13.5)', liga='Euroliga',
         torneios=['Adriatic Premier']), False),
    ("qualquer: ACUSA tick fonte ausente",
     _ap('superbet', 'nba2k', 'ah_ft', 'Handicap (Inc. prorrogação)', 'HANDICAP',
         'Boston Celtics (Berlin) (-13.5)', tick_existe=False), False),
    ("qualquer: ACUSA seleção de OU sem lado",
     _ap('betano', 'fifa', 'over_under_ft', 'Total de Gols', '13', 'Equipe 1'), False),
]


def rodar_seco(log) -> int:
    falhas = 0
    for desc, ap, deve_passar in CASOS_SECO:
        ok, erros = auditar_aposta(ap)
        certo = (ok == deve_passar)
        status = 'OK ' if certo else 'FAIL'
        det = '' if ok else f" [{'; '.join(erros)}]"
        log.msg(f"[{status}] {desc} -> {'aprova' if ok else 'acusa'}{det}")
        if not certo:
            falhas += 1
    log.msg('')
    log.msg(f"SECO: {len(CASOS_SECO) - falhas}/{len(CASOS_SECO)} casos do auditor OK")
    return falhas


# ----------------------------------------------------------------------------
# MODO BANCO
# ----------------------------------------------------------------------------
def rodar_banco(args, log) -> int:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        log.msg('ERRO: psycopg2 ausente (pip install psycopg2-binary)')
        return 1

    conn = psycopg2.connect(args.dsn)
    conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SET statement_timeout = {int(args.timeout * 1000)}")

    # bots sentinela
    cur.execute("""
        SELECT id, nome, casa, esporte, mercado, torneios
        FROM bots WHERE nome ILIKE 'SENTINELA-%%' ORDER BY nome
    """)
    bots = cur.fetchall()
    if not bots:
        log.msg("Nenhum bot com nome 'SENTINELA-%' encontrado — crie os sentinelas primeiro.")
        return 1
    log.msg(f"{len(bots)} sentinelas encontrados: " + ', '.join(b['nome'] for b in bots))
    log.msg('')

    resultado_linhas = []
    resumo = []
    gate_falhou = False

    for b in bots:
        eh_negativo = 'SENTINELA-NEG' in (b['nome'] or '').upper()
        # apostas do bot na janela + tick fonte (LEFT JOIN: tick pode ter expirado)
        cur.execute("""
            SELECT a.id AS aposta_id, a.event_id, a.mercado_tipo, a.linha,
                   a.selecao, a.odd, a.bookmaker, a.liga, a.tick_id,
                   a.apostado_em, a.mercado AS ap_mercado_nome,
                   t.id IS NOT NULL AS tick_existe,
                   t.sport AS tick_sport, t.mercado AS tick_mercado,
                   t.mercado_tipo AS tick_mercado_tipo,
                   t.selecao AS tick_selecao, t.odds AS tick_odds
            FROM apostas a
            LEFT JOIN ticks t ON t.id = a.tick_id
            WHERE a.bot_id = %s AND a.modo = 'simulado'
              AND a.apostado_em >= NOW() - make_interval(hours => %s)
            ORDER BY a.apostado_em
        """, (b['id'], int(args.horas)))
        apostas = cur.fetchall()

        ok_n = fail_n = 0
        eventos = set()
        for row in apostas:
            ap = dict(row)
            ap.update({'bot_casa': b['casa'], 'bot_esporte': b['esporte'],
                       'bot_mercado': b['mercado'],
                       'bot_torneios': b.get('torneios') or []})
            ok, erros = auditar_aposta(ap)
            eventos.add(ap.get('event_id'))
            if ok:
                ok_n += 1
            else:
                fail_n += 1
            resultado_linhas.append({
                'bot': b['nome'], 'aposta_id': ap['aposta_id'],
                'event_id': ap.get('event_id'),
                'mercado_tick': ap.get('tick_mercado') or ap.get('ap_mercado_nome'),
                'mercado_tipo': ap.get('mercado_tipo'), 'linha': ap.get('linha'),
                'selecao': ap.get('selecao'), 'odd': ap.get('odd'),
                'liga': ap.get('liga'), 'apostado_em': ap.get('apostado_em'),
                'veredito': 'OK' if ok else 'REPROVADA',
                'motivos': '; '.join(erros),
            })

        # cobertura: eventos elegiveis na celula na janela (gabarito no SQL leve:
        # so casa+esporte+torneio; a elegibilidade fina de mercado ja esta na
        # propria existencia de apostas — cobertura aqui e um TERMOMETRO, nao gate)
        esp_banco = ESPORTE_UI_PARA_BANCO.get(b['esporte'], b['esporte'])
        torneios = b.get('torneios') or []
        filtro_liga = ''
        params = [b['casa'], esp_banco, int(args.horas)]
        if torneios:
            ors = []
            for t in torneios:
                ors.append('liga ILIKE %s')
                params.append(f'%{t}%')
            filtro_liga = ' AND (' + ' OR '.join(ors) + ')'
        try:
            cur.execute(f"""
                SELECT COUNT(DISTINCT event_id) AS n FROM ticks
                WHERE bookmaker ILIKE %s AND sport = %s
                  AND ts >= NOW() - make_interval(hours => %s){filtro_liga}
            """, params)
            eventos_celula = (cur.fetchone() or {}).get('n') or 0
        except Exception as e:
            log.msg(f'[AVISO] cobertura de {b["nome"]} indisponivel ({e})')
            conn.rollback()
            eventos_celula = None

        cobertura = (100.0 * len(eventos) / eventos_celula) if eventos_celula else None
        status_bot = 'OK'
        if eh_negativo:
            if len(apostas) > 0:
                status_bot = 'VAZAMENTO'
                gate_falhou = True
        else:
            if len(apostas) == 0:
                status_bot = 'SEM_APITO'
                gate_falhou = True
            if fail_n > 0:
                status_bot = 'REPROVADAS'
                gate_falhou = True

        cob_str = f'{cobertura:.0f}%' if cobertura is not None else '-'
        log.msg(f"[{status_bot}] {b['nome']} — apostas {len(apostas)} "
                f"(ok {ok_n} / reprovadas {fail_n}) | eventos apitados "
                f"{len(eventos)}/{eventos_celula if eventos_celula is not None else '?'} "
                f"({cob_str} da celula na janela)")
        resumo.append({
            'bot': b['nome'], 'negativo': eh_negativo, 'apostas': len(apostas),
            'ok': ok_n, 'reprovadas': fail_n, 'eventos_apitados': len(eventos),
            'eventos_celula': eventos_celula, 'cobertura_pct':
                round(cobertura, 1) if cobertura is not None else '',
            'status': status_bot,
        })

    conn.close()

    if resultado_linhas:
        p1 = os.path.join(PASTA_SCRIPT, 'auditoria_resultado.csv')
        with open(p1, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=list(resultado_linhas[0].keys()), delimiter=';')
            w.writeheader()
            for r in resultado_linhas:
                w.writerow(r)
        log.msg(f'CSV por aposta: {p1}')
    if resumo:
        p2 = os.path.join(PASTA_SCRIPT, 'auditoria_resumo.csv')
        with open(p2, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=list(resumo[0].keys()), delimiter=';')
            w.writeheader()
            for r in resumo:
                w.writerow(r)
        log.msg(f'CSV resumo: {p2}')

    log.msg('')
    log.msg('GATE etapa 4: positivos apitando, negativo zerado, zero reprovadas.'
            + (' -> NAO PASSOU' if gate_falhou else ' -> PASSOU'))
    return 1 if gate_falhou else 0


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='Auditoria dos sentinelas (etapa 4)')
    ap.add_argument('--seco', action='store_true', help='autovalida o gabarito do auditor')
    ap.add_argument('--horas', type=int, default=24)
    ap.add_argument('--timeout', type=int, default=120)
    ap.add_argument('--dsn', type=str, default=DSN_DEFAULT)
    args = ap.parse_args()

    log = Log(os.path.join(PASTA_SCRIPT, 'auditoria_log.txt'))
    log.msg(f"=== auditoria_sentinelas.py v1.0 | modo={'SECO' if args.seco else 'BANCO'} ===")
    try:
        rc = rodar_seco(log) if args.seco else rodar_banco(args, log)
        sys.exit(0 if rc == 0 else 1)
    except SystemExit:
        raise
    except Exception:
        log.msg('ERRO INESPERADO:\n' + traceback.format_exc())
        sys.exit(4)
    finally:
        log.fechar()


if __name__ == '__main__':
    main()
