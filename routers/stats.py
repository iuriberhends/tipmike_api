"""
routers/stats.py - v2

ENDPOINTS ANTIGOS (mantidos):
- GET /stats/dashboard          → resumo geral do sistema
- GET /stats/bots               → performance agregada de todos os bots
- GET /stats/bots/{id}          → performance detalhada de um bot

ENDPOINTS NOVOS (v2 - pra tela Stats.jsx do frontend):
- GET /stats/overview           → KPIs do topo da tela (jogos hoje, WR medio, ligas, etc)
- GET /stats/proximos           → proximos jogos (futuros) por esporte
- GET /stats/ultimos            → ultimos jogos (com placar) por esporte
- GET /stats/heatmap            → atividade 7 dias x 24 horas
- GET /stats/distribuicoes      → histogramas de WR e ROI
- GET /stats/jogadores          → lista jogadores por esporte (dropdown comparador)
- GET /stats/torneios           → lista torneios por esporte
- GET /stats/preview-jogador    → preview WR/sequencia/oddmedia de um jogador

CONVENCAO esporte:
- 'nba2k'        → sport = 'E-Basketball'
- 'fifa'         → sport = 'E-Football'
- 'ehockey'      → sport = 'E-Hockey'
- 'etennis'      → sport = 'E-Tennis'

CONVENCAO esporte_bot (na tabela bots):
- 'nba2k', 'fifa', 'ehockey', 'etennis' (mesmo formato)
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime, timezone, timedelta
from database import db

router = APIRouter(prefix="/stats", tags=["Stats"])

# Mapeamento esporte (id frontend) -> sport (campo no banco)
ESPORTE_TO_SPORT = {
    'nba2k':   'E-Basketball',
    'fifa':    'E-Football',
    'ehockey': 'E-Hockey',
    'etennis': 'E-Tennis',
}


def _sport_from_esporte(esporte: str) -> str:
    """Converte 'fifa' -> 'E-Football'. Se nao mapear, retorna o proprio (caso o frontend mande direto)."""
    return ESPORTE_TO_SPORT.get(esporte, esporte)


# ============================================================
# ENDPOINTS ANTIGOS (sistema/bots) - mantidos sem mudanca
# ============================================================

@router.get("/dashboard")
async def dashboard():
    """Resumo geral do sistema."""
    agora = datetime.now(timezone.utc)
    inicio_hoje = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    async with db() as conn:
        bots_ativos = await conn.fetchval("SELECT COUNT(*) FROM bots WHERE status = 'ativo'")
        bots_total = await conn.fetchval("SELECT COUNT(*) FROM bots WHERE status != 'arquivado'")
        apostas_hoje = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1", inicio_hoje
        )
        apostas_pendentes = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE resultado = 'pendente'"
        )
        lucro_hoje = await conn.fetchval(
            "SELECT COALESCE(SUM(lucro_unidades), 0) FROM apostas WHERE apostado_em >= $1 AND resultado != 'pendente'",
            inicio_hoje
        )
        ganhas_hoje = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1 AND resultado = 'ganhou'",
            inicio_hoje
        )
        resolvidas_hoje = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1 AND resultado != 'pendente'",
            inicio_hoje
        )
        uma_hora_atras = agora - timedelta(hours=1)
        ticks_ultima_hora = await conn.fetchval(
            "SELECT COUNT(*) FROM ticks WHERE ts >= $1", uma_hora_atras
        )
        bookmakers_rows = await conn.fetch(
            "SELECT DISTINCT bookmaker FROM ticks WHERE ts >= $1 ORDER BY bookmaker",
            uma_hora_atras
        )

    win_rate = round((ganhas_hoje / resolvidas_hoje * 100), 1) if resolvidas_hoje > 0 else None

    return {
        "bots_ativos": bots_ativos,
        "bots_total": bots_total,
        "apostas_hoje": apostas_hoje,
        "lucro_hoje": float(lucro_hoje) if lucro_hoje else 0.0,
        "win_rate_hoje": win_rate,
        "apostas_pendentes": apostas_pendentes,
        "ticks_ultima_hora": ticks_ultima_hora,
        "bookmakers_ativos": [r["bookmaker"] for r in bookmakers_rows],
        "atualizado_em": agora.isoformat(),
    }


@router.get("/bots")
async def stats_todos_bots():
    """Performance agregada de todos os bots."""
    sql = """
        SELECT
            b.id, b.nome, b.casa, b.esporte, b.mercado, b.status,
            COUNT(a.id) AS total_apostas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'ganhou') AS ganhas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'perdeu') AS perdidas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'pendente') AS pendentes,
            COALESCE(SUM(a.lucro_unidades) FILTER (WHERE a.resultado != 'pendente'), 0) AS lucro_total,
            ROUND(
                COUNT(a.id) FILTER (WHERE a.resultado = 'ganhou') * 100.0 /
                NULLIF(COUNT(a.id) FILTER (WHERE a.resultado IN ('ganhou','perdeu')), 0),
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
    """Performance detalhada de um bot."""
    async with db() as conn:
        bot = await conn.fetchrow("SELECT * FROM bots WHERE id = $1", bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")

        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado = 'ganhou') AS ganhas,
                COUNT(*) FILTER (WHERE resultado = 'perdeu') AS perdidas,
                COUNT(*) FILTER (WHERE resultado = 'pendente') AS pendentes,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro,
                ROUND(
                    COUNT(*) FILTER (WHERE resultado = 'ganhou') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE resultado IN ('ganhou','perdeu')), 0),
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
                COUNT(*) FILTER (WHERE resultado = 'ganhou') AS ganhas,
                COUNT(*) FILTER (WHERE resultado = 'perdeu') AS perdidas,
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
# ENDPOINTS NOVOS v2 - tela Stats.jsx
# ============================================================

@router.get("/overview")
async def stats_overview(esporte: str = Query(...)):
    """
    KPIs do topo da tela Stats.jsx.

    Retorna:
    {
      kpis: { jogosHoje, wrMedio, lucro24h, ligaQuente, jogadorHot },
      ligas: [{ id, liga, tempo, jogadorA, timeA, jogadorB, timeB, wrPrev, sequencia, isHot }, ...]
    }
    """
    sport = _sport_from_esporte(esporte)
    inicio_hoje = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    agora = datetime.now(timezone.utc)
    h24 = agora - timedelta(hours=24)

    async with db() as conn:
        # jogosHoje: distintos event_id com ticks hoje pra esse esporte
        jogos_hoje = await conn.fetchval("""
            SELECT COUNT(DISTINCT event_id)
            FROM ticks
            WHERE sport = $1
              AND ts >= $2
        """, sport, inicio_hoje)

        # wrMedio: WR das apostas dos bots desse esporte nas ultimas 24h
        wr_medio = await conn.fetchval("""
            SELECT ROUND(
                COUNT(*) FILTER (WHERE resultado = 'ganhou') * 100.0 /
                NULLIF(COUNT(*) FILTER (WHERE resultado IN ('ganhou','perdeu')), 0),
                1
            )
            FROM apostas
            WHERE bot_esporte = $1
              AND apostado_em >= $2
              AND modo = 'simulado'
        """, esporte, h24)

        # lucro 24h
        lucro_24h = await conn.fetchval("""
            SELECT COALESCE(SUM(lucro_unidades), 0)
            FROM apostas
            WHERE bot_esporte = $1
              AND apostado_em >= $2
              AND resultado != 'pendente'
              AND modo = 'simulado'
        """, esporte, h24)

        # liga quente: liga com mais ticks hoje
        liga_quente_row = await conn.fetchrow("""
            SELECT liga, COUNT(*) AS qtd
            FROM ticks
            WHERE sport = $1
              AND ts >= $2
              AND liga IS NOT NULL
              AND liga != ''
            GROUP BY liga
            ORDER BY qtd DESC
            LIMIT 1
        """, sport, inicio_hoje)

        # jogador hot: jogador com mais ticks hoje
        jogador_hot_row = await conn.fetchrow("""
            SELECT jogador, COUNT(*) AS qtd FROM (
                SELECT jogador_a AS jogador FROM ticks
                WHERE sport = $1 AND ts >= $2 AND jogador_a IS NOT NULL
                UNION ALL
                SELECT jogador_b AS jogador FROM ticks
                WHERE sport = $1 AND ts >= $2 AND jogador_b IS NOT NULL
            ) sub
            GROUP BY jogador
            ORDER BY qtd DESC
            LIMIT 1
        """, sport, inicio_hoje)

        # Top 3 ligas com proximos jogos (no momento)
        ligas_top = await conn.fetch("""
            WITH proximos_por_event AS (
                SELECT DISTINCT ON (event_id)
                    event_id, liga, jogador_a, jogador_b, time_a, time_b, ts
                FROM ticks
                WHERE sport = $1
                  AND ts >= NOW() - INTERVAL '5 minutes'
                  AND liga IS NOT NULL
                  AND liga != ''
                ORDER BY event_id, ts DESC
            )
            SELECT liga, COUNT(*) AS qtd_jogos,
                   (array_agg(event_id ORDER BY ts DESC))[1] AS event_id_recente,
                   (array_agg(jogador_a ORDER BY ts DESC))[1] AS jogador_a,
                   (array_agg(jogador_b ORDER BY ts DESC))[1] AS jogador_b,
                   (array_agg(time_a ORDER BY ts DESC))[1] AS time_a,
                   (array_agg(time_b ORDER BY ts DESC))[1] AS time_b,
                   (array_agg(ts ORDER BY ts DESC))[1] AS ts
            FROM proximos_por_event
            GROUP BY liga
            ORDER BY qtd_jogos DESC
            LIMIT 3
        """, sport)

    ligas = []
    for i, r in enumerate(ligas_top):
        ts = r['ts']
        tempo = ts.strftime('%H:%M') if ts else '--:--'
        ligas.append({
            "id": str(r['event_id_recente']),
            "liga": r['liga'],
            "tempo": tempo,
            "jogadorA": r['jogador_a'] or '?',
            "timeA": r['time_a'] or '',
            "jogadorB": r['jogador_b'] or '?',
            "timeB": r['time_b'] or '',
            "wrPrev": 0,  # TODO: calcular com H2H se necessario
            "sequencia": 0,
            "isHot": i == 0,
        })

    return {
        "kpis": {
            "jogosHoje": jogos_hoje or 0,
            "wrMedio": float(wr_medio) if wr_medio else 0.0,
            "lucro24h": float(lucro_24h) if lucro_24h else 0.0,
            "ligaQuente": liga_quente_row['liga'] if liga_quente_row else '-',
            "jogadorHot": jogador_hot_row['jogador'] if jogador_hot_row else '-',
        },
        "ligas": ligas,
    }


@router.get("/proximos")
async def stats_proximos(
    esporte: str = Query(...),
    busca: Optional[str] = Query(None),
    liga: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
):
    """
    Lista jogos atualmente ativos (ticks nos ultimos 5 min) pra esse esporte.
    Nota: a Betano/casas nao tem 'proximos' agendados, entao a definicao
    aqui eh 'jogos com atividade recente'.
    """
    sport = _sport_from_esporte(esporte)

    params = [sport]
    where_extras = ""

    if liga and liga != 'todas':
        params.append(liga)
        where_extras += f" AND liga = ${len(params)}"

    if busca:
        params.append(f"%{busca}%")
        idx = len(params)
        where_extras += f"""
            AND (
                jogador_a ILIKE ${idx}
                OR jogador_b ILIKE ${idx}
                OR time_a ILIKE ${idx}
                OR time_b ILIKE ${idx}
                OR liga ILIKE ${idx}
            )
        """

    # Conta total
    sql_count = f"""
        SELECT COUNT(DISTINCT event_id)
        FROM ticks
        WHERE sport = $1
          AND ts >= NOW() - INTERVAL '5 minutes'
          {where_extras}
    """

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

    return {"jogos": jogos, "total": total or 0}


@router.get("/ultimos")
async def stats_ultimos(
    esporte: str = Query(...),
    busca: Optional[str] = Query(None),
    liga: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
):
    """Jogos passados (com placar) pra esse esporte."""
    sport = _sport_from_esporte(esporte)

    params = [sport]
    where_extras = ""

    if liga and liga != 'todas':
        params.append(liga)
        where_extras += f" AND liga = ${len(params)}"

    if busca:
        params.append(f"%{busca}%")
        idx = len(params)
        where_extras += f"""
            AND (
                jogador_a ILIKE ${idx}
                OR jogador_b ILIKE ${idx}
                OR time_a ILIKE ${idx}
                OR time_b ILIKE ${idx}
                OR liga ILIKE ${idx}
            )
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

    return {"jogos": jogos, "total": total or 0}


@router.get("/heatmap")
async def stats_heatmap(esporte: str = Query(...)):
    """
    Atividade 7 dias x 24 horas. Retorna matriz pra cada dia da semana,
    com qtd de jogos, WR medio e ROI nas apostas daquela hora.
    """
    sport = _sport_from_esporte(esporte)

    # Volume de jogos por (dia_semana, hora)
    async with db() as conn:
        ticks_rows = await conn.fetch("""
            SELECT
                EXTRACT(DOW FROM ts AT TIME ZONE 'America/Sao_Paulo')::int AS dow,
                EXTRACT(HOUR FROM ts AT TIME ZONE 'America/Sao_Paulo')::int AS hora,
                COUNT(DISTINCT event_id) AS qtd
            FROM ticks
            WHERE sport = $1
              AND ts >= NOW() - INTERVAL '14 days'
            GROUP BY dow, hora
        """, sport)

        apostas_rows = await conn.fetch("""
            SELECT
                EXTRACT(DOW FROM apostado_em AT TIME ZONE 'America/Sao_Paulo')::int AS dow,
                EXTRACT(HOUR FROM apostado_em AT TIME ZONE 'America/Sao_Paulo')::int AS hora,
                COUNT(*) FILTER (WHERE resultado = 'ganhou') AS ganhas,
                COUNT(*) FILTER (WHERE resultado IN ('ganhou','perdeu')) AS resolvidas,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro
            FROM apostas
            WHERE bot_esporte = $1
              AND apostado_em >= NOW() - INTERVAL '14 days'
              AND modo = 'simulado'
            GROUP BY dow, hora
        """, esporte)

    # Indexa por (dow, hora)
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
                "qtd": qtd,
                "wr": wr,
                "roi": round(roi, 1),
            })
        matriz.append({"dia": nome, "horas": horas})

    return {"matriz": matriz}


@router.get("/distribuicoes")
async def stats_distribuicoes(esporte: str = Query(...)):
    """
    Histogramas de WR e ROI das apostas desse esporte.
    """
    async with db() as conn:
        wr_rows = await conn.fetch("""
            WITH wr_por_bot AS (
                SELECT bot_id,
                       COUNT(*) FILTER (WHERE resultado = 'ganhou') AS g,
                       COUNT(*) FILTER (WHERE resultado IN ('ganhou','perdeu')) AS r
                FROM apostas
                WHERE bot_esporte = $1 AND modo = 'simulado'
                  AND apostado_em >= NOW() - INTERVAL '30 days'
                GROUP BY bot_id
                HAVING COUNT(*) FILTER (WHERE resultado IN ('ganhou','perdeu')) > 5
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
                WHERE bot_esporte = $1 AND modo = 'simulado'
                  AND apostado_em >= NOW() - INTERVAL '30 days'
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

    # Monta bins fixos de 10% pra WR
    wr_map = {r['bin']: r['qtd'] for r in wr_rows if r['bin'] is not None}
    wr_bins = []
    for i in range(10):
        wr_bins.append({
            "bin": f"{i*10}-{(i+1)*10}%",
            "qtd": wr_map.get(i, 0)
        })

    # Bins de 10u pra ROI (-50 a +50)
    roi_bins_dict = {i: 0 for i in range(-5, 6)}
    for r in roi_rows:
        roi_pct = r['roi_pct']
        if roi_pct is None:
            continue
        # Bucket de 10 em 10
        bucket = max(-5, min(5, int(roi_pct // 10)))
        roi_bins_dict[bucket] += r['qtd']

    roi_bins = []
    for i in range(-5, 6):
        sinal = '+' if i*10 >= 0 else ''
        roi_bins.append({
            "bin": f"{sinal}{i*10}u",
            "qtd": roi_bins_dict[i]
        })

    return {"wr": wr_bins, "roi": roi_bins}


@router.get("/jogadores")
async def stats_jogadores(
    esporte: str = Query(...),
    busca: Optional[str] = Query(None),
):
    """Lista jogadores distintos com seus times (1 time mais frequente por jogador)."""
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
              AND ts >= NOW() - INTERVAL '14 days'
            GROUP BY jogador_a, time_a
            UNION ALL
            SELECT jogador_b AS jogador, time_b AS time, COUNT(*) AS qtd
            FROM ticks
            WHERE sport = $1
              AND jogador_b IS NOT NULL AND jogador_b != ''
              AND ts >= NOW() - INTERVAL '14 days'
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
        LIMIT 500
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return {"jogadores": [{"nome": r['nome'], "time": r['time']} for r in rows]}


@router.get("/torneios")
async def stats_torneios(esporte: str = Query(...)):
    """Lista torneios (ligas) com atividade nos ultimos 14 dias pra esse esporte."""
    sport = _sport_from_esporte(esporte)

    sql = """
        SELECT liga, COUNT(*) AS qtd
        FROM ticks
        WHERE sport = $1
          AND liga IS NOT NULL AND liga != ''
          AND ts >= NOW() - INTERVAL '14 days'
        GROUP BY liga
        ORDER BY qtd DESC
        LIMIT 30
    """

    async with db() as conn:
        rows = await conn.fetch(sql, sport)

    return {"torneios": [r['liga'] for r in rows]}


@router.get("/preview-jogador")
async def stats_preview_jogador(
    esporte: str = Query(...),
    nome: str = Query(...),
):
    """
    Preview rapido de um jogador: WR ult10/30, sequencia, lucro 24h, etc.
    Usado pelo comparador H2H da tela Stats.
    """
    sport = _sport_from_esporte(esporte)

    # Pega ultimos 30 jogos do jogador (vencedor = quem fez mais score)
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
          AND ts >= NOW() - INTERVAL '30 days'
        ORDER BY event_id, ts DESC
        LIMIT 30
    """

    async with db() as conn:
        rows = await conn.fetch(sql, sport, nome)

    # Calcula WR
    def vencedor(row):
        sh = row['score_home']
        sa = row['score_away']
        if sh > sa:
            return 'A'  # jogador_a venceu
        elif sa > sh:
            return 'B'  # jogador_b venceu
        else:
            return 'E'  # empate

    wins = 0
    losses = 0
    ultimos5 = []
    for r in sorted(rows, key=lambda x: x['ts'], reverse=True):
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

    # WR ult10
    rows_recentes = sorted(rows, key=lambda x: x['ts'], reverse=True)[:10]
    wins_10 = 0
    resolvidos_10 = 0
    for r in rows_recentes:
        v = vencedor(r)
        if v == 'E':
            continue
        resolvidos_10 += 1
        eu_sou = 'A' if r['jogador_a'] == nome else 'B'
        if v == eu_sou:
            wins_10 += 1
    wr10 = round(wins_10 / resolvidos_10 * 100) if resolvidos_10 else 0

    # Sequencia (streak atual)
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

    # Lucro 24h e odd media nas apostas
    h24 = datetime.now(timezone.utc) - timedelta(hours=24)
    async with db() as conn:
        apostas_jogador = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(lucro_unidades), 0) AS lucro,
                AVG(odd) AS odd_media
            FROM apostas
            WHERE bot_esporte = $1
              AND (jogador_a = $2 OR jogador_b = $2)
              AND apostado_em >= $3
              AND resultado != 'pendente'
              AND modo = 'simulado'
        """, esporte, nome, h24)

    return {
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
