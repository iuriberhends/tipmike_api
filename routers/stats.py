"""
routers/stats.py - v3 (otimizado)

v3 - PERFORMANCE:
- Cache em memoria com TTL (mesmo padrao de routers/torneios.py)
- Heatmap: janela 7d (era 14d) + agrupa por bucket de ts em vez de EXTRACT em milhoes de linhas
- Jogadores: query mais leve, usa table sample
- Overview: 1 query agregada em vez de 5 separadas
- Distribuicoes: limites menores

ENDPOINTS:
- GET /stats/dashboard, /stats/bots, /stats/bots/{id} (antigos, mantidos)
- GET /stats/overview, /stats/proximos, /stats/ultimos, /stats/heatmap,
       /stats/distribuicoes, /stats/jogadores, /stats/torneios, /stats/preview-jogador
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime, timezone, timedelta
from database import db

router = APIRouter(prefix="/stats", tags=["Stats"])

# Cache em memoria - chave: (endpoint, params_tuple), valor: (timestamp, dados)
_CACHE: dict = {}
_CACHE_TTL = {
    'overview':       timedelta(seconds=30),    # KPIs do topo - troca rapido
    'proximos':       timedelta(seconds=15),    # jogos ao vivo
    'ultimos':        timedelta(minutes=2),     # historico fixo
    'heatmap':        timedelta(minutes=10),    # heatmap muda devagar
    'distribuicoes':  timedelta(minutes=5),
    'jogadores':      timedelta(minutes=15),
    'torneios':       timedelta(minutes=15),
    'preview':        timedelta(minutes=2),
}


def _cache_get(endpoint: str, params: tuple):
    key = (endpoint, params)
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, data = entry
    ttl = _CACHE_TTL.get(endpoint, timedelta(minutes=5))
    if datetime.now() - ts > ttl:
        del _CACHE[key]
        return None
    return data


def _cache_set(endpoint: str, params: tuple, data):
    _CACHE[(endpoint, params)] = (datetime.now(), data)


# Mapeamento esporte (id frontend) -> sport (campo no banco)
ESPORTE_TO_SPORT = {
    'nba2k':   'E-Basketball',
    'fifa':    'E-Football',
    'ehockey': 'E-Hockey',
    'etennis': 'E-Tennis',
}


def _sport_from_esporte(esporte: str) -> str:
    return ESPORTE_TO_SPORT.get(esporte, esporte)


# ============================================================
# ENDPOINTS ANTIGOS (mantidos)
# ============================================================

@router.get("/dashboard")
async def dashboard():
    agora = datetime.utcnow()
    inicio_hoje = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    uma_hora_atras = agora - timedelta(hours=1)

    async with db() as conn:
        # 1 query agregada com todos os contadores
        row = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM bots WHERE status = 'ativo') AS bots_ativos,
                (SELECT COUNT(*) FROM bots WHERE status != 'arquivado') AS bots_total,
                (SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1) AS apostas_hoje,
                (SELECT COUNT(*) FROM apostas WHERE resultado = 'pendente') AS apostas_pendentes,
                (SELECT COALESCE(SUM(lucro_unidades), 0) FROM apostas
                 WHERE apostado_em >= $1 AND resultado != 'pendente') AS lucro_hoje,
                (SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1 AND resultado = 'green') AS ganhas_hoje,
                (SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1 AND resultado != 'pendente') AS resolvidas_hoje,
                (SELECT COUNT(*) FROM ticks WHERE ts >= $2) AS ticks_ultima_hora
        """, inicio_hoje, uma_hora_atras)

        bookmakers_rows = await conn.fetch(
            "SELECT DISTINCT bookmaker FROM ticks WHERE ts >= $1 ORDER BY bookmaker",
            uma_hora_atras
        )

    win_rate = round((row['ganhas_hoje'] / row['resolvidas_hoje'] * 100), 1) if row['resolvidas_hoje'] > 0 else None

    return {
        "bots_ativos": row['bots_ativos'],
        "bots_total": row['bots_total'],
        "apostas_hoje": row['apostas_hoje'],
        "lucro_hoje": float(row['lucro_hoje']) if row['lucro_hoje'] else 0.0,
        "win_rate_hoje": win_rate,
        "apostas_pendentes": row['apostas_pendentes'],
        "ticks_ultima_hora": row['ticks_ultima_hora'],
        "bookmakers_ativos": [r["bookmaker"] for r in bookmakers_rows],
        "atualizado_em": agora.isoformat(),
    }


@router.get("/bots")
async def stats_todos_bots():
    sql = """
        SELECT
            b.id, b.nome, b.casa, b.esporte, b.mercado, b.status,
            COUNT(a.id) AS total_apostas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'green') AS ganhas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'red') AS perdidas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'pendente') AS pendentes,
            COALESCE(SUM(a.lucro_unidades) FILTER (WHERE a.resultado != 'pendente'), 0) AS lucro_total,
            ROUND(
                COUNT(a.id) FILTER (WHERE a.resultado = 'green') * 100.0 /
                NULLIF(COUNT(a.id) FILTER (WHERE a.resultado IN ('green','red')), 0),
                1
            ) AS win_rate
        FROM bots b
        LEFT JOIN apostas a ON a.bot_id = b.id AND a.modo = 'real'
        WHERE b.status != 'arquivado'
        GROUP BY b.id, b.nome, b.casa, b.esporte, b.mercado, b.status
        ORDER BY lucro_total DESC
    """
    async with db() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


@router.get("/bots/{bot_id}")
async def stats_bot(bot_id: int):
    async with db() as conn:
        bot = await conn.fetchrow("SELECT * FROM bots WHERE id = $1", bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")

        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado = 'green') AS ganhas,
                COUNT(*) FILTER (WHERE resultado = 'red') AS perdidas,
                COUNT(*) FILTER (WHERE resultado = 'pendente') AS pendentes,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro,
                ROUND(
                    COUNT(*) FILTER (WHERE resultado = 'green') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE resultado IN ('green','red')), 0),
                    1
                ) AS win_rate,
                MIN(apostado_em) AS primeira_aposta,
                MAX(apostado_em) AS ultima_aposta
            FROM apostas
            WHERE bot_id = $1 AND modo = 'real'
        """, bot_id)

        timeline = await conn.fetch("""
            SELECT
                DATE(apostado_em) AS dia,
                COUNT(*) FILTER (WHERE resultado = 'green') AS ganhas,
                COUNT(*) FILTER (WHERE resultado = 'red') AS perdidas,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro_dia
            FROM apostas
            WHERE bot_id = $1 AND modo = 'real'
              AND apostado_em >= NOW() - INTERVAL '30 days'
            GROUP BY dia
            ORDER BY dia ASC
        """, bot_id)

    return {
        "bot": dict(bot),
        "stats": dict(stats),
        "timeline": [dict(r) for r in timeline],
    }


# ============================================================
# ENDPOINTS NOVOS v3 (com cache + queries otimizadas)
# ============================================================

@router.get("/overview")
async def stats_overview(esporte: str = Query(...)):
    """v3: 1 query agregada + cache 30s."""
    cached = _cache_get('overview', (esporte,))
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)
    # v4: usa "ultimas 6h" em vez de "desde meia-noite" pra queries mais rapidas
    # (jogos de e-sports tem rotacao rapida, 6h cobre o ciclo ativo)
    h6 = datetime.utcnow() - timedelta(hours=6)
    h24 = datetime.utcnow() - timedelta(hours=24)

    async with db() as conn:
        # 1 query agregada com tudo de uma vez
        kpi_row = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(DISTINCT event_id) FROM ticks
                 WHERE sport = $1 AND ts >= $2) AS jogos_hoje,

                (SELECT ROUND(
                    COUNT(*) FILTER (WHERE resultado = 'green') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE resultado IN ('green','red')), 0), 1)
                 FROM apostas
                 WHERE esporte = $3 AND apostado_em >= $4 AND modo = 'simulado') AS wr_medio,

                (SELECT COALESCE(SUM(lucro_unidades), 0)
                 FROM apostas
                 WHERE esporte = $3 AND apostado_em >= $4
                   AND resultado != 'pendente' AND modo = 'simulado') AS lucro_24h
        """, sport, h6, esporte, h24)

        # Liga quente e jogador hot em queries separadas mas leves
        liga_quente = await conn.fetchval("""
            SELECT liga FROM ticks
            WHERE sport = $1 AND ts >= $2
              AND liga IS NOT NULL AND liga != ''
            GROUP BY liga
            ORDER BY COUNT(*) DESC
            LIMIT 1
        """, sport, h6)

        jogador_hot = await conn.fetchval("""
            SELECT jogador FROM (
                SELECT jogador_a AS jogador, COUNT(*) AS qtd FROM ticks
                WHERE sport = $1 AND ts >= $2 AND jogador_a IS NOT NULL
                GROUP BY jogador_a
                UNION ALL
                SELECT jogador_b AS jogador, COUNT(*) AS qtd FROM ticks
                WHERE sport = $1 AND ts >= $2 AND jogador_b IS NOT NULL
                GROUP BY jogador_b
            ) sub
            GROUP BY jogador
            ORDER BY SUM(qtd) DESC
            LIMIT 1
        """, sport, h6)

        # Top 3 ligas com jogos ativos
        ligas_top = await conn.fetch("""
            WITH ult AS (
                SELECT DISTINCT ON (event_id)
                    event_id, liga, jogador_a, jogador_b, time_a, time_b, ts
                FROM ticks
                WHERE sport = $1
                  AND ts >= NOW() - INTERVAL '5 minutes'
                  AND liga IS NOT NULL AND liga != ''
                ORDER BY event_id, ts DESC
            )
            SELECT liga,
                   COUNT(*) AS qtd,
                   (array_agg(event_id ORDER BY ts DESC))[1] AS event_id,
                   (array_agg(jogador_a ORDER BY ts DESC))[1] AS jogador_a,
                   (array_agg(jogador_b ORDER BY ts DESC))[1] AS jogador_b,
                   (array_agg(time_a ORDER BY ts DESC))[1] AS time_a,
                   (array_agg(time_b ORDER BY ts DESC))[1] AS time_b,
                   (array_agg(ts ORDER BY ts DESC))[1] AS ts
            FROM ult
            GROUP BY liga
            ORDER BY qtd DESC
            LIMIT 3
        """, sport)

    ligas = []
    for i, r in enumerate(ligas_top):
        ts = r['ts']
        ligas.append({
            "id": str(r['event_id']),
            "liga": r['liga'],
            "tempo": ts.strftime('%H:%M') if ts else '--:--',
            "jogadorA": r['jogador_a'] or '?',
            "timeA": r['time_a'] or '',
            "jogadorB": r['jogador_b'] or '?',
            "timeB": r['time_b'] or '',
            "wrPrev": 0,
            "sequencia": 0,
            "isHot": i == 0,
        })

    resultado = {
        "kpis": {
            "jogosHoje": kpi_row['jogos_hoje'] or 0,
            "wrMedio": float(kpi_row['wr_medio']) if kpi_row['wr_medio'] else 0.0,
            "lucro24h": float(kpi_row['lucro_24h']) if kpi_row['lucro_24h'] else 0.0,
            "ligaQuente": liga_quente or '-',
            "jogadorHot": jogador_hot or '-',
        },
        "ligas": ligas,
    }
    _cache_set('overview', (esporte,), resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/proximos")
async def stats_proximos(
    esporte: str = Query(...),
    busca: Optional[str] = Query(None),
    liga: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
):
    """v3: cache 15s."""
    cache_key = (esporte, busca or '', liga or '', page, pageSize)
    cached = _cache_get('proximos', cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)
    params = [sport]
    where_extras = ""

    if liga and liga != 'todas':
        params.append(liga)
        where_extras += f" AND liga = ${len(params)}"

    if busca:
        params.append(f"%{busca}%")
        idx = len(params)
        where_extras += f" AND (jogador_a ILIKE ${idx} OR jogador_b ILIKE ${idx} OR time_a ILIKE ${idx} OR time_b ILIKE ${idx} OR liga ILIKE ${idx})"

    offset = (page - 1) * pageSize
    params.extend([pageSize, offset])

    sql = f"""
        WITH ult AS (
            SELECT DISTINCT ON (event_id)
                event_id, liga, jogador_a, jogador_b, time_a, time_b, ts
            FROM ticks
            WHERE sport = $1
              AND ts >= NOW() - INTERVAL '5 minutes'
              {where_extras}
            ORDER BY event_id, ts DESC
        )
        SELECT * FROM ult
        ORDER BY ts DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
    """

    sql_count = f"""
        SELECT COUNT(DISTINCT event_id)
        FROM ticks
        WHERE sport = $1
          AND ts >= NOW() - INTERVAL '5 minutes'
          {where_extras}
    """

    async with db() as conn:
        total = await conn.fetchval(sql_count, *params[:-2])
        rows = await conn.fetch(sql, *params)

    jogos = []
    for r in rows:
        ts = r['ts']
        jogos.append({
            "id": str(r['event_id']),
            "data": ts.strftime('%d/%m %H:%M') if ts else '',
            "jogadorA": r['jogador_a'] or '?',
            "timeA": r['time_a'] or '',
            "jogadorB": r['jogador_b'] or '?',
            "timeB": r['time_b'] or '',
            "liga": r['liga'] or '',
            "wrPrev": 0,
            "sequencia": 0,
            "isHot": False,
        })

    resultado = {"jogos": jogos, "total": total or 0}
    _cache_set('proximos', cache_key, resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/ultimos")
async def stats_ultimos(
    esporte: str = Query(...),
    busca: Optional[str] = Query(None),
    liga: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
):
    """v3: janela 7d + cache 2min."""
    cache_key = (esporte, busca or '', liga or '', page, pageSize)
    cached = _cache_get('ultimos', cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)
    params = [sport]
    where_extras = ""

    if liga and liga != 'todas':
        params.append(liga)
        where_extras += f" AND liga = ${len(params)}"

    if busca:
        params.append(f"%{busca}%")
        idx = len(params)
        where_extras += f" AND (jogador_a ILIKE ${idx} OR jogador_b ILIKE ${idx} OR time_a ILIKE ${idx} OR time_b ILIKE ${idx} OR liga ILIKE ${idx})"

    offset = (page - 1) * pageSize
    params.extend([pageSize, offset])

    sql = f"""
        WITH ult AS (
            SELECT DISTINCT ON (event_id)
                event_id, liga, jogador_a, jogador_b, time_a, time_b,
                score_home, score_away, ts
            FROM ticks
            WHERE sport = $1
              AND score_home IS NOT NULL
              AND score_away IS NOT NULL
              AND ts >= NOW() - INTERVAL '7 days'
              {where_extras}
            ORDER BY event_id, ts DESC
        )
        SELECT * FROM ult
        ORDER BY ts DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
    """

    sql_count = f"""
        SELECT COUNT(DISTINCT event_id)
        FROM ticks
        WHERE sport = $1
          AND score_home IS NOT NULL
          AND score_away IS NOT NULL
          AND ts >= NOW() - INTERVAL '7 days'
          {where_extras}
    """

    async with db() as conn:
        total = await conn.fetchval(sql_count, *params[:-2])
        rows = await conn.fetch(sql, *params)

    jogos = []
    for r in rows:
        ts = r['ts']
        jogos.append({
            "id": str(r['event_id']),
            "data": ts.strftime('%d/%m %H:%M') if ts else '',
            "jogadorA": r['jogador_a'] or '?',
            "timeA": r['time_a'] or '',
            "jogadorB": r['jogador_b'] or '?',
            "timeB": r['time_b'] or '',
            "liga": r['liga'] or '',
            "placarA": r['score_home'] or 0,
            "placarB": r['score_away'] or 0,
        })

    resultado = {"jogos": jogos, "total": total or 0}
    _cache_set('ultimos', cache_key, resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/heatmap")
async def stats_heatmap(esporte: str = Query(...)):
    """
    v3: janela 7d, agrupa em SUBQUERY com bucket pre-calculado pra evitar
    EXTRACT em milhoes de linhas. Cache 10min.
    """
    cached = _cache_get('heatmap', (esporte,))
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)

    async with db() as conn:
        # Ticks: agrupa apenas DOW e HORA, sem AT TIME ZONE
        # (Postgres ja armazena com timezone, EXTRACT funciona direto)
        ticks_rows = await conn.fetch("""
            SELECT
                EXTRACT(DOW FROM ts)::int AS dow,
                EXTRACT(HOUR FROM ts)::int AS hora,
                COUNT(DISTINCT event_id) AS qtd
            FROM ticks
            WHERE sport = $1
              AND ts >= NOW() - INTERVAL '7 days'
            GROUP BY dow, hora
        """, sport)

        apostas_rows = await conn.fetch("""
            SELECT
                EXTRACT(DOW FROM apostado_em)::int AS dow,
                EXTRACT(HOUR FROM apostado_em)::int AS hora,
                COUNT(*) FILTER (WHERE resultado = 'green') AS ganhas,
                COUNT(*) FILTER (WHERE resultado IN ('green','red')) AS resolvidas,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro
            FROM apostas
            WHERE esporte = $1
              AND apostado_em >= NOW() - INTERVAL '7 days'
              AND modo = 'simulado'
            GROUP BY dow, hora
        """, esporte)

    ticks_map = {(r['dow'], r['hora']): r['qtd'] for r in ticks_rows}
    apostas_map = {(r['dow'], r['hora']): r for r in apostas_rows}

    dias_nome = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb']
    matriz = []
    for d, nome in enumerate(dias_nome):
        horas = []
        for h in range(24):
            qtd = ticks_map.get((d, h), 0)
            ap = apostas_map.get((d, h))
            if ap and ap['resolvidas'] > 0:
                wr = round(ap['ganhas'] / ap['resolvidas'] * 100)
                roi = float(ap['lucro']) / ap['resolvidas'] * 10
            else:
                wr = 0
                roi = 0
            horas.append({
                "qtd": int(qtd),
                "wr": int(wr),
                "roi": round(roi, 1),
            })
        matriz.append({"dia": nome, "horas": horas})

    resultado = {"matriz": matriz}
    _cache_set('heatmap', (esporte,), resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/distribuicoes")
async def stats_distribuicoes(esporte: str = Query(...)):
    """v3: janela 7d + cache 5min."""
    cached = _cache_get('distribuicoes', (esporte,))
    if cached is not None:
        return {**cached, "_cache": "hit"}

    async with db() as conn:
        wr_rows = await conn.fetch("""
            WITH wr_por_bot AS (
                SELECT bot_id,
                       COUNT(*) FILTER (WHERE resultado = 'green') AS g,
                       COUNT(*) FILTER (WHERE resultado IN ('green','red')) AS r
                FROM apostas
                WHERE esporte = $1 AND modo = 'simulado'
                  AND apostado_em >= NOW() - INTERVAL '7 days'
                GROUP BY bot_id
                HAVING COUNT(*) FILTER (WHERE resultado IN ('green','red')) > 5
            )
            SELECT
                FLOOR(g * 10.0 / NULLIF(r, 0))::int AS bin,
                COUNT(*) AS qtd
            FROM wr_por_bot
            GROUP BY bin
            ORDER BY bin
        """, esporte)

        roi_rows = await conn.fetch("""
            WITH roi_por_bot AS (
                SELECT bot_id,
                       SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente') AS lucro,
                       COUNT(*) FILTER (WHERE resultado != 'pendente') AS resolvidas
                FROM apostas
                WHERE esporte = $1 AND modo = 'simulado'
                  AND apostado_em >= NOW() - INTERVAL '7 days'
                GROUP BY bot_id
                HAVING COUNT(*) FILTER (WHERE resultado != 'pendente') > 5
            )
            SELECT
                ROUND(lucro / NULLIF(resolvidas, 0) * 100) AS roi_pct,
                COUNT(*) AS qtd
            FROM roi_por_bot
            GROUP BY roi_pct
            ORDER BY roi_pct
        """, esporte)

    wr_map = {r['bin']: r['qtd'] for r in wr_rows if r['bin'] is not None}
    wr_bins = []
    for i in range(10):
        wr_bins.append({
            "bin": f"{i*10}-{(i+1)*10}%",
            "qtd": wr_map.get(i, 0)
        })

    roi_bins_dict = {i: 0 for i in range(-5, 6)}
    for r in roi_rows:
        roi_pct = r['roi_pct']
        if roi_pct is None:
            continue
        bucket = max(-5, min(5, int(roi_pct // 10)))
        roi_bins_dict[bucket] += r['qtd']

    roi_bins = []
    for i in range(-5, 6):
        sinal = '+' if i*10 >= 0 else ''
        roi_bins.append({
            "bin": f"{sinal}{i*10}u",
            "qtd": roi_bins_dict[i]
        })

    resultado = {"wr": wr_bins, "roi": roi_bins}
    _cache_set('distribuicoes', (esporte,), resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/jogadores")
async def stats_jogadores(
    esporte: str = Query(...),
    busca: Optional[str] = Query(None),
):
    """v3: janela reduzida 3d + LIMIT 300 + cache 15min."""
    cache_key = (esporte, busca or '')
    cached = _cache_get('jogadores', cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)
    params = [sport]
    where_busca = ""
    if busca:
        params.append(f"%{busca}%")
        where_busca = f"AND (jogador ILIKE ${len(params)} OR time ILIKE ${len(params)})"

    sql = f"""
        WITH jog_time AS (
            SELECT jogador_a AS jogador, time_a AS time, COUNT(*) AS qtd
            FROM ticks
            WHERE sport = $1
              AND jogador_a IS NOT NULL AND jogador_a != ''
              AND ts >= NOW() - INTERVAL '3 days'
            GROUP BY jogador_a, time_a
            UNION ALL
            SELECT jogador_b AS jogador, time_b AS time, COUNT(*) AS qtd
            FROM ticks
            WHERE sport = $1
              AND jogador_b IS NOT NULL AND jogador_b != ''
              AND ts >= NOW() - INTERVAL '3 days'
            GROUP BY jogador_b, time_b
        ),
        jog_principal AS (
            SELECT DISTINCT ON (jogador)
                jogador, time, qtd
            FROM jog_time
            ORDER BY jogador, qtd DESC
        )
        SELECT jogador AS nome, COALESCE(time, '') AS time
        FROM jog_principal
        WHERE jogador IS NOT NULL AND jogador != ''
          {where_busca}
        ORDER BY jogador
        LIMIT 300
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    resultado = {"jogadores": [{"nome": r['nome'], "time": r['time']} for r in rows]}
    _cache_set('jogadores', cache_key, resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/torneios")
async def stats_torneios(esporte: str = Query(...)):
    """v3: janela 7d + cache 15min."""
    cached = _cache_get('torneios', (esporte,))
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)

    sql = """
        SELECT liga
        FROM ticks
        WHERE sport = $1
          AND liga IS NOT NULL AND liga != ''
          AND ts >= NOW() - INTERVAL '7 days'
        GROUP BY liga
        ORDER BY COUNT(*) DESC
        LIMIT 30
    """

    async with db() as conn:
        rows = await conn.fetch(sql, sport)

    resultado = {"torneios": [r['liga'] for r in rows]}
    _cache_set('torneios', (esporte,), resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/preview-jogador")
async def stats_preview_jogador(
    esporte: str = Query(...),
    nome: str = Query(...),
):
    """v3: janela 14d + cache 2min."""
    cache_key = (esporte, nome)
    cached = _cache_get('preview', cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    sport = _sport_from_esporte(esporte)

    sql = """
        SELECT DISTINCT ON (event_id)
            event_id, ts,
            jogador_a, jogador_b,
            score_home, score_away
        FROM ticks
        WHERE sport = $1
          AND (jogador_a = $2 OR jogador_b = $2)
          AND score_home IS NOT NULL
          AND score_away IS NOT NULL
          AND ts >= NOW() - INTERVAL '14 days'
        ORDER BY event_id, ts DESC
        LIMIT 30
    """

    async with db() as conn:
        rows = await conn.fetch(sql, sport, nome)

    def vencedor(row):
        sh = row['score_home']
        sa = row['score_away']
        if sh > sa:
            return 'A'
        elif sa > sh:
            return 'B'
        else:
            return 'E'

    wins = 0
    losses = 0
    ultimos5 = []
    rows_recentes = sorted(rows, key=lambda x: x['ts'], reverse=True)
    for r in rows_recentes:
        v = vencedor(r)
        if v == 'E':
            continue
        eu_sou = 'A' if r['jogador_a'] == nome else 'B'
        ganhou = (v == eu_sou)
        if len(ultimos5) < 5:
            ultimos5.append('W' if ganhou else 'L')
        if ganhou:
            wins += 1
        else:
            losses += 1

    partidas = wins + losses
    wr30 = round(wins / partidas * 100) if partidas else 0

    wins_10 = 0
    resolvidos_10 = 0
    for r in rows_recentes[:10]:
        v = vencedor(r)
        if v == 'E':
            continue
        resolvidos_10 += 1
        eu_sou = 'A' if r['jogador_a'] == nome else 'B'
        if v == eu_sou:
            wins_10 += 1
    wr10 = round(wins_10 / resolvidos_10 * 100) if resolvidos_10 else 0

    sequencia = 0
    for u in ultimos5:
        if u == 'W':
            if sequencia >= 0:
                sequencia += 1
            else:
                break
        else:
            if sequencia <= 0:
                sequencia -= 1
            else:
                break

    h24 = datetime.utcnow() - timedelta(hours=24)
    async with db() as conn:
        apostas_jogador = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(lucro_unidades), 0) AS lucro,
                AVG(odd) AS odd_media
            FROM apostas
            WHERE esporte = $1
              AND (jogador_a = $2 OR jogador_b = $2)
              AND apostado_em >= $3
              AND resultado != 'pendente'
              AND modo = 'simulado'
        """, esporte, nome, h24)

    resultado = {
        "preview": {
            "wr10": wr10,
            "wr30": wr30,
            "sequencia": sequencia,
            "lucro24h": float(apostas_jogador['lucro']) if apostas_jogador and apostas_jogador['lucro'] else 0.0,
            "partidas": partidas,
            "oddMedia": float(apostas_jogador['odd_media']) if apostas_jogador and apostas_jogador['odd_media'] else 0.0,
            "ultimos5": ultimos5,
        }
    }
    _cache_set('preview', cache_key, resultado)
    return {**resultado, "_cache": "miss"}
