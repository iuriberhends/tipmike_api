"""
routers/export_tm.py  (v40, 23/set)
Export .xlsx no formato da planilha da TipManager (7 abas): Tips Enviadas,
Torneios, Grades, Confrontos, Jogadores, Prob. Hist., Horarios.

MODULO NOVO E AUTOSSUFICIENTE (nao mexe em routers/historico.py).
Instalar: copiar pra routers/export_tm.py + 1 linha no main.py:
    from routers import export_tm
    app.include_router(export_tm.router)
Requer openpyxl no venv da API (pip install openpyxl).

Endpoint: GET /bots/{bot_id}/export.xlsx?modo=simulado&periodo=30d
Fonte: tabela apostas. Time A/B, Favorito/Azarao vem de stats_h2h
(gravado pelo bot_executor v36+); apostas anteriores ficam com '-'.
"""

import io
import json
import math
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from database import db
from security import get_current_user, acesso_total

router = APIRouter(prefix="/bots", tags=["export-tm"])

BRT = timezone(timedelta(hours=-3))

# mesmo mapa do routers/historico.py (duplicado de proposito: modulo isolado)
PERIODO_DIAS = {
    "dia": 1,
    "3d": 3,
    "7d": 7,
    "15d": 15,
    "30d": 30,
    "todas": 90,
}


def _slug_filename(s: str, max_len: int = 50) -> str:
    out = []
    for c in (s or ''):
        if c.isalnum():
            out.append(c)
        elif c in (' ', '-', '_'):
            out.append('_')
    slug = ''.join(out).strip('_')[:max_len]
    return slug or 'bot'


# ============================================================
# EXPORT XLSX — "planilha TipManager" (v40)
# 7 abas iguais ao export da TM: Tips Enviadas, Torneios, Grades,
# Confrontos, Jogadores, Prob. Hist., Horarios. Fonte: tabela apostas.
# Favorito/Azarao/Time A/Time B vem de stats_h2h (gravado pelo executor
# v36+); apostas antigas ficam com '-' nesses campos.
# ============================================================

XLSX_TIPS_COLS = [
    'Torneio', 'Campeonato', 'Confronto', 'Jogador A', 'Time A', 'Jogador B', 'Time B',
    'Favorito', 'Azarão', 'Data', 'Hora', 'Horário Jogo', 'Período Jogo', 'Mercado', 'Tip',
    'Linha', 'Janela 1', 'Winrate 1', 'Janela 2', 'Winrate 2', 'Odd', 'Placar Envio',
    'Placar Final', 'Red Por', 'Resultado', 'Lucro/Prej.',
]
XLSX_AGG_COLS = [
    'Qtd. Entradas', 'Qtd. Greens', 'Greens (%)', 'Qtd. ½ Greens', '½ Greens (%)',
    'Qtd. Reds', 'Reds (%)', 'Qtd. ½ Reds', '½ Reds (%)', 'Qtd. Voids', 'Voids (%)',
    'Lucro/Prej.', 'ROI',
]

_FAMILIAS_LIGA = [
    (re.compile(r'gg league', re.I), 'H2H GG League'),
    (re.compile(r'battle', re.I), 'Battle'),
    (re.compile(r'\bgt\b|gt league', re.I), 'GT League'),
    (re.compile(r'cyber live arena|\bcla\b', re.I), 'Cyber Live Arena'),
    (re.compile(r'adriatic|nextgen|\beal\b', re.I), 'EAL NextGen'),
    (re.compile(r'valkyrie', re.I), 'Valkyrie'),
    (re.compile(r'esoccer|e-soccer', re.I), 'Esoccer'),
]


def _familia_liga(liga: str) -> str:
    """Nome de familia (coluna Torneio da TM) a partir da liga real."""
    s = (liga or '').strip()
    if not s:
        return '-'
    for rx, nome in _FAMILIAS_LIGA:
        if rx.search(s):
            return nome
    return s.split(' - ')[0].strip() or s


def _f(v):
    """numeric/Decimal/str -> float ou None (blindado)."""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except Exception:
        return None


def _stats_dict(v) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _mercado_label(mercado: str, esporte: str) -> str:
    m = (mercado or '').lower()
    unid = 'Gols' if 'fifa' in (esporte or '').lower() or 'foot' in (esporte or '').lower() else 'Pontos'
    if m == 'over_under_ft':
        return unid
    if m == 'over_under_ht':
        return f'{unid} 1T'
    if m == 'over_under_ft_player':
        return f'{unid} do time'
    if m == 'over_under_ht_player':
        return f'{unid} do time 1T'
    if m == 'ah_ft':
        return 'Handicap'
    if m == 'ah_ht':
        return 'Handicap 1T'
    if m in ('ml_ft', 'ml'):
        return 'Vencedor'
    if m == 'ml_ht':
        return 'Vencedor 1T'
    return mercado or '-'


def _tip_label(mercado: str, selecao: str, stats: dict) -> str:
    """Coluna Tip: Over/Under pros totais, selecao inteira pro resto."""
    m = (mercado or '').lower()
    s = (selecao or '').strip()
    if m.startswith('over_under'):
        sl = s.lower()
        if sl.startswith(('over', 'mais', 'acima')) or '/over' in sl or ' over' in sl:
            base = 'Over'
        elif sl.startswith(('under', 'menos', 'abaixo')) or '/under' in sl or ' under' in sl:
            base = 'Under'
        else:
            base = s or '-'
        if m.endswith('_player'):
            alvo = stats.get('alvo_nome')
            return f'{base} {alvo}' if alvo else base
        return base
    return s or '-'


_RX_CLOCK = re.compile(r'(\d{1,2}:\d{2})')


def _horario_periodo(live_time: str, periodo_entrada: str, minuto_entrada) -> tuple:
    """(Horario Jogo, Periodo Jogo) a partir do live_time do tick."""
    lt = (live_time or '').strip()
    m = _RX_CLOCK.search(lt)
    horario = m.group(1) if m else ('' if minuto_entrada is None else f"{int(minuto_entrada):02d}:00")
    periodo = _RX_CLOCK.sub('', lt).strip(' -|') or (periodo_entrada or '')
    return horario or '-', periodo or '-'


_JANELA_ORDEM = [
    ('wr_all', 'Todas'), ('wr_todas', 'Todas'), ('hc_pct', 'Todas'),
]


def _janelas(stats: dict) -> list:
    """Lista [(label, wr)] das janelas de chip gravadas no stats_h2h."""
    out = []
    vistos = set()
    for k, label in _JANELA_ORDEM:
        if k in stats and _f(stats.get(k)) is not None and label not in vistos:
            out.append((label, _f(stats[k])))
            vistos.add(label)
    ults = []
    for k, v in stats.items():
        mm = re.fullmatch(r'wr_ult(\d+)', str(k))
        if mm and _f(v) is not None:
            ults.append((int(mm.group(1)), _f(v)))
    for n, v in sorted(ults, reverse=True):
        lab = f'Últ. {n}'
        if lab not in vistos:
            out.append((lab, v))
            vistos.add(lab)
    for k, v in stats.items():
        mm = re.fullmatch(r'indiv_([ab])_ult(\d+)', str(k))
        if mm and _f(v) is not None:
            out.append((f'Ind. {mm.group(1).upper()} últ. {mm.group(2)}', _f(v)))
    # chips de comparacao (z-score / media / desvio): bots sem WR (ex. total
    # por time) ficam com a janela do chip que disparou
    for pref, lab in (('z_ult', 'Z últ.'), ('media_ult', 'Média últ.'), ('desvio_ult', 'Desvio últ.')):
        for k, v in stats.items():
            mm = re.fullmatch(pref + r'(\d+)', str(k))
            if mm and _f(v) is not None:
                out.append((f'{lab} {mm.group(1)}', _f(v)))
    return out


def _red_por(mercado: str, tip: str, linha, fa, fb, resultado: str, stats: dict, ja: str = '', jb: str = ''):
    """Por quantos gols/pontos a tip perdeu (so quando red). Totais: distancia
    do total final ate a linha; handicap: distancia da margem ate a linha."""
    if (resultado or '').lower() != 'red':
        return None
    ln = _f(linha)
    if ln is None or fa is None or fb is None:
        return None
    m = (mercado or '').lower()
    try:
        if m.startswith('over_under'):
            if m.endswith('_player'):
                lado = stats.get('lado_alvo')
                total = fa if lado == 'home' else fb if lado == 'away' else None
                if total is None:
                    return None
            else:
                total = fa + fb
            t = (tip or '').lower()
            if t.startswith('over'):
                d = ln - total
            elif t.startswith('under'):
                d = total - ln
            else:
                return None
            return int(math.ceil(d)) if d > 0 else None
        if m.startswith('ah'):
            # de que lado e' a aposta: nick gravado (hc_nick) comparado com
            # jogador_a/b; fallback: qual nick aparece na selecao
            nick = (stats.get('hc_nick') or '').strip().lower()
            sel = (tip or '').lower()
            ja_l, jb_l = (ja or '').strip().lower(), (jb or '').strip().lower()
            if nick and nick == ja_l:
                lado_a = True
            elif nick and nick == jb_l:
                lado_a = False
            elif ja_l and ja_l in sel and not (jb_l and jb_l in sel):
                lado_a = True
            elif jb_l and jb_l in sel and not (ja_l and ja_l in sel):
                lado_a = False
            else:
                return None
            marg = (fa - fb) if lado_a else (fb - fa)
            d = -(marg + ln)
            return int(math.ceil(d)) if d > 0 else None
    except Exception:
        return None
    return None


def _resultado_label(resultado: str, status: str) -> str:
    r = (resultado or '').lower()
    if r == 'green':
        return 'Green'
    if r == 'red':
        return 'Red'
    if r in ('void', 'push', 'anulada', 'cancelada'):
        return 'Void'
    if 'half' in r or 'meio' in r:
        return 'Meio Green' if 'green' in r else 'Meio Red'
    return 'Pendente'


def _classe_resultado(label: str) -> str:
    return {'Green': 'g', 'Red': 'r', 'Void': 'v', 'Meio Green': 'hg', 'Meio Red': 'hr'}.get(label, 'p')


def _periodo_dia(h: int) -> str:
    if h < 6:
        return 'Madrugada'
    if h < 12:
        return 'Manhã'
    if h < 18:
        return 'Tarde'
    return 'Noite'


def _linha_tips(d: dict) -> dict:
    """Uma aposta -> dict com as 26 colunas da aba Tips Enviadas + campos
    auxiliares (_cls, _lucro, _hora) usados nas agregacoes."""
    stats = _stats_dict(d.get('stats_h2h'))
    liga = d.get('liga') or d.get('torneio') or ''
    ja, jb = d.get('jogador_a') or '-', d.get('jogador_b') or '-'
    ap = d.get('apostado_em')
    if isinstance(ap, datetime) and ap.tzinfo is not None:
        ap = ap.astimezone(BRT)
    mercado = d.get('mercado') or ''
    tip = _tip_label(mercado, d.get('selecao') or d.get('lado'), stats)
    horario, periodo = _horario_periodo(d.get('live_time'), d.get('periodo_entrada'), d.get('minuto_entrada'))
    jan = _janelas(stats)
    fa, fb = d.get('placar_final_a'), d.get('placar_final_b')
    res = _resultado_label(d.get('resultado'), d.get('status'))
    lucro = _f(d.get('lucro_unidades'))
    if res == 'Pendente':
        lucro = None
    fav = stats.get('favorito') or '-'
    aza = stats.get('azarao') or '-'
    return {
        'Torneio': _familia_liga(liga),
        'Campeonato': liga or '-',
        'Confronto': f'{ja} vs {jb}',
        'Jogador A': ja,
        'Time A': stats.get('time_a') or '-',
        'Jogador B': jb,
        'Time B': stats.get('time_b') or '-',
        'Favorito': fav,
        'Azarão': aza,
        'Data': ap.strftime('%d/%m/%Y') if isinstance(ap, datetime) else '',
        'Hora': ap.strftime('%H:%M:%S') if isinstance(ap, datetime) else '',
        'Horário Jogo': horario,
        'Período Jogo': periodo,
        'Mercado': _mercado_label(mercado, d.get('esporte')),
        'Tip': tip,
        'Linha': _f(d.get('linha')),
        'Janela 1': jan[0][0] if len(jan) > 0 else '',
        'Winrate 1': round(jan[0][1], 4) if len(jan) > 0 else None,
        'Janela 2': jan[1][0] if len(jan) > 1 else '',
        'Winrate 2': round(jan[1][1], 4) if len(jan) > 1 else None,
        'Odd': _f(d.get('odd')),
        'Placar Envio': (f"{d.get('placar_a_entrada')}-{d.get('placar_b_entrada')}"
                         if d.get('placar_a_entrada') is not None and d.get('placar_b_entrada') is not None else '-'),
        'Placar Final': f'{fa}-{fb}' if fa is not None and fb is not None else '-',
        'Red Por': _red_por(mercado, tip, d.get('linha'), fa, fb, d.get('resultado'), stats, ja, jb),
        'Resultado': res,
        'Lucro/Prej.': round(lucro, 3) if lucro is not None else None,
        '_cls': _classe_resultado(res),
        '_hora': ap.hour if isinstance(ap, datetime) else None,
    }


def _agregar(linhas: list, chaves: list, extrator) -> list:
    """Agrega as tips por chave (lista de nomes de coluna); extrator(linha)
    devolve a tupla-chave (pode devolver lista de tuplas -> conta em varias,
    usado em Jogadores, onde a aposta pertence aos 2 jogadores)."""
    acc = {}
    for ln in linhas:
        ks = extrator(ln)
        if ks is None:
            continue
        if not isinstance(ks, list):
            ks = [ks]
        for k in ks:
            a = acc.setdefault(k, {'n': 0, 'g': 0, 'hg': 0, 'r': 0, 'hr': 0, 'v': 0, 'lucro': 0.0})
            c = ln['_cls']
            if c == 'p':
                continue  # pendente nao entra nas agregacoes (igual TM)
            a['n'] += 1
            a[c] += 1
            a['lucro'] += ln['Lucro/Prej.'] or 0.0
    out = []
    def _ord(t):
        return tuple((0, x, '') if isinstance(x, (int, float)) else (1, 0, str(x or '').lower()) for x in t)

    for k in sorted(acc.keys(), key=_ord):
        a = acc[k]
        n = a['n']
        if n == 0:
            continue
        row = dict(zip(chaves, k))
        row.update({
            'Qtd. Entradas': n,
            'Qtd. Greens': a['g'], 'Greens (%)': round(a['g'] / n, 6),
            'Qtd. ½ Greens': a['hg'], '½ Greens (%)': round(a['hg'] / n, 6),
            'Qtd. Reds': a['r'], 'Reds (%)': round(a['r'] / n, 6),
            'Qtd. ½ Reds': a['hr'], '½ Reds (%)': round(a['hr'] / n, 6),
            'Qtd. Voids': a['v'], 'Voids (%)': round(a['v'] / n, 6),
            'Lucro/Prej.': round(a['lucro'], 6),
            'ROI': round(a['lucro'] / n, 6),
        })
        out.append(row)
    return out


def montar_xlsx_tipmanager(rows: list, titulo: str = '') -> bytes:
    """Recebe as apostas (dicts da tabela) e devolve o .xlsx com as 7 abas."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError("openpyxl nao instalado no venv da API (pip install openpyxl)") from e

    linhas = []
    for r in rows:
        try:
            linhas.append(_linha_tips(dict(r)))
        except Exception:
            continue
    # TM lista em ordem cronologica crescente
    linhas.sort(key=lambda x: (x['Data'][6:10] + x['Data'][3:5] + x['Data'][0:2], x['Hora']))

    wb = Workbook()
    head_font = Font(bold=True, color='FFFFFF')
    head_fill = PatternFill('solid', fgColor='1F3A5F')
    pct_cols = {'Greens (%)', '½ Greens (%)', 'Reds (%)', '½ Reds (%)', 'Voids (%)', 'ROI', 'Winrate 1', 'Winrate 2'}

    def escreve(ws, cols, dados):
        ws.append(cols)
        for c in ws[1]:
            c.font = head_font
            c.fill = head_fill
            c.alignment = Alignment(horizontal='center')
        for d in dados:
            ws.append([d.get(c) for c in cols])
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        for i, col in enumerate(cols, start=1):
            letra = get_column_letter(i)
            largura = max(10, min(28, len(col) + 2))
            ws.column_dimensions[letra].width = largura
            if col in pct_cols:
                for celula in ws[letra][1:]:
                    celula.number_format = '0.00%'
            elif col in ('Lucro/Prej.',):
                for celula in ws[letra][1:]:
                    celula.number_format = '0.00'

    ws = wb.active
    ws.title = 'Tips Enviadas'
    escreve(ws, XLSX_TIPS_COLS, linhas)

    escreve(wb.create_sheet('Torneios'), ['Torneio'] + XLSX_AGG_COLS,
            _agregar(linhas, ['Torneio'], lambda l: (l['Torneio'],)))
    escreve(wb.create_sheet('Grades'), ['Torneio', 'Campeonato'] + XLSX_AGG_COLS,
            _agregar(linhas, ['Torneio', 'Campeonato'], lambda l: (l['Torneio'], l['Campeonato'])))
    escreve(wb.create_sheet('Confrontos'), ['Torneio', 'Confronto'] + XLSX_AGG_COLS,
            _agregar(linhas, ['Torneio', 'Confronto'],
                     lambda l: (l['Torneio'], ' vs '.join(sorted([l['Jogador A'], l['Jogador B']], key=str.lower)))))
    escreve(wb.create_sheet('Jogadores'), ['Torneio', 'Jogador'] + XLSX_AGG_COLS,
            _agregar(linhas, ['Torneio', 'Jogador'],
                     lambda l: [(l['Torneio'], l['Jogador A']), (l['Torneio'], l['Jogador B'])]))
    escreve(wb.create_sheet('Prob. Hist.'), ['Torneio', 'Prob. Hist.'] + XLSX_AGG_COLS,
            _agregar(linhas, ['Torneio', 'Prob. Hist.'],
                     lambda l: (l['Torneio'], l['Winrate 1']) if l['Winrate 1'] is not None else None))
    escreve(wb.create_sheet('Horários'), ['Torneio', 'Hora do Dia', 'Período'] + XLSX_AGG_COLS,
            _agregar(linhas, ['Torneio', 'Hora do Dia', 'Período'],
                     lambda l: (l['Torneio'], l['_hora'], _periodo_dia(l['_hora'])) if l['_hora'] is not None else None))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.get("/{bot_id}/export.xlsx")
async def export_apostas_xlsx(
    bot_id: int,
    modo: str = Query("simulado", description="simulado | real"),
    periodo: str = Query("todas", description="dia | 3d | 7d | 15d | 30d | todas (todas = sem filtro de data)"),
    limit: int = Query(100000, ge=1, le=300000),
    usuario: dict = Depends(get_current_user),
):
    """
    Exporta as apostas do bot em .xlsx no formato da TipManager (7 abas):
    Tips Enviadas, Torneios, Grades, Confrontos, Jogadores, Prob. Hist., Horarios.
    """
    if modo not in ("simulado", "real"):
        raise HTTPException(400, "modo deve ser 'simulado' ou 'real'")
    if periodo not in PERIODO_DIAS:
        raise HTTPException(400, f"periodo invalido. Use: {list(PERIODO_DIAS.keys())}")

    async with db() as conn:
        bot_row = await conn.fetchrow("SELECT id, nome, user_id FROM bots WHERE id = $1", bot_id)
        if not bot_row:
            raise HTTPException(404, f"Bot {bot_id} nao encontrado")
        if not acesso_total(usuario) and bot_row["user_id"] != usuario.get("id"):
            raise HTTPException(404, f"Bot {bot_id} nao encontrado")

        where = ["bot_id = $1", "modo = $2"]
        args = [bot_id, modo]
        if periodo != 'todas':
            where.append(f"apostado_em >= NOW() - INTERVAL '{PERIODO_DIAS[periodo]} days'")

        rows = await conn.fetch(f"""
            SELECT id, apostado_em, esporte, torneio, liga,
                   jogador_a, jogador_b,
                   mercado, linha, selecao, lado, odd,
                   placar_a_entrada, placar_b_entrada,
                   placar_final_a, placar_final_b,
                   minuto_entrada, periodo_entrada, live_time,
                   status, resultado, lucro_unidades, stats_h2h
            FROM apostas
            WHERE {' AND '.join(where)}
            ORDER BY apostado_em ASC
            LIMIT {limit}
        """, *args)

    try:
        conteudo = montar_xlsx_tipmanager([dict(r) for r in rows], bot_row['nome'] or '')
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(500, f"Falha ao montar a planilha: {e}")

    nome_slug = _slug_filename(bot_row['nome'] or 'bot')
    filename = f"Bot_{bot_id}_-_{nome_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        content=conteudo,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Access-Control-Expose-Headers': 'Content-Disposition',
        },
    )
