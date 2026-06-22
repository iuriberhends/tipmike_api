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
    """
    import pandas as pd

    p = Path(caminho_arquivo)
    if not p.exists():
        raise FileNotFoundError(f"parquet nao encontrado: {caminho_arquivo}")

    df = pd.read_parquet(p)

    ok, faltando = validar_colunas(list(df.columns))
    if not ok:
        raise ValueError(f"parquet sem colunas que o motor espera: {faltando}")

    # TIMEZONE: UTC -> BRT naive (alinha com o banco)
    df['ts'] = (pd.to_datetime(df['ts'], utc=True)
                  .dt.tz_convert('America/Sao_Paulo')
                  .dt.tz_localize(None))

    # Pre-selecao igual ao SQL do banco (so se bot vier)
    if bot:
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
                mask |= df['liga'].str.contains(t, case=False, na=False)
            df = df[mask]

        torneios_excluir = bot.get('torneios_excluir') or []
        for t in torneios_excluir:
            df = df[~df['liga'].str.contains(t, case=False, na=False)]

    # Mesma ordenacao do motor
    df = df.sort_values(['event_id', 'mercado_id', 'linha', 'selecao_id', 'ts'])

    logger.info(f"[backtest_upload] parquet {p.name}: {len(df)} ticks apos filtros")
    return df.to_dict('records')


# ============================================================
# Peça 1 (helper): salvar/recuperar o arquivo upado
# ============================================================

def salvar_upload(conteudo: bytes, nome_original: str) -> str:
    """Salva o parquet upado e devolve um upload_id (o caminho ou um id).
    ESQUELETO: estrutura do storage temporario.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # TODO LOGICA: gerar id unico (uuid), validar extensao .parquet,
    #   gravar bytes, talvez registrar numa tabela backtest_uploads.
    destino = UPLOAD_DIR / nome_original
    logger.info(f"[backtest_upload] (esqueleto) salvaria em {destino}")
    return str(destino)


def caminho_do_upload(upload_id: str) -> str:
    """Resolve o upload_id pro caminho do arquivo no disco.
    ESQUELETO."""
    # TODO LOGICA: se upload_id for id de tabela, buscar o caminho; se for o
    #   proprio caminho, validar que esta dentro de UPLOAD_DIR (seguranca).
    return upload_id


# ============================================================
# Peça 3 (integracao no motor): de onde vem os ticks
# ============================================================

async def carregar_ticks(conn, bot: dict, data_inicio, data_fim,
                         upload_id: Optional[str] = None) -> list[dict]:
    """
    Fonte unica de ticks pro motor. Se `upload_id` vier, le do ARQUIVO;
    senao, le do BANCO (comportamento atual).

    A ideia e o executar_backtest chamar ISTO no lugar do SELECT direto,
    e ganhar a fonte=arquivo sem mudar o resto do motor.

    ESQUELETO: mostra o galho. A query do banco ja existe no motor - aqui
    so o esqueleto do roteamento.
    """
    if upload_id:
        caminho = caminho_do_upload(upload_id)
        ticks = parse_ticks_parquet(caminho, bot=bot)
        logger.info(f"[backtest_upload] fonte=ARQUIVO, {len(ticks)} ticks")
        return ticks

    # TODO LOGICA: fonte=BANCO -> mover pra ca o SELECT que ja existe no
    #   executar_backtest (linhas ~925-964). Por ora, sinaliza.
    logger.info("[backtest_upload] fonte=BANCO (usa query existente do motor)")
    return []
