# -*- coding: utf-8 -*-
"""
===============================================================================
 VARREDURA v6 — busca exaustiva de estrategias (planilha OU parquet de ticks)
===============================================================================
 O QUE MUDOU DA v5:
   [+] FONTE TICK: aceita o PARQUET BRUTO do coletor como entrada (--xlsx
       arquivo.parquet). O conversor interno replica o runner: monta os
       jogos finalizados, gera o escancarado zebra (1 aposta por
       jogo+lado+linha no 1o tick valido), liquida no placar FINAL e
       reconstroi os chips (cobertura da linha por janela) a partir do
       historico. --h2h dump.csv|parquet = dump do h2h_historico
       (carimbado por inserted_at <= as_of do v14) pra chips completos;
       SEM ele os chips ficam truncados ao periodo do arquivo (avisado).
       Margem ao-vivo de 15min e dedup tick x hist portados do runner.
   [+] PARIDADE: --paridade export_do_painel.xlsx compara o escancarado
       reconstruido com o export REAL do runner (casa aposta a aposta,
       reporta so-meu/so-painel e o desvio medio de chip). A auditoria
       manual virou peca do pipeline.
   [+] VIVO ENDURECIDO: vivo=1 agora exige lucro positivo em TODAS as
       janelas de recencia (3d E 7d por padrao, >=10 ap cada). A regua
       antiga (so a janela longa) mascarava virada de mare.
   [+] TENDENCIA: coluna queda_ponta = roi_3d - roi_m2 (quanto a ponta
       caiu vs a 2a metade). Negativo grande = edge morrendo.
   [+] FLAG FRAGIL: borda_chip = fracao da cesta cujo chip esta a menos
       de 1 jogo (1/N da janela) de um corte de WR da config; fragil=1
       se >30%. Banda estreita em janela curta acende aqui — e o que
       desmonta quando a fase 2 escreve 1 jogo retroativo no banco.
   [+] EXPOSICAO: recencia/vivo/queda_ponta logo depois do ROI em todas
       as abas; VIVAS_AGORA e a PRIMEIRA aba do arquivo.
===============================================================================
 O QUE MUDOU DA v4:
   [+] CACA DE WR (a regua do operador: sniper > volume): 'WR' virou criterio
       de guarda (heap proprio, com piso --min-ap-sniper pra nao encher de
       recorte minusculo) e ganhou DUAS abas novas:
         SNIPERS ........ as aprovadas em todas as reguas, ordenadas por WR
         FRONTEIRA_WR ... a melhor config (por unidades) em cada patamar de
                          WR (58, 60, 62...) — o preco de cada ponto de WR
                          em volume, pronto pra leitura
   [+] LEITURA DE FRENTE PRA TRAS (recencia): toda config ganha janelas
       ancoradas no ULTIMO dia do arquivo — ultimos 3d e 7d por padrao
       (--rec-janelas) — com ap/G/R/unidades/ROI de cada janela e a flag
       `vivo` (padrao segue de pe na janela mais longa?). Aba VIVAS_AGORA
       ordena as aprovadas da janela mais atual pra tras.
       HONESTIDADE: essas janelas cobrem os MESMOS dias do teste cego.
       Usa-las pra ESCOLHER gasta o cego (a selecao passou a olhar o
       holdout) — config escolhida por recencia so carimba no paper/vivo
       dos dias seguintes. A regua de aprovacao (sorte/placebo/z/metades/
       cego) segue exatamente a mesma.

 O QUE MUDOU DA v2 (e por que):
   [+] ODD virou eixo de PRIMEIRA CLASSE: faixa odd_min x odd_max varrida em
       grade tirada dos quantis do SEU arquivo (a v2 so tinha 4 baldes fixos
       escondidos atras de --eixos). Se a odd for chapada (ex.: 97% em 1.83),
       o eixo e detectado como MORTO e nao infla a busca.
   [+] WINRATE MAXIMO (teto de WR) no eixo principal e na 2a janela. Era um
       buraco real: o achado validado `Ult.30>=80% + Todas<=95%` (job 75) a
       v2 NUNCA encontraria, porque so varria >=.
   [+] COMPLEMENTARES automaticos: toda coluna de gap / z / media / desvio /
       tendencia / momento / folga vira eixo, com cortes nos quantis do dado
       e NAS DUAS PONTAS (>= e <=) — gap e z sao bidirecionais; varrer so >=
       perde metade do sinal (licao do O/U, job 79).
   [+] GRADE EFETIVA: antes de varrer, cada limiar que nao muda NADA no seu
       arquivo e colapsado (mesma contagem = mesma mascara). E o que garante
       "nao deixa passar nenhuma" sem pagar por eixo morto — a exaustividade
       e por CLASSE DE EQUIVALENCIA, nao por numero de linhas na saida.
   [+] DEDUP por mascara: configs diferentes que selecionam AS MESMAS apostas
       viram UMA linha (a mais frouxa) com a coluna `equiv` contando quantas
       equivalentes existiam. Acaba com o topo entupido de linhas identicas
       (o "sintoma de eixo morto" do job 97).
   [+] TODAS AS ESTATISTICAS por configuracao: apostas, jogos, apostas/jogo,
       G, R, WR, unidades, ROI, u/dia, apostas/dia, odd media, break-even,
       margem sobre o break-even, linha media, DD, lucro/DD, pior jogo,
       pior dia, melhor dia, dias +/-, maior sequencia de reds, z POR JOGO
       (clusterizado — apostas do mesmo jogo nao sao independentes), ROI da
       1a e da 2a metade do periodo, treino/cego, teto de sorte e placebo.
   [+] Aba ROBUSTAS: so quem passa em TODAS as reguas ao mesmo tempo
       (placebo, sorte, z>=2, positivo nas duas metades e no cego).
   [+] `<saida>.tudo.csv` com TUDO que foi guardado, sem corte, pra analise.
   [BLINDAGEM] placebo em streaming (a v2 montava uma matriz que estourava a
       RAM em planilha grande), Ctrl+C salva o parcial, auto-verificacao das
       mascaras reconstruidas, leitura robusta de xlsx/csv/parquet, datas em
       varios formatos, WR em 0-1 ou 0-100.

 COMO A BUSCA E ORGANIZADA (espelha o metodo: grosso -> refino -> combos):
   PASSE 1  nucleo FINO ..... janela x wr_min x wr_max x confrontos x
                              linha_min x linha_max x lado x teto
   PASSE 2  odd .............. nucleo medio x faixas de odd (se a odd vive)
   PASSE 3  duas janelas ..... nucleo grosso x 2a janela (>= e <=; medio no --modo total)
   PASSE 4  complementares ... nucleo grosso x cortes de gap/z/media/etc.
   --modo grosso  = tudo em grade grossa (passada de DIRECAO, rapida)
   --modo completo = o desenho acima (padrao)
   --modo total   = passes 2-4 tambem em grade fina (demora MUITO; a barra
                    do placebo sobe junto — achado aqui prova MENOS)

 USO
   python varredura.py --xlsx planilha.xlsx --out configs.xlsx
   python varredura.py --xlsx p.xlsx --out c.xlsx --modo grosso
   python varredura.py --xlsx p.xlsx --out c.xlsx --cego 5 --prof-extra 2
   (por padrao o teste cego liga sozinho nos ultimos ~30% dos dias)
===============================================================================
"""
import argparse, os, sys, itertools, warnings, time, heapq, math
import unicodedata
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

VERSAO = 'VARREDURA v9'

# ------------------------------------------------------------------ grades --
G_WR     = [0.00, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.87, 0.90, 0.95, 0.97]
G_WRMAX  = [1.01, 0.97, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70,
            0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30]   # 1.01 = sem teto de WR
G_QTD    = [0, 5, 10, 15, 20, 30, 40, 60, 80, 120, 160, 200]
G_QMAX   = [999, 100, 60, 30, 20, 12, 8, 5]   # 999 = sem maximo
G_TETO   = [1, 2, 3, 4, 5, 6, 7, 999]      # 999 = sem teto de apostas/jogo
G_LADOS  = ['ambos', 'zebra', 'favorito']
G_W2_GE  = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90]
G_W2_LE  = [0.99, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40]

GRADES = {
    'grosso': dict(wr=[0.00, 0.60, 0.70, 0.80, 0.90], wrmax=[1.01, 0.80, 0.50],
                   qtd=[0, 10, 20, 60], qmax=[999, 10], nlin=5, nlmax=4,
                   teto=[1, 3, 6, 999], w2ge=[0.60, 0.80], w2le=[0.95, 0.70]),
    'medio':  dict(wr=[0.00, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90],
                   wrmax=[1.01, 0.95, 0.85, 0.70, 0.55, 0.40],
                   qtd=[0, 5, 10, 20, 30, 60, 120], qmax=[999, 30, 10],
                   nlin=8, nlmax=5, teto=[1, 2, 3, 4, 6, 999],
                   w2ge=G_W2_GE, w2le=[0.99, 0.95, 0.85, 0.75, 0.65, 0.55, 0.45]),
    'fino':   dict(wr=G_WR, wrmax=G_WRMAX, qtd=G_QTD, qmax=G_QMAX, nlin=26,
                   nlmax=10, teto=G_TETO, w2ge=G_W2_GE, w2le=G_W2_LE),
}

MIN_CEGO_AP = 20          # minimo de apostas no cego pra reportar roi_cego
SEED = 11


# ------------------------------------------------------- achar colunas ------
def achar(df, *nomes):
    baixo = {str(c).strip().lower(): c for c in df.columns}
    for n in nomes:
        n = n.lower()
        for k, orig in baixo.items():
            if k == n or k.replace(' ', '') == n.replace(' ', ''):
                return orig
    for n in nomes:
        n = n.lower()
        for k, orig in baixo.items():
            if n in k:
                return orig
    return None


def _ler_qualquer(caminho):
    """xlsx/xls, parquet, csv (separador e encoding detectados). Blindado."""
    low = caminho.lower()
    erros = []
    if low.endswith(('.xlsx', '.xls', '.xlsm')):
        for eng in (None, 'openpyxl'):
            try:
                return pd.read_excel(caminho, engine=eng)
            except Exception as e:
                erros.append(f'excel({eng}): {e}')
    elif low.endswith(('.parquet', '.pq')):
        try:
            return pd.read_parquet(caminho)
        except Exception as e:
            erros.append(f'parquet: {e}')
    else:  # csv/txt e desconhecidos
        for enc in ('utf-8', 'latin-1'):
            for kw in (dict(sep=None, engine='python'), dict(sep=';'), dict(sep=',')):
                try:
                    df = pd.read_csv(caminho, encoding=enc, **kw)
                    if df.shape[1] >= 2:
                        return df
                except Exception as e:
                    erros.append(f'csv({enc},{kw.get("sep")}): {e}')
    raise ValueError('nao consegui ler o arquivo. Tentativas: ' + ' | '.join(erros[:4]))


def carregar(caminho, h2h_path=None, paridade_path=None, chips_fonte='todas'):
    if not os.path.exists(caminho):
        raise FileNotFoundError(f'arquivo nao encontrado: {caminho}')
    d = _ler_qualquer(caminho)
    if d is None or len(d) == 0:
        raise ValueError('planilha vazia')
    d.columns = [str(c).strip() for c in d.columns]
    if _eh_tick(d):
        d = apostas_de_ticks(d, h2h_path, paridade_path, chips_fonte)
        d.columns = [str(c).strip() for c in d.columns]

    # --- lucro em unidades (obrigatorio) ---
    cu = achar(d, 'Lucro/Prej.', 'lucro_unidades', 'lucro unidades', 'unidades',
               'lucro', 'pnl', 'profit')
    if cu is None:
        raise ValueError('nao achei coluna de lucro em unidades '
                         '(procurei: Lucro/Prej., lucro_unidades, pnl...)')
    d['_u'] = pd.to_numeric(d[cu], errors='coerce')
    d = d[d['_u'].notna()].copy()
    if len(d) == 0:
        raise ValueError('nenhuma aposta com lucro valido')

    # --- resultado green/red ---
    cr = achar(d, 'Resultado', 'resultado', 'status_resultado')
    if cr is not None:
        rs = d[cr].astype(str).str.strip().str.lower()
        d['_G'] = rs.isin(['green', 'g', 'win', 'won', 'ganhou', 'vitoria',
                           'vitória', '1', 'true']).astype(int)
        if d['_G'].sum() == 0 and (d['_u'] > 0).any():
            d['_G'] = (d['_u'] > 0).astype(int)   # coluna existe mas noutro formato
    else:
        d['_G'] = (d['_u'] > 0).astype(int)

    # --- data/hora ---
    cdata, chora = achar(d, 'Data'), achar(d, 'Hora')
    d['_dt'] = pd.NaT
    if cdata is not None and chora is not None:
        txt = d[cdata].astype(str).str.strip() + ' ' + d[chora].astype(str).str.strip()
        dt = pd.to_datetime(txt, format='%d/%m/%Y %H:%M:%S', errors='coerce')
        if dt.isna().mean() > 0.5:
            dt = pd.to_datetime(txt, dayfirst=True, errors='coerce')
        d['_dt'] = dt
    if d['_dt'].isna().all():
        ct = achar(d, 'apostado_em', 'data_hora', 'ts', 'timestamp', 'datahora', 'data')
        if ct is not None:
            dt = pd.to_datetime(d[ct], errors='coerce', utc=True)
            try:
                d['_dt'] = dt.dt.tz_localize(None)
            except Exception:
                d['_dt'] = pd.to_datetime(d[ct], dayfirst=True, errors='coerce')
    if d['_dt'].isna().all():
        # sem data nenhuma: ordem do arquivo vira o tempo (1 min por linha)
        d['_dt'] = pd.to_datetime('2026-01-01') + pd.to_timedelta(np.arange(len(d)), 'm')
        d.attrs['aviso_data'] = 'SEM coluna de data — usei a ordem do arquivo'
    d = d[d['_dt'].notna()].copy()
    d = d.sort_values('_dt', kind='stable').reset_index(drop=True)

    # --- unidade de JOGO (armadilha do job 88: Confronto e o PAR, nao a partida)
    GAP = 45
    cev = achar(d, 'event_id', 'evento', 'id_evento')
    if cev is not None and d[cev].notna().sum() > len(d) * 0.5:
        d['_jogo'] = d[cev].astype(str)
        d.attrs['origem_jogo'] = f'event_id ({cev})'
    else:
        cpar = achar(d, 'Confronto', 'confronto', 'partida', 'jogo')
        if cpar is None:
            d['_jogo'] = d['_dt'].dt.floor('30min').astype(str)
            d.attrs['origem_jogo'] = 'janela de 30min (sem coluna de confronto)'
        else:
            par = d[cpar].astype(str).values
            t = d['_dt'].values.astype('datetime64[m]').astype(np.int64)
            o = np.lexsort((t, pd.factorize(par)[0]))
            novo = np.empty(len(d), dtype=np.int64)
            k = -1
            par_o, t_o = par[o], t[o]
            for i in range(len(o)):
                if i == 0 or par_o[i] != par_o[i - 1] or (t_o[i] - t_o[i - 1]) > GAP:
                    k += 1
                novo[o[i]] = k
            d['_jogo'] = novo.astype(str)
            d.attrs['origem_jogo'] = f'{cpar} + intervalo de {GAP}min'

    # --- linha COM SINAL (o sinal mora no texto da selecao) ---
    csel = achar(d, 'Tip', 'selecao', 'Seleção', 'Selecao')
    cln = achar(d, 'Linha', 'linha')
    sig = None
    if csel is not None:
        sig = pd.to_numeric(d[csel].astype(str)
                            .str.extract(r'\((-?\d+(?:[.,]\d+)?)\)\s*$')[0]
                            .str.replace(',', '.', regex=False), errors='coerce')
    base = pd.to_numeric(d[cln], errors='coerce') if cln is not None else None
    if sig is not None and sig.notna().any() and base is not None:
        d['_linha'] = sig.fillna(base)
    elif sig is not None and sig.notna().any():
        d['_linha'] = sig
    elif base is not None:
        d['_linha'] = base
    else:
        raise ValueError('nao achei coluna de linha (nem no texto da selecao)')

    clado = achar(d, 'lado')
    if clado is not None:
        lv = d[clado].astype(str).str.lower()
        zeb = np.where(lv.str.contains('zebra|azar|under?dog', regex=True), 1,
                       np.where(lv.str.contains('fav'), 0, -1))
        d['_zebra'] = np.where(zeb >= 0, zeb, (d['_linha'] > 0).astype(int))
    else:
        d['_zebra'] = (d['_linha'] > 0).astype(int)

    # --- odd ---
    codd = achar(d, 'Odd', 'odds', 'odd', 'cotacao', 'preco')
    d['_odd'] = pd.to_numeric(d[codd], errors='coerce') if codd is not None else np.nan

    # --- eixos derivados do Placar Envio: tot_env (soma do placar) e folga ---
    # v10: o eixo antes chamado `momento` virou `tot_env`. O motivo e' serio:
    # no RUNNER `momento` significa ESTAGIO do jogo (1Q/1T/3Q, lido do
    # live_time), coisa completamente diferente. Config garimpada com
    # "momento>=58" nao era reproduzivel no painel — o campo com esse nome la
    # faz outra coisa e as configs davam ZERO aposta. Agora o varredor emite
    # `tot_env` e o runner (v17) tem o filtro totEnvMin/totEnvMax que le
    # exatamente a mesma conta: soma do placar no envio.
    cpe = achar(d, 'Placar Envio', 'placar_envio', 'placar envio')
    d['_tot_env'] = np.nan
    d['_folga'] = np.nan
    d['_dif'] = np.nan
    if cpe is not None:
        pe = d[cpe].astype(str).str.extract(r'(\d+)\s*[-x:]\s*(\d+)')
        ea = pd.to_numeric(pe[0], errors='coerce')
        eb = pd.to_numeric(pe[1], errors='coerce')
        d['_tot_env'] = ea + eb
        nick = (d[csel].astype(str).str.extract(r'\(([^()]+)\)\s*\(')[0]
                .str.upper().str.strip()) if csel is not None else None
        cja = achar(d, 'Jogador A', 'jogador_a')
        if nick is not None and cja is not None and nick.notna().any():
            ja = d[cja].astype(str).str.upper().str.strip()
            deficit = np.where(nick.values == ja.values,
                               (eb - ea).values, (ea - eb).values)
            d['_folga'] = np.abs(d['_linha'].values) - deficit
        d['_dif'] = np.abs((ea - eb).values)

    # --- v8: eixos de TOTAL DE PONTOS (Over/Under) ---------------------------
    # Em mercado de total, 'folga' nao existe (e conta de handicap) e quem
    # separa e o MOVIMENTO DA LINHA dentro do jogo. Derivados aqui:
    #   lin_ini = primeira linha ofertada no jogo (a abertura)
    #   desloc  = linha atual - lin_ini  (negativo = linha caiu = Over barato)
    # 'tot_env' = soma do placar no envio (era chamado 'momento' ate a v9).
    # --- v9: TAXA DE ATROPELO (propriedade do JOGADOR, nao da aposta) --------
    # % dos jogos ANTERIORES daquele jogador que terminaram com 15+ de
    # diferenca. Aposta de folga (azarao com almofada) morre em jogo que
    # desanda — e o efeito bate nos DOIS papeis, por isso e propriedade do
    # jogo e nao do lado. Medida SO com jogos ja encerrados antes da aposta
    # (o proprio jogo NUNCA entra), entao nao ha vazamento de futuro.
    # Valor da aposta = o PIOR dos dois jogadores: basta um pra estragar.
    d['_atropelo'] = np.nan
    try:
        _cA, _cB = achar(d, 'Jogador A'), achar(d, 'Jogador B')
        _cpf = achar(d, 'Placar Final', 'placar final', 'placar_final')
        if _cA and _cB and _cpf and '_dt' in d.columns and '_jogo' in d.columns:
            _pf = d[_cpf].astype(str).str.extract(r'(\d+)\s*[-x:]\s*(\d+)')
            _fa = pd.to_numeric(_pf[0], errors='coerce')
            _fb = pd.to_numeric(_pf[1], errors='coerce')
            _mg = (_fa - _fb).abs()
            _jg = (pd.DataFrame({'j': d['_jogo'].values, 't': d['_dt'].values,
                                 'A': d[_cA].astype(str).str.upper().str.strip().values,
                                 'B': d[_cB].astype(str).str.upper().str.strip().values,
                                 'm': _mg.values})
                   .dropna(subset=['m']).sort_values('t', kind='stable')
                   .drop_duplicates('j'))
            MIN_JG = 6            # abaixo disso usa a media corrente da liga
            _n = {}
            _b = {}
            _tot_n = _tot_b = 0
            _taxa = {}
            for _j, _A, _B, _m in zip(_jg.j.values, _jg.A.values,
                                      _jg.B.values, _jg.m.values):
                _lig = (_tot_b / _tot_n) if _tot_n >= 30 else 0.11
                _r = []
                for _p in (_A, _B):
                    _np_ = _n.get(_p, 0)
                    _r.append((_b.get(_p, 0) / _np_) if _np_ >= MIN_JG else _lig)
                _taxa[_j] = max(_r) * 100.0        # o pior dos dois
                _ate = 1 if _m >= 15 else 0        # so DEPOIS, nunca no proprio
                for _p in (_A, _B):
                    _n[_p] = _n.get(_p, 0) + 1
                    _b[_p] = _b.get(_p, 0) + _ate
                _tot_n += 1
                _tot_b += _ate
            d['_atropelo'] = d['_jogo'].map(_taxa).astype(float)
    except Exception:
        pass

    d['_lin_ini'] = np.nan
    d['_desloc'] = np.nan
    try:
        cconf = achar(d, 'Confronto', 'confronto')
        cev = achar(d, 'event_id', 'evento')
        if (cev is not None or cconf is not None) and '_dt' in d.columns:
            chave = (d[cev].astype(str) if cev is not None
                     else (d[cconf].astype(str) + '|'
                           + d['_dt'].dt.floor('4h').astype(str)))
            ordem = d['_dt'].values.argsort(kind='stable')
            k_ord = chave.values[ordem]
            l_ord = d['_linha'].values[ordem]
            prim = {}
            for k, lv in zip(k_ord, l_ord):
                if k not in prim and np.isfinite(lv):
                    prim[k] = lv
            d['_lin_ini'] = chave.map(prim).astype(float)
            d['_desloc'] = d['_linha'].values - d['_lin_ini'].values
    except Exception:
        pass
    return d



# ================== v6: FONTE TICK (parquet bruto do coletor) ================
_JANELAS_TICK = [('Últ. 10', 10), ('Últ. 20', 20), ('Últ. 30', 30),
                 ('Últ. 50', 50), ('Todas', None)]
_MARGEM_VIVO_MIN = 15      # runner: jogo de tick so vira historico 15min apos o fim
_DEDUP_JANELA_MIN = 40     # runner: mesmo par+placar a <40min entre fontes = mesmo jogo


def _eh_tick(d):
    """Arquivo de TICKS (coletor) em vez de planilha de apostas do painel."""
    low = {str(c).strip().lower() for c in d.columns}
    tem_tick = {'selecao', 'event_id'} <= low and ('odds' in low or 'odd' in low) \
        and ('ft_home' in low or 'mercado' in low)
    tem_aposta = any(k in low for k in ('lucro/prej.', 'lucro_unidades', 'resultado'))
    return tem_tick and not tem_aposta


def _naive(s):
    # v6.1: normaliza fuso PRESERVANDO a parede local. Colunas de texto podem
    # misturar formatos (dump sem fuso + ticks timestamptz com -03:00): o
    # to_datetime de mistura vira NaT em silencio — entao o offset e removido
    # como TEXTO antes do parse.
    if not pd.api.types.is_datetime64_any_dtype(s):
        s = s.astype(str).str.replace(r'\s*(Z|[+-]\d{2}(:?\d{2})?)\s*$', '',
                                      regex=True)
        # formatos MISTOS na mesma coluna (com/sem microssegundos): o pandas
        # infere o formato da 1a linha e converte o resto pra NaT em silencio.
        try:
            s = pd.to_datetime(s, errors='coerce', format='mixed')
        except (TypeError, ValueError):
            s = pd.to_datetime(s, errors='coerce')
        try:
            return s.dt.tz_localize(None)
        except (TypeError, AttributeError):
            return s
    s = pd.to_datetime(s, errors='coerce')
    try:
        return s.dt.tz_localize(None)
    except (TypeError, AttributeError):
        return s


def _alinhar_fuso(hh_par, hh_pl, hh_ts, ev_par, ev_pl, ev_fim, rotulo):
    """v6.3 — ANTIVAZAMENTO. O parquet (BetsAPI) grava ts em UTC naive; um
    \\copy de coluna timestamptz do Postgres sai em hora LOCAL. Sem alinhar,
    todo jogo do dump aparece ~3h ANTES do que foi — e o jogo ATUAL entra no
    proprio historico, fabricando chip 1.00 e WR 100% (vazamento de futuro).
    Aqui o desvio e medido nos jogos que existem nas DUAS fontes (par+placar
    final) e devolvido arredondado em 15min. Sem sobreposicao suficiente,
    devolve zero (dump antigo nao concorre com o periodo, entao nao vaza)."""
    m = pd.DataFrame({'k': hh_par + '|' + hh_pl, 'ts': hh_ts}).merge(
        pd.DataFrame({'k': ev_par + '|' + ev_pl, 'fim': ev_fim}),
        on='k', how='inner')
    if len(m) < 30:
        return pd.Timedelta(0), 0
    dt = (m['fim'] - m['ts']).dt.total_seconds() / 60.0
    dt = dt[dt.abs() <= 12 * 60]                     # so casamentos plausiveis
    if len(dt) < 30:
        return pd.Timedelta(0), 0
    passo = 15.0
    desvio = float(np.round(dt.median() / passo) * passo)
    if abs(desvio) < passo:
        return pd.Timedelta(0), len(dt)
    print(f'  ALINHAMENTO DE FUSO em {rotulo}: dump esta {desvio / 60:+.2f}h '
          f'fora do parquet ({len(dt):,} jogos em comum) — corrigido. '
          'SEM isso, o jogo entra no proprio historico (chip 1.00 / WR 100%).')
    return pd.Timedelta(minutes=desvio), len(dt)


def apostas_de_ticks(t, h2h_path=None, paridade_path=None, chips_fonte='todas'):
    """Replica o runner sobre o parquet bruto: escancarado HC zebra, liquidado
    no placar FINAL, com chips (COBERTURA da linha por janela) reconstruidos
    do historico. BLINDADO e barulhento: imprime cada etapa e os avisos."""
    print('=' * 78)
    print(' FONTE TICK — reconstruindo o escancarado a partir do parquet bruto')
    print('=' * 78)
    t = t.copy()
    t.columns = [str(c).strip() for c in t.columns]
    cts = achar(t, 'ts', 'timestamp', 'data_hora')
    if cts is None:
        raise ValueError('modo tick: nao achei a coluna ts')
    t['_ts'] = _naive(t[cts])
    t = t[t['_ts'].notna()].sort_values('_ts', kind='stable')

    # ---- jogos finalizados (a verdade) ----
    for c in ('ft_home', 'ft_away', 'score_home', 'score_away'):
        if c in t.columns:
            t[c] = pd.to_numeric(t[c], errors='coerce')
    ev = t.groupby('event_id').agg(
        ja=('jogador_a', 'last'), jb=('jogador_b', 'last'),
        ta=('time_a', 'last') if 'time_a' in t.columns else ('jogador_a', 'last'),
        tb=('time_b', 'last') if 'time_b' in t.columns else ('jogador_b', 'last'),
        liga=('liga', 'last') if 'liga' in t.columns else ('event_id', 'last'),
        t1=('_ts', 'max'),
        ft_h=('ft_home', 'last'), ft_a=('ft_away', 'last')).reset_index()
    ev = ev.dropna(subset=['ft_h', 'ft_a'])
    ev['ja_u'] = ev.ja.astype(str).str.upper().str.strip()
    ev['jb_u'] = ev.jb.astype(str).str.upper().str.strip()
    ev['par'] = np.where(ev.ja_u < ev.jb_u, ev.ja_u + '|' + ev.jb_u,
                         ev.jb_u + '|' + ev.ja_u)
    print(f'jogos finalizados no arquivo: {len(ev):,}')

    # ---- historico p/ chips: proprio arquivo + dump do banco (opcional) ----
    ht = pd.DataFrame({'par': ev.par, 'ja_u': ev.ja_u,
                       'pa': ev.ft_h, 'pb': ev.ft_a,
                       'eleg': ev.t1 + pd.Timedelta(minutes=_MARGEM_VIVO_MIN),
                       'fonte': 'tick'})
    if h2h_path:
        # v6.1: aceita VARIOS arquivos separados por virgula (ex.: o dump do
        # h2h_historico + o dump consolidado da tabela ticks do banco) e
        # deduplica entre eles por par+placar+ts~40min, como o runner.
        _ev_pl = (np.minimum(ev.ft_h, ev.ft_a).astype(int).astype(str) + '-'
                  + np.maximum(ev.ft_h, ev.ft_a).astype(int).astype(str))
        _partes = []
        for _pth in str(h2h_path).split(','):
            _pth = _pth.strip()
            if not _pth:
                continue
            _pd = _ler_qualquer(_pth)
            _pd.columns = [str(c).strip() for c in _pd.columns]
            _cja, _cjb = achar(_pd, 'jogador_a'), achar(_pd, 'jogador_b')
            _csh, _csa = achar(_pd, 'score_home'), achar(_pd, 'score_away')
            _cth = achar(_pd, 'ts', 'data_hora', 'fixture_date', 'data')
            if None in (_cja, _cjb, _csh, _csa, _cth):
                raise ValueError(f'--h2h {os.path.basename(_pth)}: preciso de '
                                 'jogador_a/b, score_home/away e ts')
            _au = _pd[_cja].astype(str).str.upper().str.strip()
            _bu = _pd[_cjb].astype(str).str.upper().str.strip()
            _p = pd.DataFrame({
                'par': np.where(_au < _bu, _au + '|' + _bu, _bu + '|' + _au),
                'ja_u': _au,
                'pa': pd.to_numeric(_pd[_csh], errors='coerce'),
                'pb': pd.to_numeric(_pd[_csa], errors='coerce'),
                'eleg': _naive(_pd[_cth]), 'fonte': 'hist'}).dropna()
            _pl = (np.minimum(_p.pa, _p.pb).astype(int).astype(str) + '-'
                   + np.maximum(_p.pa, _p.pb).astype(int).astype(str))
            _dts, _n = _alinhar_fuso(_p.par, _pl, _p.eleg, ev.par, _ev_pl,
                                     ev.t1, os.path.basename(_pth))
            if _dts:
                _p['eleg'] = _p['eleg'] + _dts
            _partes.append(_p)
            print(f'  --h2h: {os.path.basename(_pth)} -> {len(_p):,} jogos'
                  + (f' (fuso corrigido {_dts.total_seconds() / 3600:+.2f}h)'
                     if _dts else ''))
        hh = pd.concat(_partes, ignore_index=True)
        # dedup interno entre as fontes hist (mesmo jogo no h2h_historico E
        # no dump de ticks): par + placar + ts arredondado a 40min
        hh['_k'] = (hh.par + '|' + hh.pa.astype(int).astype(str) + '-'
                    + hh.pb.astype(int).astype(str) + '|'
                    + hh.eleg.dt.round('40min').astype(str))
        _antes = len(hh)
        hh = hh.drop_duplicates('_k').drop(columns='_k')
        if _antes - len(hh):
            print(f'  dedup interno entre fontes hist: {_antes - len(hh):,} removidos')
        # dedup runner (robusto a microssegundos): junta as fontes, ordena por
        # par+placar+tempo e remove quem repete o MESMO par+placar a menos de
        # 55min do anterior — o hist tem prioridade (vem primeiro no empate).
        _n_hh, _n_ht = len(hh), len(ht)
        hist = pd.concat([hh, ht], ignore_index=True)
        hist['_pl'] = np.minimum(hist.pa, hist.pb).astype(int).astype(str) + '-' \
            + np.maximum(hist.pa, hist.pb).astype(int).astype(str)
        hist['_pri'] = (hist.fonte != 'hist').astype(int)   # hist=0 ganha
        hist = hist.sort_values(['par', '_pl', 'eleg', '_pri'], kind='stable')
        _mesmo = (hist.par.values[1:] == hist.par.values[:-1]) \
            & (hist['_pl'].values[1:] == hist['_pl'].values[:-1])
        _dt = (hist.eleg.values[1:] - hist.eleg.values[:-1]) \
            / np.timedelta64(1, 'm')
        dup = np.concatenate([[False], _mesmo & (_dt <= _DEDUP_JANELA_MIN
                                                 + _MARGEM_VIVO_MIN)])
        hist = hist[~dup].drop(columns=['_pl', '_pri'])
        if chips_fonte == 'so-h2h':
            # v6.2 ESPELHO DO RUNNER: o backtest monta o H2H apenas do
            # banco (o lado ticks dele filtra por casa, e casas sem coletor
            # proprio nao existem la). Pra PARIDADE de chip, o historico
            # aqui usa SO as fontes --h2h — jogos do proprio arquivo ficam
            # de fora, como ficam pro runner.
            hist = hist[hist.fonte == 'hist']
            print(f'  --chips so-h2h (espelho do runner): historico = apenas '
                  f'as fontes --h2h ({len(hist):,} jogos)')
        print(f'historico p/ chips: {_n_hh:,} das fontes --h2h + {_n_ht:,} do '
              f'arquivo ({int(dup.sum()):,} dedup entre fontes)')
    else:
        hist = ht
        print('AVISO ............... SEM --h2h: chips reconstruidos SO com os jogos'
              ' do proprio arquivo — "Todas"/"Qtd" ficam TRUNCADOS ao periodo. Pra'
              ' paridade com o painel, gere o dump carimbado (inserted_at <= as_of).')
    hist = hist.sort_values('eleg', kind='stable')
    HIST = {p: (g.eleg.values.astype('datetime64[ns]'),
                g.ja_u.values, g.pa.values.astype(float), g.pb.values.astype(float))
            for p, g in hist.groupby('par')}

    # ---- escancarado: HC zebra, 1 aposta por (evento, lado, linha) ----------
    cm = achar(t, 'mercado')
    h = t[t[cm].astype(str).str.contains('Handicap', case=False, na=False)].copy() \
        if cm else t.copy()
    e2 = h['selecao'].astype(str).str.extract(
        r'\(([^()]+)\)\s*\(([+-]?\d+(?:[.,]\d+)?)\)\s*$')
    h['nick'] = e2[0].str.upper().str.strip()
    h['lin'] = pd.to_numeric(e2[1].str.replace(',', '.', regex=False), errors='coerce')
    codd = achar(h, 'odds', 'odd')
    h['_odd'] = pd.to_numeric(h[codd], errors='coerce')
    h = h[(h.lin > 0) & (h['_odd'] > 1.0)
          & h.nick.notna() & h.score_home.notna() & h.score_away.notna()]
    h = h.merge(ev[['event_id', 'ja_u', 'jb_u', 'ta', 'tb', 'par',
                    'ft_h', 'ft_a']], on='event_id', how='inner')
    if 'liga' not in h.columns:
        h['liga'] = ''
    h = h.sort_values('_ts', kind='stable')
    ap = h.drop_duplicates(subset=['event_id', 'nick', 'lin'], keep='first').copy()
    print(f'apostas escancaradas (zebra, 1 por jogo+lado+linha): {len(ap):,}')

    # ---- liquidacao no FT (regra do runner) ---------------------------------
    e_A = ap.nick.values == ap.ja_u.values
    pl = np.where(e_A, ap.ft_h, ap.ft_a)
    po = np.where(e_A, ap.ft_a, ap.ft_h)
    marg = pl + ap.lin.values - po
    push = marg == 0
    ap = ap[~push].copy()
    if push.sum():
        print(f'push (linha inteira, empate exato): {int(push.sum())} excluidas')
    e_A = ap.nick.values == ap.ja_u.values
    pl = np.where(e_A, ap.ft_h, ap.ft_a)
    po = np.where(e_A, ap.ft_a, ap.ft_h)
    ap['_green'] = (pl + ap.lin.values) > po
    ap['_lucro'] = np.where(ap['_green'], ap['_odd'] - 1.0, -1.0).round(3)

    # ---- chips: COBERTURA da linha por janela (semantica da escadinha) ------
    chips = {nome: np.full(len(ap), np.nan) for nome, _ in _JANELAS_TICK}
    qtds = {nome: np.zeros(len(ap), int) for nome, _ in _JANELAS_TICK}
    pos = {c: i for i, c in enumerate(ap.columns)}
    sem_hist = 0
    linhas = list(zip(ap.par.values, ap.nick.values, ap.lin.values,
                      ap['_ts'].values.astype('datetime64[ns]')))
    for i, (par, nick, lin, ts_) in enumerate(linhas):
        Hp = HIST.get(par)
        if Hp is None:
            sem_hist += 1
            continue
        eleg, ja_u, pa, pb = Hp
        k = int(np.searchsorted(eleg, ts_, side='left'))
        if k == 0:
            sem_hist += 1
            continue
        me_a = ja_u[:k] == nick
        pn = np.where(me_a, pa[:k], pb[:k])
        pv = np.where(me_a, pb[:k], pa[:k])
        cobre = (pn + lin) > pv
        for nome, Nw in _JANELAS_TICK:
            fat = cobre if Nw is None else cobre[-Nw:]
            qtds[nome][i] = fat.size
            if fat.size:
                chips[nome][i] = fat.mean()
    if sem_hist:
        print(f'apostas sem historico no ts (chips vazios -> caem no filtro): {sem_hist:,}')

    # ---- monta no FORMATO PLANILHA (o resto do varredor engole igual) -------
    lado_time = np.where(e_A, ap.ta.astype(str), ap.tb.astype(str))
    sinal = np.where(ap.lin.values >= 0, '+', '')
    out = pd.DataFrame({
        'Campeonato': ap['liga'].astype(str).values if 'liga' in ap.columns else '',
        'Confronto': ap.ja_u.values + ' x ' + ap.jb_u.values,
        'Jogador A': ap.ja_u.values, 'Jogador B': ap.jb_u.values,
        'Data': pd.to_datetime(ap['_ts']).dt.strftime('%d/%m/%Y'),
        'Hora': pd.to_datetime(ap['_ts']).dt.strftime('%H:%M:%S'),
        'Mercado': 'Handicap',
        'Tip': [f'{tm} ({nk}) ({sg}{ln:g})' for tm, nk, sg, ln in
                zip(lado_time, ap.nick.values, sinal, ap.lin.values)],
        'Linha': ap.lin.values, 'Odd': ap['_odd'].round(3).values,
        'Placar Envio': ap.score_home.astype(int).astype(str) + '-'
                        + ap.score_away.astype(int).astype(str),
        'Placar Final': ap.ft_h.astype(int).astype(str) + '-'
                        + ap.ft_a.astype(int).astype(str),
        'Resultado': np.where(ap['_green'], 'Green', 'Red'),
        'Lucro/Prej.': ap['_lucro'].values,
        'event_id': ap.event_id.values,
    })
    for nome, _ in _JANELAS_TICK:
        out[nome] = np.round(chips[nome], 6)
        out['Qtd ' + nome if nome != 'Todas' else 'Qtd Todas'] = qtds[nome]
    gr = int(out['Resultado'].eq('Green').sum())
    print(f'RESUMO tick->aposta: {len(out):,} apostas | {gr}G-{len(out) - gr}R | '
          f'WR {gr / max(len(out), 1) * 100:.1f}% | {out["Lucro/Prej."].sum():+.1f}u | '
          f'Qtd Todas mediana {int(np.median(qtds["Todas"]))}')

    # ---- PARIDADE contra o export real do painel ----------------------------
    if paridade_path:
        try:
            px = _ler_qualquer(paridade_path)
            px.columns = [str(c).strip() for c in px.columns]
            pl_ = px['Tip'].astype(str).str.extract(r'\(([+-]?\d+(?:[.,]\d+)?)\)\s*$')[0]
            px['_lin'] = pd.to_numeric(pl_.str.replace(',', '.', regex=False)
                                       .str.replace('+', '', regex=False), errors='coerce')
            _tsp = pd.to_datetime(px['Data'].astype(str).str.strip() + ' '
                                  + px['Hora'].astype(str).str.strip(),
                                  dayfirst=True, errors='coerce')
            px['_k'] = (px['Confronto'].astype(str).str.upper().str.replace(' ', '')
                        + '|' + _tsp.astype(str) + '|' + px['_lin'].astype(str))
            _tsm = pd.to_datetime(out['Data'] + ' ' + out['Hora'], dayfirst=True)
            ok_ = (out['Confronto'].str.upper().str.replace(' ', '') + '|' + _tsm.astype(str)
                   + '|' + out['Linha'].astype(float).astype(str))
            sp, sm = set(px['_k']), set(ok_)
            inter = len(sp & sm)
            print('-' * 78)
            print(f'PARIDADE vs {os.path.basename(paridade_path)}: '
                  f'{inter:,} em comum | so no painel {len(sp) - inter:,} | '
                  f'so aqui {len(sm) - inter:,} '
                  f'({inter / max(len(sp), 1) * 100:.1f}% do painel reproduzido)')
            if inter / max(len(sp), 1) < 0.9:
                print('ALERTA .............. paridade < 90%: confira --h2h (chips'
                      ' truncados?), periodo e a versao do runner (v14).')
            # v6.1: CHIP-CHECK — compara os chips RECONSTRUIDOS com os chips
            # que o runner anotou no export, nas apostas casadas. E o carimbo
            # objetivo do "chip perfeitamente funcionando": com --h2h completo
            # os desvios têm que ir a ~zero; sem, ele mede o buraco.
            comuns = [c for c in ('Todas', 'Últ. 10', 'Últ. 20', 'Últ. 30',
                                  'Últ. 50', 'Qtd Todas')
                      if c in px.columns and c in out.columns]
            if comuns:
                mm = (pd.DataFrame({'_k': ok_}).join(out[comuns])
                      .merge(px[['_k'] + comuns], on='_k',
                             suffixes=('_meu', '_painel')))
                print('CHIP-CHECK (apostas casadas: '
                      f'{len(mm):,}) — reconstruido vs runner:')
                for c in comuns:
                    a = pd.to_numeric(mm[c + '_meu'], errors='coerce')
                    b = pd.to_numeric(mm[c + '_painel'], errors='coerce')
                    par = a.notna() & b.notna()
                    if not par.any():
                        continue
                    dif = (a[par] - b[par]).abs()
                    tol = 0.5 if c.startswith('Qtd') else 0.005
                    print(f'  {c:<10} identicos {float((dif <= tol).mean()) * 100:5.1f}% | '
                          f'desvio medio {float(dif.mean()):.4f} | max {float(dif.max()):.3f}')
                qm = 'Qtd Todas'
                if qm in comuns:
                    a = pd.to_numeric(mm[qm + '_meu'], errors='coerce')
                    b = pd.to_numeric(mm[qm + '_painel'], errors='coerce')
                    if b.notna().any():
                        cob = float((a.fillna(0) / b.replace(0, np.nan)).clip(upper=1).mean())
                        print(f'  historico coberto: {cob * 100:.1f}% do que o banco tem '
                              '(<95% = falta dump ou dump incompleto)')
        except Exception as e:
            print(f'paridade: nao consegui comparar ({e})')
    print('=' * 78)
    return out


# ------------------------------------------------- deteccao de eixos --------
_PROIBIDAS = ('id', 'stake', 'odd', 'linha', 'placar', 'data', 'hora', 'lucro',
              'pnl', 'resultado', 'status', 'selec', 'tip', 'confronto',
              'jogador', 'evento', 'mercado', 'lado', 'unnamed')
_COMP_PISTAS = ('gap', 'desvio', 'tend', 'med', 'méd', 'tot_env', 'momento', 'ritmo',
                'streak', 'seq', 'folga', 'deficit', 'déficit', 'dif', 'pace',
                'pontos', 'total')


def detectar_eixos(d):
    """Separa as colunas em: WR (winrate por janela, 0-1), QTD (confrontos),
    COMP (complementares numericos). Normaliza WR em 0-100 pra 0-1.
    Armadilha do job 97: 'Qtd Ult.10' e capada na janela — o eixo de
    confrontos e a coluna de qtd de MAIOR maximo (historico total)."""
    wr, qtd, comp = {}, {}, {}
    for c in d.columns:
        cs = str(c)
        if cs.startswith('_'):
            continue
        low = cs.lower()
        s = pd.to_numeric(d[c], errors='coerce')
        if s.notna().sum() < len(d) * 0.5 or s.nunique(dropna=True) < 3:
            continue
        if low.startswith('qtd') or 'confronto' in low or low.startswith('n_'):
            qtd[cs] = s.fillna(0).values.astype(np.float64)
            continue
        pista_wr = ('winrate' in low or low.startswith('wr') or 'últ' in low
                    or 'ult' in low or low == 'todas' or 'cobertura' in low
                    or 'pct' in low or low.endswith('%'))
        if pista_wr:
            mx = float(s.max())
            if s.min() >= 0 and mx <= 1.0001:
                wr[cs] = s.fillna(0).values.astype(np.float64)
                continue
            if s.min() >= 0 and 1.5 < mx <= 100.0001:   # veio em %
                wr[cs] = (s.fillna(0).values / 100.0).astype(np.float64)
                continue
        pista_comp = (any(p in low for p in _COMP_PISTAS)
                      or low.startswith('z') or 'zscore' in low or 'z_' in low)
        if pista_comp and not any(p in low for p in _PROIBIDAS):
            comp[cs] = s.values.astype(np.float64)
    # derivados viram complementares se existirem
    for nome, col in (('tot_env', '_tot_env'), ('folga', '_folga'),
                      ('desloc', '_desloc'), ('lin_ini', '_lin_ini'),
                      ('dif', '_dif'), ('atropelo', '_atropelo')):
        v = d[col].values
        if np.isfinite(v).sum() > len(d) * 0.5 and len(np.unique(v[np.isfinite(v)])) >= 4:
            comp[nome] = v.astype(np.float64)
    return wr, qtd, comp


def coluna_historico(qtd):
    if not qtd:
        return None
    for nome in qtd:
        if 'todas' in str(nome).lower() or 'total' in str(nome).lower():
            return nome
    return max(qtd, key=lambda k: float(np.nanmax(qtd[k])))


# ----------------------------------------------- grade efetiva (colapso) ----
def grade_efetiva(valores, grade, op):
    """Mantem so os limiares que produzem CONTAGENS distintas no dado.
    op='ge' devolve em ordem crescente (frouxo->apertado);
    op='le' devolve em ordem DEcrescente (frouxo->apertado).
    Limiar de mascara vazia e descartado."""
    v = np.asarray(valores, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return []
    sv = np.sort(v)
    ordem = sorted(set(grade)) if op == 'ge' else sorted(set(grade), reverse=True)
    saida, vistos = [], set()
    for t in ordem:
        if op == 'ge':
            c = sv.size - int(np.searchsorted(sv, t, 'left'))
        else:
            c = int(np.searchsorted(sv, t, 'right'))
        if c > 0 and c not in vistos:
            vistos.add(c)
            saida.append(float(t))
    return saida


def candidatos_linha(alin, n_max):
    """Grade de linha tirada do PROPRIO arquivo (quantis, arredondados em .5)."""
    v = alin[np.isfinite(alin)]
    if v.size == 0:
        return [0.0]
    unicos = np.unique(np.round(v * 2) / 2)
    if unicos.size <= n_max:
        cand = unicos
    else:
        qs = np.quantile(v, np.linspace(0, 1, n_max))
        cand = np.unique(np.round(qs * 2) / 2)
    return [float(x) for x in cand]


def eixo_odd_vivo(odd):
    v = odd[np.isfinite(odd)]
    if v.size < max(50, 0.3 * odd.size):
        return False, 'poucas odds validas'
    if np.unique(np.round(v, 2)).size < 4:
        return False, f'quase tudo no mesmo preco ({np.round(np.median(v),2)})'
    p10, p90 = np.quantile(v, [0.10, 0.90])
    if (p90 - p10) < 0.05:
        return False, f'variacao minima (p10 {p10:.2f} ~ p90 {p90:.2f})'
    return True, f'p10 {p10:.2f} | mediana {np.median(v):.2f} | p90 {p90:.2f}'


def pares_odd(odd):
    v = odd[np.isfinite(odd)]
    omins = grade_efetiva(v, np.round(np.quantile(v, [0.0, 0.25, 0.50, 0.75]), 2), 'ge')
    omaxs = grade_efetiva(v, list(np.round(np.quantile(v, [0.40, 0.60, 0.80]), 2)) + [999.0], 'le')
    pares = [(a, b) for a in omins for b in omaxs if b > a + 1e-9]
    return pares


def cortes_complementar(nome, vals):
    """Cortes nas DUAS pontas + banda do meio, nos quantis do dado."""
    v = vals[np.isfinite(vals)]
    if v.size == 0:
        return []
    q = np.nanquantile(v, [0.10, 0.25, 0.50, 0.75, 0.90])

    def fmt(x):
        return f'{x:.2f}'.rstrip('0').rstrip('.')
    cand = [
        (f'{nome}>={fmt(q[2])}', vals >= q[2]),
        (f'{nome}>={fmt(q[3])}', vals >= q[3]),
        (f'{nome}>={fmt(q[4])}', vals >= q[4]),
        (f'{nome}<={fmt(q[2])}', vals <= q[2]),
        (f'{nome}<={fmt(q[1])}', vals <= q[1]),
        (f'{nome}<={fmt(q[0])}', vals <= q[0]),
        (f'{nome} {fmt(q[1])}~{fmt(q[3])}', (vals >= q[1]) & (vals <= q[3])),
    ]
    saida, vistos = [], set()
    for rot, m in cand:
        c = int(m.sum())
        if c > 0 and c not in vistos:
            vistos.add(c)
            saida.append((rot, m))
    return saida


# --------------------------------------------------------- mecanica ---------
def degrau_no_indice(idx, jid):
    """Posicao (0-based) de cada aposta DENTRO do seu jogo, contando so as
    apostas selecionadas, em ordem temporal. Recalculado por mascara — a
    ordem dos degraus muda quando o filtro muda (armadilha do job 88)."""
    gs = jid[idx]
    o = np.argsort(gs, kind='stable')          # estavel = preserva o tempo
    gso = gs[o]
    n = gso.size
    ini = np.empty(n, dtype=bool)
    ini[0] = True
    ini[1:] = gso[1:] != gso[:-1]
    starts = np.where(ini, np.arange(n), 0)
    pos = np.arange(n) - np.maximum.accumulate(starts)
    deg = np.empty(n, dtype=np.int64)
    deg[o] = pos
    return deg


def max_seq_reds(g_keep):
    """Maior sequencia de apostas red seguidas (ordem temporal)."""
    if g_keep.size == 0:
        return 0
    b = (g_keep == 0).astype(np.int8)
    if b.sum() == 0:
        return 0
    y = np.concatenate(([0], b, [0]))
    dd = np.diff(y)
    starts = np.flatnonzero(dd == 1)
    ends = np.flatnonzero(dd == -1)
    return int((ends - starts).max())


# ------------------------------------------------------------------ main ----
# ---------------------------------------------------------------------------
# API DE BIBLIOTECA (v10) — pro varredor rodar como JOB do sistema.
# Deliberadamente FINA: `varrer()` so monta o argv e chama main(), entao o
# caminho executado e' EXATAMENTE o mesmo da linha de comando ja validada
# (mesmos defaults, mesmas validacoes, mesma saida). Zero refatoracao do miolo
# = zero risco de a versao do painel divergir da que passou no T1/T2.
# ---------------------------------------------------------------------------
PROGRESSO_CB = None          # callable(dict) -> None. Nunca deixar excecao subir.


def _emitir_progresso(**dados):
    """Chama o callback do job, se houver. BLINDADO: se o callback falhar
    (banco fora do ar, por ex.), a varredura NAO para por causa disso."""
    cb = PROGRESSO_CB
    if cb is None:
        return
    try:
        cb(dados)
    except Exception:
        pass


def varrer(argumentos, on_progress=None):
    """Roda a varredura como se fosse a CLI. `argumentos` e' a lista de flags
    (ex.: ['--xlsx', 'a.xlsx', '--out', 'b.xlsx', '--modo', 'total']).

    Devolve o codigo de saida (0 = ok). Restaura sys.argv e o callback no fim,
    de modo que chamar duas vezes no mesmo processo e' seguro.
    """
    global PROGRESSO_CB
    argv_antigo = sys.argv
    cb_antigo = PROGRESSO_CB
    PROGRESSO_CB = on_progress
    sys.argv = ['varredura.py'] + [str(x) for x in argumentos]
    try:
        main()
        return 0
    except SystemExit as e:                 # --plano e erros de flag saem assim
        return int(e.code or 0)
    finally:
        sys.argv = argv_antigo
        PROGRESSO_CB = cb_antigo


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=VERSAO)
    ap.add_argument('--xlsx', required=True, help='planilha do backtest (xlsx/csv/parquet)')
    ap.add_argument('--out', default='configs.xlsx')
    ap.add_argument('--modo', choices=['grosso', 'completo', 'total'], default='completo')
    ap.add_argument('--min-jogos', type=int, default=30)
    ap.add_argument('--min-apostas', type=int, default=40)
    ap.add_argument('--topo', type=int, default=500, help='linhas por aba de ranking')
    ap.add_argument('--guardar', type=int, default=30000, help='melhores guardadas por criterio')
    ap.add_argument('--cego', type=int, default=-1,
                    help='dias finais como teste cego (-1=auto ~30%% dos dias, 0=off)')
    ap.add_argument('--placebo', type=int, default=12,
                    help='embaralhamentos por jogo pra barra do placebo (0=off)')
    ap.add_argument('--prof-extra', type=int, choices=[1, 2], default=1,
                    help='1=um complementar por vez, 2=tambem pares de complementares')
    ap.add_argument('--janelas', default='', help='limita as janelas de WR (ex: "ult.10,todas")')
    ap.add_argument('--min-ap-sniper', type=int, default=150,
                    help='piso de apostas pra caca de WR (heap WR + abas SNIPERS/FRONTEIRA_WR)')
    ap.add_argument('--chips', choices=['todas', 'so-h2h'], default='todas',
                    help="fonte do historico dos chips: 'todas' = --h2h + jogos do "
                         "proprio arquivo (verdade maxima); 'so-h2h' = ESPELHO DO "
                         "RUNNER (so os dumps do banco — use com --paridade)")
    ap.add_argument('--ate', default=None,
                    help='PRE-COMPROMISSO: so enxerga apostas ATE esta data '
                         '(AAAA-MM-DD). O que vier depois fica FORA da busca — '
                         'e vira holdout de verdade, que voce testa depois com '
                         'repontua.py --de. Sem isso o "cego" do varredor e '
                         'apenas os ultimos dias de um dado que ele JA VIU '
                         'inteiro ao escolher os cortes')
    ap.add_argument('--de', default=None,
                    help='so enxerga apostas A PARTIR desta data (AAAA-MM-DD)')
    ap.add_argument('--plano', action='store_true',
                    help='SO mostra o contrato da rodada (grades, janelas, '
                         'eixos, plano) e SAI, sem varrer. Use SEMPRE antes '
                         'de uma rodada longa: um filtro que nao casou custa '
                         '10 segundos aqui e horas depois')
    ap.add_argument('--nlin', type=int, default=None,
                    help='quantos PISOS de linha testar (linha >= X)')
    ap.add_argument('--nlmax', type=int, default=None,
                    help='quantos TETOS de linha testar (linha <= X). Em '
                         'mercado de TOTAL DE PONTOS o corte que decide e o '
                         'TETO — use >= ao --nlin. O padrao do modo da menos '
                         'tetos que pisos, porque a grade nasceu do handicap')
    ap.add_argument('--h2h', default=None,
                    help='dump do h2h_historico (csv/parquet) pra chips completos '
                         'no modo tick — gere com inserted_at <= h2h_as_of (v14)')
    ap.add_argument('--paridade', default=None,
                    help='export do painel (xlsx) pra conferir o escancarado '
                         'reconstruido no modo tick, aposta a aposta')
    ap.add_argument('--rec-janelas', default='3,7',
                    help='janelas da leitura de recencia, em dias ancorados no ULTIMO dia '
                         '(ex: "3,7" = ultimos 3 e ultimos 7 dias; vazio desliga)')
    ap.add_argument('--sem-odd', action='store_true')
    ap.add_argument('--sem-duplo', action='store_true')
    ap.add_argument('--sem-extras', action='store_true')
    ap.add_argument('--estrategia', '-e', action='append', metavar='SPEC',
                    help='avalia UMA config especifica e sai (repetivel). '
                         'SPEC = pares chave=valor separados por ";". Chaves: '
                         'janela, wr_min, wr_max, janela2, op2(ge/le), wr2, '
                         'conf_min, conf_max, linha_min, linha_max, odd_min, '
                         'odd_max, lado, folga_min, folga_max, tot_env_min, '
                         'tot_env_max, extra(nome>=x), teto. '
                         'Ex: -e "janela=ult.10; wr_min=0.70; linha_min=6.5; folga_min=5.5; teto=3"')
    a = ap.parse_args()

    print('=' * 96)
    print(f' {VERSAO} | modo {a.modo} | arquivo {a.xlsx}')
    print('=' * 96)

    try:
        d = carregar(a.xlsx, h2h_path=a.h2h, paridade_path=a.paridade,
                     chips_fonte=a.chips)
        if (a.ate or a.de) and '_dt' in d.columns:
            _n0 = len(d)
            if a.de:
                d = d[d['_dt'] >= pd.Timestamp(a.de)]
            if a.ate:
                d = d[d['_dt'] < pd.Timestamp(a.ate) + pd.Timedelta(days=1)]
            if not len(d):
                raise ValueError('--de/--ate nao deixaram nenhuma aposta')
            print('=' * 78)
            print(f' PRE-COMPROMISSO: usando {len(d):,} de {_n0:,} apostas '
                  f'({d["_dt"].min():%d/%m} a {d["_dt"].max():%d/%m}).')
            print(f' As {_n0 - len(d):,} de fora sao HOLDOUT — a busca nao as ve.')
            print(' Depois, teste as escolhidas nelas com:')
            _dia_seg = ((pd.Timestamp(a.ate) + pd.Timedelta(days=1)).date()
                        if a.ate else '?')
            _dump = a.out.rsplit('.', 1)[0] + '.tudo.csv'
            print(f'   python repontua.py --garimpo {_dump} --apostas '
                  f'{a.xlsx} --de {_dia_seg}')
            print('=' * 78)
            d = d.reset_index(drop=True)
    except Exception as e:
        print(f'ERRO ao ler: {e}')
        return 2

    N = len(d)
    u = d['_u'].values.astype(np.float64)
    g = d['_G'].values.astype(np.int8)
    lin = d['_linha'].values.astype(np.float64)
    alin = np.abs(lin)
    zeb = d['_zebra'].values.astype(np.int8)
    odd = d['_odd'].values.astype(np.float64)
    dias_arr = d['_dt'].dt.date.values
    jid_all = pd.factorize(d['_jogo'])[0]
    # v8: eixos de IDENTIDADE, so pra AUDITORIA (nunca pra filtrar).
    # Servem pra responder "esse lucro vem de mecanismo ou de 3 nomes?" —
    # a armadilha do job 313, onde o topo do ranking era 64% tres jogadores.
    _cpar = achar(d, 'Confronto', 'confronto', 'partida')
    par_all = (pd.factorize(d[_cpar].astype(str).str.upper().str.replace(' ', ''))[0]
               if _cpar else np.zeros(N, np.int64))
    _cnick = None
    for _c in ('Tip', 'selecao', 'Selecao'):
        if _c in d.columns:
            _cnick = _c
            break
    if _cnick is not None:
        _nk = d[_cnick].astype(str).str.extract(r'\(([^()]+)\)\s*\(')[0]
        alvo_all = pd.factorize(_nk.fillna('?').str.upper().str.strip())[0]
    else:
        alvo_all = np.zeros(N, np.int64)
    _n_par = int(par_all.max()) + 1
    _n_alvo = int(alvo_all.max()) + 1
    dia_idx = pd.factorize(pd.Series(dias_arr))[0]
    n_jogos_tot = int(jid_all.max()) + 1
    n_dias_tot = int(dia_idx.max()) + 1
    ndias = max(len(np.unique(dias_arr)), 1)
    maxpj = int(np.bincount(jid_all).max())

    rng = np.random.default_rng(SEED)
    RH = rng.integers(1, 2 ** 63 - 1, size=N, dtype=np.int64).astype(np.uint64)

    def hash_idx(idx):
        x = int(np.bitwise_xor.reduce(RH[idx])) if idx.size else 0
        return idx.size * (1 << 64) + x          # unico por (tamanho, xor)

    # --- metades do periodo (o padrao se mantem no tempo?) ---
    dias_ord = sorted(np.unique(dias_arr))
    meio = dias_ord[len(dias_ord) // 2]
    meta2 = dias_arr >= meio

    # --- treino / cego ---
    cego_dias = a.cego
    if cego_dias < 0:
        cego_dias = 0 if ndias < 6 else max(3, int(round(ndias * 0.30)))
    if 0 < cego_dias < ndias:
        corte = dias_ord[-cego_dias]
        te = dias_arr >= corte
        tr = ~te
        tem_cego = True
    else:
        te = np.zeros(N, bool); tr = np.ones(N, bool); tem_cego = False

    # --- leitura de FRENTE (mais atual) pra TRAS (mais antigo) ---
    # Janelas ancoradas no ultimo dia do arquivo: "ultimos w dias". Nao e
    # regua de aprovacao — e leitura de recencia (o padrao esta vivo AGORA?).
    rec_js = []
    for tok in str(a.rec_janelas).split(','):
        tok = tok.strip()
        if tok.isdigit() and 0 < int(tok) < ndias:
            rec_js.append(int(tok))
    rec_js = sorted(set(rec_js))
    rec_masks = {w: (dias_arr >= dias_ord[-w]) for w in rec_js}
    if rec_js:
        print(f'  recencia: janelas de {rec_js} dia(s) ancoradas em {dias_ord[-1]}')

    # --- teto de sorte por tamanho (bootstrap POR JOGO) ---
    jg_un = np.unique(jid_all)
    curva_sorte = {}
    for nsz in [30, 50, 100, 200, 300, 500, 800, 1200, 2000, 4000]:
        if nsz > len(jg_un):
            break
        sims = [u[np.isin(jid_all, rng.choice(jg_un, nsz, replace=False))].mean() * 100
                for _ in range(200)]
        curva_sorte[nsz] = float(np.percentile(sims, 95))
    sz_ns = sorted(curva_sorte)

    def sorte(nj):
        if not sz_ns:
            return 0.0
        return float(np.interp(min(max(nj, sz_ns[0]), sz_ns[-1]), sz_ns,
                               [curva_sorte[k] for k in sz_ns]))

    # --- eixos ---
    WR, QTD, COMP = detectar_eixos(d)
    if a.janelas.strip():
        # v8: casar SEM ACENTO. Antes 'ult.10' virava 'ult10' e a coluna
        # 'Últ. 10' virava 'últ10' — nao casava, e a janela sumia da busca EM
        # SILENCIO. Uma rodada de 4h chegou a varrer so 'Todas' por causa disso.
        import unicodedata as _ud

        def _norm(s):
            s = _ud.normalize('NFD', str(s))
            s = ''.join(c for c in s if _ud.category(c) != 'Mn')
            return s.lower().replace(' ', '').replace('.', '')

        pedidas = [_norm(x) for x in a.janelas.split(',') if x.strip()]
        antes = list(WR)
        # IGUALDADE, nao substring: 'ult10' estava casando com 'Últ. 100'.
        # So cai pra substring quando o pedido nao bate exato com ninguem.
        def _casa(p, k):
            n = _norm(k)
            return (p == n) or (p not in [_norm(x) for x in antes] and p in n)
        WR = {k: v for k, v in WR.items()
              if any(_casa(p, k) for p in pedidas)}
        sem_uso = [p for p in pedidas
                   if not any(_casa(p, k) for k in antes)]
        if sem_uso:
            print(f'AVISO ............... --janelas nao casou com nada: {sem_uso} '
                  f'(disponiveis: {antes})')
        if not WR:
            print('AVISO ............... --janelas zerou TODAS as janelas; '
                  'seguindo SEM filtro de janela')
            WR = {k: v for k, v in zip(antes, [None] * len(antes))} and \
                 {k: v for k, v in WR.items()} or WR
        print(f'janelas em uso ...... {list(WR) or antes}')
    qhist = coluna_historico(QTD)
    qv = QTD[qhist] if qhist else np.full(N, 1e9)
    janelas = list(WR) or ['-']

    odd_ok, odd_info = eixo_odd_vivo(odd)
    if a.sem_odd:
        odd_ok = False
        odd_info = 'desligado por flag'

    lados_mask = {'ambos': np.ones(N, bool), 'zebra': zeb == 1, 'favorito': zeb == 0}
    lados_uteis = [ld for ld in G_LADOS if lados_mask[ld].sum() >= a.min_apostas]
    if 'ambos' in lados_uteis and len(lados_uteis) == 2:
        # so existe um lado no arquivo -> 'ambos' e duplicata dele (dedup pegaria,
        # mas nem vale gastar)
        so = [ld for ld in lados_uteis if ld != 'ambos'][0]
        if lados_mask[so].sum() == N:
            lados_uteis = [so]

    eixos_comp = []
    if not a.sem_extras:
        for nome, vals in COMP.items():
            cts = cortes_complementar(nome, vals)
            if cts:
                eixos_comp.append((nome, cts))
        eixos_comp = eixos_comp[:12]

    # --- cabecalho ---
    print(f'{N:,} apostas | {len(jg_un):,} jogos | {ndias} dias | '
          f'{N / max(len(jg_un), 1):.2f} apostas/jogo (max {maxpj}/jogo)')
    print(f'unidade de jogo ..... {d.attrs.get("origem_jogo", "?")}')
    if 'aviso_data' in d.attrs:
        print(f'AVISO ............... {d.attrs["aviso_data"]}')
    print(f'baseline ............ {u.sum():+.2f}u | ROI {u.mean() * 100:+.2f}% | '
          f'WR {g.mean() * 100:.1f}%')
    print(f'janelas de WR ....... {janelas}')
    print(f'eixo de confrontos .. {qhist or "(nenhum)"}'
          + (f' (max {np.nanmax(qv):.0f})' if qhist else ''))
    ign = [q for q in QTD if q != qhist]
    if ign:
        print(f'  qtd ignoradas (capadas na janela): {", ".join(ign)}')
    print(f'eixo de odd ......... {"VIVO — " + odd_info if odd_ok else "morto — " + odd_info}')
    print(f'complementares ...... {[n for n, _ in eixos_comp] or "(nenhum)"}')
    print(f'teto de sorte (p95 por n de jogos): '
          + ' | '.join(f'{k}j {v:+.1f}%' for k, v in curva_sorte.items()))
    if tem_cego:
        print(f'teste cego .......... ultimos {cego_dias} dias '
              f'({int(te.sum())} apostas) a partir de {corte}')
    else:
        print('teste cego .......... OFF (periodo curto ou --cego 0)')
    print(f'metades ............. 1a ate {meio}, 2a de {meio} em diante')

    # --- grades por modo ---
    cfg_fino = GRADES['fino'] if a.modo != 'grosso' else GRADES['grosso']
    cfg_meio = GRADES['medio'] if a.modo == 'completo' else (
        GRADES['fino'] if a.modo == 'total' else GRADES['grosso'])

    def grades_de(cfg):
        _nlin = a.nlin or cfg['nlin']
        _nlmax = a.nlmax or cfg['nlmax']
        lmins = candidatos_linha(alin, _nlin)
        lmins = grade_efetiva(alin, lmins, 'ge')
        lmaxs = candidatos_linha(alin, _nlmax)
        lmaxs = grade_efetiva(alin, list(lmaxs) + [999.0], 'le')
        _lv = alin[np.isfinite(alin)]
        if _lv.size and not globals().get('_GRADE_JA_IMPRESSA'):
            globals()['_GRADE_JA_IMPRESSA'] = True
            def _fmt(xs):
                s = ', '.join(f'{x:g}' for x in xs[:14])
                return s + ('...' if len(xs) > 14 else '')
            print(f'eixo de linha ...... dado de {_lv.min():g} a {_lv.max():g}')
            print(f'  pisos (>=X) [{len(lmins)}]: {_fmt(lmins)}')
            _lx = [x for x in lmaxs if x < 900]
            # a dica do teto so vale em mercado de TOTAL (linha na casa das
            # dezenas). No handicap o piso e que manda — nao poluir o log.
            _eh_total = float(np.nanmedian(_lv)) >= 40
            _av = ('   <-- em TOTAL DE PONTOS o teto e o filtro que decide; '
                   'suba --nlmax se isso nao cobrir a parte baixa do dado'
                   if (_eh_total and len(_lx) < len(lmins)) else '')
            print(f'  tetos (<=X) [{len(_lx)}]: {_fmt(_lx) or "nenhum"}{_av}')
        tetos = sorted({t for t in cfg['teto'] if t <= maxpj or t >= 999})
        return dict(wr=cfg['wr'], wrmax=cfg['wrmax'],
                    qtd=grade_efetiva(qv, cfg['qtd'], 'ge') or [0.0],
                    qmaxs=(grade_efetiva(qv, cfg['qmax'], 'le') or [999])
                          if qv is not None else [999],
                    lmins=lmins or [0.0], lmaxs=lmaxs or [999.0], tetos=tetos,
                    w2ge=cfg['w2ge'], w2le=cfg['w2le'])

    GF, GM = grades_de(cfg_fino), grades_de(cfg_meio)

    odd_pares = pares_odd(odd) if odd_ok else []
    ODD_GE = {om: odd >= om for om in {p[0] for p in odd_pares}}
    ODD_LE = {ox: (odd <= ox) | ~np.isfinite(odd) if ox >= 999 else odd <= ox
              for ox in {p[1] for p in odd_pares}}
    # odd NaN: passa no neutro, cai fora de qualquer faixa real
    for om in ODD_GE:
        ODD_GE[om] &= np.isfinite(odd)

    # --- 2a janela ---
    segundas = []
    if not a.sem_duplo and len(WR) >= 2:
        for n2, v2 in WR.items():
            for thr in GM['w2ge']:
                mm = v2 >= thr
                if mm.sum() >= a.min_apostas:
                    segundas.append((n2, '>=', thr, mm))
            for thr in GM['w2le']:
                mm = v2 <= thr
                c = int(mm.sum())
                if a.min_apostas <= c < N:      # <N: senao e neutro
                    segundas.append((n2, '<=', thr, mm))

    # --- combos de complementares ---
    combos_extra = []
    for nome, cts in eixos_comp:
        for rot, m in cts:
            combos_extra.append((rot, m))
    if a.prof_extra == 2 and len(eixos_comp) >= 2:
        for (na_, ca), (nb_, cb) in itertools.combinations(eixos_comp, 2):
            for (ra, ma), (rb, mb) in itertools.product(ca, cb):
                mm = ma & mb
                if mm.sum() >= a.min_apostas:
                    combos_extra.append((f'{ra} & {rb}', mm))

    # --- estimativa ---
    def estimar(gr, n_extra, n_seg, n_odd):
        pares_l = sum(1 for lm in gr['lmins'] for lx in gr['lmaxs'] if lx >= lm)
        pares_w = sum(1 for wx in gr['wrmax'] for wm in gr['wr']
                      if wx >= 1.0 or wm <= 0
                      or 0.10 - 1e-9 <= wx - wm <= 0.30 + 1e-9)
        pares_q = sum(1 for qm in gr['qtd'] for qx in gr['qmaxs']
                      if qx >= qm and (qx >= 999 or qm <= 0 or qm * 2 <= qx))
        return (len(janelas) * pares_w * pares_q
                * len(lados_uteis) * pares_l * max(n_odd, 1) * max(n_seg, 1)
                * max(n_extra, 1) * len(gr['tetos']))

    GG = grades_de(GRADES['grosso'])
    est = {
        'P1 nucleo': estimar(GF, 0, 0, 0),
        'P2 odd': estimar(GM, 0, 0, len(odd_pares)) if odd_pares else 0,
        'P3 duas janelas': estimar(GG, 0, len(segundas), 0) if segundas else 0,
        'P4 complementares': (estimar(GG, len(combos_extra), 0, 0)
                              if combos_extra else 0),
    }
    if a.modo == 'total':
        est['P2 odd'] = estimar(GF, 0, 0, len(odd_pares)) if odd_pares else 0
        est['P3 duas janelas'] = estimar(GM, 0, len(segundas), 0) if segundas else 0
        est['P4 complementares'] = estimar(GM, len(combos_extra), 0, 0) if combos_extra else 0
    total_est = sum(est.values())
    print('\nplano da varredura (teto superior — a poda derruba isso):')
    for k, v in est.items():
        if v:
            print(f'  {k:<20} ate {v:,} configuracoes')
    print(f'  {"TOTAL":<20} ate {total_est:,}')
    if total_est > 5_000_000:
        print('  (busca larga: a barra do placebo sobe junto — achado grande '
              'aqui prova MENOS. A aba ROBUSTAS ja desconta isso.)')

    if a.plano:
        print('\n' + '=' * 78)
        print(' MODO --plano: nada foi varrido. Confira ACIMA, nesta ordem:')
        print('   1. "janelas em uso"  — as janelas de chip que vao ser testadas')
        print('   2. "eixo de linha"   — pisos E tetos cobrindo o dado?')
        print('   3. "complementares"  — os eixos derivados que ele detectou')
        print('   4. "teste cego"      — quantos dias ficam de fora')
        print('   5. o TOTAL estimado  — cabe no tempo que voce tem?')
        print(' Se algo estiver errado, corrija a flag e rode --plano de novo.')
        print('=' * 78)
        return

    # ------------------------------------------------------ estado da busca --
    CRIT = ('unidades', 'lucro_dd', 'ROI', 'u_dia', 'z_jogo', 'WR')
    tops = {c: [] for c in CRIT}
    cont = [0]
    testadas = [0]
    distintas = [0]
    vistos_base = set()          # hashes de mascara-base ja vistas
    seen_keep = set()            # hashes de mascara final (pos-teto)
    equivmap = {}                # hash -> rec guardado (pra contar `equiv`)
    EQUIV_CAP = 400_000          # trava de memoria em busca gigante
    prox_print = [250_000]
    t0 = time.time()

    def guardar(rec):
        cont[0] += 1
        entrou = False
        for crit in CRIT:
            v = rec.get(crit)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            if crit == 'WR' and rec.get('apostas', 0) < a.min_ap_sniper:
                continue   # caca de WR so acima do piso — WR alto em recorte minusculo e sorte
            h = tops[crit]
            if len(h) < a.guardar:
                heapq.heappush(h, (v, cont[0], rec))
                entrou = True
            elif v > h[0][0]:
                heapq.heapreplace(h, (v, cont[0], rec))
                entrou = True
        return entrou

    def pode_entrar(n_ap, vals):
        """v7: porteiro barato. Diz se a config tem chance de entrar em ALGUM
        heap, olhando so os 6 numeros que os rankings usam. Serve pra abortar
        a metricas ANTES da parte cara (max_seq_reds, recencia, odd, dias,
        cego, borda_chip e ~28 arredondamentos) — que hoje e calculada pra
        todo mundo e jogada fora na quase totalidade."""
        for crit in CRIT:
            v = vals.get(crit)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            if crit == 'WR' and n_ap < a.min_ap_sniper:
                continue
            h = tops[crit]
            if len(h) < a.guardar or v > h[0][0]:
                return True
        return False

    def progresso():
        if testadas[0] >= prox_print[0]:
            prox_print[0] += 250_000
            dt_ = time.time() - t0
            taxa = testadas[0] / max(dt_, 1e-9)
            resta = (total_est - testadas[0]) / max(taxa, 1)
            print(f'  ... {testadas[0]:,} testadas | {distintas[0]:,} distintas | '
                  f'{cont[0]:,} guardadas | {dt_:.0f}s | ~{taxa:,.0f}/s | '
                  f'ETA (teto) {resta / 60:.0f}min', flush=True)
            _emitir_progresso(testadas=testadas[0], distintas=distintas[0],
                              guardadas=cont[0], total_estimado=total_est,
                              segundos=round(dt_), taxa=round(taxa),
                              eta_min=round(resta / 60),
                              pct=min(99, round(testadas[0] / max(total_est, 1) * 100)))

    def metricas(keep, teto, rotulos):
        n = keep.size
        su = u[keep]
        soma = float(su.sum())
        roi = float(su.mean()) * 100
        jsel = jid_all[keep]
        ju = np.bincount(jsel, weights=su, minlength=n_jogos_tot)
        jc = np.bincount(jsel, minlength=n_jogos_tot)
        tem_j = jc > 0
        nj = int(tem_j.sum())
        if nj < a.min_jogos:
            return None
        cum = np.cumsum(su)
        dd = float((np.maximum.accumulate(np.maximum(cum, 0)) - cum).max())
        di = dia_idx[keep]
        du = np.bincount(di, weights=su, minlength=n_dias_tot)
        dc = np.bincount(di, minlength=n_dias_tot)
        tem_d = dc > 0
        nG = int(g[keep].sum())
        ju_s = ju[tem_j]
        if nj >= 3:
            sd = float(ju_s.std(ddof=1))
            zj = float(ju_s.mean() / (sd / math.sqrt(nj))) if sd > 0 else float('nan')
        else:
            zj = float('nan')
        # --- v7: PORTEIRO (antes da parte cara) ---
        _ldd = (soma / dd) if dd > 0 else float('nan')
        if not pode_entrar(n, {'unidades': soma, 'lucro_dd': _ldd, 'ROI': roi,
                               'u_dia': soma / ndias, 'z_jogo': zj,
                               'WR': nG / n * 100}):
            return None
        ov = odd[keep]
        ov = ov[np.isfinite(ov)]
        odd_med = float(ov.mean()) if ov.size else float('nan')
        be = 100.0 / odd_med if odd_med and odd_med > 1 else float('nan')
        wrp = nG / n * 100
        m2k = meta2[keep]
        n2 = int(m2k.sum()); n1 = n - n2
        roi1 = float(su[~m2k].mean()) * 100 if n1 >= 10 else float('nan')
        roi2 = float(su[m2k].mean()) * 100 if n2 >= 10 else float('nan')
        rec = dict(rotulos)
        # --- v8: CONCENTRACAO POR IDENTIDADE (auditoria, nao filtro) ---
        # Quanto do lucro vem dos 3 pares (e dos 3 alvos) mais lucrativos?
        # Alto = o "padrao" pode ser whitelist disfarcada de filtro numerico.
        # E o espelho: quanto do PREJUIZO vem dos 3 piores (troll aparente).
        for _rot, _cod, _nn in (('par', par_all, _n_par), ('alvo', alvo_all, _n_alvo)):
            if _nn <= 1:
                rec[f'conc_{_rot}'] = None
                continue
            _s = np.bincount(_cod[keep], weights=su, minlength=_nn)
            _c = np.bincount(_cod[keep], minlength=_nn)
            _s = _s[_c > 0]
            rec[f'n_{_rot}'] = int(_s.size)
            if _s.size >= 4 and soma > 0:
                _top = np.sort(_s)[-3:].sum()
                rec[f'conc_{_rot}'] = round(float(_top / soma) * 100, 1)
                _bot = np.sort(_s)[:3].sum()
                rec[f'dano_{_rot}'] = round(float(_bot), 2)
            else:
                rec[f'conc_{_rot}'] = None
        _cp = rec.get('conc_par')
        _ca = rec.get('conc_alvo')
        rec['id_suspeita'] = 1 if ((_cp is not None and _cp > 40)
                                   or (_ca is not None and _ca > 40)) else 0
        rec.update(
            teto=(teto if teto < 999 else '-'),
            apostas=n, jogos=nj, por_jogo=round(n / nj, 2),
            G=nG, R=n - nG, WR=round(wrp, 1),
            unidades=round(soma, 2), ROI=round(roi, 2),
            u_dia=round(soma / ndias, 2), ap_dia=round(n / ndias, 1),
            odd_media=round(odd_med, 3) if math.isfinite(odd_med) else None,
            break_even=round(be, 1) if math.isfinite(be) else None,
            margem_be=round(wrp - be, 1) if math.isfinite(be) else None,
            linha_media=round(float(alin[keep].mean()), 1),
            DD=round(dd, 2),
            lucro_dd=round(soma / dd, 1) if dd > 0 else 999.0,
            pior_jogo=round(float(ju_s.min()), 2),
            pior_dia=round(float(du[tem_d].min()), 2),
            melhor_dia=round(float(du[tem_d].max()), 2),
            dias_pos=int(((du > 0) & tem_d).sum()),
            dias_neg=int(((du < 0) & tem_d).sum()),
            max_reds=max_seq_reds(g[keep]),
            z_jogo=round(zj, 2) if math.isfinite(zj) else None,
            roi_m1=round(roi1, 2) if math.isfinite(roi1) else None,
            roi_m2=round(roi2, 2) if math.isfinite(roi2) else None,
            acima_sorte=round(roi - sorte(nj), 2),
            equiv=1,
        )
        # v6: FRAGILIDADE — fracao da cesta cujo chip esta a menos de UM JOGO
        # (1/N da janela; Todas usa a Qtd da propria aposta) de um corte de WR
        # da config. E o que desmonta quando a fase 2 escreve 1 jogo retroativo
        # no banco: banda estreita em janela curta acende aqui.
        _borda = 0.0
        _nb = 0
        for _jn, _mn_r, _mx_r in ((rotulos.get('janela'), rotulos.get('wr_min'),
                                   rotulos.get('wr_max')),
                                  (rotulos.get('janela2'), rotulos.get('wr2'), None)):
            if not _jn or _jn == '-' or _jn not in WR:
                continue
            _v = WR[_jn][keep]
            _num = ''.join(ch for ch in str(_jn) if ch.isdigit())
            _passo = (1.0 / float(_num)) if _num else 1.0 / np.maximum(qv[keep], 1)
            _hit = np.zeros(n, bool)
            for _c in (_mn_r, _mx_r):
                try:
                    _cv = float(_c)
                except (TypeError, ValueError):
                    continue
                _hit |= np.abs(_v - _cv) < _passo
            _fin = np.isfinite(_v)
            if _fin.any():
                _borda += float(_hit[_fin].mean())
                _nb += 1
        if _nb:
            _borda /= _nb
            rec['borda_chip'] = round(_borda * 100, 1)
            rec['fragil'] = 1 if _borda > 0.30 else 0
        else:
            rec['borda_chip'] = None
            rec['fragil'] = None
        if tem_cego:
            kt = tr[keep]; kc = te[keep]
            nt = int(kt.sum()); nc = int(kc.sum())
            rt = float(su[kt].mean()) * 100 if nt else float('nan')
            rc = float(su[kc].mean()) * 100 if nc >= MIN_CEGO_AP else float('nan')
            rec['roi_treino'] = round(rt, 2) if nt else None
            rec['roi_cego'] = round(rc, 2) if math.isfinite(rc) else None
            rec['ap_cego'] = nc
            rec['desvio_cego'] = (round(rc - rt, 2)
                                  if (math.isfinite(rc) and nt) else None)
        # --- recencia: janelas ancoradas no ultimo dia (frente -> tras) ---
        for w in rec_js:
            kw = rec_masks[w][keep]
            nw = int(kw.sum())
            rec[f'ap_{w}d'] = nw
            if nw:
                Gw = int(g[keep][kw].sum())
                uw = float(su[kw].sum())
                rec[f'G_{w}d'] = Gw
                rec[f'R_{w}d'] = nw - Gw
                rec[f'u_{w}d'] = round(uw, 2)
                rec[f'roi_{w}d'] = round(uw / nw * 100, 2) if nw >= 10 else None
            else:
                rec[f'G_{w}d'] = 0; rec[f'R_{w}d'] = 0
                rec[f'u_{w}d'] = 0.0; rec[f'roi_{w}d'] = None
        if rec_js:
            # v6: vivo ENDURECIDO — exige lucro positivo em TODAS as janelas
            # de recencia com >=10 ap cada (3d E 7d por padrao). A regua
            # antiga (so a janela longa) mascarou a virada do baseline b365.
            wmax = rec_js[-1]
            if rec[f'ap_{wmax}d'] < 10:
                rec['vivo'] = None
            else:
                ok = True
                for w in rec_js:
                    if rec[f'ap_{w}d'] >= 10 and rec[f'u_{w}d'] <= 0:
                        ok = False
                        break
                rec['vivo'] = 1 if ok else 0
            # v6: TENDENCIA — quanto a ponta caiu vs a 2a metade do periodo.
            _rp = rec.get(f'roi_{rec_js[0]}d')
            _r2 = rec.get('roi_m2')
            rec['queda_ponta'] = (round(_rp - _r2, 2)
                                  if _rp is not None and _r2 is not None else None)
        return rec

    # ------------------------------------------------- modo ESTRATEGIA -------
    # Avalia config(s) ESPECIFICA(s) com a MESMA maquinaria da busca (mascaras,
    # teto por degrau, metricas, sorte, cego, metades) e sai sem varrer.
    # HONESTIDADE: acima_placebo NAO se aplica aqui — e a penalidade da BUSCA
    # (108k comparacoes); estrategia pre-especificada paga so a barra da sorte
    # (p95 pro n de jogos) + cego + metades + z. E exatamente por isso que
    # testar aqui vale MAIS que achar o mesmo numero na varredura.
    if a.estrategia:
        def _norm_tok(s):
            s = unicodedata.normalize('NFD', str(s).lower())
            s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
            return s.replace(' ', '').replace('.', '').replace('_', '')

        def _acha_janela(pedida):
            alvo = _norm_tok(pedida).replace('last', 'ult')
            for k in WR:
                if alvo in _norm_tok(k).replace('last', 'ult'):
                    return k
            return None

        def _acha_comp(pedida):
            alvo = _norm_tok(pedida)
            for k in COMP:
                if alvo in _norm_tok(k):
                    return k
            return None

        def _f(v):
            x = float(str(v).replace(',', '.'))
            return x

        def _wr_norm(v):
            x = _f(v)
            return x / 100.0 if x > 1.0 else x

        fichas = []
        erro_algum = False
        for si, spec in enumerate(a.estrategia, 1):
            kv = {}
            for parte in str(spec).replace('|', ';').split(';'):
                parte = parte.strip()
                if not parte:
                    continue
                if '=' not in parte:
                    print(f'[estrategia {si}] ignorando pedaco sem "=": {parte!r}')
                    continue
                ch, _, vl = parte.partition('=')
                kv[ch.strip().lower()] = vl.strip()

            rot = dict(passe='EST', janela='-', wr_min='-', wr_max='-',
                       janela2='-', op2='-', wr2='-', conf_min='-', conf_max='-',
                       linha_min='-', linha_max='-', odd_min='-', odd_max='-',
                       lado='-', extra='-')
            m = np.ones(N, bool)
            problemas = []
            try:
                # janela principal
                if 'janela' in kv:
                    jk = _acha_janela(kv['janela'])
                    if jk is None:
                        problemas.append(f"janela {kv['janela']!r} nao achada "
                                         f"(tenho: {list(WR)})")
                    else:
                        rot['janela'] = jk
                        if 'wr_min' in kv:
                            t = _wr_norm(kv['wr_min']); rot['wr_min'] = t
                            m &= WR[jk] >= t
                        if 'wr_max' in kv:
                            t = _wr_norm(kv['wr_max']); rot['wr_max'] = t
                            m &= WR[jk] <= t
                elif 'wr_min' in kv or 'wr_max' in kv:
                    problemas.append('wr_min/wr_max sem "janela="')
                # segunda janela
                if 'janela2' in kv:
                    jk2 = _acha_janela(kv['janela2'])
                    if jk2 is None:
                        problemas.append(f"janela2 {kv['janela2']!r} nao achada")
                    else:
                        op2 = kv.get('op2', 'ge').strip().lower()
                        op2 = '>=' if op2 in ('ge', '>=', 'min') else '<='
                        if 'wr2' not in kv:
                            problemas.append('janela2 sem "wr2="')
                        else:
                            t2 = _wr_norm(kv['wr2'])
                            rot['janela2'], rot['op2'], rot['wr2'] = jk2, op2, t2
                            m &= (WR[jk2] >= t2) if op2 == '>=' else (WR[jk2] <= t2)
                # confrontos (Qtd Todas / historico)
                if 'conf_min' in kv or 'conf_max' in kv:
                    if qhist is None:
                        problemas.append('conf_min/max: planilha sem coluna de '
                                         'confrontos (Qtd Todas)')
                    else:
                        if 'conf_min' in kv:
                            t = _f(kv['conf_min']); rot['conf_min'] = int(t)
                            m &= qv >= t
                        if 'conf_max' in kv:
                            t = _f(kv['conf_max']); rot['conf_max'] = int(t)
                            m &= qv <= t
                # linha (valor ABSOLUTO, igual a busca)
                if 'linha_min' in kv:
                    t = _f(kv['linha_min']); rot['linha_min'] = t; m &= alin >= t
                if 'linha_max' in kv:
                    t = _f(kv['linha_max']); rot['linha_max'] = t; m &= alin <= t
                # odd
                if 'odd_min' in kv:
                    t = _f(kv['odd_min']); rot['odd_min'] = t
                    m &= np.isfinite(odd) & (odd >= t)
                if 'odd_max' in kv:
                    t = _f(kv['odd_max']); rot['odd_max'] = t
                    m &= np.isfinite(odd) & (odd <= t)
                # lado
                ld = kv.get('lado', 'zebra' if 'zebra' in lados_mask else 'ambos')
                ld = ld.strip().lower()
                if ld not in lados_mask:
                    problemas.append(f'lado {ld!r} invalido')
                else:
                    rot['lado'] = ld
                    m &= lados_mask[ld]
                # extras (folga/momento/qualquer complementar)
                extras_rot = []
                for ch, nome_busca in (('folga_min', 'folga'), ('folga_max', 'folga'),
                                       ('momento_min', 'tot_env'), ('momento_max', 'tot_env'),
                                       ('tot_env_min', 'tot_env'), ('tot_env_max', 'tot_env')):
                    if ch in kv:
                        ck = _acha_comp(nome_busca)
                        if ck is None:
                            problemas.append(f'{ch}: eixo {nome_busca!r} nao existe '
                                             f'nesta planilha (tenho: {list(COMP)})')
                            continue
                        t = _f(kv[ch])
                        vv = COMP[ck]
                        if ch.endswith('_min'):
                            m &= np.isfinite(vv) & (vv >= t)
                            extras_rot.append(f'{nome_busca}>={t:g}')
                        else:
                            m &= np.isfinite(vv) & (vv <= t)
                            extras_rot.append(f'{nome_busca}<={t:g}')
                if 'extra' in kv:
                    mm = re.match(r'\s*([^<>=]+?)\s*(>=|<=)\s*([\-\d.,]+)\s*$', kv['extra'])
                    if not mm:
                        problemas.append(f"extra {kv['extra']!r} invalido (use nome>=x ou nome<=x)")
                    else:
                        ck = _acha_comp(mm.group(1))
                        if ck is None:
                            problemas.append(f"extra: eixo {mm.group(1)!r} nao existe "
                                             f"(tenho: {list(COMP)})")
                        else:
                            t = _f(mm.group(3)); vv = COMP[ck]
                            if mm.group(2) == '>=':
                                m &= np.isfinite(vv) & (vv >= t)
                            else:
                                m &= np.isfinite(vv) & (vv <= t)
                            extras_rot.append(f'{ck}{mm.group(2)}{t:g}')
                if extras_rot:
                    rot['extra'] = ' & '.join(extras_rot)
                # teto (degrau por jogo, recalculado POS-filtro — igual a busca)
                teto = int(_f(kv['teto'])) if 'teto' in kv else 999
            except (ValueError, TypeError) as e:
                problemas.append(f'valor invalido: {e}')

            print()
            print('=' * 96)
            print(f' ESTRATEGIA {si}: {spec}')
            print('=' * 96)
            if problemas:
                erro_algum = True
                for p in problemas:
                    print(f'  ERRO: {p}')
                continue
            idx = np.where(m)[0]
            if idx.size == 0:
                print('  0 apostas passam nesse filtro.')
                erro_algum = True
                continue
            if teto < 999:
                deg = degrau_no_indice(idx, jid_all)
                idx = idx[deg < teto]
            rec = metricas(idx, teto, rot)
            if rec is None:
                print(f'  {idx.size} apostas mas menos de {a.min_jogos} jogos '
                      f'(--min-jogos) — amostra pequena demais pra ficha.')
                erro_algum = True
                continue
            rec['acima_placebo'] = None   # nao se aplica (penalidade da BUSCA)
            fichas.append(rec)
            lbl = {
                'apostas': 'apostas', 'jogos': 'jogos', 'por_jogo': 'por jogo',
                'G': 'greens', 'R': 'reds', 'WR': 'WR %', 'unidades': 'unidades',
                'ROI': 'ROI %', 'u_dia': 'u/dia', 'ap_dia': 'apostas/dia',
                'odd_media': 'odd media', 'break_even': 'break-even %',
                'margem_be': 'margem s/ BE', 'linha_media': 'linha media',
                'DD': 'drawdown (u)', 'lucro_dd': 'lucro/DD',
                'pior_jogo': 'pior jogo (u)', 'pior_dia': 'pior dia (u)',
                'melhor_dia': 'melhor dia (u)', 'dias_pos': 'dias verdes',
                'dias_neg': 'dias vermelhos', 'max_reds': 'streak red',
                'z_jogo': 'z por jogo', 'roi_m1': 'ROI 1a metade',
                'roi_m2': 'ROI 2a metade', 'acima_sorte': 'acima da sorte (p95)',
                'roi_treino': 'ROI treino', 'roi_cego': 'ROI cego',
                'ap_cego': 'apostas no cego', 'desvio_cego': 'desvio no cego',
            }
            for k, nome in lbl.items():
                if k in rec and rec[k] is not None:
                    print(f'  {nome:<22} {rec[k]}')
            for w in rec_js:      # leitura de frente pra tras
                if rec.get(f'ap_{w}d'):
                    print(f'  ultimos {w}d{"":<12} {rec[f"ap_{w}d"]}ap = '
                          f'{rec[f"G_{w}d"]}G-{rec[f"R_{w}d"]}R | '
                          f'{rec[f"u_{w}d"]:+.2f}u | ROI '
                          f'{rec[f"roi_{w}d"] if rec[f"roi_{w}d"] is not None else "-"}')
            if rec.get('vivo') is not None:
                print(f'  vivo agora?            {"SIM" if rec["vivo"] == 1 else "NAO (apagou na janela recente)"}')
            print('  (acima_placebo nao se aplica: e a regua da BUSCA — '
                  'estrategia pre-especificada paga sorte + cego + metades + z)')

        if fichas:
            base_out = os.path.splitext(a.out)[0] + '_estrategias.csv'
            pd.DataFrame(fichas).to_csv(base_out, index=False, sep=';', decimal=',')
            print(f'\n{len(fichas)} ficha(s) salva(s) em {base_out}')
        return 1 if erro_algum and not fichas else 0

    # ------------------------------------------------------ nucleo da busca --
    def varre(gr, extras, segs, odds, passe):
        neutro_seg = [('-', '', 0.0, None)]
        neutro_ext = [('-', None)]
        neutro_odd = [(None, None)]
        lista_seg = segs if segs else neutro_seg
        lista_ext = extras if extras else neutro_ext
        lista_odd = odds if odds else neutro_odd
        for wname in janelas:
            wv = WR.get(wname)
            if wv is None:
                wv = np.ones(N)
            wr_eff = grade_efetiva(wv, gr['wr'], 'ge') or [0.0]
            wrmax_eff = grade_efetiva(wv, gr['wrmax'], 'le') or [1.01]
            for ext_rot, ext_m in lista_ext:
                for j2, op2, thr2, m2 in lista_seg:
                    if m2 is not None and j2 == wname:
                        continue    # 2a janela igual a principal = redundante
                    base0 = np.ones(N, bool)
                    if ext_m is not None:
                        base0 = base0 & ext_m
                    if m2 is not None:
                        base0 = base0 & m2
                    if int(base0.sum()) < a.min_apostas:
                        continue
                    for wmax in wrmax_eff:                     # frouxo -> apertado
                        mA = base0 if wmax >= 1.0 else (base0 & (wv <= wmax))
                        if int(mA.sum()) < a.min_apostas:
                            break
                        for wmin in wr_eff:                    # crescente
                            if not (wmax >= 1.0 or wmin <= 0
                                    or 0.10 - 1e-9 <= wmax - wmin <= 0.30 + 1e-9):
                                continue    # banda larga demais: ja coberta pelos puros
                            mB = mA if wmin <= 0 else (mA & (wv >= wmin))
                            if int(mB.sum()) < a.min_apostas:
                                break
                            for qmin in gr['qtd']:             # crescente
                                mC0 = mB if qmin <= 0 else (mB & (qv >= qmin))
                                if int(mC0.sum()) < a.min_apostas:
                                    break
                                for qmax in gr['qmaxs']:       # DEcrescente
                                    if qmax < qmin:
                                        break
                                    if not (qmax >= 999 or qmin <= 0
                                            or qmin * 2 <= qmax):
                                        continue
                                    mC = mC0 if qmax >= 999 else (mC0 & (qv <= qmax))
                                    if int(mC.sum()) < a.min_apostas:
                                        break
                                    # v7: destes niveis pra baixo o trabalho e por
                                    # INDICE, nao por mascara de N posicoes. Nos
                                    # lacos profundos costumam sobrar centenas de
                                    # apostas de dezenas de milhares — o & e o
                                    # .sum() cheios pagavam o array inteiro toda
                                    # vez, e e aqui que roda milhoes de vezes.
                                    idxC = np.flatnonzero(mC)
                                    for lado in lados_uteis:
                                        idxD = (idxC if lado == 'ambos'
                                                else idxC[lados_mask[lado][idxC]])
                                        if idxD.size < a.min_apostas:
                                            continue
                                        alinD = alin[idxD]
                                        for lmin in gr['lmins']:   # crescente
                                            idxE = idxD[alinD >= lmin]
                                            if idxE.size < a.min_apostas:
                                                break
                                            alinE = alin[idxE]
                                            for lmax in gr['lmaxs']:   # DEcrescente
                                                if lmax < lmin:
                                                    break
                                                idxF = (idxE if lmax >= 999
                                                        else idxE[alinE <= lmax])
                                                if idxF.size < a.min_apostas:
                                                    break
                                                for omin, omax in lista_odd:
                                                    if omin is None:
                                                        idx = idxF
                                                    else:
                                                        idx = idxF[ODD_GE[omin][idxF]
                                                                   & ODD_LE[omax][idxF]]
                                                        if idx.size < a.min_apostas:
                                                            testadas[0] += len(gr['tetos'])
                                                            progresso()
                                                            continue
                                                    hb = hash_idx(idx)
                                                    if hb in vistos_base:
                                                        testadas[0] += len(gr['tetos'])
                                                        progresso()
                                                        continue
                                                    vistos_base.add(hb)
                                                    deg = degrau_no_indice(idx, jid_all)
                                                    sem_wr = (wmin <= 0 and wmax >= 1.0)
                                                    rot = dict(
                                                        passe=passe,
                                                        janela=('-' if sem_wr else wname),
                                                        wr_min=wmin,
                                                        wr_max=(wmax if wmax < 1.0 else '-'),
                                                        conf_min=int(qmin),
                                                        conf_max=(int(qmax) if qmax < 999 else '-'),
                                                        linha_min=lmin,
                                                        linha_max=(lmax if lmax < 999 else '-'),
                                                        odd_min=(omin if omin is not None else '-'),
                                                        odd_max=(omax if omin is not None and omax < 999 else '-'),
                                                        lado=lado,
                                                        janela2=(j2 if m2 is not None else '-'),
                                                        op2=op2 if m2 is not None else '',
                                                        wr2=(thr2 if m2 is not None else '-'),
                                                        extra=ext_rot,
                                                    )
                                                    for teto in gr['tetos']:
                                                        testadas[0] += 1
                                                        progresso()
                                                        sel = deg < teto
                                                        ns = int(sel.sum())
                                                        if ns < a.min_apostas:
                                                            continue
                                                        keep = idx[sel]
                                                        hk = hash_idx(keep)
                                                        if hk in seen_keep:
                                                            r0 = equivmap.get(hk)
                                                            if r0 is not None:
                                                                r0['equiv'] += 1
                                                            continue
                                                        seen_keep.add(hk)
                                                        distintas[0] += 1
                                                        rec = metricas(keep, teto, rot)
                                                        if rec is None:
                                                            continue
                                                        rec['_hk'] = hk
                                                        if guardar(rec) and len(equivmap) < EQUIV_CAP:
                                                            equivmap[hk] = rec

    parcial = False
    try:
        print(f'\nPASSE 1 — nucleo ({"fino" if a.modo != "grosso" else "grosso"})...')
        varre(GF, None, None, None, 'P1')
        if odd_pares:
            print('PASSE 2 — faixas de odd...')
            varre(GM, None, None, odd_pares, 'P2')
        if segundas:
            print('PASSE 3 — duas janelas de winrate...')
            varre(GM if a.modo == 'total' else GG, None, segundas, None, 'P3')
        if combos_extra:
            print('PASSE 4 — complementares...')
            varre(GM if a.modo == 'total' else GG,
                  combos_extra, None, None, 'P4')
    except KeyboardInterrupt:
        parcial = True
        print('\nINTERROMPIDO (Ctrl+C) — salvando o que ja foi varrido...')

    # ------------------------------------------------------------- resultado --
    juntas, vistos_id = [], set()
    for crit in CRIT:
        for _, cid, rec in tops[crit]:
            if cid not in vistos_id:
                vistos_id.add(cid)
                juntas.append(rec)
    if not juntas:
        print('\nnenhuma configuracao atingiu os minimos. '
              'Baixe --min-jogos / --min-apostas.')
        return 1
    R = pd.DataFrame(juntas)
    print(f'\n{testadas[0]:,} testadas | {distintas[0]:,} distintas | '
          f'{len(R):,} guardadas | {time.time() - t0:.1f}s'
          + (' | PARCIAL' if parcial else ''))

    # --- barra do placebo (embaralha resultados POR JOGO, mede o pico) ---
    barra = None
    if parcial and a.placebo > 0:
        print('parcial: pulando a regua do placebo (rode inteiro pra te-la).')
    if a.placebo > 0 and len(R) > 0 and not parcial:
        try:
            print('regua do placebo: embaralhando os resultados por jogo...')
            EXTRA_M = {rot: m for rot, m in (combos_extra or [])}
            SEG_M = {(j2, op2, thr2): m for j2, op2, thr2, m in (segundas or [])}

            def mascara(rec):
                wname = rec['janela']
                wv = WR.get(wname) if wname != '-' else None
                m = np.ones(N, bool)
                if wv is not None:
                    if rec['wr_max'] != '-':
                        m &= wv <= float(rec['wr_max'])
                    if rec['wr_min'] > 0:
                        m &= wv >= float(rec['wr_min'])
                if rec['conf_min'] > 0:
                    m &= qv >= rec['conf_min']
                if rec.get('conf_max', '-') != '-':
                    m &= qv <= int(rec['conf_max'])
                m &= alin >= float(rec['linha_min'])
                if rec['linha_max'] != '-':
                    m &= alin <= float(rec['linha_max'])
                if rec['odd_min'] != '-':
                    m &= ODD_GE[float(rec['odd_min'])]
                    ox = 999.0 if rec['odd_max'] == '-' else float(rec['odd_max'])
                    m &= ODD_LE[ox]
                if rec['lado'] != 'ambos':
                    m &= lados_mask[rec['lado']]
                if rec['janela2'] != '-':
                    m &= SEG_M[(rec['janela2'], rec['op2'], float(rec['wr2']))]
                if rec['extra'] != '-':
                    m &= EXTRA_M[rec['extra']]
                idx = np.flatnonzero(m)
                deg = degrau_no_indice(idx, jid_all)
                teto = 999 if rec['teto'] == '-' else int(rec['teto'])
                return idx[deg < teto]

            # permutacoes por jogo
            ordj = np.argsort(jid_all, kind='stable')
            tamj = np.bincount(jid_all)
            inij = np.r_[0, np.cumsum(tamj)[:-1]]
            uord = u[ordj]
            P = a.placebo
            perms = np.empty((N, P), np.float32)
            rgp = np.random.default_rng(SEED + 1)
            ngames = len(tamj)
            for p in range(P):
                mp = rgp.permutation(ngames)
                pos = np.concatenate([
                    inij[mp[gg]] + (np.arange(tamj[gg]) % max(tamj[mp[gg]], 1))
                    for gg in range(ngames)])
                nv = np.empty(N, np.float32)
                nv[ordj] = uord[pos]
                perms[:, p] = nv

            # reconstrucao + verificacao das mascaras (auto-checagem)
            porcrit = 3000
            alvo_ids = set()
            alvo = []
            for crit in CRIT:
                for _, cid, rec in sorted(tops[crit], key=lambda x: -x[0])[:porcrit]:
                    if cid not in alvo_ids:
                        alvo_ids.add(cid)
                        alvo.append(rec)
            # barra POR FAIXA DE TAMANHO: config de 2000 jogos nao pode ser
            # cobrada pela barra da loteria de 40 jogos (e vice-versa)
            BORDAS = np.array([0, 60, 150, 400, 1000, 10 ** 9])
            NB = len(BORDAS) - 1
            picos = np.full((NB, P), -1e9, np.float64)
            n_bucket = np.zeros(NB, int)
            erros_masc = 0
            for rec in alvo:
                keep = mascara(rec)
                if hash_idx(keep) != rec.get('_hk'):
                    erros_masc += 1
                    continue
                b = int(np.searchsorted(BORDAS, rec['jogos'], 'right') - 1)
                n_bucket[b] += 1
                med = perms[keep].mean(axis=0) * 100.0
                picos[b] = np.maximum(picos[b], med)
            if erros_masc:
                print(f'  AVISO: {erros_masc} mascaras nao bateram na reconstrucao '
                      f'(ignoradas na barra) — me avise se passar de 1%')
            barra_b = np.full(NB, np.nan)
            for b in range(NB):
                if n_bucket[b] > 0:
                    barra_b[b] = float(np.percentile(picos[b], 95))
            # bucket vazio herda a barra do bucket MENOR mais proximo (mais alta
            # = conservador); se nao houver, a do maior mais proximo
            for b in range(NB):
                if math.isnan(barra_b[b]):
                    ante = [barra_b[i] for i in range(b - 1, -1, -1)
                            if not math.isnan(barra_b[i])]
                    post = [barra_b[i] for i in range(b + 1, NB)
                            if not math.isnan(barra_b[i])]
                    barra_b[b] = ante[0] if ante else (post[0] if post else 0.0)
            bidx = np.searchsorted(BORDAS, R['jogos'].values, 'right') - 1
            R['acima_placebo'] = (R['ROI'].values - barra_b[bidx]).round(2)
            barra = {f'{BORDAS[b]}-{BORDAS[b+1] if BORDAS[b+1] < 10**9 else "+"}j':
                     round(float(barra_b[b]), 2) for b in range(NB) if n_bucket[b] > 0}
            print(f'BARRA DO PLACEBO por faixa de jogos '
                  f'({len(alvo):,} configs de topo x {P} embaralhadas):')
            for b in range(NB):
                if n_bucket[b] > 0:
                    fim = BORDAS[b + 1] if BORDAS[b + 1] < 10 ** 9 else '+'
                    print(f'  {BORDAS[b]:>5}-{fim} jogos: p95 {barra_b[b]:+.2f}%  '
                          f'({n_bucket[b]:,} configs na faixa)')
            print('  -> so confie em config com acima_placebo > 0 '
                  'E desvio_cego pequeno E z_jogo >= 2')
        except MemoryError:
            print('  placebo pulado: memoria insuficiente (use --placebo 6)')
        except KeyboardInterrupt:
            print('  placebo interrompido — seguindo sem a barra')
        except Exception as e:
            print(f'  placebo falhou ({e}) — seguindo sem a barra')

    # --- ROBUSTAS: passa em TODAS as reguas ao mesmo tempo ---
    cond = pd.Series(True, index=R.index)
    cond &= R['acima_sorte'] > 0
    if 'acima_placebo' in R.columns:
        cond &= R['acima_placebo'] > 0
    cond &= R['z_jogo'].fillna(-9) >= 2.0
    cond &= (R['roi_m1'].fillna(-9) > 0) & (R['roi_m2'].fillna(-9) > 0)
    if tem_cego:
        cond &= R['roi_cego'].notna() & (R['roi_cego'] > 0)
        folga = np.maximum(5.0, 0.5 * R['roi_treino'].abs().fillna(0))
        cond &= (R['roi_treino'] - R['roi_cego']) <= folga   # nao pode DESABAR no cego
    ROB = R[cond].sort_values(['unidades'], ascending=False)

    # v6: recencia/vivo/tendencia/fragilidade LOGO depois do ROI — a primeira
    # coisa que o olho pega e o que decide se a config esta de pe AGORA.
    cols = ['passe', 'janela', 'wr_min', 'wr_max', 'janela2', 'op2', 'wr2',
            'conf_min', 'conf_max', 'linha_min', 'linha_max', 'odd_min', 'odd_max', 'lado',
            'extra', 'teto', 'apostas', 'jogos', 'por_jogo', 'G', 'R', 'WR',
            'unidades', 'ROI', 'vivo', 'queda_ponta']
    for w in rec_js:                       # leitura frente -> tras
        cols += [f'roi_{w}d', f'u_{w}d', f'G_{w}d', f'R_{w}d', f'ap_{w}d']
    cols += ['borda_chip', 'fragil', 'conc_par', 'conc_alvo', 'n_par',
             'n_alvo', 'dano_par', 'id_suspeita',
             'u_dia', 'ap_dia', 'odd_media', 'break_even',
             'margem_be', 'linha_media', 'DD', 'lucro_dd', 'pior_jogo',
             'pior_dia', 'melhor_dia', 'dias_pos', 'dias_neg', 'max_reds',
             'z_jogo', 'roi_m1', 'roi_m2', 'acima_sorte', 'acima_placebo',
             'roi_treino', 'roi_cego', 'ap_cego', 'desvio_cego', 'equiv']
    cols = [c for c in cols if c in R.columns]

    LEGENDA = pd.DataFrame([
        ('passe', 'P1 nucleo | P2 odd | P3 duas janelas | P4 complementares'),
        ('janela / wr_min / wr_max', 'janela de winrate e faixa exigida (max=- e sem teto)'),
        ('janela2 / op2 / wr2', 'segunda janela de winrate (>= ou <=)'),
        ('conf_min', f'minimo de confrontos no historico total ({qhist or "-"})'),
        ('conf_max', 'maximo de confrontos ("-" = sem maximo; baixo = novato)'),
        ('linha_min / linha_max', 'faixa da linha (valor absoluto)'),
        ('odd_min / odd_max', 'faixa de odd (- = sem filtro de odd)'),
        ('extra', 'corte complementar (gap/z/media/tendencia/tot_env/folga)'),
        ('teto', 'maximo de apostas por jogo (escadinha), recalculado POS-filtro'),
        ('apostas / jogos / por_jogo', 'volume; amostra de verdade e JOGOS (reds vem em bloco)'),
        ('unidades / ROI / u_dia', 'lucro total, por aposta e por dia'),
        ('odd_media / break_even / margem_be', 'preco medio, WR de empate e folga do WR sobre ele'),
        ('DD / lucro_dd / pior_jogo / pior_dia', 'drawdown maximo e piores perdas'),
        ('max_reds', 'maior sequencia de apostas red seguidas'),
        ('z_jogo', 'z clusterizado por JOGO (>=2 comeca a valer; aposta do mesmo jogo nao e independente)'),
        ('roi_m1 / roi_m2', 'ROI na 1a e na 2a metade do periodo (o padrao se mantem?)'),
        ('acima_sorte', 'ROI menos o teto de sorte p95 pro MESMO numero de jogos'),
        ('acima_placebo', 'ROI menos a barra p95 da busca rodada em dado embaralhado'),
        ('roi_treino / roi_cego / desvio_cego', 'treino vs ultimos dias nunca vistos pela busca'),
        ('equiv', 'quantas configuracoes diferentes selecionam EXATAMENTE as mesmas apostas'),
        ('ROBUSTAS', 'so quem passa em TUDO: sorte, placebo, z>=2, duas metades positivas e cego'),
        ('u_Nd / roi_Nd / G_Nd-R_Nd / ap_Nd', 'LEITURA DE FRENTE PRA TRAS: ultimos N dias ancorados no ultimo dia do arquivo'),
        ('vivo', 'v6 ENDURECIDO: 1 = lucro positivo em TODAS as janelas de recencia com >=10 ap (3d E 7d); 0 = apagou em alguma; vazio = pouco dado'),
        ('queda_ponta', 'roi_3d - roi_m2: quanto a ponta caiu vs a 2a metade. Negativo grande = edge morrendo'),
        ('borda_chip', 'pct da cesta cujo chip esta a menos de 1 jogo de um corte de WR da config'),
        ('fragil', '1 = borda_chip>30: a config desmonta com 1 jogo retroativo no banco (nao operar sem re-garimpo no banco atual)'),
        ('conc_par', 'pct do LUCRO vindo dos 3 PARES mais lucrativos. Acima de 40 a config e suspeita de whitelist disfarcada'),
        ('conc_alvo', 'idem para os 3 JOGADORES em que se aposta'),
        ('n_par / n_alvo', 'quantos pares/jogadores distintos a config toca — poucos = sem lastro'),
        ('dano_par', 'unidades perdidas nos 3 PARES piores (o espelho: quanto um troll estaria custando)'),
        ('id_suspeita', '1 = concentracao acima de 40% em pares OU em jogadores: audite antes de operar'),
        ('FONTE TICK', 'entrada pode ser o parquet BRUTO: o conversor replica o runner (escancarado zebra, FT, chips por cobertura). --h2h = dump carimbado do banco; --paridade = export do painel pra conferencia'),
        ('SNIPERS', 'aprovadas em todas as reguas com apostas >= --min-ap-sniper, ordenadas por WR (a regua sniper)'),
        ('FRONTEIRA_WR', 'a melhor config (por unidades) em cada patamar de WR — o preco de cada ponto de WR em volume'),
        ('VIVAS_AGORA', 'aprovadas ordenadas da janela mais ATUAL pra tras (roi ultimos 3d, depois 7d, depois total)'),
        ('ATENCAO recencia', 'as janelas de recencia cobrem os mesmos dias do teste cego: ESCOLHER por elas gasta o cego — o carimbo vira o paper/vivo dos dias seguintes'),
    ], columns=['coluna', 'significado'])

    for crit, titulo in [('unidades', 'MAIS UNIDADES'),
                         ('lucro_dd', 'MELHOR LUCRO / DRAWDOWN'),
                         ('ROI', 'MAIOR ROI'),
                         ('z_jogo', 'MAIOR z POR JOGO')]:
        print('\n' + '=' * 96)
        print(f' {titulo}')
        print('=' * 96)
        vis = ['janela', 'wr_min', 'wr_max', 'janela2', 'wr2', 'conf_min', 'conf_max',
               'linha_min', 'linha_max', 'odd_min', 'odd_max', 'lado', 'extra',
               'teto', 'apostas', 'jogos', 'unidades', 'ROI', 'WR', 'DD',
               'u_dia', 'z_jogo', 'roi_cego', 'acima_placebo']
        vis = [c for c in vis if c in R.columns]
        print(R.sort_values(crit, ascending=False).head(10)[vis].to_string(index=False))

    print('\n' + '=' * 96)
    print(f' ROBUSTAS (passam em todas as reguas): {len(ROB):,}')
    print('=' * 96)
    if len(ROB):
        print(ROB.head(15)[vis].to_string(index=False))
    else:
        print(' nenhuma. E uma resposta honesta: neste arquivo, nada sobrevive a')
        print(' TODAS as reguas — o que aparece nas outras abas e sorte/overfit')
        print(' ate prova em contrario (mais dias de dado, teste cego maior).')

    # ------------------------------------------------------------ salvar -----
    R = R.drop(columns=['_hk'], errors='ignore')
    ROB = ROB.drop(columns=['_hk'], errors='ignore')

    # --- caca de WR + leitura de recencia (sempre SOBRE AS APROVADAS) ---
    SNIP = ROB[ROB['apostas'] >= a.min_ap_sniper].sort_values(
        ['WR', 'ROI', 'unidades'], ascending=False) if len(ROB) else ROB

    FRONT = pd.DataFrame()
    if len(SNIP):
        linhas_f, vistos_f = [], set()
        for piso in range(58, 92, 2):
            cand = SNIP[SNIP['WR'] >= piso]
            if not len(cand):
                break
            r0 = cand.sort_values('unidades', ascending=False).iloc[0]
            chave = (r0['apostas'], r0['unidades'])
            if chave in vistos_f:
                continue          # mesma config domina patamares seguidos: uma linha so
            vistos_f.add(chave)
            rec0 = r0.to_dict(); rec0['wr_piso'] = piso
            linhas_f.append(rec0)
        FRONT = pd.DataFrame(linhas_f)

    VIVAS = pd.DataFrame()
    if rec_js and len(ROB) and 'vivo' in ROB.columns:
        ordem = [c for c in ([f'roi_{w}d' for w in rec_js] + ['ROI'])
                 if c in ROB.columns]
        VIVAS = ROB[ROB['vivo'] == 1].sort_values(
            ordem, ascending=False, na_position='last')

    if len(SNIP):
        print('\n' + '=' * 96)
        print(f' SNIPERS (aprovadas com >= {a.min_ap_sniper} apostas, por WR) — a regua sniper')
        print('=' * 96)
        vis_s = [c for c in vis + ['G', 'R']
                 if c in SNIP.columns]
        print(SNIP.head(10)[vis_s].to_string(index=False))
    if len(VIVAS):
        print('\n' + '=' * 96)
        print(f' VIVAS AGORA (aprovadas, da janela mais atual pra tras): {len(VIVAS):,}')
        print('=' * 96)
        vis_v = [c for c in (['janela', 'wr_min', 'linha_min', 'linha_max', 'extra',
                              'teto', 'apostas', 'G', 'R', 'WR', 'unidades', 'ROI']
                             + [f'u_{w}d' for w in rec_js]
                             + [f'roi_{w}d' for w in rec_js]) if c in VIVAS.columns]
        print(VIVAS.head(10)[vis_v].to_string(index=False))

    def salvar(caminho):
        ult = None
        for motor in ('xlsxwriter', 'openpyxl'):
            try:
                with pd.ExcelWriter(caminho, engine=motor) as w:
                    if len(VIVAS):
                        VIVAS.head(a.topo)[cols].to_excel(w, sheet_name='VIVAS_AGORA', index=False)
                    if len(SNIP):
                        SNIP.head(a.topo)[cols].to_excel(w, sheet_name='SNIPERS', index=False)
                    if len(FRONT):
                        colsf = ['wr_piso'] + cols
                        FRONT[[c for c in colsf if c in FRONT.columns]] \
                            .to_excel(w, sheet_name='FRONTEIRA_WR', index=False)
                    ROB.head(a.topo)[cols].to_excel(w, sheet_name='ROBUSTAS', index=False)
                    for crit, aba in [('unidades', 'POR_UNIDADES'),
                                      ('lucro_dd', 'POR_LUCRO_DD'),
                                      ('ROI', 'POR_ROI'),
                                      ('u_dia', 'POR_U_DIA'),
                                      ('z_jogo', 'POR_Z_JOGO')]:
                        (R.sort_values(crit, ascending=False).head(a.topo)[cols]
                         .to_excel(w, sheet_name=aba, index=False))
                    if 'acima_placebo' in R.columns:
                        val = R[R['acima_placebo'] > 0]
                        (val.sort_values('unidades', ascending=False).head(a.topo)[cols]
                         .to_excel(w, sheet_name='PASSAM_PLACEBO', index=False))
                    R.sort_values('unidades', ascending=False).head(200_000)[cols] \
                        .to_excel(w, sheet_name='TUDO', index=False)
                    LEGENDA.to_excel(w, sheet_name='LEGENDA', index=False)
                print(f'\narquivo: {caminho}  (motor {motor})')
                return
            except Exception as e:
                ult = e
        alt = (caminho[:-5] if caminho.lower().endswith('.xlsx') else caminho) + '.csv'
        R[cols].to_csv(alt, index=False)
        print(f'nao salvou xlsx ({ult}); csv em {alt}')

    salvar(a.out)
    try:
        tudo_csv = (a.out[:-5] if a.out.lower().endswith('.xlsx') else a.out) + '.tudo.csv'
        R.sort_values('unidades', ascending=False)[cols].to_csv(tudo_csv, index=False)
        print(f'dump completo: {tudo_csv}  ({len(R):,} linhas)')
    except Exception as e:
        print(f'nao salvou o dump completo ({e})')
    if barra is not None:
        print(f'\nresumo: barras do placebo {barra} | baseline '
              f'{u.mean() * 100:+.2f}% | ROBUSTAS: {len(ROB):,}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\ninterrompido.')
        sys.exit(130)
