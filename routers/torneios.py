"""routers/torneios.py - torneio + grade + jogadores E times + whitelist/blacklist + cache

v5 - fix: NBA aparecendo em FIFA (e cross-pollination entre esportes)
  - Adiciona ESPORTE_LIGA_BLACKLIST: patterns que NUNCA podem aparecer em cada esporte.
  - Pra fifa: bloqueia '%NBA%', '%Basket%', '%NHL%', '%Hockey%', '%Tennis%', '%ATP%', '%WTA%'.
  - Pra nba2k: bloqueia '%NHL%', '%Hockey%', '%Tennis%', '%ATP%', '%WTA%' e padroes de futebol.
  - Aplica no WHERE como AND NOT (liga LIKE ... OR liga LIKE ...).

v4 - tradutor de IDs:
  - SUPERBET_ID_TO_NAME: converte IDs numericos da Superbet (94993, 89069, etc)
    pra nomes humanos (Cyber Live Arena, EAL - NextGen).
  - BET365_CODE_TO_NAME: converte codigos crus da Bet365 (ESOC-GTL-12MP)
    pra nomes humanos (GT Leagues - 2x6).
  - traduzir_liga() roda antes do classificar_pai() em todas as queries.

v3 - endpoint /disponiveis retorna torneios pai com grades agrupadas.
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime, timedelta
from database import db

router = APIRouter(prefix="/torneios", tags=["Torneios"])

_CACHE: dict = {}
_CACHE_TTL = timedelta(hours=1)

TORNEIO_PATTERNS = {
    "Battle":              "Battle%",
    "GT League":           "GT %",
    "ECF (Volta)":         "%Volta%",
    "Adriatic League":     "Adriatic League%",
    "eAdriatic League":    "eAdriatic%",
    "H2H GG League":       "%H2H GG%",
    "Live Arena":          "%Live Arena%",
    "Champions League":    "%Champions League%",
    "Liga dos Campeões":   "%Liga dos Campeões%",
    "Premier League":      "Premier League",
    "La Liga":             "La Liga",
    "Bundesliga":          "Bundesliga",
    "Serie A":             "Serie A",
    "Ligue 1":             "Ligue 1",
    "Super Lig":           "Super Lig",
    "Europa League":       "Europa League",
    "FC26":                "FC26%",
    "Cup":                 "%Cup%",
    "ESportsBattle":       "ESportsBattle%",
    "Esports Battle":      "Esports Battle%",
    "NBA League":          "NBA%",
    "NHL Esports":         "NHL%",
    "Volta":               "%Volta%",
    "Valhalla":            "Valhalla%",
    "Valkyrie":            "Valkyrie%",
}


ESPORTE_LIGA_PATTERNS = {
    'fifa': [
        '%Battle%', 'GT %', 'GT League', '%Volta%', '%H2H GG%', '%Live Arena%',
        'Valhalla%', 'Valkyrie%', 'FC%', '%ECF%', '%Champions%',
        '%Premier League%', '%La Liga%', '%LaLiga%', '%Bundesliga%', '%Serie A%',
        '%Ligue 1%', '%Super Lig%', '%Europa League%', 'ESportsBattle%',
        'Esports Battle%', '%Cup%', '%Liga dos%', 'EAL%', '%Estrelas%',
    ],
    'nba2k': [
        'NBA%', '%Adriatic%', '%Euroliga%', '%NBA League%',
    ],
    'ehockey': [
        'NHL%', '%Hockey%', '%IIHF%', '%KHL%',
    ],
    'etennis': [
        '%Tennis%', '%ATP%', '%WTA%', '%Grand Slam%', '%Masters%',
    ],
}


# v5: blacklist de patterns que NUNCA podem aparecer no esporte
# (resolve casos de patterns muito genericos tipo '%H2H GG%' pegando 'NBA H2H GG')
ESPORTE_LIGA_BLACKLIST = {
    'fifa': [
        '%NBA%', '%Basket%', '%E-Basket%', '%E Basket%',     # nba2k
        '%NHL%', '%Hockey%', '%E-Hockey%', '%E Hockey%',     # ehockey
        '%Tennis%', '%Tenis%', '%ATP%', '%WTA%',             # etennis
    ],
    'nba2k': [
        '%NHL%', '%Hockey%',
        '%Tennis%', '%Tenis%', '%ATP%', '%WTA%',
        # Padroes claramente de futebol
        '%FIFA%', '%FC %', '%Soccer%', '%E-Football%', '%E Football%',
        '%minutos de jogo%',  # esports de futebol da Betano
    ],
    'ehockey': [
        '%NBA%', '%Basket%',
        '%Tennis%', '%Tenis%', '%ATP%', '%WTA%',
        '%FIFA%', '%FC %', '%Soccer%',
    ],
    'etennis': [
        '%NBA%', '%Basket%',
        '%NHL%', '%Hockey%',
        '%FIFA%', '%FC %', '%Soccer%',
    ],
}


# Ligas que nao tem hifen mas pertencem a um pai
PAIS_ESPECIAIS = {
    'GT League': 'GT',
    'Liga das Estrelas': 'Liga das Estrelas',
    'Live Arena': 'Live Arena',
    'ECF (Volta)': 'ECF (Volta)',
    'Liga dos Campeoes 2x6': 'Liga dos Campeoes',
    'Liga dos Campeões 2x6': 'Liga dos Campeões',
}

# Aliases (apelidos) para exibicao no frontend
# EAL = Adriatic (mesmo torneio com nome diferente em casas distintas)
PAI_ALIASES = {
    'EAL': 'EAL (Adriatic)',
}


# ============================================================
# TRADUTORES DE LIGA - converte IDs/codigos brutos -> nome humano
# ============================================================

SUPERBET_ID_TO_NAME = {
    # FIFA (E-Football)
    "49959": "Battle - Premier League",
    "49964": "Battle - Liga dos Campeões 1 2x4",
    "49965": "Battle - Internacional 1 2x4",
    "49968": "Battle - Europa League 2x4",
    "51264": "Battle - LaLiga 1 2x4",
    "61751": "GT - Liga dos Campeões 1",
    "61753": "GT - Conference League 2x6",
    "61755": "GT - Liga dos Campeões 3 2x6",
    "61756": "GT - Europa League 1 2x6",
    "61757": "GT - Bundesliga 2x6",
    "61758": "GT - Premier League 2x6",
    "67118": "Battle - Bundesliga 2x4",
    "67383": "EAL - Premier League",
    "67400": "EAL - Série A",
    "67380": "EAL - Liga dos Campeões 2x5",
    "67556": "EAL - Premier League 2x5",
    "67892": "EAL - Internacional 2x5",
    "71851": "Liga dos Campeões 2x6",
    "72619": "Battle - Volta Liga dos Campeões 2x3",
    "72621": "Battle - Volta Premier League 2x3",
    "72623": "Battle - Volta Bundesliga 2x3",
    "72624": "Battle - Volta Liga dos Campeões 2x3",
    "80560": "H2H - GG League 2x4",
    "81968": "Battle - Portugal Primera 2x4",
    "81987": "Battle - Portugal Primera 2x4",
    "81988": "Battle - Argentina Super League 2x6",
    "91005": "Tênis Esports",
    "91014": "NHL Esports",
    "91015": "NHL Esports League",
    "94993": "Cyber Live Arena",
    "97337": "Battle - Copa do Mundo 2x4",
    "97693": "Battle - Copa do Mundo B",
    "98257": "National Teams 3x4",
    # NBA (E-Basketball)
    "75124": "Battle - NBA 1",
    "80566": "NBA League 2x4",
    "89069": "EAL - NextGen",
    "92679": "European Conference 4x5",
}

BET365_CODE_TO_NAME = {
    "ESOC-GTL-12MP":   "GT Leagues - 2x6",
    "ESOCH2HGG-8MP":   "H2H GG League - 2x4",
    "ESOCBATVOL-6":    "Battle Volta - 2x3",
    "ESOCCERBATTLE":   "Battle - 2x4",
    "B-EBASKBLITZ4X5": "H2H GG League - 4x5",
    "B-EBASKBAT4X5":   "Battle - 5x5",
}

SUPERBET_ALIASES = {
    "Live Arena": "Cyber Live Arena",
}


def traduzir_liga(bookmaker: str, liga: str) -> str:
    if not liga:
        return liga
    if bookmaker == 'superbet':
        if liga in SUPERBET_ID_TO_NAME:
            return SUPERBET_ID_TO_NAME[liga]
        if liga in SUPERBET_ALIASES:
            return SUPERBET_ALIASES[liga]
    if bookmaker == 'bet365' and liga in BET365_CODE_TO_NAME:
        return BET365_CODE_TO_NAME[liga]
    return liga


SUPERBET_ID_TO_ESPORTE = {
    "75124": "nba2k", "80566": "nba2k", "89069": "nba2k", "92679": "nba2k",
    "91014": "ehockey", "91015": "ehockey",
    "91005": "etennis",
}

BET365_CODE_TO_ESPORTE = {
    "B-EBASKBLITZ4X5": "nba2k",
    "B-EBASKBAT4X5":   "nba2k",
}


def _ids_brutos_para_esporte(casa: str, esporte: str) -> list:
    out = []
    if casa == 'superbet':
        for liga_id in SUPERBET_ID_TO_NAME.keys():
            esp = SUPERBET_ID_TO_ESPORTE.get(liga_id, 'fifa')
            if esp == esporte:
                out.append(liga_id)
    elif casa == 'bet365':
        for codigo in BET365_CODE_TO_NAME.keys():
            esp = BET365_CODE_TO_ESPORTE.get(codigo, 'fifa')
            if esp == esporte:
                out.append(codigo)
    return out



def classificar_pai(nome_liga: str) -> str:
    if not nome_liga:
        return ''
    if nome_liga in PAIS_ESPECIAIS:
        return PAIS_ESPECIAIS[nome_liga]
    if ' - ' in nome_liga:
        return nome_liga.split(' - ')[0].strip()
    return nome_liga


def nome_pai_para_exibicao(pai: str) -> str:
    return PAI_ALIASES.get(pai, pai)


def _cache_get(key):
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, data = entry
    if datetime.now() - ts > _CACHE_TTL:
        del _CACHE[key]
        return None
    return data


def _cache_set(key, data):
    _CACHE[key] = (datetime.now(), data)


async def _buscar_times(filtro_sql: str, params: list):
    sql = f"""
        SELECT DISTINCT time FROM (
            SELECT time_a AS time FROM ticks
            WHERE {filtro_sql}
              AND time_a IS NOT NULL AND time_a != ''
            UNION
            SELECT time_b AS time FROM ticks
            WHERE {filtro_sql}
              AND time_b IS NOT NULL AND time_b != ''
        ) sub
        ORDER BY time
    """
    async with db() as conn:
        rows = await conn.fetch(sql, *params)
    return [r["time"] for r in rows if r["time"]]


@router.get("/disponiveis")
async def listar_torneios_disponiveis(
    casa: str = Query(...),
    esporte: str = Query(...),
    dias: int = Query(7, ge=1, le=30),
    min_ticks: int = Query(100, ge=0),
):
    """v5: filtra cross-pollination com blacklist (NBA nao aparece em FIFA, etc)"""
    cache_key = ("disponiveis_v5", casa, esporte, dias, min_ticks)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    patterns = ESPORTE_LIGA_PATTERNS.get(esporte, [])
    if not patterns:
        raise HTTPException(
            status_code=400,
            detail=f"Esporte '{esporte}' nao suportado. Use: {', '.join(ESPORTE_LIGA_PATTERNS.keys())}"
        )

    placeholders = []
    params = [casa]
    for p in patterns:
        params.append(p)
        placeholders.append(f"liga LIKE ${len(params)}")

    ids_brutos = _ids_brutos_para_esporte(casa, esporte)
    if ids_brutos:
        in_placeholders = []
        for id_bruto in ids_brutos:
            params.append(id_bruto)
            in_placeholders.append(f"${len(params)}")
        placeholders.append(f"liga IN ({','.join(in_placeholders)})")

    where_esporte = " OR ".join(placeholders)

    # v5: blacklist - NBA nao aparece em FIFA, etc
    blacklist_patterns = ESPORTE_LIGA_BLACKLIST.get(esporte, [])
    where_blacklist = ""
    if blacklist_patterns:
        bl_placeholders = []
        for bp in blacklist_patterns:
            params.append(bp)
            bl_placeholders.append(f"liga LIKE ${len(params)}")
        where_blacklist = f"AND NOT ({' OR '.join(bl_placeholders)})"

    sql = f"""
        SELECT liga, COUNT(*) AS ticks
        FROM ticks
        WHERE bookmaker = $1
          AND liga IS NOT NULL
          AND liga != ''
          AND ts >= NOW() - INTERVAL '{dias} days'
          AND ({where_esporte})
          {where_blacklist}
        GROUP BY liga
        HAVING COUNT(*) >= {min_ticks}
        ORDER BY ticks DESC
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    pais_dict = {}

    for r in rows:
        liga_bruta = r["liga"]
        ticks = r["ticks"]
        liga = traduzir_liga(casa, liga_bruta)
        pai = classificar_pai(liga)

        if not pai:
            continue

        if pai not in pais_dict:
            pais_dict[pai] = {
                "grades": {},
                "ticks_total": 0,
            }

        if liga not in pais_dict[pai]["grades"]:
            pais_dict[pai]["grades"][liga] = ticks
            pais_dict[pai]["ticks_total"] += ticks

    torneios = []
    for pai, dados in sorted(
        pais_dict.items(),
        key=lambda x: x[1]["ticks_total"],
        reverse=True,
    ):
        grades_lista = [
            {"nome": nome, "ticks": ticks}
            for nome, ticks in sorted(
                dados["grades"].items(),
                key=lambda g: g[1],
                reverse=True,
            )
        ]
        torneios.append({
            "nome_pai": nome_pai_para_exibicao(pai),
            "nome_pai_real": pai,
            "ticks_total": dados["ticks_total"],
            "grades": grades_lista,
        })

    resultado = {
        "casa": casa,
        "esporte": esporte,
        "dias": dias,
        "min_ticks": min_ticks,
        "total_pais": len(torneios),
        "torneios": torneios,
    }
    _cache_set(cache_key, resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/{torneio_id}/grades")
async def listar_grades(torneio_id: str):
    cache_key = ("grades", torneio_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    pattern = TORNEIO_PATTERNS.get(torneio_id, torneio_id)

    sql = """
        SELECT DISTINCT liga, COUNT(*) AS ticks
        FROM ticks
        WHERE liga LIKE $1
          AND liga IS NOT NULL
          AND ts >= NOW() - INTERVAL '14 days'
        GROUP BY liga
        ORDER BY ticks DESC
    """

    async with db() as conn:
        rows = await conn.fetch(sql, pattern)

    grades = [{"nome": r["liga"], "ticks": r["ticks"]} for r in rows]
    resultado = {
        "torneio": torneio_id,
        "pattern": pattern,
        "total": len(grades),
        "grades": grades,
    }
    _cache_set(cache_key, resultado)
    return resultado


@router.get("/{torneio_id}/participantes")
async def get_participantes(
    torneio_id: str,
    grades: Optional[str] = Query(None),
    grades_modo: str = Query("whitelist"),
    bookmaker: Optional[str] = None,
):
    cache_key = ("participantes", torneio_id, grades or '', grades_modo, bookmaker or '')
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "_cache": "memory"}

    pattern = TORNEIO_PATTERNS.get(torneio_id, torneio_id)

    if grades:
        grades_list = [g.strip() for g in grades.split('|') if g.strip()]
        if not grades_list:
            raise HTTPException(status_code=400, detail="Lista de grades vazia")

        if grades_modo == "blacklist":
            params = [pattern] + grades_list
            placeholders_excl = ','.join(f"${i+2}" for i in range(len(grades_list)))
            bm_clause = ""
            if bookmaker:
                params.append(bookmaker)
                bm_clause = f"AND bookmaker = ${len(params)}"

            base_filter = f"""liga LIKE $1
                AND liga NOT IN ({placeholders_excl})
                AND ts >= NOW() - INTERVAL '14 days' {bm_clause}"""

            sql_jog = f"""
                SELECT DISTINCT jogador FROM (
                    SELECT jogador_a AS jogador FROM ticks
                    WHERE {base_filter}
                      AND jogador_a IS NOT NULL AND jogador_a != ''
                    UNION
                    SELECT jogador_b AS jogador FROM ticks
                    WHERE {base_filter}
                      AND jogador_b IS NOT NULL AND jogador_b != ''
                ) sub ORDER BY jogador
            """
            async with db() as conn:
                rows = await conn.fetch(sql_jog, *params)
            jogadores = [r["jogador"] for r in rows if r["jogador"]]

            times = await _buscar_times(base_filter, params)

            resultado = {
                "torneio": torneio_id,
                "modo": "blacklist",
                "grades_excluidas": grades_list,
                "total": len(jogadores),
                "jogadores": jogadores,
                "times": times,
            }
            _cache_set(cache_key, resultado)
            return {**resultado, "_cache": "blacklist"}

        else:
            params = grades_list[:]
            placeholders = ','.join(f"${i+1}" for i in range(len(params)))
            bm_clause = ""
            if bookmaker:
                params.append(bookmaker)
                bm_clause = f"AND bookmaker = ${len(params)}"

            base_filter = f"""liga IN ({placeholders})
                AND ts >= NOW() - INTERVAL '14 days' {bm_clause}"""

            sql_jog = f"""
                SELECT DISTINCT jogador FROM (
                    SELECT jogador_a AS jogador FROM ticks
                    WHERE {base_filter}
                      AND jogador_a IS NOT NULL AND jogador_a != ''
                    UNION
                    SELECT jogador_b AS jogador FROM ticks
                    WHERE {base_filter}
                      AND jogador_b IS NOT NULL AND jogador_b != ''
                ) sub ORDER BY jogador
            """
            async with db() as conn:
                rows = await conn.fetch(sql_jog, *params)
            jogadores = [r["jogador"] for r in rows if r["jogador"]]

            times = await _buscar_times(base_filter, params)

            resultado = {
                "torneio": torneio_id,
                "modo": "whitelist",
                "grades_filtradas": grades_list,
                "total": len(jogadores),
                "jogadores": jogadores,
                "times": times,
            }
            _cache_set(cache_key, resultado)
            return {**resultado, "_cache": "whitelist"}

    bm_clause = ""
    params = [pattern]
    if bookmaker:
        params.append(bookmaker)
        bm_clause = f"AND bookmaker = ${len(params)}"

    base_filter = f"""liga LIKE $1
        AND ts >= NOW() - INTERVAL '14 days' {bm_clause}"""

    sql_jog = f"""
        SELECT DISTINCT jogador FROM (
            SELECT jogador_a AS jogador FROM ticks
            WHERE {base_filter}
              AND jogador_a IS NOT NULL AND jogador_a != ''
            UNION
            SELECT jogador_b AS jogador FROM ticks
            WHERE {base_filter}
              AND jogador_b IS NOT NULL AND jogador_b != ''
        ) sub ORDER BY jogador
    """
    async with db() as conn:
        rows = await conn.fetch(sql_jog, *params)
    jogadores = [r["jogador"] for r in rows if r["jogador"]]

    if not jogadores:
        raise HTTPException(status_code=404, detail=f"Nenhum participante para '{torneio_id}'")

    times = await _buscar_times(base_filter, params)

    resultado = {
        "torneio": torneio_id,
        "pattern": pattern,
        "total": len(jogadores),
        "total_times": len(times),
        "jogadores": jogadores,
        "times": times,
    }
    _cache_set(cache_key, resultado)
    return {**resultado, "_cache": "aggregate"}


@router.post("/{torneio_id}/cache/invalidar")
async def invalidar_cache(torneio_id: str):
    keys_to_remove = [k for k in _CACHE.keys() if len(k) >= 2 and k[1] == torneio_id]
    for k in keys_to_remove:
        del _CACHE[k]
    return {"removidos": len(keys_to_remove)}
