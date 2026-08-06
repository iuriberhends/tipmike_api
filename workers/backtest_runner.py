"""
workers/backtest_runner.py - Worker do backtest (v12 + v15 maxPartidas)

v15 - TETO DE CONFRONTOS (maxPartidas): espelha o minPartidas nos mesmos
  4 pontos de validacao. Ausente/None = sem teto (mudanca ADITIVA, zero
  efeito em bot ou job que nao usa o campo).

v12 - Filtro FOLGA (so handicap):
- folga = hc_assinado - (pts_adversario - pts_do_lado_apostado), calculada no
  placar do TICK (momento da aposta). folga > 0 <=> o lado apostado esta
  cobrindo a linha AGORA. Config no filtros jsonb: folgaAtivo / folgaMin /
  folgaMax — bot antigo sem as chaves = filtro desligado (zero mudanca).
- FAIL CLOSED: tick sem placar/nick/hc -> rejeitado (contador 'folga');
  folga ligada em bot NAO-handicap -> rejeita com 'folga_so_hc' (nunca roda
  "sem o filtro" em silencio). O bot_executor importa e aplica a MESMA
  funcao no ponto espelhado (fonte unica: backtest e vivo nao divergem).

v11.2 - "Ambos" de verdade no evitarLinhasSeq:
- A trava de 1 aposta por mercado passou a incluir o LADO:
  (jogo, mercado_tipo, lado). Bot de lado unico e HC: comportamento IDENTICO
  (lado constante/None = mesma trava de antes). Lado 'Ambos' em over/under:
  cada lado ganha a SUA aposta no jogo — antes o primeiro tick gravado pelo
  coletor (Under, na estrelabet) vencia o desempate em 100% dos jogos e o
  'Ambos' saia um lado so (job 58: 4167/4167 Under). O bot_executor v11.2
  espelha a MESMA trava no vivo (fonte unica, sem divergir).

v11.1 - Colunas de WR DINAMICAS na planilha (coleta de dados):
- apostas_detalhe ganha 'wr_cols': TODAS as janelas de TODOS os chips viram
  coluna no export (nao so 2). Chip INDIVIDUAL de O/U exporta 3 numeros por
  janela: "(ind pior)" (min dos dois — o valor que decidiria um gate AND),
  "(ind A)" e "(ind B)". Chip individual de HC com alvo 'ambos' exporta
  tambem "(ind fav)". + colunas Qtd Ind A/B (total individual de cada um).
- janela_1/2 e winrate_1/2 continuam gravados (compat com jobs antigos e
  qualquer leitor do formato legado).

v11 - Filtro INDIVIDUAL (base=individual) + FAIL CLOSED:
- Chips de historico com base=individual agora FUNCIONAM: a janela (ult10 etc)
  passa a olhar o historico INDIVIDUAL de cada jogador (contra QUALQUER
  adversario), em vez do confronto direto do par.
    * Over/Under: WR individual calculado pros DOIS jogadores; regra AND —
      os dois precisam passar no chip (decisao do usuario, 17/jul).
    * Handicap: pct individual da ZEBRA cobrir a linha nas ultimas N dela.
      Chip com indivAlvo='ambos' TAMBEM exige o favorito: % dos jogos dele em
      que o ADVERSARIO cobriu +linha (= favorito nao abriu mais que a linha).
      Default indivAlvo='zebra'.
    * minPartidas = maturidade do TOTAL individual de cada jogador (espelha a
      semantica v6 dos filtros hist por par).
- Novo HistIndividualCache (mesmo contrato do H2HCache: cutoff temporal,
  margem ao-vivo, dedup tick x hist — helpers compartilhados, sem divergir).
- FAIL CLOSED: filtro hist com combo ainda nao suportado (tipo != 'all',
  base desconhecida) NAO e mais descartado em silencio. Ele fica MARCADO
  (_nao_suportado) e o avaliador REJEITA o tick com motivo claro. Antes o bot
  rodava SEM o filtro configurado — inaceitavel com dinheiro real.
- Escadinha HC (_avaliar_escadinha_hc) ganhou o ramo individual e o
  bot_executor v11 passa a usar a MESMA escadinha ao vivo (fonte unica).

v5 - Stats H2H usam jogos disponiveis ate N (em vez de exigir N exato):
- _calcular_stats_h2h: WR ult 20 com 12 jogos agora calcula com 12 (em vez de
  retornar None). Tambem grava wr_ult{N}_qtd e media_ult{N}_qtd indicando
  quantos jogos foram usados.
- _aplicar_filtros_complementares: valida min_partidas contra qtd ESPECIFICA
  daquela janela. Antes validava contra qtd_h2h global, o que era o mesmo,
  mas agora a diferenca eh que wr_ult{N}_qtd pode ser MENOR que qtd_h2h se
  jogos < N. Mas como min_partidas eh o piso aceitavel, isso permite
  "WR ult20, min=10" passar com 12 jogos.

v4 - Le filtrosHistAdicionados (formato antigo) alem de filtrosCompAdicionados:
- _normalizar_filtros_hist converte formato antigo {base, janela:"last_N", prob:[min,max]} pro novo
- _extrair_janelas_dos_filtros agora le dos 2 lugares
- _aplicar_filtros_complementares aceita filtros normalizados de ambas fontes
- (v11) base=individual passou a ser suportado; tipo!=all agora e fail closed

v3 - Janelas H2H dinamicas:
- _calcular_stats_h2h aceita lista de janelas (qualquer N de 3-100)
- _aplicar_filtros_complementares usa janela exata do filtro em vez de mapeamento fixo
- Mantem janelas padrao 5/10/15/20 sempre presentes pra compatibilidade

v2 - Adiciona filtros estatisticos H2H:
- WR ult5/10/15, Media ult5/10/20, Gap, Tendencia, DIFF, Cenario
"""

from datetime import date, datetime, timedelta
from typing import Any, Optional
from decimal import Decimal
import json
import re
import unicodedata
import logging
import asyncio

from database import get_pool

logger = logging.getLogger(__name__)

# =====================================================================
# v15 (02/ago) — PERNA "TICKS" DO H2H LIDA DA h2h_matches
# Medido (EXPLAIN): a consulta por par nos ticks brutos custava 2.3-3.9s
# (56.404 linhas lidas+ordenadas pra destilar 35 jogos; ~400x por job =
# ~26min so de warm-up do cache). A h2h_matches — mantida pelo
# atualizar_h2h a cada 60s — ja guarda EXATAMENTE esse resumo (1 linha
# por evento, placar final, par normalizado): 5ms na mesma pergunta.
# Semantica preservada: ts_fim = ts do ultimo tick (igual ao DISTINCT ON
# ... ORDER BY ts DESC); score_a e SEMPRE o placar do jogador_a da linha
# (a normalizacao alfabetica reordena par E placar juntos); fonte segue
# rotulada 'tick' pro dedup entre fontes preferir o hist (placar oficial
# da TM) — politica identica a de hoje.
# Diferenca honesta: a matches guarda jogos que o expurgo ja comeu dos
# ticks — chips podem ver historico MAIS COMPLETO (mais fiel, nao menos).
# Reversao: USAR_H2H_MATCHES = False volta ao SQL antigo sem reinstalar.
# =====================================================================
USAR_H2H_MATCHES = True

# =====================================================================
# v16 (02/ago) — CUTOFF POR EVENTO + MEMO DE STATS (aval do Santos:
# "faca de tudo para que saia perfeito, tratamento de erro e blinde").
#
# MEDIDO: _aplicar_cutoff_jogos varria a lista INTEIRA do par/jogador a
# cada tick avaliado (re-parseando datas por item), e _calcular_stats_h2h
# re-somava as janelas por tick — 1,5M ticks x 2-3 sujeitos x milhares de
# jogos = bilhoes de operacoes Python. Era o custo real dos jobs de 45-49
# min (multi-chip sem folga) e do chip INDIVIDUAL "muito lento".
#
# O QUE MUDA (matematica IDENTICA por construcao — nada e computado
# diferente; apenas deixa de recomputar o que e provadamente igual):
#   1. datas normalizadas UMA vez no load do cache (nao por tick);
#   2. corte calculado UMA vez por EVENTO e reusado pelos ticks daquele
#      jogo — a lista historica e constante enquanto o jogo corre. Unica
#      excecao real: jogo do sujeito cujo LIMIAR de margem (inicio+margem
#      hist, fim+margem ao-vivo, ou ts futuro) cai DENTRO do evento; esses
#      viram "pendentes" e, quando o relogio da aposta cruza o limiar, o
#      corte e RECALCULADO do zero (exatidao preservada; sem aproximacao);
#   3. stats das janelas memoizadas por (lista-do-evento, linha, lado) —
#      janelas de QUANTIDADE apenas; janela de TEMPO depende do ts do tick
#      e NUNCA e memoizada (calcula direto, como sempre).
#
# BLINDAGEM: qualquer excecao no caminho novo -> log + FALLBACK AUTOMATICO
# pro caminho v15 naquela consulta (resultado nunca em risco). Reversao
# total: USAR_CUTOFF_V16 = False. Contadores em _V16_STATS.
# =====================================================================
USAR_CUTOFF_V16 = True

_V16_STATS = {"cortes_calculados": 0, "cortes_reusados": 0,
              "invalidacoes_pendente": 0, "fallbacks_corte": 0,
              "stats_memo_hits": 0, "stats_memo_miss": 0,
              "fallbacks_stats": 0}


class _ListaCortada(list):
    """Lista de jogos cortada, com espaco pra memo de stats. O memo vive e
    morre COM a lista (trocou o evento -> lista nova -> memo novo): zero
    risco de reuso indevido e zero vazamento."""
    __slots__ = ("memo",)

    def __init__(self, *a):
        super().__init__(*a)
        self.memo = {}


def _preparar_jogos_inplace(jogos: list) -> list:
    """v16: normaliza ts/ultimo_tick_ts UMA vez no load (tira tz mantendo o
    horario de parede — mesma regra do _dt_naive). Antes isso rodava por
    JOGO x por TICK. BLINDADO: item problematico fica como esta (o corte
    v15/v16 ja pula ts None)."""
    for j in jogos:
        try:
            j['ts'] = _dt_naive(j.get('ts'))
            j['ultimo_tick_ts'] = _dt_naive(j.get('ultimo_tick_ts'))
        except Exception:
            continue
    return jogos


class _SlotCorte:
    """Corte cacheado de UM evento para UM sujeito (par ou jogador).
    'pendentes' = limiares (datetimes) de jogos hoje EXCLUIDOS que passam a
    ENTRAR quando antes_de_ts cruzar o limiar — ai o corte e refeito."""
    __slots__ = ("ev", "antes_base", "lista", "pendentes")

    def __init__(self, ev, antes_base, lista, pendentes):
        self.ev = ev
        self.antes_base = antes_base
        self.lista = lista
        self.pendentes = pendentes


def _cortar_e_pendentes(jogos, antes, ev_str, m_vivo, m_hist):
    """Corte identico ao v15 (mesmas 4 regras, mesma ordem) + coleta dos
    limiares de transicao. Roda sobre datas JA normalizadas (load)."""
    corte_ao_vivo = antes - timedelta(minutes=m_vivo)
    corte_hist = antes - timedelta(minutes=m_hist)
    out = _ListaCortada()
    pend = []
    for j in jogos:
        tsj = j.get('ts')
        if tsj is None:
            continue
        try:
            if tsj >= antes:
                # futuro AGORA; entra quando antes > tsj -> pendente
                pend.append(tsj)
                continue
            ult = j.get('ultimo_tick_ts')
            if ult is not None:
                if ult >= corte_ao_vivo:
                    pend.append(ult + timedelta(minutes=m_vivo))
                    continue
            elif j.get('fonte') == 'tick':
                continue           # tick sem fim: conservador (igual v15)
            else:
                if tsj > corte_hist:
                    pend.append(tsj + timedelta(minutes=m_hist))
                    continue
        except TypeError:
            continue
        if ev_str is not None:
            if str(j.get('event_id')) == ev_str or str(j.get('event_id_tick')) == ev_str:
                continue
        out.append(j)
    return out, pend


def _cutoff_v16(slots: dict, jogos_prep: list, antes_de_ts, event_id_excluir,
                m_vivo: int, m_hist: int):
    """Substitui o corte por-tick pelo corte por-EVENTO com invalidacao por
    pendentes. slots = dict {sujeito -> _SlotCorte} da instancia de cache
    (1 slot por sujeito: o mesmo par/jogador nao tem 2 eventos simultaneos).
    BLINDADO: qualquer surpresa -> fallback exato pro v15 nesta consulta."""
    try:
        antes = _dt_naive(antes_de_ts)
        if antes is None:
            return _ListaCortada()
        ev = None if event_id_excluir is None else str(event_id_excluir)
        slot = slots.get('_')
        if (slot is not None and slot.ev == ev and antes >= slot.antes_base):
            # mesmo evento, relogio so avancou: valido a menos que algum
            # pendente tenha cruzado o limiar (jogo passaria a ENTRAR)
            if not any(lim <= antes for lim in slot.pendentes):
                _V16_STATS["cortes_reusados"] += 1
                return slot.lista
            _V16_STATS["invalidacoes_pendente"] += 1
        lista, pend = _cortar_e_pendentes(jogos_prep, antes, ev, m_vivo, m_hist)
        slots['_'] = _SlotCorte(ev, antes, lista, pend)
        _V16_STATS["cortes_calculados"] += 1
        return lista
    except Exception:
        _V16_STATS["fallbacks_corte"] += 1
        logger.exception("[v16] corte falhou — fallback pro caminho v15")
        # AUTO-CURA (pego na bancada de sabotagem): sem isto, um slot
        # corrompido ficava no cache e TODA consulta seguinte do sujeito
        # caia em fallback ate o fim do job — degradacao permanente e
        # silenciosa. Removendo, a proxima consulta reconstroi limpo e o
        # ganho volta sozinho.
        try:
            slots.pop('_', None)
        except Exception:
            pass
        # tipo consistente: _ListaCortada mesmo no fallback, pro memo de
        # stats continuar valendo na sequencia.
        return _ListaCortada(_aplicar_cutoff_jogos(
            jogos_prep, antes_de_ts, event_id_excluir, m_vivo, m_hist))


def _tem_janela_de_tempo(*conjuntos) -> bool:
    for c in conjuntos:
        if not c:
            continue
        for x in c:
            if not isinstance(x, int):
                return True
    return False


def _stats_h2h_memo(jogos, linha_atual, janelas_wr=None, janelas_media=None,
                    lado=None, ts_ref=None):
    """Memo de _calcular_stats_h2h por (lista-do-evento, linha, lado).
    So memoiza quando: a lista e uma _ListaCortada v16 (memo atrelado ao
    evento) E nao ha janela de TEMPO (essas dependem do ts do tick).
    O calculo em si e a funcao ORIGINAL, intocada. BLINDADO: erro no memo
    -> calcula direto."""
    try:
        memo = getattr(jogos, 'memo', None)
        if memo is None or _tem_janela_de_tempo(janelas_wr, janelas_media):
            return _calcular_stats_h2h(jogos, linha_atual, janelas_wr,
                                       janelas_media, lado, ts_ref)
        chave = (linha_atual, lado,
                 frozenset(janelas_wr) if janelas_wr else None,
                 frozenset(janelas_media) if janelas_media else None)
        st = memo.get(chave)
        if st is not None:
            _V16_STATS["stats_memo_hits"] += 1
            return st
        _V16_STATS["stats_memo_miss"] += 1
        st = _calcular_stats_h2h(jogos, linha_atual, janelas_wr,
                                 janelas_media, lado, ts_ref)
        memo[chave] = st
        return st
    except Exception:
        _V16_STATS["fallbacks_stats"] += 1
        logger.exception("[v16] memo de stats falhou — calculando direto")
        return _calcular_stats_h2h(jogos, linha_atual, janelas_wr,
                                   janelas_media, lado, ts_ref)


ESPORTE_UI_PARA_BANCO = {
    'fifa':    'E-Football',
    'nba2k':   'E-Basketball',
    'ehockey': 'E-Hockey',
    'etennis': 'E-Tennis',
}


MERCADO_TIPOS_POR_CASA = {
    'betano': {
        'over_under_ft':        ['13', '157'],
        'asian_over_under_ft':  ['189'],
        'ml_ft':                ['1'],
        'btts_ft':              ['15'],
        'ah_ft':                ['156'],
        'over_under_ft_player': ['84', '85'],
        # '14' = "Total de gols - 1o Tempo" (confirmado nos ticks reais).
        # O '83' que estava aqui nao existe na betano (e da estrelabet, e la e 2o tempo).
        'over_under_ht':        ['14'],
        'ml_ht':                ['60'],
    },
    'estrelabet': {
        'over_under_ft':        ['18'],
        'ml_ft':                ['1'],
        'btts_ft':              ['29'],
        'ah_ft':                ['16'],
        'over_under_ht':        ['68'],
        'ml_ht':                ['60'],
        'ah_ht':                ['66'],
        'double_chance_ft':     ['10'],
    },
    'superbet': {
        'over_under_ft':        ['OVER_UNDER'],
        'asian_over_under_ft':  ['OVER_UNDER'],
        'ml_ft':                ['MATCH_RESULT'],
        'btts_ft':              ['BTTS'],
        'ah_ft':                ['HANDICAP'],
        'correct_score':        ['CORRECT_SCORE'],
        'over_under_ht':        ['PERIOD_TOTAL'],
        'ml_ht':                ['PERIOD_RESULT'],
        'double_chance_ft':     ['DOUBLE_CHANCE'],
        'odd_even':             ['ODD_EVEN'],
    },
    'bet365': {
        'over_under_ft':        ['1450'],
        'ah_ft':                ['1446'],
        'ml_ft':                ['180032'],
        # 03/ago: codigos de 1o TEMPO. Origem: o mapa de protocolo do
        # coletor CDP, o mesmo que o converter_betsapi grava no parquet
        # (180062 = 1st half total, 180061 = 1st half spread, 180060 =
        # 1st half money line). Antes disto, mercado HT da bet365 so
        # casava pelo fallback de PALAVRA-CHAVE (porta lateral: funciona,
        # mas depende do nome do mercado vir bonitinho).
        # NAO VERIFICADO em e-Soccer — se um desses codigos significar
        # outra coisa la, o cinto que segura e' a checagem de PERIODO pelo
        # NOME do mercado (_periodo_do_mercado), que exige "1o Tempo"/"1st
        # half" no rotulo pra aceitar como HT. Verificar no primeiro teste
        # de e-Soccer: a planilha tem que mostrar placar de INTERVALO.
        'over_under_ht':        ['180062'],
        'ah_ht':                ['180061'],
        'ml_ht':                ['180060'],
    },
}


MERCADO_KEYWORDS = {
    'over_under_ft': [
        'total de gols', 'total - jogo', 'total - partida',
        'total de pontos', 'total ', 'over/under',
    ],
    'over_under_ht': [
        '1° tempo - total', '1¬ tempo - total', '1║ tempo - total',
        'primeiro tempo - total', '1st half - total', 'total ht',
    ],
    'asian_over_under_ft': [
        'asiatico (mais/menos)', 'total de gols asiatico', 'asian total',
    ],
    'asian_over_under_ht': ['asiatico - 1', 'asian total - 1'],
    'ah_ft': [
        'handicap asiatico', 'asian handicap', 'handicap (incl', 'handicap',
    ],
    'ah_ht': ['handicap - 1', '1║ tempo - handicap'],
    'eh_ft': ['handicap europeu', 'european handicap', 'handicap 3-way'],
    'over_under_ft_player': ['(esports) - total de gols', 'jogador - total', 'pontos jogador'],
    'over_under_ht_player': ['(esports) - total - 1'],
    'ml_ft': [
        'resultado final', 'resultado (1x2)', '1x2',
        'match winner', 'vencedor', 'para ganhar',
    ],
    'ml_ht': ['resultado - 1', '1║ tempo - resultado', 'half time', '1x2 ht'],
    'btts_ft': [
        'ambas equipes marcam', 'ambas as equipes marcam',
        'ambos marcam', 'both teams to score', 'btts',
    ],
    'double_chance_ft': ['chance dupla', 'dupla chance', 'double chance'],
    'odd_even': ['par/impar', 'impar/par', 'odd/even'],
    'correct_score': ['resultado correto', 'correct score', 'placar correto'],
}


def _demojibake(txt: str) -> str:
    """Conserta dupla codificacao UTF-8->Latin-1 vinda dos coletores
    ('1Âº Tempo' -> '1º Tempo', 'prorrogaÃ§Ã£o' -> 'prorrogação').
    So age se a string tem os marcadores 'Ã'/'Â'; so aceita o resultado se ele
    REDUZIU os marcadores (nao corrompe texto legitimo). BLINDADO."""
    if not txt or ('Ã' not in txt and 'Â' not in txt):
        return txt
    try:
        arrumado = txt.encode('latin-1', errors='ignore').decode('utf-8', errors='ignore')
        if arrumado and (arrumado.count('Ã') + arrumado.count('Â')) < (txt.count('Ã') + txt.count('Â')):
            return arrumado
    except Exception:
        pass
    return txt


def _sem_acento(s) -> str:
    """minusculas sem acento, pra casar nomes de mercado entre casas.
    BLINDADO: None/numero/bytes/qualquer coisa -> str() ou '' sem crashar.
    JOB42: passa por _demojibake antes ('1Âº Tempo' era classificado como FT)."""
    if s is None:
        return ''
    try:
        txt = s if isinstance(s, str) else str(s)
        txt = _demojibake(txt)
        return ''.join(c for c in unicodedata.normalize('NFD', txt)
                       if unicodedata.category(c) != 'Mn').lower()
    except Exception:
        return ''


def _periodo_do_mercado(nome_mercado: str) -> str:
    """Deduz o PERIODO pelo NOME do mercado. Necessario porque o mercado_tipo e
    AMBIGUO em varias casas: a superbet manda '1o Tempo - Handicap',
    '2o Tempo - Handicap' e 'Quarto 3 - Handicap' TODOS como mercado_tipo
    'HANDICAP'; idem 'PERIOD_TOTAL', 'BTTS', 'DOUBLE_CHANCE', 'CORRECT_SCORE',
    'ODD_EVEN' (todos tem variante de 1o tempo).
    Sem isso, um bot de ah_ft (tempo integral) aposta em handicap de 1o tempo e
    resolve com o placar FINAL -> green/red invalido (dinheiro real).

    Retorna: 'ht' (1o tempo) | '2t' (2o tempo) | 'parcial' (quarto/set/periodo)
             | 'ft' (jogo todo; default quando nao ha marcador de periodo).
    BLINDADO: qualquer entrada (None, numero, bytes) -> nunca crasha."""
    try:
        s = _sem_acento(nome_mercado)
    except Exception:
        return 'ft'
    if not s:
        return 'ft'
    # 1o tempo (pt/en). JOB42: {0,2} tolera residuo de mojibake ('1aº tempo').
    # BANCADA E5b: inclui 'ª' (ordinal FEMININO) — estrelabet manda
    # '1ª tempo - Total de gols'; sem o ª o regex nao casava e a blindagem
    # jogava em 'parcial' -> bot de over_under_ht nunca apitava.
    if (re.search(r'\b1\s*[ao°ºª]{0,2}\s*tempo\b', s) or '1st half' in s
            or 'first half' in s or 'primeiro tempo' in s
            or re.search(r'\bht\b', s) or re.search(r'\b1\s*[ao°ºª]{0,2}\s*half\b', s)):
        return 'ht'
    # 2o tempo (pt/en)
    if (re.search(r'\b2\s*[ao°ºª]{0,2}\s*tempo\b', s) or '2nd half' in s
            or 'second half' in s or 'segundo tempo' in s):
        return '2t'
    # parciais: quarto / quarter / set / periodo / period
    if (re.search(r'\bquarto\b', s) or re.search(r'\bquarter\b', s)
            or re.search(r'\bset\b', s) or re.search(r'\bperiodo\b', s)
            or re.search(r'\bperiod\b', s)):
        return 'parcial'
    # BLINDAGEM (job 42): digito 1/2 + ate 3 chars de lixo + tempo/half que NAO
    # foi classificado acima = periodo desconhecido/corrompido. Nunca pode
    # passar como jogo inteiro - devolve 'parcial' (bot de FT rejeita).
    if re.search(r'\b[12]\S{0,3}\s*(tempo|half)\b', s):
        return 'parcial'
    return 'ft'


def _periodo_do_bot(mercado_bot: str) -> str:
    """Periodo que o mercado do bot exige.
    ATENCAO: nao basta endswith('_ht') - 'over_under_ht_player' tem o _ht no MEIO.
    Usa regex '_ht' seguido de fim-ou-underscore. Default 'ft'."""
    m = (mercado_bot or '').strip().lower()
    if re.search(r'_ht(_|$)', m):
        return 'ht'
    return 'ft'


def _matches_mercado(mercado_bot: str, tick_mercado: str, tick_mercado_tipo: str, casa: str = '') -> bool:
    if not mercado_bot:
        return True

    casa_lower = (casa or '').lower()
    mapping_casa = MERCADO_TIPOS_POR_CASA.get(casa_lower, {})

    if mercado_bot in mapping_casa:
        tipos_validos = mapping_casa[mercado_bot]
        if tick_mercado_tipo not in tipos_validos:
            return False
        if casa_lower == 'betano' and mercado_bot == 'over_under_ft':
            mercado_lower = (tick_mercado or '').lower()
            if '(esports)' in mercado_lower:
                return False
        # DESAMBIGUACAO DE PERIODO: o mercado_tipo sozinho nao distingue
        # 1o tempo / 2o tempo / quarto (ver _periodo_do_mercado). Um bot de FT
        # so aceita tick de FT; um bot de HT so aceita tick de 1o tempo.
        if _periodo_do_mercado(tick_mercado) != _periodo_do_bot(mercado_bot):
            return False
        # DESAMBIGUACAO POR NOME (bancada S10/S13): na superbet o mesmo
        # mercado_tipo cobre variantes DIFERENTES do mercado —
        # 'HANDICAP' = Handicap Asiatico + Handicap 3-Way + 2-way;
        # 'OVER_UNDER' = Total de Gols + Total de Gols Asiatico.
        # Sem este filtro um bot ah_ft apostava 3-Way (europeu: empate PERDE)
        # e um over_under_ft pegava linha asiatica .25/.75 (meio green/red que
        # o resolvedor nao trata). Regra: cada mercado_bot rejeita a variante
        # que tem mercado_bot proprio.
        nome_norm = _sem_acento(tick_mercado)
        eh_asiatico_nome = ('asiatic' in nome_norm) or ('asian' in nome_norm)
        eh_3way_nome = ('3-way' in nome_norm) or ('3 way' in nome_norm
                        or 'europeu' in nome_norm or 'european' in nome_norm)
        if mercado_bot in ('over_under_ft', 'over_under_ht') and eh_asiatico_nome:
            return False
        if mercado_bot in ('asian_over_under_ft', 'asian_over_under_ht') and not eh_asiatico_nome:
            return False
        if mercado_bot in ('ah_ft', 'ah_ht') and eh_3way_nome:
            return False
        if mercado_bot == 'eh_ft' and not eh_3way_nome:
            return False
        return True

    keywords = MERCADO_KEYWORDS.get(mercado_bot, [])
    if not keywords:
        return True
    haystack = (
        (tick_mercado or '').lower() + ' ' +
        (tick_mercado_tipo or '').lower()
    )
    return any(kw in haystack for kw in keywords)


def _parse_linha(linha_text: str) -> Optional[float]:
    if linha_text is None or linha_text == '':
        return None
    try:
        s = str(linha_text).strip()
        if '|' in s:
            s = s.split('|')[0]
        if s.startswith('+'):
            s = s[1:]
        return float(s)
    except (ValueError, TypeError):
        return None


def _normalizar(s: str) -> str:
    if s is None:
        return ''
    s = str(s).lower().strip()
    repl = {'á':'a','à':'a','ã':'a','â':'a','é':'e','ê':'e','í':'i','ó':'o','ô':'o','õ':'o','ú':'u','ç':'c'}
    for old, new in repl.items():
        s = s.replace(old, new)
    return s


def _lado_aposta(selecao: str) -> Optional[str]:
    """v9: deriva o lado ('over'/'under') da selecao do tick, pra calcular o WR
    do lado certo. Retorna None se nao for um mercado over/under."""
    s = _normalizar(selecao)
    if not s:
        return None
    # JOB42: word-boundary obrigatorio. Substring pegava 'Oklahoma City
    # ThUNDER' -> lado under (tip errada na planilha + corte errado no
    # filtro de lado quando o bot nao e 'ambos').
    if re.search(r'\b(mais|over|acima)\b', s) or s.startswith('+') or s in ('sim', 'yes'):
        return 'over'
    if re.search(r'\b(menos|under|abaixo)\b', s) or s.startswith('-') or s in ('nao', 'no'):
        return 'under'
    return None


def _resolve_resultado(mercado: str, selecao: str, linha: float,
                       score_home: int, score_away: int) -> Optional[str]:
    if score_home is None or score_away is None:
        return None
    mercados_com_linha = ('over_under_ft', 'over_under_ht', 'asian_over_under_ft',
                          'asian_over_under_ht', 'ah_ft', 'ah_ht', 'eh_ft',
                          'over_under_ft_player', 'over_under_ht_player')
    if mercado in mercados_com_linha and linha is None:
        return None

    sel = _normalizar(selecao)

    if mercado in ('over_under_ft', 'asian_over_under_ft', 'over_under_ht', 'asian_over_under_ht'):
        # over_under_ht AGORA e resolvido — mas SO com o placar do INTERVALO, que
        # o chamador passa em score_home/score_away quando ha marcador de periodo
        # nos ticks (live_time='HT'/'2Q'; hoje so a superbet emite). Sem placar de
        # HT o chamador nem chega aqui (cai no balde 'mercado_ht_sem_suporte'),
        # entao a soma abaixo ja e o total do 1o tempo.
        # O asiatico de 1o tempo (linha .25/.75) segue SEM suporte: o resolvedor
        # nao trata meio-green/meio-red.
        if mercado == 'asian_over_under_ht':
            return None

        total = score_home + score_away
        is_under = 'menos' in sel or 'under' in sel or 'abaixo' in sel or sel == 'under'
        is_over  = (not is_under) and ('mais' in sel or 'over' in sel or 'acima' in sel or sel == 'over')

        if is_over:
            if total > linha: return 'green'
            elif total < linha: return 'red'
            else: return 'void'
        elif is_under:
            if total < linha: return 'green'
            elif total > linha: return 'red'
            else: return 'void'
        return None

    if mercado == 'ah_ft':
        if 'home' in sel or 'casa' in sel or 'time a' in sel or '1' == sel:
            ajuste = score_home + linha - score_away
            if ajuste > 0: return 'green'
            elif ajuste < 0: return 'red'
            else: return 'void'
        elif 'away' in sel or 'visitante' in sel or 'fora' in sel or 'time b' in sel or '2' == sel:
            ajuste = score_away + linha - score_home
            if ajuste > 0: return 'green'
            elif ajuste < 0: return 'red'
            else: return 'void'
        return None

    if mercado == 'ml_ft':
        if score_home > score_away:
            vencedor = 'home'
        elif score_away > score_home:
            vencedor = 'away'
        else:
            vencedor = 'draw'
        if sel in ('1', 'home', 'casa') or 'home' in sel or 'casa' in sel:
            return 'green' if vencedor == 'home' else 'red'
        elif sel in ('2', 'away', 'fora') or 'away' in sel or 'visitante' in sel or 'fora' in sel:
            return 'green' if vencedor == 'away' else 'red'
        elif sel in ('x', 'draw', 'empate') or 'draw' in sel or 'empate' in sel:
            return 'green' if vencedor == 'draw' else 'red'
        return None

    if mercado == 'btts_ft':
        ambos_marcaram = score_home > 0 and score_away > 0
        is_sim = sel in ('sim', 'yes', '1') or 'sim' in sel or 'yes' in sel
        is_nao = sel in ('nao', 'no', '2') or 'nao' in sel or 'no' in sel or 'nπo' in sel
        if is_sim:
            return 'green' if ambos_marcaram else 'red'
        elif is_nao:
            return 'green' if not ambos_marcaram else 'red'
        return None

    return None


# ============================================================
# HANDICAP (ah_ft) - funcoes NOVAS (nao alteram over/under)
# ----------------------------------------------------------------------------
# Importadas tambem pelo bot_executor (fonte unica: backtest e ao vivo usam as
# MESMAS funcoes). LOGICA VALIDADA:
#   - pct vs TipManager: BERLIN x DUBLIN 480 jogos -> 97.3% (func) vs 97.2% (TM)
#   - green/red vs placares REAIS: 11/11 casos (e-football + e-basket)
#   - filtro pct: rejeita <min, blindado contra config ruim
# Funciona nos 2 esportes (basket 13.5-40, football 0.5-3.5) e 2 formatos de
# casa (estrelabet "(Nick)", sporty "[Nick]").
# ============================================================

def _cobriu_handicap(pts_time, pts_adv, linha) -> Optional[bool]:
    """Cobre <=> pts_time + linha > pts_adv. None se placar invalido."""
    if pts_time is None or pts_adv is None:
        return None
    try:
        return (float(pts_time) + float(linha)) > float(pts_adv)
    except (TypeError, ValueError):
        return None


def _resolver_pts_hc(jogo: dict, alvo_upper: str) -> tuple:
    """(pts_alvo, pts_adv) conforme o alvo seja jogador_a (home) ou _b (away)
    do jogo. (None, None) se o alvo nao esta no jogo. Blindado."""
    ja = (jogo.get('jogador_a') or '').strip().upper()
    jb = (jogo.get('jogador_b') or '').strip().upper()
    sh = jogo.get('score_home')
    sa = jogo.get('score_away')
    if ja == alvo_upper:
        return sh, sa
    if jb == alvo_upper:
        return sa, sh
    return None, None


def _pct_team_plus(jogos: list, alvo: str, linha: float) -> tuple:
    """(% cobertura, qtd_valida) do `alvo` cobrindo `linha` nos confrontos.
    Reproduz historical.all.pct_team_plus do TM (validado 97.3~97.2).
    (None, 0) se sem jogo valido."""
    if not jogos:
        return None, 0
    alvo_u = (alvo or '').strip().upper()
    cobriu = validos = 0
    for j in jogos:
        pa, pv = _resolver_pts_hc(j, alvo_u)
        r = _cobriu_handicap(pa, pv, linha)
        if r is None:
            continue
        validos += 1
        if r:
            cobriu += 1
    if validos == 0:
        return None, 0
    return cobriu / validos, validos


def _pct_adversario_cobre(jogos: list, alvo: str, linha: float) -> tuple:
    """v11 (filtro individual HC, alvo='ambos').
    (% dos jogos do `alvo` em que o ADVERSARIO cobriu +linha, qtd_valida) —
    ou seja: o alvo NAO venceu por mais que a linha, exatamente o que a aposta
    na zebra +linha precisa que aconteca com o FAVORITO. Como as linhas sao .5,
    e o complemento exato de 'alvo cobre -linha' (sem push possivel).
    (None, 0) se sem jogo valido. BLINDADO (mesma disciplina da _pct_team_plus)."""
    if not jogos:
        return None, 0
    alvo_u = (alvo or '').strip().upper()
    ok = validos = 0
    for j in jogos:
        pa, pv = _resolver_pts_hc(j, alvo_u)
        r = _cobriu_handicap(pv, pa, linha)  # adversario + linha > alvo
        if r is None:
            continue
        validos += 1
        if r:
            ok += 1
    if validos == 0:
        return None, 0
    return ok / validos, validos


def _extrair_nick_hc(selecao: str) -> Optional[str]:
    """Nick (UPPER) da selecao. 'Algeria (Kylian) (-0.5)' -> 'KYLIAN' ;
    'Home [Kiev] (+14.5)' -> 'KIEV'. Prioriza [colchete], depois (parenteses)."""
    if not selecao:
        return None
    m = re.search(r'\[([^\[\]]+)\]', selecao)
    if not m:
        m = re.search(r'\(([^()]+)\)', selecao)
    return m.group(1).strip().upper() if m else None


def _selecao_hc_valor(selecao: str) -> Optional[float]:
    """Valor com sinal do handicap no fim da selecao. Trata os 3 formatos de casa:
      estrelabet: 'Algeria (Kylian) (-0.5)'    -> -0.5   (entre parenteses)
      superbet:   'Bucks (Sevilla) (14.5)'     -> 14.5   (entre parenteses)
      betano:     'Partizan (tapachan) -15.5'  -> -15.5  (solto, sem parenteses)
      sporty:     'Home [Kiev] (+14.5)'        -> 14.5
    None se nao achar (blindado)."""
    if not selecao:
        return None
    s = str(selecao).strip()
    # 1) valor entre parenteses no fim (estrelabet/superbet/sporty)
    m = re.search(r'\(([+-]?\d+(?:\.\d+)?)\)\s*$', s)
    # 2) valor solto no fim (betano)
    if not m:
        m = re.search(r'([+-]?\d+(?:\.\d+)?)\s*$', s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, TypeError):
        return None


def _resolve_resultado_hc(selecao, jogador_a, jogador_b,
                          score_home, score_away) -> Optional[str]:
    """green/red/void de HC por NICK. Casa o nick com o lado (home/away) e
    aplica cobertura. None se nao resolver (blindado, nunca inverte)."""
    if score_home is None or score_away is None:
        return None
    nick = _extrair_nick_hc(selecao)
    hc = _selecao_hc_valor(selecao)
    if nick is None or hc is None:
        return None
    ja = (jogador_a or '').strip().upper()
    jb = (jogador_b or '').strip().upper()
    if nick == ja:
        pts_nick, pts_adv = score_home, score_away
    elif nick == jb:
        pts_nick, pts_adv = score_away, score_home
    else:
        if ja and (nick in ja or ja in nick):
            pts_nick, pts_adv = score_home, score_away
        elif jb and (nick in jb or jb in nick):
            pts_nick, pts_adv = score_away, score_home
        else:
            return None
    try:
        ajuste = float(pts_nick) + float(hc) - float(pts_adv)
    except (TypeError, ValueError):
        return None
    if ajuste > 0:
        return 'green'
    if ajuste < 0:
        return 'red'
    return 'void'


def calcular_stat_hc(jogos_h2h: list, selecao: str,
                     jogador_a: str, jogador_b: str) -> dict:
    """Stat de HC pro tick: pct do NICK cobrir a LINHA da selecao sobre todos os
    confrontos (cutoff ja aplicado pelo get_jogos). Retorna
    {'hc_pct','hc_pct_qtd','hc_linha','hc_nick'}. Blindado (selecao ruim -> pct None)."""
    nick = _extrair_nick_hc(selecao)
    hc_val = _selecao_hc_valor(selecao)
    out = {'hc_pct': None, 'hc_pct_qtd': 0, 'hc_linha': hc_val, 'hc_nick': nick}
    if nick is None or hc_val is None:
        return out
    pct, qtd = _pct_team_plus(jogos_h2h, nick, hc_val)
    out['hc_pct'] = pct
    out['hc_pct_qtd'] = qtd
    return out


def _fatiar_jogos_janela(jogos: list, janela, ts_ref) -> list:
    """Fatia a lista de confrontos pela janela de UM filtro (ramo HC).
    Convencao IDENTICA ao _calcular_stats_h2h (over/under): a lista vem do
    MAIS RECENTE pro mais antigo, entao janela de QUANTIDADE = jogos[:N]
    (0 = 'todas'); janela de TEMPO ('8h'/'24h'/'7d') = jogos com ts em
    [ts_ref - seg, ts_ref), mesmo corte do v10 do o/u.
    BLINDADO: janela invalida -> todos os jogos; ts_ref ausente em janela de
    tempo -> [] (nunca usa NOW(), nao vaza no backtest); ts de jogo ruim ->
    pula o jogo. Nunca crasha."""
    if not jogos:
        return []
    modo, valor = _parse_janela(janela)
    if modo == 'qtd':
        return list(jogos) if valor == 0 else list(jogos[:valor])
    if modo == 'tempo':
        ref = _dt_naive(ts_ref)
        if ref is None:
            return []
        try:
            corte = ref - timedelta(seconds=valor)
        except (TypeError, ValueError, OverflowError):
            return []
        sel = []
        for j in jogos:
            t = _dt_naive(j.get('ts'))
            if t is None:
                continue
            try:
                if corte <= t < ref:
                    sel.append(j)
            except TypeError:
                continue
        return sel
    return list(jogos)


def _avaliar_escadinha_hc(jogos_h2h: list, stats: dict,
                          filtros_wr: list, ts_ref,
                          jogos_indiv: Optional[dict] = None) -> tuple:
    """ESCADINHA de WR no ramo HC: TODOS os filtros valem (AND) e cada um
    computa a COBERTURA na SUA janela. (Antes: so o 1o filtro, com break, e
    sempre sobre todos os confrontos — 2o chip e janela ignorados em silencio.)

    Grava no stats as chaves wr_ult{tok} / wr_ult{tok}_qtd — as MESMAS que o
    _wr_cols da planilha ja le, entao Janela/Winrate 1-2 passam a sair
    preenchidos no export do HC sem mudanca no detalhe.

    minPartidas segue a semantica v6 do o/u:
      - _origem='hist' (chips da UI): maturidade contra o TOTAL de confrontos
        do par (qtd_h2h) — 'Ult. 5 + min 10' = analisa 5, exige par com >=10.
      - _origem='comp' (filtro complementar hc_wr): contra a qtd DA JANELA.

    v11 (filtro INDIVIDUAL): chip com base=individual computa a cobertura no
    historico INDIVIDUAL (contra qualquer adversario) em vez do confronto do
    par. `jogos_indiv` = {'zebra_nome','favorito_nome','zebra':[...],
    'favorito':[...]|None}. Semantica:
      - zebra: % dos jogos DELA em que ela cobriu +linha (_pct_team_plus).
      - hist_indiv_alvo='ambos': ALEM da zebra, o favorito tambem e checado —
        % dos jogos DELE em que o ADVERSARIO cobriu +linha (= o favorito NAO
        abriu mais que a linha), via _pct_adversario_cobre. Mesmos min/max (AND).
      - minPartidas = maturidade do TOTAL individual de cada um.
      - Chaves gravadas: wr_ult{tok}_ind (zebra) e wr_ult{tok}_indfav
        (favorito), + _qtd — sem colidir com as chaves do par.
    Filtro marcado _nao_suportado REJEITA (fail closed, v11).
    (passou, motivo). BLINDADO: falha fechada com motivo legivel."""
    nick = stats.get('hc_nick')
    hc_linha = stats.get('hc_linha')
    if nick is None or hc_linha is None:
        return False, 'stat_hc_selecao_invalida'
    qtd_global = stats.get('qtd_h2h', 0) or 0
    for _i, _f in enumerate(filtros_wr, 1):
        if _f.get('_nao_suportado'):
            return False, f"hc_f{_i}_filtro_nao_suportado({_f.get('_nao_suportado')})"
        _jw = _f.get('janela')
        # token IGUAL ao do _wr_cols (chave da planilha tem que bater 1:1)
        if isinstance(_jw, str):
            _tok = _jw
        elif _jw:
            try:
                _tok = str(int(_jw))
            except (TypeError, ValueError):
                _tok = '0'
        else:
            _tok = '0'
        # minPartidas (maturidade) — parse unico pros dois ramos
        _mp_raw = _f.get('hist_min_partidas')
        try:
            _mp = int(_mp_raw) if _mp_raw is not None else 20
        except (TypeError, ValueError):
            _mp = 20
        _mx_raw = _f.get('hist_max_partidas')
        try:
            _mxp = int(_mx_raw) if _mx_raw not in (None, '', '-') else None
        except (TypeError, ValueError):
            _mxp = None

        # ===== v11: ramo INDIVIDUAL =====
        if _eh_filtro_individual(_f):
            _ji = jogos_indiv or {}
            _z_nome = _ji.get('zebra_nome')
            _zl = _ji.get('zebra')
            if not _z_nome or _zl is None:
                return False, f'hc_f{_i}_indiv_dados_indisponiveis'
            _pz_tot, _qz_tot = _pct_team_plus(_zl, _z_nome, hc_linha)
            _fatia_z = _fatiar_jogos_janela(_zl, _jw, ts_ref)
            _pz, _qz = _pct_team_plus(_fatia_z, _z_nome, hc_linha)
            stats[f'wr_ult{_tok}_ind'] = _pz
            stats[f'wr_ult{_tok}_ind_qtd'] = _qz
            if _mxp is not None and (_qz_tot or 0) > _mxp:
                return False, f'hc_f{_i}_indiv_zebra_qtd_{_qz_tot or 0}_gt_max_{_mxp}'
            if (_qz_tot or 0) < _mp:
                return False, f'hc_f{_i}_indiv_zebra_insuf_qtd_{_qz_tot or 0}_min_{_mp}'
            if _pz is None:
                return False, f'hc_f{_i}_indiv_pct_indisponivel'
            _mn = _f.get('min') if _f.get('minAtivo') else None
            _mx = _f.get('max') if _f.get('maxAtivo') else None
            if _mn is not None:
                _v, _e = _num_seguro(_mn)
                if _e is not None:
                    return False, f'hc_f{_i}_min_{_e}'
                _v = (_v / 100.0) if _v > 1 else _v
                if _pz < _v:
                    return False, f'hc_f{_i}_indiv_pct_{_pz:.3f}_lt_min_{_v}'
            if _mx is not None:
                _v, _e = _num_seguro(_mx)
                if _e is not None:
                    return False, f'hc_f{_i}_max_{_e}'
                _v = (_v / 100.0) if _v > 1 else _v
                if _pz > _v:
                    return False, f'hc_f{_i}_indiv_pct_{_pz:.3f}_gt_max_{_v}'
            if (_f.get('hist_indiv_alvo') or 'zebra') == 'ambos':
                _f_nome = _ji.get('favorito_nome')
                _fl = _ji.get('favorito')
                if not _f_nome or _fl is None:
                    return False, f'hc_f{_i}_indiv_fav_dados_indisponiveis'
                _pf_tot, _qf_tot = _pct_adversario_cobre(_fl, _f_nome, hc_linha)
                _fatia_f = _fatiar_jogos_janela(_fl, _jw, ts_ref)
                _pf, _qf = _pct_adversario_cobre(_fatia_f, _f_nome, hc_linha)
                stats[f'wr_ult{_tok}_indfav'] = _pf
                stats[f'wr_ult{_tok}_indfav_qtd'] = _qf
                if (_qf_tot or 0) < _mp:
                    return False, f'hc_f{_i}_indiv_fav_insuf_qtd_{_qf_tot or 0}_min_{_mp}'
                if _pf is None:
                    return False, f'hc_f{_i}_indiv_fav_pct_indisponivel'
                if _mn is not None:
                    _v, _e = _num_seguro(_mn)
                    if _e is not None:
                        return False, f'hc_f{_i}_min_{_e}'
                    _v = (_v / 100.0) if _v > 1 else _v
                    if _pf < _v:
                        return False, f'hc_f{_i}_indiv_fav_pct_{_pf:.3f}_lt_min_{_v}'
                if _mx is not None:
                    _v, _e = _num_seguro(_mx)
                    if _e is not None:
                        return False, f'hc_f{_i}_max_{_e}'
                    _v = (_v / 100.0) if _v > 1 else _v
                    if _pf > _v:
                        return False, f'hc_f{_i}_indiv_fav_pct_{_pf:.3f}_gt_max_{_v}'
            continue

        # ===== ramo POR PAR (comportamento original, intacto) =====
        fatia = _fatiar_jogos_janela(jogos_h2h, _jw, ts_ref)
        pct, qtd_janela = _pct_team_plus(fatia, nick, hc_linha)
        stats[f'wr_ult{_tok}'] = pct
        stats[f'wr_ult{_tok}_qtd'] = qtd_janela
        # minPartidas (maturidade): hist -> total do par; comp -> qtd da janela
        _qtd_validar = qtd_global if _f.get('_origem', 'comp') == 'hist' else (qtd_janela or 0)
        if _mxp is not None and _qtd_validar > _mxp:
            return False, f'hc_f{_i}_qtd_{_qtd_validar}_gt_max_{_mxp}'
        if _qtd_validar < _mp:
            return False, f'hc_f{_i}_insuf_qtd_{_qtd_validar}_min_{_mp}'
        if pct is None:
            return False, f'hc_f{_i}_pct_indisponivel'
        _mn = _f.get('min') if _f.get('minAtivo') else None
        _mx = _f.get('max') if _f.get('maxAtivo') else None
        if _mn is not None:
            _v, _e = _num_seguro(_mn)
            if _e is not None:
                return False, f'hc_f{_i}_min_{_e}'
            _v = (_v / 100.0) if _v > 1 else _v
            if pct < _v:
                return False, f'hc_f{_i}_pct_{pct:.3f}_lt_min_{_v}'
        if _mx is not None:
            _v, _e = _num_seguro(_mx)
            if _e is not None:
                return False, f'hc_f{_i}_max_{_e}'
            _v = (_v / 100.0) if _v > 1 else _v
            if pct > _v:
                return False, f'hc_f{_i}_pct_{pct:.3f}_gt_max_{_v}'
    return True, ''


def _mercado_eh_hc(mercado: str) -> bool:
    """True se o mercado e handicap (ah_ft/ah_ht/eh_ft)."""
    return (mercado or '').strip().lower() in ('ah_ft', 'ah_ht', 'eh_ft')


def _hc_blacklist_bloqueia(selecao, jogador_a, jogador_b,
                           blacklist_zebra, blacklist_favorito) -> tuple:
    """Filtros 6 e 7 do HC. (bloqueia: bool, motivo: str).
      6) zebra    = o NICK apostado (lado +). Na blacklist_zebra -> bloqueia.
      7) favorito = o ADVERSARIO (lado -, o outro do par). Na blacklist_favorito
                    -> bloqueia.
    BLINDADO: listas vazias/None -> nunca bloqueia. Nick indeterminado -> nao
    bloqueia (deixa a resolucao/outros filtros decidirem; nunca inventa corte)."""
    zebra = _extrair_nick_hc(selecao)
    if zebra is None:
        return False, ''
    zebra_u = zebra.strip().upper()
    ja = (jogador_a or '').strip().upper()
    jb = (jogador_b or '').strip().upper()
    # favorito = o lado que NAO e a zebra (mesma logica de casamento do resolve_hc)
    if zebra_u == ja:
        favorito = jb
    elif zebra_u == jb:
        favorito = ja
    elif ja and (zebra_u in ja or ja in zebra_u):
        favorito = jb
    elif jb and (zebra_u in jb or jb in zebra_u):
        favorito = ja
    else:
        favorito = None
    bl_z = {str(x).strip().upper() for x in (blacklist_zebra or []) if x}
    bl_f = {str(x).strip().upper() for x in (blacklist_favorito or []) if x}
    if zebra_u in bl_z:
        return True, f'blacklist_zebra_{zebra_u}'
    if favorito and favorito in bl_f:
        return True, f'blacklist_favorito_{favorito}'
    return False, ''


def _zebra_favorito(selecao, jogador_a, jogador_b) -> tuple:
    """v11. (nome_zebra, nome_favorito) com os NOMES ORIGINAIS do tick (ja/jb),
    casando o nick da selecao com o lado — mesma logica de casamento do
    _resolve_resultado_hc / _hc_blacklist_bloqueia. Devolve os nomes EXATOS do
    tick (nao o nick upper) pra busca no cache individual bater com o banco.
    (None, None) se o nick nao casar (blindado: quem chama trata fail closed)."""
    nick = _extrair_nick_hc(selecao)
    if nick is None:
        return None, None
    nu = nick.strip().upper()
    ja_o = (jogador_a or '').strip()
    jb_o = (jogador_b or '').strip()
    ja_u = ja_o.upper()
    jb_u = jb_o.upper()
    if nu and nu == ja_u:
        return ja_o, jb_o
    if nu and nu == jb_u:
        return jb_o, ja_o
    if ja_u and (nu in ja_u or ja_u in nu):
        return ja_o, jb_o
    if jb_u and (nu in jb_u or jb_u in nu):
        return jb_o, ja_o
    return None, None


def _ramo_hc_pct(stats: dict, min_v, max_v, min_partidas: int) -> tuple:
    """Ramo de filtro do HC: valida qtd>=min_partidas e hc_pct vs min/max.
    (passou, motivo). Blindado (config ruim -> falha fechada com motivo)."""
    qtd = stats.get('hc_pct_qtd', 0) or 0
    if qtd < min_partidas:
        return False, f'hc_h2h_insuf_qtd_{qtd}_min_{min_partidas}'
    valor = stats.get('hc_pct')
    if valor is None:
        return False, 'stat_hc_pct_indisponivel'
    if min_v is not None:
        mn, err = _num_seguro(min_v)
        if err is not None:
            return False, f'bot.hc_min_{err}'
        if valor < mn:
            return False, f'hc_pct_{valor:.3f}_lt_min_{mn}'
    if max_v is not None:
        mx, err = _num_seguro(max_v)
        if err is not None:
            return False, f'bot.hc_max_{err}'
        if valor > mx:
            return False, f'hc_pct_{valor:.3f}_gt_max_{mx}'
    return True, ''


# ============================================================
# H2H CACHE
# ============================================================

def _dt_naive(dt):
    """Remove o tzinfo mantendo o horario de PAREDE (nao converte fuso).

    Serve pra comparar datas naive (do banco) com aware (do parquet, que vem
    com 'Z'). Regra do projeto: o 'Z' da TM/coletor ja e BRT -> NUNCA converter
    UTC->BRT, so tirar o tz. Assim naive-do-banco e (ex-)aware-do-parquet ficam
    no mesmo fuso e a comparacao para de estourar 'can't compare naive/aware'.
    """
    if dt is not None and getattr(dt, 'tzinfo', None) is not None:
        return dt.replace(tzinfo=None)
    return dt


def _margem_hist_min_por_esporte(esporte) -> int:
    """v12: margem de seguranca (min) pra jogo que so existe no h2h_historico
    (sem tick correspondente). O ts do hist e o INICIO oficial do jogo — nao
    da pra saber o fim. A margem cobre a DURACAO MAXIMA de um jogo + lag de
    publicacao da TM, garantindo que o jogo ja tinha TERMINADO (e sido
    publicado) antes do momento da aposta. Duracao real tipica: e-football
    2x4min ~12-15min; e-hockey/e-tennis curtos; e-basketball 4x5min ~30-40min
    com pausas. Conservador por design: melhor descartar um jogo legitimo
    muito recente do que deixar o placar final de um jogo em andamento
    (incluindo o PROPRIO jogo da aposta) vazar pro Ult.N."""
    e = str(esporte or '').lower()
    if 'foot' in e or 'futebol' in e:
        return 20
    if 'hock' in e or 'tenn' in e or 'tenis' in e:
        return 25
    return 45


def _aplicar_cutoff_jogos(jogos: list, antes_de_ts, event_id_excluir,
                          margem_ao_vivo_min: int,
                          margem_hist_min: int = 45) -> list:
    """v12 (fix do look-ahead, 28/jul): corte temporal da lista bruta do cache
    pro momento da aposta — compartilhado pelos caches H2H e INDIVIDUAL.
      - so jogos com ts < antes_de_ts (nunca vaza futuro);
      - jogo com FIM conhecido (ultimo_tick_ts — nativo do tick, ou herdado
        pelo hist no dedup): fim >= antes-margem_ao_vivo = ainda AO VIVO no
        momento da aposta (placar parcial / resultado ainda nao publicado)
        -> descartado;
      - jogo de hist SEM fim conhecido (nao casou com nenhum tick — tipico
        de jogo antigo cujos ticks ja sairam da retencao, ou importado por
        CSV): o ts e o INICIO oficial; o placar final desse registro pode
        nem existir ainda no momento da aposta (jogo em andamento — incluindo
        o PROPRIO jogo). So entra se comecou ha pelo menos margem_hist_min
        antes da aposta;
      - exclui o jogo atual por event_id (da casa) E por event_id_tick
        (herdado no dedup — o event_id nativo do hist e o da TM, nao bate).
    Era exatamente por aqui que o backtest enxergava o resultado do proprio
    jogo (wr_bt != wr_vivo, provado no bot 54 em 28/jul): o registro hist
    entrava com ts=inicio e placar final, sem margem e sem exclusao por id.
    BLINDADO: ts None / aware-vs-naive -> pula o jogo em vez de crashar."""
    antes = _dt_naive(antes_de_ts)
    if antes is None:
        return []
    try:
        corte_ao_vivo = antes - timedelta(minutes=margem_ao_vivo_min)
        corte_hist = antes - timedelta(minutes=margem_hist_min)
    except (TypeError, ValueError, OverflowError):
        return []
    out = []
    for j in jogos:
        tsj = _dt_naive(j.get('ts'))
        if tsj is None:
            continue
        try:
            if tsj >= antes:
                continue
            ult = _dt_naive(j.get('ultimo_tick_ts'))
            if ult is not None:
                # fim (ultima atividade) conhecido — vale pra tick E pra hist
                # que herdou o fim no dedup
                if ult >= corte_ao_vivo:
                    continue
            elif j.get('fonte') == 'tick':
                # tick sem ultimo_tick_ts (nao deveria ocorrer): conservador
                continue
            else:
                # hist puro: exige inicio anterior a margem de seguranca
                if tsj > corte_hist:
                    continue
        except TypeError:
            continue
        out.append(j)
    if event_id_excluir is not None:
        eid_str = str(event_id_excluir)
        out = [j for j in out
               if str(j.get('event_id')) != eid_str
               and str(j.get('event_id_tick')) != eid_str]
    return out


def _montar_jogos_e_dedup(rows) -> list:
    """v11: converte rows do banco em dicts de jogo + dedup ENTRE fontes
    (tick x hist) — logica v8 do H2HCache, EXTRAIDA pra reuso do cache
    individual. O mesmo jogo pode estar no ticks (ts = quando o coletor
    capturou) E no h2h_historico (ts = horario oficial da TM), com lag de
    ~20-40min e placar possivelmente invertido (perspectiva A/B trocada).
    Sem dedup, o jogo conta 2x e infla a amostra.

    Criterio: mesmo PLACAR NORMALIZADO (ordenado, pega inversao) + mesmo PAR
    de jogadores (case-insensitive) dentro de JANELA_DEDUP_MIN, e SO entre
    fontes diferentes (tick vs hist). Mantem o do historico (placar oficial
    TM) e descarta o do tick. Jogos da MESMA fonte com mesmo placar ficam
    preservados (jogos reais distintos).
    (O criterio de PAR e redundante no cache por par — la os dois lados sao
    sempre os mesmos — e protege o cache INDIVIDUAL de deduplicar jogos
    distintos do mesmo jogador contra adversarios diferentes.)
    Retorna ordenado do mais recente pro mais antigo."""
    jogos = []
    for r in rows:
        sh = r['score_home']
        sa = r['score_away']
        jogos.append({
            'event_id': r['event_id'],
            'ts': r['ts'],
            'jogador_a': r['jogador_a'],
            'jogador_b': r['jogador_b'],
            'score_home': sh,
            'score_away': sa,
            # BLINDADO: coage os placares a numero seguro antes de somar. Se
            # sh/sa vierem None -> 0; string numerica -> parseia; lixo -> 0.
            # Sem isso, um placar string ('5') faria '5'+'3'='53' (concat) e
            # o media()/desvio() quebrariam. Garante 'total' SEMPRE int.
            'total': int(_num_seguro(sh)[0] or 0) + int(_num_seguro(sa)[0] or 0),
            'fonte': r['fonte'],
            'ultimo_tick_ts': r['ultimo_tick_ts'],
        })

    JANELA_DEDUP_MIN = 45  # lag tipico tick<->TM
    jogos.sort(key=lambda x: x['ts'])  # mais antigo primeiro
    manter = []
    for jg in jogos:
        placar_norm = tuple(sorted([jg['score_home'] or 0, jg['score_away'] or 0]))
        par_norm = tuple(sorted([(jg.get('jogador_a') or '').strip().upper(),
                                 (jg.get('jogador_b') or '').strip().upper()]))
        achou = None
        for m in manter:
            if m.get('_descartado'):
                continue
            if m['fonte'] == jg['fonte']:
                continue  # so dedup entre fontes diferentes
            m_norm = tuple(sorted([m['score_home'] or 0, m['score_away'] or 0]))
            if m_norm != placar_norm:
                continue
            m_par = tuple(sorted([(m.get('jogador_a') or '').strip().upper(),
                                  (m.get('jogador_b') or '').strip().upper()]))
            if m_par != par_norm:
                continue
            dt = abs((jg['ts'] - m['ts']).total_seconds()) / 60.0
            if dt <= JANELA_DEDUP_MIN:
                achou = m
                break
        if achou is not None:
            # mesmo jogo na outra fonte: mantem o 'hist' (placar oficial TM),
            # descarta o 'tick' — MAS antes o registro mantido HERDA do tick:
            #   - ultimo_tick_ts (o FIM real do jogo): permite ao cutoff saber
            #     que o jogo ja tinha TERMINADO no momento da aposta (o ts do
            #     hist e o INICIO oficial — sozinho ele deixa o placar final
            #     de um jogo ainda em andamento vazar pro Ult.N);
            #   - event_id_tick (o id da CASA): permite ao cutoff excluir o
            #     PROPRIO jogo da aposta tambem pelo lado hist (o event_id
            #     nativo do hist e o da TM e nao bate com o da casa).
            # Sem essa heranca, o backtest enxergava o resultado do proprio
            # jogo no Ult.N (look-ahead) — vazamento provado 28/jul (bot 54:
            # 22/80 apostas com wr_bt != wr_vivo, reds sumindo do backtest).
            if achou['fonte'] == 'hist':
                achou['ultimo_tick_ts'] = (achou.get('ultimo_tick_ts')
                                           or jg.get('ultimo_tick_ts')
                                           or jg.get('ts'))
                if jg.get('event_id') is not None:
                    achou['event_id_tick'] = jg.get('event_id')
                jg['_descartado'] = True
            else:
                jg['ultimo_tick_ts'] = (jg.get('ultimo_tick_ts')
                                        or achou.get('ultimo_tick_ts')
                                        or achou.get('ts'))
                if achou.get('event_id') is not None:
                    jg['event_id_tick'] = achou.get('event_id')
                achou['_descartado'] = True
                jg['_descartado'] = False
            manter.append(jg)
        else:
            jg['_descartado'] = False
            manter.append(jg)

    jogos = [j for j in manter if not j.get('_descartado')]
    for j in jogos:
        j.pop('_descartado', None)
    jogos.sort(key=lambda x: x['ts'], reverse=True)
    return jogos


class H2HCache:
    LIMITE_JOGOS_POR_PAR = 100      # janela padrao/maxima dos filtros normais
    TETO_BUSCA = 5000               # teto absoluto do _buscar (suporta 'todas')

    def __init__(self, pool, casa: str, esporte_banco: str):
        self._pool = pool
        self._casa = casa
        self._esporte = esporte_banco
        self._cache: dict = {}
        self._cortes: dict = {}   # v16: {par -> {'_': _SlotCorte}}

    @staticmethod
    def _normalizar_par(ja: str, jb: str) -> tuple:
        a = (ja or '').strip()
        b = (jb or '').strip()
        return tuple(sorted([a, b]))

    # v7 FIX (18/06/2026): margem ao-vivo. Se o ultimo tick de um jogo
    # (vindo de 'ticks') foi ha menos de MARGEM_AO_VIVO_MIN minutos ANTES
    # do tick avaliado, o jogo ainda estava AO VIVO (placar parcial) e NAO
    # deve contar no WR. Jogos de h2h_historico (placar final) nunca filtram.
    MARGEM_AO_VIVO_MIN = 15

    async def get_jogos(self, ja: str, jb: str, antes_de_ts, event_id_excluir=None) -> list:
        """
        Retorna jogos do par com ts < antes_de_ts.

        v5: aceita event_id_excluir pra remover o jogo atual da lista H2H
        (evita contar o placar parcial do jogo em andamento no calculo do WR).
        v7 (FIX): exclui jogos que vieram de TICKS e ainda estavam ao vivo
        no momento da aposta (ultimo tick < MARGEM_AO_VIVO_MIN antes de
        antes_de_ts). Esses tem placar PARCIAL e poluiam o WR. Jogos de
        h2h_historico (placar final real da TM/CSV) nunca sao filtrados.
        Usa antes_de_ts (nao NOW()) -> funciona igual em backtest e ao vivo.
        v11: corte extraido pro helper _aplicar_cutoff_jogos (compartilhado
        com o cache INDIVIDUAL) — comportamento identico ao v7.
        """
        par = self._normalizar_par(ja, jb)
        if not par[0] or not par[1]:
            return []

        if par not in self._cache:
            self._cache[par] = _preparar_jogos_inplace(
                await self._buscar(par[0], par[1]))

        if USAR_CUTOFF_V16:
            return _cutoff_v16(self._cortes.setdefault(par, {}),
                               self._cache[par], antes_de_ts, event_id_excluir,
                               self.MARGEM_AO_VIVO_MIN,
                               _margem_hist_min_por_esporte(
                                   getattr(self, '_esporte', None)))
        return _aplicar_cutoff_jogos(self._cache[par], antes_de_ts,
                                     event_id_excluir, self.MARGEM_AO_VIVO_MIN,
                                     _margem_hist_min_por_esporte(
                                         getattr(self, '_esporte', None)))

    async def _buscar(self, j1: str, j2: str) -> list:
        # v7: o lado ticks traz tambem o ts do ultimo tick (ultimo_tick_ts)
        # e marca fonte='tick' vs fonte='hist', pra get_jogos saber se o jogo
        # estava ao vivo no momento da aposta e filtrar so os de tick.
        sql = """
        SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
               ultimo_tick_ts, fonte
        FROM (
            """ + ("""
            SELECT event_id, ts_fim AS ts, jogador_a, jogador_b,
                   score_a AS score_home, score_b AS score_away,
                   ts_fim AS ultimo_tick_ts, 'tick' AS fonte
            FROM h2h_matches
            WHERE bookmaker = $1
              AND sport = $2
              AND ((jogador_a = $3 AND jogador_b = $4)
                OR (jogador_a = $4 AND jogador_b = $3))
              AND score_a IS NOT NULL AND score_b IS NOT NULL
            """ if USAR_H2H_MATCHES else """
            SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
                   ts AS ultimo_tick_ts, 'tick' AS fonte
            FROM (
                SELECT DISTINCT ON (event_id)
                    event_id, ts, jogador_a, jogador_b, score_home, score_away
                FROM ticks
                WHERE bookmaker = $1
                  AND sport = $2
                  AND ((jogador_a = $3 AND jogador_b = $4)
                    OR (jogador_a = $4 AND jogador_b = $3))
                  AND score_home IS NOT NULL
                  AND score_away IS NOT NULL
                ORDER BY event_id, ts DESC
            ) ticks_distinct
            """) + """

            UNION ALL

            SELECT event_id,
                   -- v12.1 (fix do fuso, 28/jul): a coluna ts do h2h_historico
                   -- e timestamp SEM timezone gravada em HORARIO DE BRASILIA
                   -- (seeder da TM). Sem a conversao explicita, a promocao na
                   -- UNION com o lado ticks (timestamptz) depende do TimeZone
                   -- da sessao — em UTC, todo registro parece 3h MAIS ANTIGO,
                   -- o que furou a margem_hist e o dedup: o registro do
                   -- PROPRIO jogo (inicio 20:03Z gravado como 17:03) passava
                   -- por "jogo antigo" e vazava o resultado pro Ult.N
                   -- (provado no gate: KARMA|TAAPZ wr 1.0->0.9 pos-fix-v12).
                   (ts AT TIME ZONE 'America/Sao_Paulo') AS ts,
                   jogador_a, jogador_b, score_home, score_away,
                   NULL::timestamptz AS ultimo_tick_ts, 'hist' AS fonte
            FROM h2h_historico
            WHERE sport = $2
              AND ((UPPER(jogador_a) = UPPER($3) AND UPPER(jogador_b) = UPPER($4))
                OR (UPPER(jogador_a) = UPPER($4) AND UPPER(jogador_b) = UPPER($3)))
              AND score_home IS NOT NULL
              AND score_away IS NOT NULL
        ) combinado
        ORDER BY ts DESC
        LIMIT $5
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    sql, self._casa, self._esporte, j1, j2, self.TETO_BUSCA
                )
        except Exception as e:
            logger.exception(f"[h2h] Erro buscando par ({j1}, {j2}): {e}")
            return []

        # v11: montagem + dedup entre fontes extraidos pro helper compartilhado
        # (_montar_jogos_e_dedup) — logica v8 identica.
        return _montar_jogos_e_dedup(rows)

    @property
    def stats_cache(self):
        return {
            'pares_carregados': len(self._cache),
            'jogos_total': sum(len(v) for v in self._cache.values()),
        }


class HistIndividualCache:
    """v11: cache de historico INDIVIDUAL por jogador (filtros base=individual).

    Mesmo contrato do H2HCache, mas a chave e UM jogador e a busca traz TODOS
    os jogos dele (contra QUALQUER adversario) — ticks + h2h_historico, com o
    MESMO cutoff temporal, margem ao-vivo e dedup entre fontes do cache por par
    (helpers compartilhados _aplicar_cutoff_jogos / _montar_jogos_e_dedup),
    pra backtest e ao vivo NUNCA divergirem.

    RESSALVA DE DADO (honestidade): o h2h_historico e semeado por PAR via
    TipManager, entao o 'individual' do banco e a UNIAO dos pares ja sincados
    daquele jogador — pra ligas ativas cobre bem, mas nao e o last_10_player
    oficial do TM.
    """
    TETO_BUSCA = H2HCache.TETO_BUSCA
    MARGEM_AO_VIVO_MIN = H2HCache.MARGEM_AO_VIVO_MIN

    def __init__(self, pool, casa: str, esporte_banco: str):
        self._pool = pool
        self._casa = casa
        self._esporte = esporte_banco
        self._cache: dict = {}
        self._cortes: dict = {}   # v16: {jogador -> {'_': _SlotCorte}}

    @staticmethod
    def _chave(jogador: str) -> str:
        return (jogador or '').strip()

    async def get_jogos(self, jogador: str, antes_de_ts, event_id_excluir=None) -> list:
        """Jogos INDIVIDUAIS do jogador com ts < antes_de_ts (contra qualquer
        adversario), mesmas regras de corte do cache por par."""
        ch = self._chave(jogador)
        if not ch:
            return []
        if ch not in self._cache:
            self._cache[ch] = _preparar_jogos_inplace(await self._buscar(ch))
        if USAR_CUTOFF_V16:
            return _cutoff_v16(self._cortes.setdefault(ch, {}),
                               self._cache[ch], antes_de_ts, event_id_excluir,
                               self.MARGEM_AO_VIVO_MIN,
                               _margem_hist_min_por_esporte(
                                   getattr(self, '_esporte', None)))
        return _aplicar_cutoff_jogos(self._cache[ch], antes_de_ts,
                                     event_id_excluir, self.MARGEM_AO_VIVO_MIN,
                                     _margem_hist_min_por_esporte(
                                         getattr(self, '_esporte', None)))

    async def _buscar(self, jogador: str) -> list:
        # Espelho do _buscar do par, com UM jogador em QUALQUER lado.
        # ticks: match exato (usa indice; os nomes vem do proprio tick ou do
        # ja/jb casado pelo _zebra_favorito, entao batem com o banco).
        # h2h_historico: UPPER dos dois lados (mesma tolerancia do par).
        sql = """
        SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
               ultimo_tick_ts, fonte
        FROM (
            """ + ("""
            SELECT event_id, ts_fim AS ts, jogador_a, jogador_b,
                   score_a AS score_home, score_b AS score_away,
                   ts_fim AS ultimo_tick_ts, 'tick' AS fonte
            FROM h2h_matches
            WHERE bookmaker = $1
              AND sport = $2
              AND (jogador_a = $3 OR jogador_b = $3)
              AND score_a IS NOT NULL AND score_b IS NOT NULL
            """ if USAR_H2H_MATCHES else """
            SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
                   ts AS ultimo_tick_ts, 'tick' AS fonte
            FROM (
                SELECT DISTINCT ON (event_id)
                    event_id, ts, jogador_a, jogador_b, score_home, score_away
                FROM ticks
                WHERE bookmaker = $1
                  AND sport = $2
                  AND (jogador_a = $3 OR jogador_b = $3)
                  AND score_home IS NOT NULL
                  AND score_away IS NOT NULL
                ORDER BY event_id, ts DESC
            ) ticks_distinct
            """) + """

            UNION ALL

            SELECT event_id,
                   -- v12.1: mesmo fix do fuso do cache por par (ver comentario
                   -- la) — ts do h2h_historico e naive-BRT; converte explicito.
                   (ts AT TIME ZONE 'America/Sao_Paulo') AS ts,
                   jogador_a, jogador_b, score_home, score_away,
                   NULL::timestamptz AS ultimo_tick_ts, 'hist' AS fonte
            FROM h2h_historico
            WHERE sport = $2
              AND (UPPER(jogador_a) = UPPER($3) OR UPPER(jogador_b) = UPPER($3))
              AND score_home IS NOT NULL
              AND score_away IS NOT NULL
        ) combinado
        ORDER BY ts DESC
        LIMIT $4
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    sql, self._casa, self._esporte, jogador, self.TETO_BUSCA
                )
        except Exception as e:
            logger.exception(f"[indiv] Erro buscando jogador ({jogador}): {e}")
            return []

        return _montar_jogos_e_dedup(rows)

    @property
    def stats_cache(self):
        return {
            'jogadores_carregados': len(self._cache),
            'jogos_total': sum(len(v) for v in self._cache.values()),
        }


# ============================================================
# NORMALIZACAO E EXTRAÇAO DE FILTROS (v4 / v11)
# ============================================================

def _normalizar_indiv_alvo(v) -> str:
    """v11. Alvo do filtro INDIVIDUAL no HC: 'zebra' (default) ou 'ambos'
    (zebra E favorito). Qualquer valor desconhecido vira 'zebra' — falha
    SEGURA: nunca inventa exigencia sobre o favorito que o usuario nao pediu.
    No over/under o campo e ignorado (la o AND dos dois jogadores e fixo)."""
    s = str(v or '').strip().lower()
    if s in ('ambos', 'os_dois', 'os dois', 'zebra_favorito', 'zebra+favorito', 'both'):
        return 'ambos'
    return 'zebra'


def _normalizar_filtros_hist(filtros_hist: list) -> list:
    """
    Converte filtrosHistAdicionados (formato antigo) pro mesmo formato
    do filtrosCompAdicionados, pra unificar o processamento.

    Formato origem (filtrosHistAdicionados):
      {
        "base": "match" | "individual",
        "janela": "last_10",
        "prob": [70, 100],
        "tipo": "all" | "same_grade" | "specific_teams",
        "versao": "all",
        "minPartidas": 10,
        "indivAlvo": "zebra" | "ambos"   # v11, opcional (so HC individual)
      }

    Formato destino:
      {
        "tipo": "wr",
        "janela": 10,
        "min": 0.7,           # 70/100 (decimal)
        "max": 1.0,           # 100/100 (decimal)
        "minAtivo": True,
        "maxAtivo": False,    # max=1.0 (100%) eh "nao filtrado"
        "hist_base": "match",
        "hist_tipo": "all",
        "hist_min_partidas": 10,
        "hist_indiv_alvo": "zebra",   # v11
        "_origem": "hist",
      }
    """
    normalizados = []
    for fh in filtros_hist or []:
        if not isinstance(fh, dict):
            continue

        # Parse janela. Aceita:
        #   'all'         -> 0 (TODAS, usa todo o historico do par)
        #   'last_0'      -> 0 (TODAS, alias)
        #   'last_N'      -> N (quantidade: ultimos N jogos)
        #   'last_8h'/'last_7d' -> '8h'/'7d' (TEMPO: janela por horas/dias)
        # v10: janelas de TEMPO agora SAO suportadas (antes eram descartadas).
        # O _calcular_stats_h2h sabe processar tanto quantidade (int) quanto
        # tempo (token '8h'/'7d'), usando o ts de cada jogo + ts_ref da aposta.
        janela_str = str(fh.get('janela', '')).strip().lower()
        janela_norm = None  # int (qtd) OU str token de tempo ('8h')
        if janela_str == 'all':
            janela_norm = 0
        elif janela_str.startswith('last_'):
            resto = janela_str.replace('last_', '').strip()
            modo, _ = _parse_janela(resto)
            if modo == 'qtd':
                janela_norm = int(resto)
            elif modo == 'tempo':
                janela_norm = _janela_token(resto)  # '8h','24h','7d'...
            # resto invalido -> janela_norm None -> filtro descartado abaixo
        if janela_norm is None:
            continue
        if isinstance(janela_norm, int) and janela_norm < 0:
            continue

        # prob: [min, max] em % (0-100)
        prob = fh.get('prob') or [0, 100]
        if not isinstance(prob, list) or len(prob) < 2:
            prob = [0, 100]
        prob_min = float(prob[0]) if prob[0] is not None else 0
        prob_max = float(prob[1]) if prob[1] is not None else 100

        # Converte % (0-100) pra decimal (0.0-1.0) usado nos stats
        min_v = prob_min / 100.0
        max_v = prob_max / 100.0

        # Min/Max so "ativos" se nao forem o extremo (0% ou 100% = sem filtro real)
        min_ativo = prob_min > 0
        max_ativo = prob_max < 100

        normalizados.append({
            'tipo': 'wr',
            'janela': janela_norm,
            'min': min_v,
            'max': max_v,
            'minAtivo': min_ativo,
            'maxAtivo': max_ativo,
            'hist_base': fh.get('base', 'match'),
            'hist_tipo': fh.get('tipo', 'all'),
            'hist_min_partidas': fh.get('minPartidas'),
            # v15: TETO de maturidade (maxPartidas). Mesma unidade do
            # minPartidas: total do par p/ filtros hist, qtd da janela p/ comp.
            # None/ausente = SEM teto — todo bot e job existente segue identico.
            'hist_max_partidas': fh.get('maxPartidas'),
            'hist_indiv_alvo': _normalizar_indiv_alvo(
                fh.get('indivAlvo', fh.get('indiv_alvo'))),
            '_origem': 'hist',
        })

    return normalizados


def _coletar_todos_filtros(filtros: dict) -> list:
    """
    Pega filtros dos 2 lugares e retorna lista unificada.

    v11: base='individual' passou a ser SUPORTADO (WR sobre o historico
    individual de cada jogador — ver _aplicar_filtros_complementares e
    _avaliar_escadinha_hc). Combos ainda nao suportados (tipo != 'all',
    base desconhecida) NAO sao mais descartados em silencio: ficam na lista
    MARCADOS com '_nao_suportado' e o avaliador REJEITA o tick com motivo
    claro (FAIL CLOSED). Antes o bot rodava SEM o filtro configurado,
    apostando como se ele nao existisse — inaceitavel com dinheiro real.

    Retorna sempre lista (pode ser vazia).
    """
    filtros_comp = filtros.get('filtrosCompAdicionados') or []
    filtros_hist = filtros.get('filtrosHistAdicionados') or []
    filtros_hist_norm = _normalizar_filtros_hist(filtros_hist)

    filtros_hist_final = []
    for f in filtros_hist_norm:
        base = (f.get('hist_base') or 'match')
        tipo = (f.get('hist_tipo') or 'all')
        if base in ('match', 'individual') and tipo == 'all':
            filtros_hist_final.append(f)
        else:
            f['_nao_suportado'] = f'base={base},tipo={tipo}'
            filtros_hist_final.append(f)
            logger.warning(
                f"FiltroHist NAO SUPORTADO (base={base}, tipo={tipo}, "
                f"janela={f.get('janela')}) -> ticks deste bot serao "
                f"REJEITADOS (fail closed, v11)"
            )

    return list(filtros_comp) + filtros_hist_final


def _eh_filtro_individual(f) -> bool:
    """v11: True se o filtro e um chip de WR valido com base=individual."""
    return (isinstance(f, dict)
            and not f.get('_nao_suportado')
            and (f.get('hist_base') or 'match') == 'individual'
            and (f.get('tipo') or '').lower().strip() in ('wr', 'hc_wr'))


def _tem_filtro_individual(filtros_unificados: list) -> bool:
    """v11: True se existe ao menos um chip individual valido."""
    return any(_eh_filtro_individual(f) for f in (filtros_unificados or []))


def _primeiro_nao_suportado(filtros_unificados: list) -> Optional[str]:
    """v11: motivo do 1o filtro marcado _nao_suportado, ou None. Usado pelos
    executores pra rejeitar o tick CEDO (fail closed) com contador proprio."""
    for f in (filtros_unificados or []):
        if isinstance(f, dict) and f.get('_nao_suportado'):
            return str(f.get('_nao_suportado'))
    return None


def _janelas_wr_individuais(filtros_unificados: list) -> set:
    """v11: janelas (int de qtd ou token de tempo '24h') dos chips INDIVIDUAIS
    de WR — passadas ao _calcular_stats_h2h de cada jogador. Blindada."""
    jans: set = set()
    for f in (filtros_unificados or []):
        if not _eh_filtro_individual(f):
            continue
        janela = f.get('janela')
        modo, _ = _parse_janela(janela)
        if modo == 'qtd':
            j = int(janela)
            if j == 0 or (1 <= j <= H2HCache.TETO_BUSCA):
                jans.add(j)
        elif modo == 'tempo':
            tok = _janela_token(janela)
            if tok is not None:
                jans.add(tok)
    return jans


def _espelhar_stats_individuais(stats: dict, stats_a: dict, stats_b: dict) -> None:
    """v11: copia os numeros INDIVIDUAIS pro stats salvo (jsonb da aposta,
    planilha, telegram): indiv_a_wr_ult{tok} / indiv_b_wr_ult{tok} (+ _qtd),
    qtd_indiv_a/b, e o combinado wr_ult{tok}_indmin = MIN(a, b) — o numero que
    efetivamente decide o gate AND (os dois passam <=> o pior dos dois passa).
    BLINDADO: erro no espelho nunca derruba a avaliacao da aposta."""
    try:
        stats['qtd_indiv_a'] = stats_a.get('qtd_h2h', 0)
        stats['qtd_indiv_b'] = stats_b.get('qtd_h2h', 0)
        for k, v in stats_a.items():
            if k.startswith('wr_ult'):
                stats[f'indiv_a_{k}'] = v
        for k, v in stats_b.items():
            if k.startswith('wr_ult'):
                stats[f'indiv_b_{k}'] = v
        for k, va in stats_a.items():
            if k.startswith('wr_ult') and not k.endswith('_qtd'):
                vb = stats_b.get(k)
                stats[f'{k}_indmin'] = (min(va, vb)
                                        if (va is not None and vb is not None)
                                        else None)
    except Exception as e:
        logger.warning(f"[stats] espelho individual falhou (segue sem): {e}")


# ============================================================
# CALCULO DE STATS H2H
# ============================================================

MIN_H2H_DEFAULT = 5

# Janelas padrao SEMPRE calculadas (compatibilidade backwards)
JANELAS_PADRAO_WR = (5, 10, 15)
JANELAS_PADRAO_MEDIA = (5, 10, 20)


# ============================================================
# JANELAS: quantidade (int) OU tempo (string "24h"/"7d")
# ============================================================
# Uma janela de filtro pode ser:
#   - QUANTIDADE: int N -> ultimos N jogos (ex: 10 = ult10). 0 = TODAS.
#   - TEMPO: string "Nh"/"Nd" -> jogos nas ultimas N horas/dias (ex: "24h","7d").
# As janelas de tempo so do PAR (H2H) por enquanto. O ts de referencia e o
# momento da aposta (tick['ts'] no ao vivo, ts do tick no backtest) - mesmo
# cutoff temporal das janelas de quantidade, sem leak.
_RE_JANELA_TEMPO = re.compile(r'^\s*(\d+)\s*([hd])\s*$', re.IGNORECASE)


def _parse_janela(janela):
    """Normaliza uma janela. Retorna (modo, valor):
      ('qtd', n)            quantidade (int>=1, ou 0='todas')
      ('tempo', segundos)   tempo ('24h'->86400, '7d'->604800)
      (None, None)          invalida
    """
    if isinstance(janela, bool):  # bool e subclasse de int - barra antes
        return (None, None)
    if isinstance(janela, int):
        return ('qtd', janela) if (janela == 0 or janela >= 1) else (None, None)
    if janela is None:
        return (None, None)
    s = str(janela).strip().lower()
    m = _RE_JANELA_TEMPO.match(s)
    if m:
        num = int(m.group(1))
        if num <= 0:
            return (None, None)
        return ('tempo', num * 3600 if m.group(2) == 'h' else num * 86400)
    try:
        n = int(s)
        return ('qtd', n) if (n == 0 or n >= 1) else (None, None)
    except (TypeError, ValueError):
        return (None, None)


def _janela_token(janela):
    """Token canonico da janela pra montar a chave de stats:
      10 -> '10', '24h' -> '24h', '7d' -> '7d'. None se invalida."""
    modo, _ = _parse_janela(janela)
    if modo == 'tempo':
        return str(janela).strip().lower().replace(" ", "")
    if modo == 'qtd':
        return str(int(janela))
    return None


def _extrair_janelas_dos_filtros(filtros_unificados: list) -> tuple[set, set]:
    """
    Extrai janelas customizadas (alem das padrao) dos filtros unificados.
    Retorna (janelas_wr, janelas_media).
    Recebe lista JA UNIFICADA (saida de _coletar_todos_filtros).
    """
    # Cada set guarda janelas de QUANTIDADE (int) E de TEMPO (string "24h"/"7d").
    # _calcular_stats_h2h sabe distinguir via _parse_janela.
    janelas_wr = set(JANELAS_PADRAO_WR)
    janelas_media = set(JANELAS_PADRAO_MEDIA)

    if not filtros_unificados:
        return janelas_wr, janelas_media

    for f in filtros_unificados:
        if not isinstance(f, dict):
            continue
        tipo = (f.get('tipo') or '').lower().strip()
        janela = f.get('janela')
        if janela is None:
            continue

        modo, _ = _parse_janela(janela)
        if modo is None:
            continue  # janela invalida, ignora
        if modo == 'qtd':
            j = int(janela)
            # janela 0 = 'TODAS'. 1..TETO_BUSCA aceito.
            if j != 0 and (j < 1 or j > H2HCache.TETO_BUSCA):
                continue
            chave = j
        else:
            # tempo: guarda o token normalizado ('24h','7d') no set
            chave = _janela_token(janela)
            if chave is None:
                continue

        if tipo == 'wr':
            janelas_wr.add(chave)
        elif tipo == 'media':
            janelas_media.add(chave)
        elif tipo in ('gap_media', 'gap'):
            # gap_media = media_ult{janela} - linha. Precisa que a media daquela
            # janela seja calculada -> adiciona a janela em janelas_media.
            janelas_media.add(chave)
        elif tipo in ('zscore', 'z'):
            # z = (media-linha)/desvio: precisa da media (e o desvio e calculado
            # junto no mesmo loop de janelas_media). Registra a janela.
            janelas_media.add(chave)

    return janelas_wr, janelas_media


def _calcular_stats_h2h(jogos: list, linha_atual: float,
                        janelas_wr: Optional[set] = None,
                        janelas_media: Optional[set] = None,
                        lado: Optional[str] = None,
                        ts_ref=None) -> dict:
    """
    Calcula stats H2H com janelas dinamicas.

    v5: usa MIN(qtd, N) jogos quando qtd < N (em vez de retornar None).
    Tambem grava 'wr_ult{N}_qtd' e 'media_ult{N}_qtd' indicando quantos jogos
    foram realmente usados, pra que _aplicar_filtros_complementares possa
    validar contra min_partidas do filtro.

    v9: parametro `lado` ('over'/'under'/None). O WR e calculado como % de
    jogos que VENCERIAM a aposta DAQUELE lado:
      - over  (ou None, legado): % com total > linha
      - under: % com total < linha
    Como as linhas sao sempre .5, total (inteiro) nunca empata na linha,
    entao wr_under = 1 - wr_over exatamente. Antes (ate v8) o WR era SEMPRE
    do over e usado pros dois lados - um under apitava olhando o WR do over.

    v10: janelas de TEMPO ("24h","7d"). Alem de janela por quantidade (ultimos
    N jogos), aceita janela por tempo: jogos cujo ts esta nas ultimas N horas/
    dias a partir de ts_ref. A chave de stats usa o token: wr_ult24h, wr_ult7d.
    ts_ref e o momento da aposta (tick['ts']); se nao vier, janelas de tempo
    sao puladas (retornam None) - nunca usa NOW() pra nao vazar no backtest.
    As janelas de quantidade continuam 100% iguais.

    v11: a MESMA funcao e reusada pro historico INDIVIDUAL (lista de jogos de
    UM jogador contra qualquer adversario) — nenhuma mudanca aqui: a funcao e
    agnostica a origem da lista.
    """
    qtd = len(jogos)

    # v10 FIX: normaliza ts_ref (tira tz mantendo horario de parede) pra bater
    # com o ts dos jogos do banco (naive) nas janelas de TEMPO. Sem isso, tick do
    # parquet (aware, 'Z') vs jogo do banco (naive) pulava tudo -> janela vazia.
    ts_ref = _dt_naive(ts_ref)

    if janelas_wr is None:
        janelas_wr = set(JANELAS_PADRAO_WR)
    if janelas_media is None:
        janelas_media = set(JANELAS_PADRAO_MEDIA)

    eh_under = (str(lado).lower().strip() == 'under') if lado else False

    def wr(n):
        # v5: usa o que tiver (ate N). Se nao tem nada, retorna None.
        # n==0 = 'TODAS' -> usa todos os jogos disponiveis do par.
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        slice_ = jogos[:usar]
        # v9: conta o lado certo. under = total < linha; over = total > linha.
        if eh_under:
            passou = sum(1 for j in slice_ if j['total'] < linha_atual)
        else:
            passou = sum(1 for j in slice_ if j['total'] > linha_atual)
        return passou / usar, usar

    def media(n):
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        slice_ = jogos[:usar]
        return sum(j['total'] for j in slice_) / usar, usar

    def desvio(n):
        # Desvio padrao POPULACIONAL (÷N) dos totais na janela. Mesmo slice que
        # media(). Precisa de >=2 jogos (com 1 nao existe dispersao). Retorna
        # (desvio, qtd_usada) ou (None, qtd) se qtd<2.
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        if usar < 2:
            return None, usar
        slice_ = jogos[:usar]
        m = sum(j['total'] for j in slice_) / usar
        var = sum((j['total'] - m) ** 2 for j in slice_) / usar
        return var ** 0.5, usar

    # ---- v10: janelas de TEMPO ----
    # Seleciona jogos com ts em [ts_ref - segundos, ts_ref). Reusa o ts que ja
    # vem em cada jogo (mesmo cutoff temporal das janelas de quantidade).
    # BLINDADO: ts None, tipos de timestamp incompativeis (aware vs naive), e
    # qualquer erro de comparacao -> ignora o jogo problematico em vez de
    # derrubar a avaliacao da aposta. Dinheiro real: melhor amostra menor (ou
    # None) do que crash. ts_ref e o MESMO tick['ts'] que o get_jogos ja compara
    # com sucesso, entao na pratica os tipos batem; isto e defesa em profundidade.
    def _jogos_na_janela_tempo(segundos):
        if ts_ref is None:
            return None  # sem referencia temporal -> nao da pra calcular
        try:
            corte = ts_ref - timedelta(seconds=segundos)
        except (TypeError, ValueError, OverflowError):
            return None
        sel = []
        for j in jogos:
            t = _dt_naive(j.get('ts'))
            if t is None:
                continue
            try:
                if corte <= t < ts_ref:
                    sel.append(j)
            except TypeError:
                # ts incompativel com ts_ref (ex: aware vs naive). Pula o jogo.
                continue
        return sel

    def _total_ok(j):
        """total do jogo como numero, ou None se faltar/invalido."""
        t = j.get('total')
        if t is None:
            return None
        try:
            return float(t)
        except (TypeError, ValueError):
            return None

    def wr_tempo(segundos):
        sel = _jogos_na_janela_tempo(segundos)
        if not sel:
            return None, 0
        if linha_atual is None:
            return None, 0
        # ignora jogos sem 'total' valido (nao conta nem no numerador nem no denom)
        validos = [t for t in (_total_ok(j) for j in sel) if t is not None]
        if not validos:
            return None, 0
        if eh_under:
            passou = sum(1 for t in validos if t < linha_atual)
        else:
            passou = sum(1 for t in validos if t > linha_atual)
        # denominador = jogos VALIDOS (com total), nao o total bruto da janela
        return passou / len(validos), len(validos)

    def media_tempo(segundos):
        sel = _jogos_na_janela_tempo(segundos)
        if not sel:
            return None, 0
        validos = [t for t in (_total_ok(j) for j in sel) if t is not None]
        if not validos:
            return None, 0
        return sum(validos) / len(validos), len(validos)

    def desvio_tempo(segundos):
        # Desvio populacional dos totais na janela de TEMPO. Precisa de >=2.
        sel = _jogos_na_janela_tempo(segundos)
        if not sel:
            return None, 0
        validos = [t for t in (_total_ok(j) for j in sel) if t is not None]
        if len(validos) < 2:
            return None, len(validos)
        m = sum(validos) / len(validos)
        var = sum((t - m) ** 2 for t in validos) / len(validos)
        return var ** 0.5, len(validos)

    out: dict = {'qtd_h2h': qtd}

    for jan in janelas_wr:
        modo, valor = _parse_janela(jan)
        if modo == 'tempo':
            v, usados = wr_tempo(valor)
            tok = _janela_token(jan)
        elif modo == 'qtd':
            v, usados = wr(valor)
            tok = str(valor)
        else:
            continue
        out[f'wr_ult{tok}'] = v
        out[f'wr_ult{tok}_qtd'] = usados
    for jan in janelas_media:
        modo, valor = _parse_janela(jan)
        if modo == 'tempo':
            v, usados = media_tempo(valor)
            tok = _janela_token(jan)
        elif modo == 'qtd':
            v, usados = media(valor)
            tok = str(valor)
        else:
            continue
        out[f'media_ult{tok}'] = v
        out[f'media_ult{tok}_qtd'] = usados
        # --- desvio + z-score (BLINDADO) ---
        # z-score LADO-AWARE: quantos desvios a media esta do lado favoravel da
        # linha. over -> (media-linha)/desvio ; under -> (linha-media)/desvio.
        # Assim um threshold unico "z >= X" vale pros dois lados. Bordas:
        #   desvio None (janela <2 jogos) -> z None (filtro rejeita: sem amostra)
        #   desvio 0 (par faz sempre o mesmo total) -> z +/-999 simbolico:
        #     +999 se media do lado certo da linha (trava perfeita), -999 se nao.
        #     (total e int, linha e .5, entao media==linha nunca acontece aqui.)
        # Qualquer erro inesperado -> desvio/z=None e loga (falha FECHADA: o
        # filtro rejeita, nunca aposta com numero furado). Nao afeta media_ult
        # (ja gravado acima pelo media()/media_tempo() intocado).
        try:
            dv, _ = desvio_tempo(valor) if modo == 'tempo' else desvio(valor)
            out[f'desvio_ult{tok}'] = dv
            if v is None or dv is None or linha_atual is None:
                out[f'z_ult{tok}'] = None
            else:
                margem = (linha_atual - v) if eh_under else (v - linha_atual)
                if dv == 0:
                    out[f'z_ult{tok}'] = 999.0 if margem > 0 else -999.0
                else:
                    out[f'z_ult{tok}'] = margem / dv
        except Exception as e:
            logger.warning(f"[stats] desvio/z janela {jan} (tok={tok}) falhou: {e}")
            out[f'desvio_ult{tok}'] = None
            out[f'z_ult{tok}'] = None

    # Gap = media_ult20 - linha
    m20 = out.get('media_ult20')
    out['gap'] = (m20 - linha_atual) if m20 is not None else None

    # Tendencia = media_ult5 - media_ult20
    m5 = out.get('media_ult5')
    out['tendencia'] = (m5 - m20) if (m5 is not None and m20 is not None) else None

    return out


def _valor_coluna(st: dict, chave: str):
    """Resolve o valor de UMA coluna da planilha a partir do stats da aposta.

    v_colunas: chave normal sai direto do stats; chave 'CALC:...' e DERIVADA —
    o gap por janela nao existe como chave propria (o stats so grava o 'gap'
    fixo da janela 20), entao ele e calculado aqui: media_ult{tok} menos a
    linha daquela aposta. BLINDADO: qualquer coisa torta vira None em vez de
    derrubar o export inteiro."""
    try:
        if not chave:
            return None
        if not str(chave).startswith('CALC:'):
            return st.get(chave)
        partes = str(chave).split(':')
        tipo = partes[1] if len(partes) > 1 else ''
        linha = st.get('linha_atual')
        if tipo == 'gap':
            tok = partes[2] if len(partes) > 2 else '20'
            media = st.get(f'media_ult{tok}')
            if media is None or linha is None:
                return st.get('gap')          # fallback: gap padrao (ult20)
            return media - linha
        if tipo == 'gaplinha':
            media = st.get('media_ult20')
            if media is None or linha is None:
                return None
            return abs(media - linha)
        return None
    except Exception:
        return None


def _aplicar_filtros_complementares(stats: dict, filtros_unificados: list,
                                    min_h2h: int = MIN_H2H_DEFAULT,
                                    stats_indiv: Optional[dict] = None) -> tuple[bool, str]:
    """
    Aplica filtros unificados (comp + hist normalizado).
    Pre-condicao: _calcular_stats_h2h foi chamado COM as janelas dos filtros.

    v6 FIX (14/05/2026): filtros hist (origem='hist', vindos do
    filtrosHistAdicionados) usam minPartidas como MATURIDADE DA
    AMOSTRA H2H TOTAL (qtd_h2h), nao como tamanho efetivo usado
    na janela.

    Exemplo de uso pretendido pelo usuario:
      janela=last_5, minPartidas=10
      Significado: "analiso WR nas ultimas 5 partidas, MAS so
      processo se o par tem >=10 partidas no historico total"
      (filtro de maturidade — evita pares com pouco historico).

    Antes (v5): validava qtd_validar contra wr_ult{N}_qtd
    (max=N), o que tornava "janela=5 + min=10" matematicamente
    impossivel (qtd_validar maximo=5 < 10 sempre). Bot 17 com
    filtros [last_10/min=10, last_5/min=10] rejeitava 100% dos
    ticks pelo segundo filtro.

    Comportamento agora:
      - origem='hist': valida contra qtd_h2h GLOBAL.
        Ex: last_5+min=10 passa se par tem >=10 jogos no historico.
      - origem='comp' (padrao se nao tiver _origem): mantem
        comportamento v5 (qtd da janela). Filtros comp usam
        min_h2h default=5 que sempre <= janela, entao nao geram
        contradicao matematica.

    v11 - filtro INDIVIDUAL (base=individual) + FAIL CLOSED:
      - `stats_indiv` = {'a': stats_do_jogador_a, 'b': stats_do_jogador_b}
        (saida do _calcular_stats_h2h sobre a lista INDIVIDUAL de cada um).
      - Chip individual: os DOIS jogadores precisam passar (regra AND) no
        mesmo min/max da janela. minPartidas = maturidade do TOTAL individual
        de CADA jogador (espelha a semantica v6 dos hist por par).
      - Filtro marcado _nao_suportado REJEITA o tick com motivo claro (antes
        do v11 era descartado em silencio no coletar e o bot rodava sem ele).
    """
    if not filtros_unificados:
        return True, ''

    qtd_global = stats.get('qtd_h2h', 0)

    for f in filtros_unificados:
        tipo = (f.get('tipo') or '').lower().strip()
        janela = f.get('janela')
        min_v = f.get('min') if f.get('minAtivo') else None
        max_v = f.get('max') if f.get('maxAtivo') else None
        origem = f.get('_origem', 'comp')

        # v11 FAIL CLOSED: filtro configurado que o backend nao suporta ->
        # rejeita o tick (nunca roda "sem o filtro" em silencio).
        if f.get('_nao_suportado'):
            return False, f"filtro_nao_suportado({f.get('_nao_suportado')})"

        # Filtros hist tem min_partidas proprio; senao usa default
        min_partidas = f.get('hist_min_partidas') or min_h2h
        try:
            min_partidas = int(min_partidas)
        except (TypeError, ValueError):
            min_partidas = min_h2h
        _mx_p_raw = f.get('hist_max_partidas')
        try:
            max_partidas = (int(_mx_p_raw)
                            if _mx_p_raw not in (None, '', '-') else None)
        except (TypeError, ValueError):
            max_partidas = None

        # ===== v11: filtro INDIVIDUAL (base=individual) =====
        # WR da janela sobre o historico de CADA jogador (contra qualquer
        # adversario). Regra AND: os DOIS precisam passar no mesmo chip.
        if _eh_filtro_individual(f):
            if not stats_indiv or 'a' not in stats_indiv or 'b' not in stats_indiv:
                return False, 'indiv_stats_indisponivel'
            tok = _janela_token(janela)
            if tok is None:
                return False, f'indiv_janela_invalida_{janela}'
            for _lado_k in ('a', 'b'):
                _st_j = stats_indiv.get(_lado_k) or {}
                _qtd_j = _st_j.get('qtd_h2h', 0) or 0
                if max_partidas is not None and _qtd_j > max_partidas:
                    return False, f'indiv_{_lado_k}_qtd_{_qtd_j}_gt_max_{max_partidas}'
                if _qtd_j < min_partidas:
                    return False, f'indiv_{_lado_k}_insuficiente_qtd_{_qtd_j}_min_{min_partidas}'
                _valor_j = _st_j.get(f'wr_ult{tok}')
                if _valor_j is None:
                    return False, f'stat_indiv_{_lado_k}_wr_ult{janela}_indisponivel'
                if min_v is not None:
                    mn, err = _num_seguro(min_v)
                    if err is not None:
                        logger.warning(f"[comp] wr individual min invalido: {min_v!r} ({err}) -> rejeita")
                        return False, 'bot.wr_min_invalido'
                    if _valor_j < mn:
                        return False, f'indiv_{_lado_k}_wr_ult{janela}_lt_min'
                if max_v is not None:
                    mx, err = _num_seguro(max_v)
                    if err is not None:
                        logger.warning(f"[comp] wr individual max invalido: {max_v!r} ({err}) -> rejeita")
                        return False, 'bot.wr_max_invalido'
                    if _valor_j > mx:
                        return False, f'indiv_{_lado_k}_wr_ult{janela}_gt_max'
            continue

        # v6 FIX: filtros HIST validam minPartidas contra qtd_h2h GLOBAL
        # (= maturidade do par). Filtros COMP continuam validando
        # contra qtd da janela (compat backwards v5).
        if origem == 'hist':
            qtd_validar = qtd_global
        else:
            # v5 (comp): qtd a validar depende do tipo+janela do filtro
            # v10: usa _janela_token (funciona p/ quantidade E tempo "24h")
            qtd_validar = qtd_global
            tok = _janela_token(janela)
            if tipo == 'wr' and tok is not None:
                qtd_validar = stats.get(f'wr_ult{tok}_qtd', qtd_global) or 0
            elif tipo == 'media' and tok is not None:
                qtd_validar = stats.get(f'media_ult{tok}_qtd', qtd_global) or 0

        if max_partidas is not None and qtd_validar > max_partidas:
            return False, f'h2h_qtd_{qtd_validar}_gt_max_{max_partidas}'
        if qtd_validar < min_partidas:
            return False, f'h2h_insuficiente_qtd_{qtd_validar}_min_{min_partidas}'

        valor = None

        if tipo == 'media' and janela is not None:
            tok = _janela_token(janela)
            valor = stats.get(f'media_ult{tok}') if tok is not None else None
        elif tipo == 'wr' and janela is not None:
            tok = _janela_token(janela)
            valor = stats.get(f'wr_ult{tok}') if tok is not None else None
        elif tipo in ('gap_media', 'gap'):
            # gap_media = media_ult{janela} - linha, EM CADA JANELA.
            # Se o filtro tem janela, usa a media daquela janela; senao cai no
            # gap padrao (media_ult20 - linha) por compatibilidade.
            linha_g = stats.get('linha_atual')
            tok = _janela_token(janela) if janela is not None else None
            if tok is not None:
                media_jan = stats.get(f'media_ult{tok}')
                if media_jan is not None and linha_g is not None:
                    valor = media_jan - linha_g
                else:
                    valor = None
            else:
                valor = stats.get('gap')
        elif tipo == 'tendencia':
            valor = stats.get('tendencia')
        elif tipo in ('zscore', 'z'):
            # z-score lado-aware ja calculado por janela em _calcular_stats_h2h
            # (grava z_ult{tok}). Sem janela -> usa a janela 20 (default eBasket).
            tok = _janela_token(janela) if janela is not None else '20'
            valor = stats.get(f'z_ult{tok}')
        elif tipo == 'gap_linha':
            media = stats.get('media_ult20')
            if media is not None:
                valor = abs(media - (stats.get('linha_atual') or 0))
        elif tipo == 'qtd_h2h':
            valor = stats.get('qtd_h2h')

        if valor is None:
            return False, f'stat_{tipo}_ult{janela}_indisponivel'

        # BLINDADO: coage o threshold com _num_seguro em vez de float() cru. Config
        # do bot com min/max invalido -> REJEITA com motivo claro (falha FECHADA,
        # nunca aposta), igual _avaliar_filtros_basicos faz com odd_min ruim. Pra
        # dinheiro real: prefere NAO apostar a apostar sem o gate configurado.
        # Comportamento identico p/ min/max validos.
        if min_v is not None:
            mn, err = _num_seguro(min_v)
            if err is not None:
                logger.warning(f"[comp] {tipo} min invalido: {min_v!r} ({err}) -> rejeita")
                return False, f'bot.{tipo}_min_invalido'
            if valor < mn:
                return False, f'{tipo}_ult{janela}_lt_min'
        if max_v is not None:
            mx, err = _num_seguro(max_v)
            if err is not None:
                logger.warning(f"[comp] {tipo} max invalido: {max_v!r} ({err}) -> rejeita")
                return False, f'bot.{tipo}_max_invalido'
            if valor > mx:
                return False, f'{tipo}_ult{janela}_gt_max'

    return True, ''


def _aplicar_filtro_cenario(tick: dict, cenario: str) -> bool:
    sh = tick.get('score_home')
    sa = tick.get('score_away')
    if sh is None or sa is None:
        return False

    if cenario == 'casa_vencendo':
        return sh > sa
    if cenario == 'casa_perdendo':
        return sh < sa
    if cenario == 'empate':
        return sh == sa
    if cenario == 'casa_ou_empate':
        return sh >= sa
    if cenario == 'visitante_ou_empate':
        return sa >= sh
    if cenario == 'casa_ou_visitante':
        return sh != sa
    return True


def _aplicar_filtro_diff_placar(tick: dict, diff_min: int) -> bool:
    sh = tick.get('score_home')
    sa = tick.get('score_away')
    if sh is None or sa is None:
        return False
    return abs(sh - sa) >= diff_min


def _aplicar_filtro_folga(tick: dict, selecao: str, folga_min, folga_max) -> tuple:
    """v12. FOLGA (so handicap): quanto o LADO APOSTADO esta cobrindo a linha
    com o placar ATUAL do tick.

        folga = hc_assinado - (pts_adversario - pts_do_lado_apostado)

    O hc_assinado vem da SELECAO (_selecao_hc_valor) — a coluna 'linha' do
    tick NAO e confiavel no HC (superbet manda positiva pros 2 lados). O lado
    vem do nick (_extrair_nick_hc), com o MESMO casamento nick->lado do
    _resolve_resultado_hc (fonte unica, nunca inverte). folga > 0 <=> o lado
    apostado cobre a linha agora; zebra +13.5 perdendo por 8 -> folga 5.5;
    zebra +6.5 GANHANDO por 2 -> folga 8.5 (deficit negativo soma).

    Retorna (passou: bool, motivo: str). FAIL CLOSED: sem placar, selecao sem
    nick/valor, nick que nao casa com o par, ou config min/max invalida ->
    (False, motivo claro). Nunca crasha, nunca aposta com numero furado."""
    sh = tick.get('score_home')
    sa = tick.get('score_away')
    if sh is None or sa is None:
        return False, 'folga_sem_placar'
    nick = _extrair_nick_hc(selecao)
    hc = _selecao_hc_valor(selecao)
    if nick is None or hc is None:
        return False, 'folga_selecao_invalida'
    ja = (tick.get('jogador_a') or '').strip().upper()
    jb = (tick.get('jogador_b') or '').strip().upper()
    if nick == ja:
        pts_nick, pts_adv = sh, sa
    elif nick == jb:
        pts_nick, pts_adv = sa, sh
    elif ja and (nick in ja or ja in nick):
        pts_nick, pts_adv = sh, sa
    elif jb and (nick in jb or jb in nick):
        pts_nick, pts_adv = sa, sh
    else:
        return False, 'folga_nick_nao_casa'
    try:
        folga = float(hc) - (float(pts_adv) - float(pts_nick))
    except (TypeError, ValueError):
        return False, 'folga_placar_invalido'
    if folga_min is not None:
        mn, err = _num_seguro(folga_min)
        if err is not None:
            return False, f'bot.folga_min_{err}'
        if folga < mn:
            return False, f'folga_{folga:.1f}_lt_min_{mn}'
    if folga_max is not None:
        mx, err = _num_seguro(folga_max)
        if err is not None:
            return False, f'bot.folga_max_{err}'
        if folga > mx:
            return False, f'folga_{folga:.1f}_gt_max_{mx}'
    return True, ''


# ===================== MOMENTO (v13) — quando no jogo =====================
# Escala numerica de "quanto o jogo ja avancou" no instante da aposta, lida
# do live_time do tick (o coletor da superbet marca 1Q/2Q/HT/3Q/4Q/OT/B/END).
# NAO e minuto cronometrado — e o ESTAGIO do jogo, granularidade de quarto,
# que e o que o coletor entrega hoje. Mapa (basket 4 quartos):
#   1Q -> 1   (comeco, linha da casa ainda defasada = onde mora o edge)
#   2Q -> 2 ; HT -> 2 (intervalo conta como fim do 2o)
#   3Q -> 3 ; 4Q/OT -> 4 ; B/END/FT -> 5 (bola parada / fim)
# Filtro momentoMax=2 => "so 1o tempo". Comprovado no vivo (bot 56): 1Q+2Q
# +18,7% ROI vs 3Q+4Q -7,6%. FAIL CLOSED: live_time ausente/desconhecido e
# momento ativo -> tick rejeitado (nunca aposta "sem saber quando").
_MOMENTO_MAPA = {
    '1Q': 1, 'Q1': 1, '1': 1,
    '2Q': 2, 'Q2': 2, '2': 2, 'HT': 2, 'HALFTIME': 2, 'INTERVALO': 2,
    '3Q': 3, 'Q3': 3, '3': 3,
    '4Q': 4, 'Q4': 4, '4': 4, 'OT': 4, 'PRORROGACAO': 4, 'OVERTIME': 4,
    'B': 5, 'END': 5, 'FT': 5, 'ENDED': 5, 'FIM': 5,
}


def _momento_do_tick(tick):
    """Estagio numerico do jogo (1..5) a partir do live_time. None se ausente
    ou nao reconhecido (o chamador trata como fail-closed quando o filtro esta
    ligado). BLINDADO: nunca levanta — tick nao-dict, live_time exotico ou
    valor fora do mapa -> None."""
    try:
        lt = tick.get('live_time') if isinstance(tick, dict) else None
    except Exception:
        return None
    if lt is None:
        return None
    try:
        s = str(lt).strip().upper()
    except Exception:
        return None
    if not s:
        return None
    return _MOMENTO_MAPA.get(s)


def _aplicar_filtro_momento(tick, momento_min, momento_max) -> tuple:
    """v13. So aposta quando o ESTAGIO do jogo esta na faixa [min, max].
    Ex.: momentoMax=2 => so 1o tempo (1Q/2Q/HT). Retorna (passou, motivo).
    FAIL CLOSED: live_time ausente/desconhecido, config invalida, ou faixa
    impossivel (min > max) -> (False, motivo). BLINDADO: qualquer excecao
    inesperada tambem vira rejeicao (nunca aposta, nunca crasha)."""
    try:
        m = _momento_do_tick(tick)
        if m is None:
            return False, 'momento_sem_live_time'
        mn = mx = None
        if momento_min is not None:
            mn, err = _num_seguro(momento_min)
            if err is not None:
                return False, f'bot.momento_min_{err}'
        if momento_max is not None:
            mx, err = _num_seguro(momento_max)
            if err is not None:
                return False, f'bot.momento_max_{err}'
        if mn is not None and mx is not None and mn > mx:
            return False, f'momento_faixa_invalida_{mn:g}_{mx:g}'
        if mn is not None and m < mn:
            return False, f'momento_{m}_lt_min_{mn:g}'
        if mx is not None and m > mx:
            return False, f'momento_{m}_gt_max_{mx:g}'
        return True, ''
    except Exception as e:
        return False, 'momento_erro_' + type(e).__name__


def _num_seguro(v):
    """Coage qualquer valor a float de forma segura, tratando os tipos que
    chegam do parquet/banco/snapshot. Retorna (float, None) em sucesso ou
    (None, motivo) em falha. Cobre:
      - None -> ausente
      - string numerica ('1.85', '+2.5', 'away|0.5') -> parseia (igual linha)
      - string vazia / nao-numerica -> invalido
      - NaN (float('nan') ou numpy.nan) -> invalido (NaN quebra comparacoes)
      - int/float/Decimal/numpy number -> float()
    Dinheiro real: prefere reportar 'invalido' a deixar passar NaN (que faz
    toda comparacao virar False silenciosamente) ou quebrar com str<float.
    """
    if v is None:
        return None, 'ausente'
    # ja numerico?
    if isinstance(v, bool):
        # bool e int em python; nao deveria ser odd/linha -> trata como invalido
        return None, 'tipo_bool'
    if isinstance(v, (int, float, Decimal)):
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            return None, 'nao_convertivel'
        if f != f:  # NaN (NaN != NaN é True)
            return None, 'nan'
        return f, None
    # string (ou numpy str, ou outro) -> tenta parsear como a linha faz
    try:
        s = str(v).strip()
    except Exception:
        return None, 'nao_stringificavel'
    if not s:
        return None, 'vazio'
    # trata formatos da casa: 'away|0.5' -> '0.5'; '+2.5' -> '2.5'
    if '|' in s:
        s = s.split('|')[0]
    if s.startswith('+'):
        s = s[1:]
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None, f'nao_numerico({s[:20]})'
    if f != f:
        return None, 'nan'
    return f, None


def _avaliar_filtros_basicos(tick: dict, bot: dict) -> tuple[bool, str]:
    """
    Avalia os filtros basicos do tick. BLINDADO: cada comparacao numerica coage
    os dois lados (tick e bot) com _num_seguro e, se algum nao converter, retorna
    motivo CLARO em vez de quebrar (str<float) ou passar NaN silenciosamente.
    Filosofia (dinheiro real): tick com campo invalido (odd/linha que nao da pra
    ler) = NAO apostar, com motivo legivel. Nunca crash, nunca comparacao furada.
    """
    # --- LINHA ---
    # Pro HANDICAP, a coluna 'linha' do tick NAO e confiavel pro lado apostado:
    # a superbet manda a coluna sempre positiva (14.5) para os dois lados
    # (+14.5 e -14.5), e a estrelabet as vezes inverte. O valor VERDADEIRO
    # (com sinal do lado) esta na SELECAO: 'Kiev (+13.5)' -> +13.5. Entao pro HC
    # a linha filtrada vem de _selecao_hc_valor, e o filtro linha_min/linha_max
    # passa a operar sobre o valor COM SINAL. Assim "+13.5 a +40.5" no seletor
    # filtra exatamente o lado + na faixa certa (e "-40.5 a -0.5" o lado -).
    if _mercado_eh_hc(bot.get('mercado', '')):
        linha = _selecao_hc_valor(tick.get('selecao'))
    else:
        linha = _parse_linha(tick.get('linha'))
    if linha is None:
        return False, 'linha_invalida'

    lmin = bot.get('linha_min')
    if lmin is not None:
        lmin_f, err = _num_seguro(lmin)
        if err is not None:
            return False, f'bot.linha_min_{err}'  # config do bot ruim -> reporta
        if linha < lmin_f:
            return False, f'linha_{linha}_lt_min_{lmin_f}'
    lmax = bot.get('linha_max')
    if lmax is not None:
        lmax_f, err = _num_seguro(lmax)
        if err is not None:
            return False, f'bot.linha_max_{err}'
        if linha > lmax_f:
            return False, f'linha_{linha}_gt_max_{lmax_f}'

    # --- ODD ---
    odd_f, err = _num_seguro(tick.get('odds'))
    if err is not None:
        # odd do tick invalida (ausente, NaN, string). 'ausente'/'vazio'/'nan'
        # -> tick sem odd real, nao aposta.
        return False, f'odd_{err}'
    # Odd <= 1 NAO e cotacao real: mercado SUSPENSO (odd_status suspenso, odds
    # 0.00) ou tick de score_update/fechamento. Nunca e apostavel (1.0 = sem
    # retorno, <1 = impossivel). Corta SEMPRE, independente de odd_min - senao um
    # run sem piso de odds (backtest avulso seta odd_min=None) apostaria esses
    # ticks a 0.00, cada um um -stake fake que corrompe WR/ROI. O PLACAR nao e
    # afetado: e montado de todos os ticks antes (inclusive os suspensos, que
    # carregam o score final quando o mercado fecha no fim do jogo).
    if odd_f <= 1.0:
        return False, 'odd_suspensa_lte1'
    omin = bot.get('odd_min')
    if omin is not None:
        omin_f, err = _num_seguro(omin)
        if err is not None:
            return False, f'bot.odd_min_{err}'
        if odd_f < omin_f:
            return False, f'odd_{odd_f}_lt_min_{omin_f}'
    omax = bot.get('odd_max')
    if omax is not None:
        omax_f, err = _num_seguro(omax)
        if err is not None:
            return False, f'bot.odd_max_{err}'
        if odd_f > omax_f:
            return False, f'odd_{odd_f}_gt_max_{omax_f}'

    # --- MERCADO ---
    # _matches_mercado compara mercado_tipo (string) com mapping. Coage o tipo a
    # string pra nao falhar se vier numero do parquet (18 vs '18').
    mtipo = tick.get('mercado_tipo')
    mtipo_str = '' if mtipo is None else str(mtipo).strip()
    if not _matches_mercado(bot.get('mercado', ''), tick.get('mercado', ''),
                            mtipo_str, bot.get('casa', '')):
        return False, f'mercado_nao_bate(tipo={mtipo_str[:12]})'

    # --- BLACKLIST / WHITELIST (strings, nao quebram, mas blinda .lower()) ---
    blacklist_pares = bot.get('blacklist_pares') or []
    if blacklist_pares:
        ja = (tick.get('jogador_a') or '').lower()
        jb = (tick.get('jogador_b') or '').lower()
        ta = (tick.get('time_a') or '').lower()
        tb = (tick.get('time_b') or '').lower()
        for entry in blacklist_pares:
            if not isinstance(entry, dict):
                continue
            j1 = (entry.get('j1') or '').lower()
            j2 = (entry.get('j2') or '').lower()
            t1 = (entry.get('t1') or '').lower()
            t2 = (entry.get('t2') or '').lower()
            if j1 and (j1 == ja or j1 == jb): return False, f'blacklist_{j1}'
            if j2 and (j2 == ja or j2 == jb): return False, f'blacklist_{j2}'
            if t1 and (t1 == ta or t1 == tb): return False, f'blacklist_time_{t1}'
            if t2 and (t2 == ta or t2 == tb): return False, f'blacklist_time_{t2}'

    whitelist_pares = bot.get('whitelist_pares') or []
    if whitelist_pares:
        ja = (tick.get('jogador_a') or '').lower()
        jb = (tick.get('jogador_b') or '').lower()
        ta = (tick.get('time_a') or '').lower()
        tb = (tick.get('time_b') or '').lower()
        match = False
        for entry in whitelist_pares:
            j1 = (entry.get('j1') or '').lower()
            j2 = (entry.get('j2') or '').lower()
            t1 = (entry.get('t1') or '').lower()
            t2 = (entry.get('t2') or '').lower()
            j1_ok = not j1 or (j1 == ja or j1 == jb)
            j2_ok = not j2 or (j2 == ja or j2 == jb)
            t1_ok = not t1 or (t1 == ta or t1 == tb)
            t2_ok = not t2 or (t2 == ta or t2 == tb)
            if (j1 or j2 or t1 or t2) and j1_ok and j2_ok and t1_ok and t2_ok:
                match = True
                break
        if not match:
            return False, 'fora da whitelist'

    return True, ''


# ============================================================
# WORKER PRINCIPAL
# ============================================================

async def executar_backtest(job_id: int):
    pool = get_pool()
    logger.info(f"[backtest] Iniciando job {job_id}")

    try:
        async with pool.acquire() as conn:
            job_row = await conn.fetchrow(
                "SELECT * FROM backtest_jobs WHERE id = $1", job_id
            )
            if not job_row:
                logger.error(f"[backtest] Job {job_id} nao encontrado")
                return

            bot = job_row['bot_snapshot']
            if isinstance(bot, str):
                bot = json.loads(bot)

            data_inicio = job_row['data_inicio']
            data_fim = job_row['data_fim']
            stake_modo = job_row['stake_modo']
            stake_valor = float(job_row['stake_valor'])
            banca_inicial = float(job_row['banca_inicial'] or 1000)

            # v10 (peca 3): fonte dos ticks. Se upload_id vier preenchido, le do
            # ARQUIVO parquet; senao, le do BANCO por periodo (comportamento atual).
            # job_row pode nao ter a coluna (migration nao rodada) -> trata como None.
            try:
                upload_id = job_row['upload_id']
            except (KeyError, IndexError):
                upload_id = None

            fonte_ticks = 'arquivo' if upload_id else 'banco'

            await conn.execute(
                "UPDATE backtest_jobs SET status='rodando', progresso=5, "
                "progresso_msg=$2 WHERE id=$1",
                job_id,
                f"Buscando ticks ({fonte_ticks})",
            )

        filtros = bot.get('filtros') or {}
        cenario_ativo = filtros.get('cenarioPartidaAtivo', False)
        cenario_partida = filtros.get('cenarioPartida') if cenario_ativo else None
        diff_ativo = filtros.get('diferencaPlacarAtivo', False)
        diff_min = filtros.get('diferencaPlacar', 0) if diff_ativo else 0

        # v12 — FOLGA (so handicap). Chaves no filtros jsonb; bot antigo sem
        # elas = filtro desligado (comportamento identico ao de antes).
        folga_ativo = filtros.get('folgaAtivo', False)
        folga_min = filtros.get('folgaMin') if folga_ativo else None
        folga_max = filtros.get('folgaMax') if folga_ativo else None
        # v13 — MOMENTO (estagio do jogo via live_time). Bot antigo sem as
        # chaves = filtro desligado. Vale pra QUALQUER mercado (nao so HC),
        # mas depende de o coletor marcar o periodo no live_time (hoje: superbet).
        momento_ativo = bool(filtros.get('momentoAtivo', False)) if isinstance(filtros, dict) else False
        momento_min = filtros.get('momentoMin') if momento_ativo else None
        momento_max = filtros.get('momentoMax') if momento_ativo else None
        # Guarda: ligado mas SEM nenhuma borda = sem efeito. Desliga (evita
        # que 'ativo' com bordas None deixe todo tick passar por engano).
        if momento_ativo and momento_min is None and momento_max is None:
            momento_ativo = False
        # FAIL CLOSED (filosofia v11): folga so faz sentido em handicap. Num
        # bot NAO-HC com folga ligada, TODO tick e rejeitado com motivo
        # proprio ('folga_so_hc') — nunca roda "sem o filtro" em silencio.
        folga_fora_de_hc = folga_ativo and not _mercado_eh_hc(bot.get('mercado', ''))

        # FIX (over-entry): replica o evitarLinhasSeq do bot_executor (default True).
        # AO VIVO o bot aposta 1 vez por mercado_tipo por jogo (trava qualquer 2a
        # linha do mesmo mercado no mesmo event_id). Sem isso, o backtest apostava
        # TODA linha (Over 0.5, 1.5, 2.5...7.5) do mesmo jogo -> ~9x mais apostas e
        # WR colapsando pra taxa-base (as linhas altas perdem). Agora bate com o vivo.
        evitar_linhas_seq = filtros.get('evitarLinhasSeq', True)
        # v11.2: a trava inclui o LADO -> {(event_id, mercado_tipo, lado)}.
        # Lado unico e HC: nada muda (lado constante/None = mesma trava).
        # Lado 'Ambos' em over/under: cada lado ganha a sua vaga — "Ambos"
        # vira os dois de verdade, nao "o primeiro que o coletor gravou".
        mercado_apostado_evt: set = set()

        # FIX (lado / falso-positivo): replica o filtro de LADO do bot_executor
        # (linhas 374-385). AO VIVO o bot so aposta os lados configurados em
        # filtros.lados / filtros.lado (ex.: ['over']). SEM esse filtro o backtest
        # gerava candidato pro OVER **e** pro UNDER da mesma linha/jogo e apostava
        # os dois -> WR colado em ~55% (cara-ou-coroa por construcao), MASCARANDO a
        # performance real do lado configurado (o over puro do #32 da 39%/-27%).
        # 'ambos' (ou ausencia) => lados_bot_norm=None => nao filtra (aceita os dois).
        lados_bot = filtros.get('lados')
        if lados_bot is None and filtros.get('lado'):
            _lado_str = str(filtros.get('lado')).lower().strip()
            lados_bot = [] if _lado_str == 'ambos' else [_lado_str]
        lados_bot_norm = None
        if lados_bot and isinstance(lados_bot, list) and len(lados_bot) > 0:
            lados_bot_norm = [str(l).lower().strip() for l in lados_bot if l]

        # v4: coleta filtros dos 2 lugares (comp + hist normalizado)
        filtros_unificados = _coletar_todos_filtros(filtros)
        janelas_wr, janelas_media = _extrair_janelas_dos_filtros(filtros_unificados)

        # v11: filtros INDIVIDUAIS (base=individual)
        tem_indiv = _tem_filtro_individual(filtros_unificados)
        janelas_wr_indiv = _janelas_wr_individuais(filtros_unificados) if tem_indiv else set()
        indiv_precisa_fav = any(
            _eh_filtro_individual(f) and f.get('hist_indiv_alvo') == 'ambos'
            for f in (filtros_unificados or [])
        )

        if filtros_unificados:
            tipos_resumo = [f"{f.get('tipo')}_ult{f.get('janela')}" for f in filtros_unificados]
            logger.info(f"[backtest] Filtros unificados: {tipos_resumo}"
                        + (" [+individual]" if tem_indiv else ""))

        async with pool.acquire() as conn:
            torneios = bot.get('torneios') or []
            torneios_excluir = bot.get('torneios_excluir') or []

            # === v10 (peca 3): FONTE = ARQUIVO ===
            if upload_id:
                try:
                    from workers.backtest_upload import (parse_ticks_parquet,
                                                         caminho_do_upload,
                                                         BacktestUploadError)
                except ImportError as e:
                    raise RuntimeError(
                        "modulo backtest_upload nao encontrado no worker"
                    ) from e

                try:
                    caminho = caminho_do_upload(upload_id)
                    # JOB42/upload: parse de parquet grande (1M+ linhas) e
                    # PESADO e sincrono - fora do event loop, senao a API
                    # inteira congela durante o inicio do job.
                    ticks = await asyncio.to_thread(parse_ticks_parquet, caminho, bot)
                except BacktestUploadError as e:
                    # erro previsivel de arquivo: marca job erro com msg clara
                    raise RuntimeError(f"Falha no arquivo de ticks: {e}") from e

                total_ticks = len(ticks)
                logger.info(f"[backtest] Job {job_id}: {total_ticks} ticks do ARQUIVO")

                await conn.execute(
                    "UPDATE backtest_jobs SET progresso=15, "
                    "progresso_msg=$2, total_ticks_avaliados=$3 WHERE id=$1",
                    job_id,
                    f"Lidos {total_ticks} ticks do arquivo. Aplicando filtros...",
                    total_ticks,
                )

                if total_ticks == 0:
                    await conn.execute(
                        """
                        UPDATE backtest_jobs SET
                            status='concluido', progresso=100,
                            progresso_msg='Arquivo sem ticks apos filtros do bot',
                            total_apostas=0, green=0, red=0, void_count=0,
                            pnl=0, roi=0, win_rate=0, drawdown_max=0, max_streak_red=0,
                            dias_verdes=0, dias_total=0,
                            equity_curve='[]'::jsonb, apostas_detalhe='[]'::jsonb,
                            pnl_por_dia='[]'::jsonb, concluido_em=NOW()
                        WHERE id=$1
                        """,
                        job_id,
                    )
                    return

            # === FONTE = BANCO (comportamento atual) ===
            else:
                sql = """
                    SELECT id, ts, bookmaker, sport, liga, event_id,
                           jogador_a, jogador_b, time_a, time_b,
                           score_home, score_away, live_time,
                           mercado, mercado_id, mercado_tipo, linha, selecao, selecao_id,
                           odds
                    FROM ticks
                    WHERE bookmaker = $1
                      AND ts >= $2::timestamp
                      AND ts < $3::timestamp + INTERVAL '1 day'
                """
                params: list = [bot.get('casa'), data_inicio, data_fim]
                n = 4

                if bot.get('esporte'):
                    sport_banco = ESPORTE_UI_PARA_BANCO.get(bot['esporte'], bot['esporte'])
                    sql += f" AND sport = ${n}"
                    params.append(sport_banco)
                    n += 1

                if torneios:
                    ors = []
                    for t in torneios:
                        ors.append(f"liga ILIKE ${n}")
                        params.append(f"%{t}%")
                        n += 1
                    sql += f" AND ({' OR '.join(ors)})"

                if torneios_excluir:
                    ands = []
                    for t in torneios_excluir:
                        ands.append(f"liga NOT ILIKE ${n}")
                        params.append(f"%{t}%")
                        n += 1
                    sql += f" AND ({' AND '.join(ands)})"

                sql += " ORDER BY event_id, mercado_id, linha, selecao_id, ts ASC"
                logger.info(f"[backtest] SQL params count: {len(params)}")

                ticks = await conn.fetch(sql, *params)
                total_ticks = len(ticks)
                logger.info(f"[backtest] Job {job_id}: {total_ticks} ticks brutos")

                await conn.execute(
                    "UPDATE backtest_jobs SET progresso=15, "
                    "progresso_msg=$2, total_ticks_avaliados=$3 WHERE id=$1",
                    job_id, f"Encontrados {total_ticks} ticks. Aplicando filtros...", total_ticks,
                )

                if total_ticks == 0:
                    await conn.execute(
                        """
                        UPDATE backtest_jobs SET
                            status='concluido', progresso=100,
                            progresso_msg='Nenhum tick no periodo',
                            total_apostas=0, green=0, red=0, void_count=0,
                            pnl=0, roi=0, win_rate=0, drawdown_max=0, max_streak_red=0,
                            dias_verdes=0, dias_total=0,
                            equity_curve='[]'::jsonb, apostas_detalhe='[]'::jsonb, pnl_por_dia='[]'::jsonb,
                            concluido_em=NOW()
                        WHERE id=$1
                        """,
                        job_id,
                    )
                    return

        placar_final: dict = {}
        _placar_ts: dict = {}   # evt -> ts do tick que deu o placar (pega o MAIS RECENTE)

        for t in ticks:
            evt = t['event_id']
            sh = t['score_home']
            sa = t['score_away']
            if sh is not None and sa is not None:
                # FIX (dado falso): placar FINAL = score do tick com MAIOR ts do
                # evento - exatamente como o telegram_notifier resolve em producao
                # (SELECT ... ORDER BY ts DESC LIMIT 1). Antes pegava o "ultimo na
                # ordem do SQL" (event_id, mercado_id, linha, selecao_id, ts), que
                # NAO e o ultimo no tempo: se o ultimo mercado/selecao do evento
                # parou de atualizar antes do fim, o placar 'final' saia errado e
                # o backtest resolvia GREEN/RED contra um placar parcial.
                tts = t['ts']
                if evt not in _placar_ts or tts >= _placar_ts[evt]:
                    placar_final[evt] = (sh, sa)
                    _placar_ts[evt] = tts

        # === PLACAR DO INTERVALO (HT) — pra resolver over/under de 1o tempo ===
        # over_under_ht so pode ser gradeado com o placar do FIM DO 1o TEMPO, nao
        # o final. Recupera do proprio fluxo de ticks: prefere live_time='HT'
        # (intervalo); na falta, o ULTIMO tick de '2Q' (fim do 2o quarto) — os
        # dois carregam o mesmo placar de intervalo. So casas cujo coletor marca
        # o periodo preenchem isto (hoje: superbet). Evento sem marcador fica de
        # fora e seu over_under_ht cai no balde 'mercado_ht_sem_suporte'.
        # BLINDADO: acesso a live_time protegido (Record OU dict OU ausente).
        placar_ht: dict = {}
        _ht_ts: dict = {}      # evt -> ts do melhor tick de HT
        _ht_fonte: dict = {}   # evt -> 'HT' | '2Q' (prioridade: HT vence 2Q)
        for t in ticks:
            try:
                lt = t['live_time']
            except Exception:
                lt = None
            if lt not in ('HT', '2Q'):
                continue
            evt = t['event_id']
            sh = t['score_home']
            sa = t['score_away']
            if sh is None or sa is None:
                continue
            fonte = _ht_fonte.get(evt)
            # ja temos HT desse evento e este tick e so 2Q -> ignora
            if fonte == 'HT' and lt == '2Q':
                continue
            # promove pra HT, ou atualiza pelo tick de maior ts na mesma fonte
            melhor = (lt == 'HT' and fonte != 'HT') \
                or (evt not in _ht_ts) or (t['ts'] >= _ht_ts[evt])
            if melhor:
                placar_ht[evt] = (sh, sa)
                _ht_ts[evt] = t['ts']
                _ht_fonte[evt] = lt

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_jobs SET progresso=30, progresso_msg='Calculando stats H2H' WHERE id=$1",
                job_id,
            )

        sport_banco = ESPORTE_UI_PARA_BANCO.get(bot.get('esporte', ''), bot.get('esporte', ''))
        h2h_cache = H2HCache(pool, bot.get('casa', ''), sport_banco)
        # v11: cache de historico INDIVIDUAL (so consultado se tem_indiv)
        indiv_cache = HistIndividualCache(pool, bot.get('casa', ''), sport_banco)

        max_apostas_partida = bot.get('max_apostas_partida')
        apostas_por_evento: dict = {}
        candidatas = []

        rej = {
            'cap_jogo': 0, 'basico': 0, 'cenario': 0, 'diff': 0, 'folga': 0,
            'h2h_insuf': 0, 'comp': 0, 'sem_par': 0,
            'sem_placar': 0, 'sem_resultado': 0, 'lado': 0,
        }
        # Detalhe das rejeicoes BASICAS por sub-motivo (odd_lt_min, mercado_nao_bate,
        # linha_invalida, odd_ausente, etc). Permanente, vai pro relatorio - assim
        # 'basico=N' nunca mais e uma caixa-preta: a UI mostra a quebra.
        rej_basico_detalhe: dict = {}
        # v10: contadores de QUALIDADE (nao rejeitam, mas reportam no relatorio).
        # Filosofia: num backtest de dinheiro real, o numero precisa vir com
        # ressalva honesta. Esconder que X apostas tiveram h2h fraco = mentir.
        qualidade = {
            'apostas_h2h_fraco': 0,   # apostou mas com poucos jogos h2h (<min saudavel)
            'eventos_sem_placar_final': 0,  # tick sem placar pra resolver
            'ticks_odds_invalida': 0,  # odds NaN/<=1 (score_update etc) - pulados
        }
        H2H_MIN_SAUDAVEL = 10  # abaixo disso, marca como amostra fraca

        # REENTRADA (fidelidade com o vivo): o bot ao vivo usa LISTEN/NOTIFY e
        # processa CADA tick inserido, apostando todo que passa o filtro ate bater
        # o cap (max_apostas_partida) - sem deduplicar por linha. Resultado: ele
        # RE-APOSTA a mesma linha varias vezes no mesmo jogo (ex.: over 7.5 a 1.78,
        # depois 1.85, depois 1.95 conforme a odd anda). O backtest tem que fazer
        # igual, senao mente pra menos (da 55% onde o vivo da 39%).
        #   - evitarLinhasSeq=False  -> NAO deduplica: processa todos os ticks, a
        #     mesma linha pode ser apostada ate o cap (reentrada, igual ao vivo).
        #   - evitarLinhasSeq=True   -> a trava ja garante 1 aposta por mercado_tipo
        #     por jogo, entao deduplicar por linha (1 tick/linha) da o MESMO
        #     resultado e e muito mais rapido. Mantem o comportamento antigo.
        # Obs.: o cap (checado no topo do loop) corta cedo os ticks de jogos ja
        # cheios, entao processar todos os ticks nao explode o custo.
        # v15 (escada de linhas): a DEDUPLICACAO POR LINHA agora vale nos DOIS
        # modos. Antes ela so rodava com evitar_linhas_seq=True; no modo escada
        # o motor nao deduplicava nada e reapostava a MESMA linha varias vezes
        # seguidas enquanto a odd balancava — num job real 59% das apostas eram
        # reentrada, e o teto por jogo (max_apostas_partida) queimava nos
        # primeiros minutos. Resultado: o jogo nunca chegava nas linhas fundas,
        # que so nascem com a partida andada. Com a dedup ligada, cada vaga do
        # teto vira uma LINHA DIFERENTE e a escada sobe de verdade.
        #
        # A chave inclui event_id DE PROPOSITO: o mesmo par joga varias vezes no
        # periodo e cada jogo precisa da sua propria escada. Se a chave fosse
        # por PAR, o motor pegaria a escada do primeiro jogo e ignoraria todos
        # os confrontos seguintes daquela dupla.
        primeiros: dict = {}
        for t in ticks:
            chave = (t['event_id'], t['mercado_id'] or '', t['linha'] or '', t['selecao_id'] or '')
            if chave not in primeiros:
                primeiros[chave] = dict(t)
        ticks_ordenados = sorted(primeiros.values(), key=lambda x: x['ts'])
        total_candidatos = len(ticks_ordenados)
        try:
            _n_ev = len({t['event_id'] for t in ticks_ordenados})
            logger.info(f"[backtest {job_id}] escada: {total_candidatos} linhas unicas "
                        f"em {_n_ev} eventos "
                        f"({total_candidatos / max(_n_ev, 1):.1f} por jogo) | "
                        f"trava de mercado={'ON' if evitar_linhas_seq else 'OFF'}")
        except Exception:
            pass

        for i, tick in enumerate(ticks_ordenados):
            if i > 0 and i % 200 == 0:
                pct = 30 + int(40 * i / total_candidatos)
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE backtest_jobs SET progresso=$1, progresso_msg=$2 WHERE id=$3",
                        pct, f"Aplicando filtros ({i}/{total_candidatos})", job_id,
                    )

            evt = tick['event_id']

            # filtro de LADO (over/under) - igual ao executor (linhas 374-385).
            # Tick cujo lado nao esta na lista do bot e cortado ANTES de tudo:
            # nao consome cap_jogo nem trava mercado, exatamente como ao vivo.
            if lados_bot_norm is not None:
                _sel_lado = _lado_aposta(tick.get('selecao'))
                if _sel_lado is not None and _sel_lado not in lados_bot_norm:
                    rej['lado'] += 1
                    continue

            if max_apostas_partida is not None and apostas_por_evento.get(evt, 0) >= max_apostas_partida:
                rej['cap_jogo'] += 1
                continue

            # evitarLinhasSeq: 1 aposta por mercado_tipo por jogo (igual ao vivo).
            # ticks_ordenados esta em ordem de ts, entao o 1o aceitado por
            # (evento, mercado_tipo) e o mais cedo no tempo = o que o bot ao vivo
            # teria apostado (primeiro tick que passa, depois trava o mercado).
            if evitar_linhas_seq:
                _mtipo_evt = tick.get('mercado_tipo')
                _lado_evt = _lado_aposta(tick.get('selecao'))
                if (evt, _mtipo_evt, _lado_evt) in mercado_apostado_evt:
                    rej['mercado_repetido'] = rej.get('mercado_repetido', 0) + 1
                    continue

            passou, _motivo_basico = _avaliar_filtros_basicos(tick, bot)
            if not passou:
                rej['basico'] += 1
                # Agrupa o SUB-MOTIVO da rejeicao basica (sem os valores, so a
                # categoria) pra reportar no relatorio: assim a UI mostra POR QUE
                # os ticks caem no basico (odd_lt_min, mercado_nao_bate, etc) em
                # vez de so 'basico=N'. Normaliza tirando numeros pra agrupar.
                _cat = re.sub(r'[0-9.]+', '#', _motivo_basico)
                rej_basico_detalhe[_cat] = rej_basico_detalhe.get(_cat, 0) + 1
                continue

            if cenario_partida:
                if not _aplicar_filtro_cenario(tick, cenario_partida):
                    rej['cenario'] += 1
                    continue

            if diff_ativo and diff_min > 0:
                if not _aplicar_filtro_diff_placar(tick, diff_min):
                    rej['diff'] += 1
                    continue

            # v12 — FOLGA (so handicap): folga = hc_assinado - deficit do lado
            # apostado, no placar DESTE tick. Mesma funcao que o bot_executor
            # aplica no vivo (fonte unica).
            if folga_ativo:
                if folga_fora_de_hc:
                    rej['folga_so_hc'] = rej.get('folga_so_hc', 0) + 1
                    continue
                _ok_folga, _mot_folga = _aplicar_filtro_folga(
                    tick, tick.get('selecao', ''), folga_min, folga_max)
                if not _ok_folga:
                    rej['folga'] += 1
                    continue

            # v13 — MOMENTO (estagio do jogo via live_time). Vale pra qualquer
            # mercado; fail-closed quando o tick nao tem periodo reconhecivel.
            if momento_ativo:
                _ok_mom, _mot_mom = _aplicar_filtro_momento(
                    tick, momento_min, momento_max)
                if not _ok_mom:
                    rej['momento'] = rej.get('momento', 0) + 1
                    continue

            # v4: aplica filtros unificados (comp + hist normalizado)
            # mercado do bot (usado pro desvio HC vs over/under). Definido aqui
            # pra estar disponivel tanto no filtro quanto na resolucao abaixo.
            mercado_bot_loop = bot.get('mercado', '')
            stats = None
            if filtros_unificados:
                # v11 FAIL CLOSED: filtro configurado que o backend nao suporta
                # (tipo same_grade/specific_teams, base desconhecida) -> REJEITA
                # o tick com contador proprio; nunca roda "sem o filtro".
                _mot_ns = _primeiro_nao_suportado(filtros_unificados)
                if _mot_ns:
                    rej['filtro_nao_suportado'] = rej.get('filtro_nao_suportado', 0) + 1
                    continue

                ja = tick.get('jogador_a')
                jb = tick.get('jogador_b')
                if not ja or not jb:
                    rej['sem_par'] += 1
                    continue

                # v10: get_jogos toca o banco - blinda. Se falhar a busca do h2h
                # de UM tick, nao derruba o backtest inteiro: conta e pula esse.
                try:
                    jogos_h2h = await h2h_cache.get_jogos(
                        ja, jb, tick['ts'], event_id_excluir=tick.get('event_id'))
                except Exception as e:
                    logger.warning(
                        f"[backtest] job {job_id}: falha h2h {ja}x{jb}: {e}")
                    rej['sem_par'] += 1
                    continue

                linha_num = _parse_linha(tick.get('linha')) or 0

                # ===== RAMO HANDICAP (ah_ft) =====
                # Se o bot e de handicap, o filtro NAO e WR de total (over/under),
                # e sim o pct de cobertura do handicap (validado vs TM). Caminho
                # ISOLADO: nao toca a logica de over/under abaixo.
                if _mercado_eh_hc(mercado_bot_loop):
                    stats = calcular_stat_hc(
                        jogos_h2h, tick.get('selecao', ''), ja, jb)
                    stats['linha_atual'] = linha_num
                    stats['qtd_h2h'] = stats.get('hc_pct_qtd', 0)

                    # Config do filtro de HC (v_escadinha). Precedencia:
                    #  1) campos dedicados no bot (hc_pct_min/max, hc_min_partidas)
                    #     -> comportamento original (_ramo_hc_pct sobre todos).
                    #  2) filtros de WR da UI (tipo 'wr' dos chips E 'hc_wr' dos
                    #     complementares): ESCADINHA — TODOS valem (AND) e cada um
                    #     computa a cobertura na SUA janela. v11: chips com
                    #     base=individual computam no historico INDIVIDUAL.
                    #  3) defaults seguros da estrategia (min 0.87, 20 partidas).
                    hc_min = bot.get('hc_pct_min', bot.get('hc_wr_min'))
                    hc_max = bot.get('hc_pct_max')
                    hc_min_part = bot.get('hc_min_partidas')

                    # FILTROS 6 e 7: blacklist de zebra / favorito (HC).
                    # Listas no filtros jsonb do bot. Isolado, blindado.
                    _blz = (bot.get('filtros') or {}).get('blacklist_zebra')
                    _blf = (bot.get('filtros') or {}).get('blacklist_favorito')
                    _bloq, _mot_bl = _hc_blacklist_bloqueia(
                        tick.get('selecao', ''), ja, jb, _blz, _blf)
                    if _bloq:
                        rej['hc_blacklist'] = rej.get('hc_blacklist', 0) + 1
                        continue

                    if hc_min is not None or hc_max is not None:
                        # 1) campos dedicados do bot — comportamento original
                        try:
                            hc_min_part = int(hc_min_part if hc_min_part is not None else 20)
                        except (TypeError, ValueError):
                            hc_min_part = 20
                        passou_hc, motivo_hc = _ramo_hc_pct(
                            stats, hc_min, hc_max, hc_min_part)
                    else:
                        _fs_wr = [f for f in (filtros_unificados or [])
                                  if (f.get('tipo') or '').lower() in ('wr', 'hc_wr')]
                        if _fs_wr:
                            # v11: chips individuais na escadinha — busca o
                            # historico individual da ZEBRA (e do FAVORITO se
                            # algum chip pedir alvo 'ambos'). Fail closed.
                            jogos_indiv_hc = None
                            if tem_indiv:
                                z_nome, f_nome = _zebra_favorito(
                                    tick.get('selecao', ''), ja, jb)
                                if z_nome is None:
                                    rej['comp'] += 1
                                    continue
                                try:
                                    _zl = await indiv_cache.get_jogos(
                                        z_nome, tick['ts'],
                                        event_id_excluir=tick.get('event_id'))
                                    _fl = None
                                    if indiv_precisa_fav and f_nome:
                                        _fl = await indiv_cache.get_jogos(
                                            f_nome, tick['ts'],
                                            event_id_excluir=tick.get('event_id'))
                                except Exception as e:
                                    logger.warning(
                                        f"[backtest] job {job_id}: falha hist "
                                        f"individual HC {z_nome}/{f_nome}: {e}")
                                    rej['indiv_erro'] = rej.get('indiv_erro', 0) + 1
                                    continue
                                jogos_indiv_hc = {
                                    'zebra_nome': z_nome,
                                    'favorito_nome': f_nome,
                                    'zebra': _zl,
                                    'favorito': _fl,
                                }
                            # 2) ESCADINHA: todos os filtros, cada um na sua janela
                            passou_hc, motivo_hc = _avaliar_escadinha_hc(
                                jogos_h2h, stats, _fs_wr, tick['ts'],
                                jogos_indiv=jogos_indiv_hc)
                        else:
                            # 3) default seguro da estrategia
                            passou_hc, motivo_hc = _ramo_hc_pct(
                                stats, 0.87, None, 20)
                    if not passou_hc:
                        if 'insuf' in motivo_hc:
                            rej['h2h_insuf'] += 1
                        else:
                            rej['comp'] += 1
                        continue
                    qtd_h2h = stats.get('hc_pct_qtd', 0) or 0
                    if qtd_h2h < H2H_MIN_SAUDAVEL:
                        qualidade['apostas_h2h_fraco'] += 1

                # ===== RAMO OVER/UNDER (comportamento original, intacto) =====
                else:
                    stats = _stats_h2h_memo(jogos_h2h, linha_num, janelas_wr, janelas_media,
                                                lado=_lado_aposta(tick.get('selecao')),
                                                ts_ref=tick['ts'])
                    stats['linha_atual'] = linha_num

                    # v11: filtros INDIVIDUAIS (base=individual) — WR das ultimas
                    # N partidas de CADA jogador contra QUALQUER adversario.
                    # Regra AND: os dois precisam passar (aplicada dentro do
                    # _aplicar_filtros_complementares). BLINDADO: falha na busca
                    # individual -> tick rejeitado (fail closed), backtest segue.
                    stats_indiv = None
                    if tem_indiv:
                        try:
                            jogos_ind_a = await indiv_cache.get_jogos(
                                ja, tick['ts'], event_id_excluir=tick.get('event_id'))
                            jogos_ind_b = await indiv_cache.get_jogos(
                                jb, tick['ts'], event_id_excluir=tick.get('event_id'))
                        except Exception as e:
                            logger.warning(
                                f"[backtest] job {job_id}: falha hist individual {ja}/{jb}: {e}")
                            rej['indiv_erro'] = rej.get('indiv_erro', 0) + 1
                            continue
                        _lado_t = _lado_aposta(tick.get('selecao'))
                        st_ind_a = _stats_h2h_memo(jogos_ind_a, linha_num,
                                                       janelas_wr_indiv, set(),
                                                       lado=_lado_t, ts_ref=tick['ts'])
                        st_ind_b = _stats_h2h_memo(jogos_ind_b, linha_num,
                                                       janelas_wr_indiv, set(),
                                                       lado=_lado_t, ts_ref=tick['ts'])
                        stats_indiv = {'a': st_ind_a, 'b': st_ind_b}
                        _espelhar_stats_individuais(stats, st_ind_a, st_ind_b)

                    passou_comp, motivo = _aplicar_filtros_complementares(
                        stats, filtros_unificados, stats_indiv=stats_indiv)
                    if not passou_comp:
                        if 'h2h_insuficiente' in motivo:
                            rej['h2h_insuf'] += 1
                        elif 'indiv' in motivo and 'insuficiente' in motivo:
                            rej['h2h_insuf'] += 1
                        else:
                            rej['comp'] += 1
                        continue

                    # v10: passou nos filtros, mas com amostra h2h fraca? Marca pra
                    # reportar (NAO rejeita - so transparencia). qtd_h2h vem do stats.
                    qtd_h2h = stats.get('qtd_h2h', 0) or 0
                    if qtd_h2h < H2H_MIN_SAUDAVEL:
                        qualidade['apostas_h2h_fraco'] += 1

            mercado_bot = bot.get('mercado', '')
            # Mercado de 1o tempo (HT) resolve com o placar do INTERVALO; todo o
            # resto com o placar FINAL. Se o bot e HT mas nao ha placar de HT
            # recuperavel (casa nao marca periodo, ou evento sem tick de HT/2Q),
            # nao da pra gradear -> balde 'mercado_ht_sem_suporte' (mesmo destino
            # de antes, quando NADA de HT era resolvido).
            if _periodo_do_bot(mercado_bot) == 'ht':
                placar = placar_ht.get(evt)
                if not placar:
                    rej['mercado_ht_sem_suporte'] = rej.get('mercado_ht_sem_suporte', 0) + 1
                    continue
            else:
                placar = placar_final.get(evt)
                if not placar:
                    rej['sem_placar'] += 1
                    qualidade['eventos_sem_placar_final'] += 1
                    continue
            score_home, score_away = placar

            linha_num = _parse_linha(tick.get('linha'))
            # ===== resolucao HANDICAP por NICK (isolada) =====
            if _mercado_eh_hc(mercado_bot):
                resultado = _resolve_resultado_hc(
                    tick.get('selecao', ''),
                    tick.get('jogador_a'), tick.get('jogador_b'),
                    score_home, score_away,
                )
            else:
                resultado = _resolve_resultado(
                    mercado_bot, tick.get('selecao', ''),
                    linha_num, score_home, score_away,
                )
            if resultado is None:
                # Mercados de 1o tempo (HT) nao tem como ser resolvidos com o placar
                # FINAL - precisariam do placar do intervalo (nao disponivel aqui).
                # Em vez de cair mudo em 'sem_resultado', conta num balde proprio
                # pra UI deixar claro POR QUE deu 0 (e nao parecer bug de "nada").
                if mercado_bot in ('over_under_ht', 'asian_over_under_ht',
                                    'over_under_ht_player', 'ml_ht', 'ah_ht', 'asian_over_under_ht'):
                    rej['mercado_ht_sem_suporte'] = rej.get('mercado_ht_sem_suporte', 0) + 1
                else:
                    rej['sem_resultado'] += 1
                continue

            apostas_por_evento[evt] = apostas_por_evento.get(evt, 0) + 1
            if evitar_linhas_seq:
                mercado_apostado_evt.add((evt, tick.get('mercado_tipo'),
                                          _lado_aposta(tick.get('selecao'))))
            candidatas.append({
                'tick': tick,
                'linha_num': linha_num,
                'score_home': score_home,
                'score_away': score_away,
                'resultado': resultado,
                'stats': stats,
            })

        rej_str = ', '.join(f'{k}={v}' for k, v in rej.items() if v > 0) or 'nenhuma'
        # monta o detalhe do basico (top sub-motivos) pra log e relatorio
        basico_det_str = ''
        if rej_basico_detalhe:
            top = sorted(rej_basico_detalhe.items(), key=lambda x: -x[1])
            basico_det_str = ' [basico: ' + ', '.join(f'{k}={v}' for k, v in top[:8]) + ']'

        logger.info(
            f"[backtest] Job {job_id}: {len(candidatas)} candidatas. "
            f"Rejeicoes: {rej_str}{basico_det_str}. H2H cache: {h2h_cache.stats_cache}"
            + (f". Indiv cache: {indiv_cache.stats_cache}" if tem_indiv else "")
        )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_jobs SET progresso=80, progresso_msg=$2 WHERE id=$1",
                job_id, f"{len(candidatas)} validadas. Rej: {rej_str[:120]}{basico_det_str[:180]}",
            )

        banca = banca_inicial
        banca_pico = banca_inicial
        green = red = void_count = 0
        streak_red_atual = 0
        max_streak_red = 0
        apostas_detalhe = []
        equity_curve = []
        pnl_por_dia: dict = {}
        # JOB42: DD verdadeiro (pico->vale cronologico) + acumulado em unidades
        drawdown_max = 0.0
        pnl_u_acum = 0.0
        pnl_u_pico = 0.0
        drawdown_unidades = 0.0

        # JOB42 (streak/DD reais): liquida na ordem em que os JOGOS TERMINAM
        # (_placar_ts = ts do ultimo tick com placar do evento), nao na ordem
        # de entrada da aposta. Com jogos sobrepostos, a ordem de entrada
        # intercala green/red de jogos diferentes e mente o streak e o DD
        # (job 42: streak 4 na ordem de entrada vs 5 na ordem real de caixa).
        # Desempate: ts da aposta. Fallback blindado: sem _placar_ts, usa o
        # proprio ts da aposta (nunca crasha).
        candidatas.sort(key=lambda x: (
            _placar_ts.get(x['tick']['event_id'], x['tick']['ts']),
            x['tick']['ts'],
        ))

        # Colunas de WR pra planilha (estilo bot 313886): pega as janelas dos
        # filtros de WR (tipo='wr') -> (rotulo, chave_no_stats). Ex: janela 10 ->
        # ("Últ. 10", "wr_ult10").
        # v11: chip INDIVIDUAL aponta pra chave individual — O/U mostra o PIOR
        # dos dois jogadores (o numero que decide o gate AND); HC mostra o pct
        # da ZEBRA na janela. Sem colidir com as chaves do par.
        # v11.1: a lista carrega TODAS as colunas (todos os chips; individuais
        # de O/U tambem A e B; HC alvo 'ambos' tambem o favorito). A planilha
        # monta uma coluna por item ('wr_cols' no detalhe); janela_1/2 seguem
        # sendo as 2 primeiras, por compatibilidade.
        # v_colunas (mineracao): alem dos chips de WR, os COMPLEMENTARES
        # (media, gap, z-score, desvio, tendencia) e a QUANTIDADE de confrontos
        # tambem viram coluna. Sem isso o minerador nao enxerga os eixos que
        # decidem over/under (gap e z) nem o "tamanho do historico" — que era
        # o '20+ conf.' escolhido na mao. Chip com filtro ABERTO (0%) nao corta
        # nada e so anota: e assim que se gera a planilha de mineracao.
        _TIPOS_COLUNA = ('wr', 'hc_wr', 'media', 'gap_media', 'gap',
                         'gap_linha', 'zscore', 'z', 'tendencia', 'qtd_h2h')
        _wr_cols = []
        for _f in filtros_unificados:
            _tipo_f = (_f.get('tipo') or '').lower().strip()
            if _tipo_f not in _TIPOS_COLUNA:
                continue
            _jw = _f.get('janela')
            if isinstance(_jw, str):
                _tok = _jw; _lbl = f"Últ. {_jw}"
            elif _jw:
                _tok = str(int(_jw)); _lbl = f"Últ. {int(_jw)}"
            else:
                _tok = '0'; _lbl = "Todas"

            if _tipo_f in ('wr', 'hc_wr'):
                if _eh_filtro_individual(_f):
                    if _mercado_eh_hc(bot.get('mercado', '')):
                        _wr_cols.append((f"{_lbl} (ind zebra)", f'wr_ult{_tok}_ind'))
                        if _f.get('hist_indiv_alvo') == 'ambos':
                            # v11.1: pct do favorito agora vira coluna tambem
                            _wr_cols.append((f"{_lbl} (ind fav)", f'wr_ult{_tok}_indfav'))
                    else:
                        _wr_cols.append((f"{_lbl} (ind pior)", f'wr_ult{_tok}_indmin'))
                        _wr_cols.append((f"{_lbl} (ind A)", f'indiv_a_wr_ult{_tok}'))
                        _wr_cols.append((f"{_lbl} (ind B)", f'indiv_b_wr_ult{_tok}'))
                else:
                    _wr_cols.append((_lbl, f'wr_ult{_tok}'))
                    # v_colunas: quantos confrontos a janela realmente usou
                    _wr_cols.append((f"Qtd {_lbl}", f'wr_ult{_tok}_qtd'))
            elif _tipo_f == 'media':
                _wr_cols.append((f"Média {_lbl}", f'media_ult{_tok}'))
                _wr_cols.append((f"Qtd Média {_lbl}", f'media_ult{_tok}_qtd'))
            elif _tipo_f in ('gap_media', 'gap'):
                # derivada: media da janela MENOS a linha daquela aposta
                _wr_cols.append((f"Gap {_lbl}", f'CALC:gap:{_tok}'))
            elif _tipo_f in ('zscore', 'z'):
                _wr_cols.append((f"Z {_lbl}", f'z_ult{_tok}'))
                _wr_cols.append((f"Desvio {_lbl}", f'desvio_ult{_tok}'))
            elif _tipo_f == 'tendencia':
                _wr_cols.append(("Tendência", 'tendencia'))
            elif _tipo_f == 'gap_linha':
                _wr_cols.append(("Gap Linha", 'CALC:gaplinha'))
            elif _tipo_f == 'qtd_h2h':
                _wr_cols.append(("Qtd H2H", 'qtd_h2h'))

        # legado (janela_1/2 e winrate_1/2): so as colunas de WR PERCENTUAL,
        # na ordem — as novas colunas nao podem entrar aqui senao quebram o
        # formato antigo que jobs velhos e leitores externos esperam.
        _wr_legado = [_c for _c in _wr_cols
                      if str(_c[1]).startswith(('wr_ult', 'indiv_a_wr', 'indiv_b_wr'))
                      and not str(_c[1]).endswith('_qtd')]

        for i, c in enumerate(candidatas):
            tick = c['tick']
            odd = float(tick['odds'])
            resultado = c['resultado']

            if stake_modo == 'fixo':
                stake = stake_valor
            else:
                stake = banca_pico * (stake_valor / 100.0)
                stake = round(stake, 2)

            if resultado == 'green':
                pnl_aposta = stake * (odd - 1)
                green += 1
                streak_red_atual = 0
            elif resultado == 'red':
                pnl_aposta = -stake
                red += 1
                streak_red_atual += 1
                if streak_red_atual > max_streak_red:
                    max_streak_red = streak_red_atual
            else:
                pnl_aposta = 0
                void_count += 1
                # JOB42: void NAO e green - nao quebra a sequencia de reds.
                # (streak_red_atual fica como esta: nao soma, nao zera.)

            banca += pnl_aposta
            if banca > banca_pico:
                banca_pico = banca
            # JOB42: DD pico->vale em ORDEM cronologica (o calculo antigo
            # max(banca)-min(banca) misturava vale ANTERIOR ao pico: 6171
            # onde o verdadeiro era 3652). banca_pico e o pico corrente.
            if (banca_pico - banca) > drawdown_max:
                drawdown_max = banca_pico - banca
            # acumulado em UNIDADES (1u = stake da aposta; vale pro fixo e pro %)
            pnl_u_acum += (pnl_aposta / stake) if stake else 0.0
            if pnl_u_acum > pnl_u_pico:
                pnl_u_pico = pnl_u_acum
            if (pnl_u_pico - pnl_u_acum) > drawdown_unidades:
                drawdown_unidades = pnl_u_pico - pnl_u_acum

            st = c.get('stats') or {}
            _sel = tick.get('selecao', '') or ''
            if _mercado_eh_hc(bot.get('mercado', '')):
                # HC nao tem lado Over/Under: a tip e a propria selecao
                # (JOB42: 'ThUNDER' virava tip 'Under' na planilha).
                _tip = _sel
            else:
                _ld = _lado_aposta(_sel)
                _tip = 'Over' if _ld == 'over' else ('Under' if _ld == 'under' else _sel)
            apostas_detalhe.append({
                'n': i + 1,
                'event_id': tick['event_id'],
                'ts': tick['ts'].isoformat() if hasattr(tick['ts'], 'isoformat') else str(tick['ts']),
                # JOB42: quando o jogo terminou (ordem de liquidacao/caixa).
                'ts_resolucao': (_placar_ts.get(tick['event_id']).isoformat()
                                 if hasattr(_placar_ts.get(tick['event_id'], None), 'isoformat')
                                 else str(_placar_ts.get(tick['event_id'], ''))),
                'torneio': tick.get('torneio') or bot.get('torneio') or '',
                'liga': tick.get('liga', '') or '',
                'jogador_a': tick['jogador_a'],
                'jogador_b': tick['jogador_b'],
                'time_a': tick.get('time_a', '') or '',
                'time_b': tick.get('time_b', '') or '',
                'mercado': tick['mercado'],
                'tip': _tip,
                'linha': c['linha_num'],
                'selecao': _sel,
                # WR/janelas que o backtest usou (pra bater com a planilha do bot)
                'janela_1': _wr_legado[0][0] if len(_wr_legado) > 0 else '',
                'winrate_1': st.get(_wr_legado[0][1]) if len(_wr_legado) > 0 else None,
                'janela_2': _wr_legado[1][0] if len(_wr_legado) > 1 else '',
                'winrate_2': st.get(_wr_legado[1][1]) if len(_wr_legado) > 1 else None,
                # v11.1: TODAS as colunas de WR (a planilha monta dinamico) +
                # qtd individual de cada jogador (pra filtrar maturidade no Excel)
                'wr_cols': [{'l': _l, 'v': _valor_coluna(st, _k)} for _l, _k in _wr_cols],
                'qtd_ind_a': st.get('qtd_indiv_a'),
                'qtd_ind_b': st.get('qtd_indiv_b'),
                'odd': odd,
                'stake': stake,
                'placar_envio': f"{tick.get('score_home')}-{tick.get('score_away')}",
                'score_final': f"{c['score_home']}-{c['score_away']}",
                'resultado': resultado,
                'pnl': round(pnl_aposta, 2),
                'lucro_unidades': round(pnl_aposta / stake, 3) if stake else 0,
                'banca_apos': round(banca, 2),
            })

            equity_curve.append({
                'n': i + 1,
                'banca': round(banca, 2),
                'pnl_acum': round(banca - banca_inicial, 2),
                'u_acum': round(pnl_u_acum, 2),
                'ts': tick['ts'].isoformat() if hasattr(tick['ts'], 'isoformat') else str(tick['ts']),
            })

            dia_key = tick['ts'].date().isoformat() if hasattr(tick['ts'], 'date') else str(tick['ts'])[:10]
            if dia_key not in pnl_por_dia:
                pnl_por_dia[dia_key] = {'data': dia_key, 'apostas': 0, 'pnl': 0.0}
            pnl_por_dia[dia_key]['apostas'] += 1
            pnl_por_dia[dia_key]['pnl'] = round(pnl_por_dia[dia_key]['pnl'] + pnl_aposta, 2)

        total_apostas = green + red + void_count
        total_stake = sum(a['stake'] for a in apostas_detalhe) if apostas_detalhe else 0
        pnl_total = banca - banca_inicial

        roi = (pnl_total / total_stake) if total_stake > 0 else 0
        win_rate = (green / (green + red)) if (green + red) > 0 else 0
        # JOB42: drawdown_max ja foi calculado DENTRO do loop (pico->vale
        # cronologico). Aqui so garante nao-negativo.
        drawdown_max = max(0.0, drawdown_max)

        pnl_por_dia_lista = sorted(pnl_por_dia.values(), key=lambda d: d['data'])
        dias_verdes = sum(1 for d in pnl_por_dia_lista if d['pnl'] > 0)
        dias_total = len(pnl_por_dia_lista)

        rej_resumo = ', '.join(f'{k}={v}' for k, v in rej.items() if v > 0) or 'nenhuma'

        # v10: monta avisos de QUALIDADE (transparencia do resultado).
        # Num backtest de dinheiro real, o numero precisa vir com ressalvas.
        avisos = []
        if total_apostas > 0:
            frac_fraco = qualidade['apostas_h2h_fraco'] / total_apostas
            if qualidade['apostas_h2h_fraco'] > 0:
                avisos.append(
                    f"{qualidade['apostas_h2h_fraco']} apostas "
                    f"({frac_fraco:.0%}) com h2h fraco (<{H2H_MIN_SAUDAVEL} jogos)"
                )
        if qualidade['eventos_sem_placar_final'] > 0:
            avisos.append(
                f"{qualidade['eventos_sem_placar_final']} sinais sem placar final "
                f"(nao resolvidos)"
            )
        # v11: bot com filtro nao suportado = 100% rejeitado de proposito.
        # Deixa isso ESCRITO no relatorio pro usuario entender o 0 apostas.
        if rej.get('filtro_nao_suportado'):
            avisos.append(
                f"{rej['filtro_nao_suportado']} ticks rejeitados por filtro "
                f"NAO SUPORTADO no bot (fail closed) — revise os chips"
            )

        partes_msg = []
        if total_apostas == 0:
            # 0 apostas: mostra a quebra do basico (a UI ve POR QUE deu 0).
            partes_msg.append(f"Concluido. Rej: {rej_resumo}{basico_det_str}")
        else:
            partes_msg.append("Concluido")
        if avisos:
            partes_msg.append("RESSALVAS: " + "; ".join(avisos))
        msg_final = ' | '.join(partes_msg)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE backtest_jobs SET
                    status='concluido', progresso=100,
                    progresso_msg=$16,
                    total_apostas=$2, green=$3, red=$4, void_count=$5,
                    pnl=$6, roi=$7, win_rate=$8, drawdown_max=$9, max_streak_red=$10,
                    dias_verdes=$11, dias_total=$12,
                    equity_curve=$13::jsonb, apostas_detalhe=$14::jsonb, pnl_por_dia=$15::jsonb,
                    pnl_unidades=$17, drawdown_unidades=$18,
                    concluido_em=NOW()
                WHERE id=$1
                """,
                job_id, total_apostas, green, red, void_count,
                round(pnl_total, 2), round(roi, 4), round(win_rate, 4),
                round(drawdown_max, 2), max_streak_red,
                dias_verdes, dias_total,
                json.dumps(equity_curve, default=str),
                json.dumps(apostas_detalhe, default=str),
                json.dumps(pnl_por_dia_lista, default=str),
                msg_final[:500],
                round(pnl_u_acum, 2), round(drawdown_unidades, 2),
            )

        logger.info(
            f"[backtest] Job {job_id} concluido: {total_apostas} apostas, "
            f"ROI {roi*100:.2f}%, WR {win_rate*100:.2f}%, PnL {pnl_total:.2f}"
        )

    except Exception as e:
        logger.exception(f"[backtest] Erro no job {job_id}: {e}")
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE backtest_jobs SET status='erro', erro=$2, concluido_em=NOW() WHERE id=$1",
                    job_id, str(e)[:500],
                )
        except Exception as e2:
            logger.exception(f"[backtest] Falha ao salvar erro: {e2}")
