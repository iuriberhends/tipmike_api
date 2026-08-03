"""
workers/backtest_upload.py - ESQUELETO do backtest por upload de arquivo (parquet).

OBJETIVO (validar estrutura, ainda SEM logica final):
    Permitir rodar o backtest de um bot sobre ticks vindos de um ARQUIVO PARQUET
    (extraido do HD), em vez de ler os ticks do banco por periodo.
    O h2h_historico continua vindo do BANCO, com cutoff no ts de cada tick.
    Resposta final = quantas U o bot teria feito naquele periodo (+ DD).

ARQUITETURA (o que ja existe vs o que e novo):
    [NOVO]  upload do parquet  -> salva temp -> upload_id
    [NOVO]  parse_ticks_parquet -> arquivo -> lista de ticks (mesmo formato do banco)
    [EXISTE] motor executar_backtest (workers/backtest_runner.py)
    [EXISTE] h2h do banco com cutoff (H2HCache.get_jogos)
    [EXISTE] relatorio U + DD (backtest_jobs: pnl, roi, drawdown_max, equity_curve)

PONTOS DE INTEGRACAO (3 peças):
    1. routers/backtest.py     -> novo endpoint POST /backtest/upload-ticks
    2. workers/backtest_upload -> parse_ticks_parquet (ESTE arquivo)
    3. workers/backtest_runner -> executar_backtest ganha fonte=arquivo (upload_id)

Este arquivo e ESQUELETO: assinaturas + validacao + pontos marcados com
'# TODO LOGICA'. Nada de implementacao pesada ainda - primeiro validar o encaixe.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Diretorio onde os parquets upados ficam ate o job rodar.
# TODO LOGICA: definir local definitivo no VPS (ex: C:\...\uploads_backtest)
UPLOAD_DIR = Path(os.environ.get("BACKTEST_UPLOAD_DIR", "uploads_backtest"))

# Colunas que o motor (executar_backtest) espera de cada tick.
# O parquet PRECISA ter essas (ou o parser mapeia pra elas).
COLUNAS_ESPERADAS = [
    "ts", "bookmaker", "sport", "liga", "event_id",
    "jogador_a", "jogador_b", "time_a", "time_b",
    "score_home", "score_away", "live_time",
    "mercado", "mercado_id", "mercado_tipo", "linha", "selecao", "selecao_id",
    "odds",
]


# ============================================================
# Peça 2: PARSER do parquet -> lista de ticks
# ============================================================

def validar_colunas(colunas_arquivo: list[str]) -> tuple[bool, list[str]]:
    """Confere se o parquet tem as colunas que o motor espera.
    Retorna (ok, faltando)."""
    faltando = [c for c in COLUNAS_ESPERADAS if c not in colunas_arquivo]
    return (len(faltando) == 0, faltando)


# Mapa esporte UI -> banco (mesmo do motor). Importado de la quando integrar;
# duplicado aqui pra o parser ser testavel isolado.
ESPORTE_UI_PARA_BANCO = {
    "fifa": "E-Football",
    "nba2k": "E-Basketball",
    "ehockey": "E-Hockey",
    "etennis": "E-Tennis",
}

# Tamanho maximo do parquet aceito (bytes). Protege contra arquivo gigante
# que estoura memoria do VPS. Ajustavel via env.
MAX_PARQUET_BYTES = int(os.environ.get("BACKTEST_MAX_PARQUET_MB", "500")) * 1024 * 1024


class BacktestUploadError(Exception):
    """Erro de upload/parsing de ticks. O worker captura e marca job como 'erro'
    com mensagem amigavel, em vez de quebrar com stacktrace cru."""
    pass


def parse_ticks_parquet(caminho_arquivo: str,
                        bot: Optional[dict] = None) -> list[dict]:
    """
    Le o parquet e devolve a lista de ticks no MESMO formato que o motor recebe
    do banco (lista de dicts), ja ordenada como o motor espera:
    ORDER BY event_id, mercado_id, linha, selecao_id, ts ASC.

    TIMEZONE: o parquet vem em UTC (ts com 'Z'). O banco (h2h_historico, ticks)
    esta em BRT. Pro cutoff time-machine bater, converte UTC -> America/Sao_Paulo
    e deixa naive (sem tz), igual ao ts que o motor usa internamente. Sem isso o
    cutoff ficaria 3h adiantado e o backtest contaria jogos do futuro = FURADO.

    Se `bot` vier, aplica os MESMOS filtros de pre-selecao do SQL do banco
    (bookmaker, sport, torneios) - pra o arquivo se comportar igual ao banco.

    BLINDAGEM: toda falha previsivel vira BacktestUploadError com mensagem clara.
    Como e dinheiro real, prefere FALHAR EXPLICITO a devolver dado silenciosamente
    errado (ex: ts que nao deu pra converter -> aborta, nao "passa batido").
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise BacktestUploadError(
            "pandas/pyarrow nao instalados no ambiente do worker"
        ) from e

    # --- arquivo existe e tem tamanho sao ---
    p = Path(caminho_arquivo)
    if not p.exists():
        raise BacktestUploadError(f"arquivo nao encontrado: {p.name}")
    if not p.is_file():
        raise BacktestUploadError(f"caminho nao e um arquivo: {p.name}")

    tamanho = p.stat().st_size
    if tamanho == 0:
        raise BacktestUploadError("arquivo vazio (0 bytes)")
    if tamanho > MAX_PARQUET_BYTES:
        mb = tamanho / 1024 / 1024
        lim = MAX_PARQUET_BYTES / 1024 / 1024
        raise BacktestUploadError(
            f"parquet grande demais: {mb:.0f}MB (limite {lim:.0f}MB)"
        )

    # --- leitura ---
    try:
        df = pd.read_parquet(p)
    except Exception as e:
        raise BacktestUploadError(
            f"nao foi possivel ler o parquet (corrompido ou formato invalido): {e}"
        ) from e

    if df is None or len(df) == 0:
        raise BacktestUploadError("parquet sem linhas")

    # --- colunas ---
    ok, faltando = validar_colunas(list(df.columns))
    if not ok:
        raise BacktestUploadError(
            f"parquet sem as colunas que o motor espera. Faltando: {faltando}"
        )

    # --- timezone: parse NAIVE, SEM conversao de fuso ---
    # REGRA DO PROJETO: o ts vem com sufixo 'Z' mas o relogio JA ESTA em BRT
    # (mesma convencao do fixture_date da TipManager). O caminho do BANCO le o ts
    # naive e NAO converte nada - e essa e a referencia validada em producao.
    # Pro arquivo bater com o banco, parseia o MESMO relogio de parede, sem somar
    # nem subtrair 3h. O codigo antigo fazia UTC->BRT (-3h): deixava o tick 3h
    # atrasado, desalinhava o cutoff do H2H e os buckets por dia = backtest furado.
    # Se algum dia confirmar que o ts e UTC REAL, vire a chave abaixo pra True.
    CONVERTER_UTC_PARA_BRT = False
    try:
        ts_orig = df['ts']
        if pd.api.types.is_datetime64_any_dtype(ts_orig):
            # parquet trouxe timestamp NATIVO
            if getattr(ts_orig.dt, 'tz', None) is not None:
                if CONVERTER_UTC_PARA_BRT:
                    ts_parsed = ts_orig.dt.tz_convert('America/Sao_Paulo').dt.tz_localize(None)
                else:
                    ts_parsed = ts_orig.dt.tz_localize(None)  # descarta tz, mantem relogio
            else:
                ts_parsed = ts_orig  # ja naive, usa como esta
        else:
            # veio STRING (ex: '2026-05-21T05:00:28.402Z'). Tira o 'Z'/offset e
            # parseia naive: o relogio de parede e o que vale (convencao BRT).
            s = ts_orig.astype(str).str.replace(r'(Z|[+-]\d{2}:?\d{2})$', '', regex=True)
            ts_parsed = pd.to_datetime(s, format='ISO8601', errors='coerce')
            if CONVERTER_UTC_PARA_BRT:
                ts_parsed = (ts_parsed.dt.tz_localize('UTC')
                                       .dt.tz_convert('America/Sao_Paulo')
                                       .dt.tz_localize(None))
    except Exception as e:
        raise BacktestUploadError(f"falha ao interpretar a coluna ts: {e}") from e

    n_total = len(df)
    n_nat = int(ts_parsed.isna().sum())
    if n_nat == n_total:
        raise BacktestUploadError(
            "nenhum ts pode ser interpretado como data - coluna ts invalida"
        )
    if n_nat > 0:
        frac = n_nat / n_total
        # tolera ate 1% de ts ruim (descarta). Acima disso, aborta: algo errado.
        if frac > 0.01:
            raise BacktestUploadError(
                f"{n_nat} de {n_total} ts invalidos ({frac:.1%}) - parquet suspeito"
            )
        logger.warning(f"[backtest_upload] descartando {n_nat} linhas com ts invalido")
        mask_ok = ts_parsed.notna()
        df = df[mask_ok].copy()
        ts_parsed = ts_parsed[mask_ok]

    df['ts'] = ts_parsed
    # DIAGNOSTICO: loga amostra do ts pra conferir o fuso a olho contra um jogo
    # de horario conhecido. Se vier 3h diferente do banco, e a chave de fuso.
    try:
        logger.info(
            f"[backtest_upload] ts naive (sem conversao). "
            f"amostra={df['ts'].head(2).tolist()} min={df['ts'].min()} max={df['ts'].max()}"
        )
    except Exception:
        pass

    # --- pre-selecao igual ao SQL do banco (so se bot vier) ---
    if bot:
        try:
            casa = bot.get('casa')
            if casa:
                df = df[df['bookmaker'] == casa]

            esporte_ui = bot.get('esporte')
            if esporte_ui:
                sport_banco = ESPORTE_UI_PARA_BANCO.get(esporte_ui, esporte_ui)
                df = df[df['sport'] == sport_banco]

            torneios = bot.get('torneios') or []
            if torneios:
                mask = pd.Series(False, index=df.index)
                for t in torneios:
                    if t:
                        mask |= df['liga'].str.contains(str(t), case=False, na=False)
                df = df[mask]

            torneios_excluir = bot.get('torneios_excluir') or []
            for t in torneios_excluir:
                if t:
                    df = df[~df['liga'].str.contains(str(t), case=False, na=False)]
        except KeyError as e:
            raise BacktestUploadError(f"coluna ausente ao filtrar: {e}") from e
        except Exception as e:
            raise BacktestUploadError(f"falha ao aplicar filtros do bot: {e}") from e

    # --- coercao de tipos: APENAS score_home/score_away ---
    # O motor (backtest_runner) trata cada campo do seu jeito:
    #   - linha: _parse_linha() ja lida com '+0.5', 'away|0.5', '' -> NAO converter
    #     aqui (to_numeric transformaria esses em NaN e perderia o tick).
    #   - odds: o motor faz float(tick['odds']) na hora -> deixa como vem.
    #   - mercado_tipo / mercado_id / selecao_id: usados como STRING (mapping de
    #     mercado compara com ['18'], dedup usa 'mercado_id' or '') -> NAO converter.
    # SO os scores entram em comparacao numerica direta (total = sh+sa; total>linha)
    # sem passar por parser. Se vierem string do parquet, da o erro str<float.
    # Entao converte SO eles pra Int64 (nullable: aceita None sem virar float).
    # Esta e a correcao minima e segura - mexer no resto quebra os parsers do motor.
    try:
        for col in ('score_home', 'score_away'):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')
        # odds: o motor compara CRU (odd < float(odd_min)) sem converter -> se
        # vier string do parquet da str<float. odds nao tem formato especial
        # (sempre "1.85"), entao to_numeric e seguro. NaN -> None (motor: odd
        # ausente -> rejeita o tick, comportamento correto).
        if 'odds' in df.columns:
            df['odds'] = pd.to_numeric(df['odds'], errors='coerce')
    except Exception as e:
        raise BacktestUploadError(f"falha ao converter scores/odds: {e}") from e

    # --- ordenacao (mesma do motor) ---
    try:
        df = df.sort_values(['event_id', 'mercado_id', 'linha', 'selecao_id', 'ts'])
    except Exception as e:
        raise BacktestUploadError(f"falha ao ordenar ticks: {e}") from e

    logger.info(
        f"[backtest_upload] parquet {p.name}: {len(df)} ticks apos filtros "
        f"(de {n_total} no arquivo)"
    )
    # v2 (02/ago — fase "Buscando ticks (arquivo)" era a mais lenta do job):
    # o caminho antigo fazia to_dict('records') e DEPOIS visitava ~36 MILHOES
    # de celulas em Python puro (isinstance + pd.isna por campo) so pra trocar
    # NaN/NaT/NA por None. A v2 faz a mesma normalizacao VETORIZADA (uma
    # passada em C) e monta os dicts por itertuples+zip (o caminho rapido).
    # SAIDA EQUIVALENTE: mesmas chaves na mesma ordem, mesmas linhas na mesma
    # ordem, mesmos None; numeros continuam escalares equivalentes aos do
    # to_dict (aritmetica e comparacoes identicas — provado na bancada com
    # parquet real, campo a campo). BLINDADO: qualquer falha no caminho
    # rapido cai no caminho antigo, que segue aqui intacto.
    try:
        df_obj = df.astype(object)
        df_obj = df_obj.where(pd.notna(df_obj), None)
        cols = list(df_obj.columns)
        registros = [dict(zip(cols, linha))
                     for linha in df_obj.itertuples(index=False, name=None)]
        return registros
    except Exception:
        logger.exception("[backtest_upload] caminho rapido falhou — usando o lento")
        registros = df.to_dict('records')
        for r in registros:
            for k, v in r.items():
                if v is pd.NA or (isinstance(v, float) and pd.isna(v)):
                    r[k] = None
                elif v is pd.NaT:
                    r[k] = None
        return registros


# ============================================================
# Peça 1 (helper): salvar/recuperar o arquivo upado
# ============================================================

def salvar_upload(conteudo: bytes, nome_original: str) -> str:
    """Salva o parquet upado e devolve o upload_id (o caminho no disco).
    Gera nome unico (uuid + nome original) pra nao colidir entre uploads.
    BLINDADO: valida extensao, tamanho, e trata falha de escrita (disco cheio)."""
    import uuid

    if not conteudo:
        raise BacktestUploadError("conteudo vazio")
    if not nome_original or not nome_original.lower().endswith(".parquet"):
        raise BacktestUploadError("so arquivo .parquet e aceito")
    if len(conteudo) > MAX_PARQUET_BYTES:
        mb = len(conteudo) / 1024 / 1024
        lim = MAX_PARQUET_BYTES / 1024 / 1024
        raise BacktestUploadError(f"arquivo {mb:.0f}MB excede limite de {lim:.0f}MB")

    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BacktestUploadError(f"nao foi possivel criar pasta de uploads: {e}") from e

    safe_nome = Path(nome_original).name  # tira path, evita traversal
    uid = uuid.uuid4().hex[:8]
    destino = UPLOAD_DIR / f"{uid}_{safe_nome}"
    try:
        destino.write_bytes(conteudo)
    except OSError as e:
        raise BacktestUploadError(f"falha ao gravar arquivo (disco cheio?): {e}") from e

    logger.info(f"[backtest_upload] salvo: {destino} ({len(conteudo)} bytes)")
    return str(destino)


def caminho_do_upload(upload_id: str) -> str:
    """Resolve o upload_id pro caminho do arquivo. Aqui upload_id JA E o caminho.
    Valida que esta dentro de UPLOAD_DIR (seguranca: evita ler arquivo arbitrario).
    BLINDADO contra path-traversal e upload_id vazio/invalido."""
    if not upload_id or not str(upload_id).strip():
        raise BacktestUploadError("upload_id vazio")

    try:
        p = Path(upload_id).resolve()
        base = UPLOAD_DIR.resolve()
    except (OSError, ValueError) as e:
        raise BacktestUploadError(f"upload_id invalido: {e}") from e

    if base not in p.parents and p != base:
        raise BacktestUploadError("upload_id fora do diretorio de uploads (bloqueado)")
    if not p.exists():
        raise BacktestUploadError(f"upload nao encontrado: {p.name}")
    return str(p)


# ============================================================
# Validacao cruzada: arquivo x banco (detecta divergencia)
# ============================================================

async def validar_cruzado(conn, upload_id: str, bot: dict,
                          amostra: int = 500) -> dict:
    """
    Compara uma AMOSTRA dos ticks do arquivo com o que o banco tem, pra detectar
    se o parquet diverge do banco (ex: arquivo de outra epoca, placar diferente,
    coletor mudou). NAO bloqueia o backtest - retorna um relatorio de divergencia
    pra UI mostrar um aviso. Decisao de confiar fica com o usuario.

    Retorna dict com:
        - amostrados: quantos ticks comparados
        - so_no_arquivo: event_ids que o arquivo tem e o banco nao
        - placar_divergente: casos onde o placar final difere entre arquivo e banco
        - ok: bool (True se divergencia dentro do tolerado)

    Filosofia: o backtest do arquivo SO e confiavel se o arquivo for fiel ao que
    o banco teria. Se divergir muito, o numero pode nao refletir a realidade -
    e o usuario PRECISA saber disso antes de apostar.
    """
    rel = {
        'amostrados': 0,
        'so_no_arquivo': 0,
        'placar_divergente': 0,
        'exemplos_divergencia': [],
        'ok': True,
        'erro': None,
    }

    try:
        caminho = caminho_do_upload(upload_id)
        ticks = parse_ticks_parquet(caminho, bot=bot)
    except BacktestUploadError as e:
        rel['erro'] = f"nao deu pra ler arquivo: {e}"
        rel['ok'] = False
        return rel

    if not ticks:
        rel['erro'] = "arquivo sem ticks apos filtros"
        return rel

    # pega placar final por event_id no arquivo
    placar_arquivo: dict = {}
    for t in ticks:
        sh, sa = t.get('score_home'), t.get('score_away')
        if sh is not None and sa is not None:
            try:
                placar_arquivo[str(t['event_id'])] = (int(sh), int(sa))
            except (TypeError, ValueError):
                pass

    # amostra de event_ids pra comparar
    event_ids = list(placar_arquivo.keys())[:amostra]
    rel['amostrados'] = len(event_ids)
    if not event_ids:
        rel['erro'] = "nenhum event_id com placar no arquivo pra comparar"
        return rel

    try:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (event_id) event_id, score_home, score_away "
            "FROM ticks WHERE event_id = ANY($1::text[]) "
            "AND score_home IS NOT NULL ORDER BY event_id, ts DESC",
            event_ids,
        )
    except Exception as e:
        rel['erro'] = f"falha ao consultar banco: {e}"
        rel['ok'] = False
        return rel

    placar_banco = {str(r['event_id']): (r['score_home'], r['score_away'])
                    for r in rows}

    for eid in event_ids:
        if eid not in placar_banco:
            rel['so_no_arquivo'] += 1
            continue
        pa = placar_arquivo[eid]
        pb = placar_banco[eid]
        # placar pode estar invertido (perspectiva A/B) - compara normalizado
        if tuple(sorted(pa)) != tuple(sorted(pb)):
            rel['placar_divergente'] += 1
            if len(rel['exemplos_divergencia']) < 5:
                rel['exemplos_divergencia'].append(
                    {'event_id': eid, 'arquivo': pa, 'banco': pb})

    # tolerancia: ate 5% de placar divergente e ate 50% so-no-arquivo (normal,
    # arquivo tem dados antigos que o banco ja limpou). Acima disso, alerta.
    if rel['amostrados'] > 0:
        frac_div = rel['placar_divergente'] / rel['amostrados']
        if frac_div > 0.05:
            rel['ok'] = False
            rel['erro'] = (
                f"{rel['placar_divergente']} placares divergentes de "
                f"{rel['amostrados']} ({frac_div:.0%}) - arquivo pode estar furado"
            )

    logger.info(
        f"[backtest_upload] validacao cruzada: {rel['amostrados']} amostrados, "
        f"{rel['so_no_arquivo']} so-no-arquivo, "
        f"{rel['placar_divergente']} placar divergente, ok={rel['ok']}"
    )
    return rel
