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

# SUPERBET_ID_TO_NAME = dict BASE (hardcoded, o mesmo de sempre) + o que vier do
# superbet_ligas.json (a MESMA fonte que o coletor usa) sobreposto por cima.
#
# BLINDAGEM (por que isso NAO pode quebrar nada):
#   - a BASE abaixo e o dicionario original completo. Se o JSON nao existir,
#     estiver corrompido, vazio, ilegivel ou com formato errado, o modulo cai
#     na BASE e o comportamento fica IDENTICO ao de antes desta mudanca.
#   - nenhuma excecao escapa: qualquer erro de leitura/parse e engolido. O
#     import de routers/torneios.py nunca falha por causa do JSON (se falhasse,
#     a API inteira nao subiria).
#   - so entram entradas validas: chave nao-metadata ('_...'), valor string
#     nao-vazia e que nao seja so numero (nome numerico nao traduz nada).
#   - o JSON so ACRESCENTA ou CORRIGE nomes. Nenhum id da BASE e removido.
import json as _json_ligas
import os as _os_ligas
import time as _time_ligas

# --- BASE: dicionario original (fallback total). NAO remover entradas daqui. ---
_SUPERBET_BASE = {
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

# caminhos onde o superbet_ligas.json pode estar (primeiro que servir, vence).
# IMPORTANTE: aponte para o MESMO arquivo que o coletor le. Nao faca copias —
# duas copias voltam a divergir, que e exatamente o problema que isso resolve.
_LIGAS_JSON_CANDIDATOS = [
    _os_ligas.environ.get("SUPERBET_LIGAS_JSON", ""),          # 1) o mais explicito
    _os_ligas.path.join(
        _os_ligas.path.dirname(_os_ligas.path.dirname(_os_ligas.path.abspath(__file__))),
        "superbet_ligas.json"),                                # 2) raiz do tipmike_api
    _os_ligas.path.join(
        _os_ligas.path.dirname(_os_ligas.path.dirname(_os_ligas.path.dirname(
            _os_ligas.path.abspath(__file__)))),
        "superbet_ligas.json"),                                # 3) PASTA PAI do projeto
    _os_ligas.path.join(_os_ligas.path.dirname(_os_ligas.path.abspath(__file__)),
                        "superbet_ligas.json"),                # 4) ao lado de routers/
    r"C:\Users\Administrator\PyCharmMiscProject\superbet_ligas.json",
    r"C:\Users\Administrator\PycharmProjects\MIKEDB\superbet_ligas.json",
    "superbet_ligas.json",                                     # 7) diretorio corrente
]


def diagnostico_ligas() -> dict:
    """Ajuda a descobrir de onde (ou se) o JSON foi carregado. Uso:
       python -c "from routers.torneios import diagnostico_ligas as d; print(d())"
    """
    try:
        mapa, assinatura = _ler_json_ligas()
        return {
            "arquivo_usado": assinatura[0] if assinatura else None,
            "ids_no_json": len(mapa) if mapa else 0,
            "ids_em_uso": len(SUPERBET_ID_TO_NAME),
            "ids_na_base": len(_SUPERBET_BASE),
            "candidatos": [{"caminho": c, "existe": bool(c) and _os_ligas.path.exists(c)}
                           for c in _LIGAS_JSON_CANDIDATOS if c],
        }
    except Exception as e:
        return {"erro": repr(e), "ids_em_uso": len(SUPERBET_ID_TO_NAME)}

_LIGAS_TTL_SEG = 60          # so checa o arquivo no maximo 1x por minuto
_ligas_prox_check = 0.0      # timestamp da proxima verificacao
_ligas_assinatura = None     # (caminho, mtime, tamanho) do que ja foi carregado


def _ler_json_ligas():
    """Le o primeiro superbet_ligas.json valido. Devolve (mapa, assinatura) ou
    (None, None). NUNCA levanta excecao."""
    for caminho in _LIGAS_JSON_CANDIDATOS:
        if not caminho:
            continue
        try:
            st = _os_ligas.stat(caminho)
            with open(caminho, encoding="utf-8") as fh:
                raw = _json_ligas.load(fh)
            if not isinstance(raw, dict):        # JSON valido mas formato errado
                continue
            mapa = {}
            for k, v in raw.items():
                try:
                    chave = str(k).strip()
                    if not chave or chave.startswith("_"):
                        continue                  # metadata (_comentario etc)
                    if not isinstance(v, str):
                        continue                  # so aceita nome em texto
                    nome = v.strip()
                    if not nome or nome.isdigit():
                        continue                  # vazio ou "nome" numerico
                    mapa[chave] = nome
                except Exception:
                    continue                      # entrada podre: pula so ela
            if mapa:
                return mapa, (caminho, st.st_mtime, st.st_size)
        except Exception:
            continue                              # arquivo sumiu/corrompido/sem permissao
    return None, None


def _montar_mapa_ligas() -> dict:
    """BASE + JSON por cima. Em qualquer falha, devolve a BASE intacta."""
    try:
        mapa = dict(_SUPERBET_BASE)
        doJson, assinatura = _ler_json_ligas()
        if doJson:
            mapa.update(doJson)                   # JSON corrige/acrescenta
        return mapa, assinatura
    except Exception:
        return dict(_SUPERBET_BASE), None


def _talvez_recarregar_ligas():
    """Recarrega o mapa se o JSON mudou (checa no maximo 1x/min). Assim um id
    novo nomeado no JSON entra sem reiniciar a API. NUNCA levanta excecao."""
    global SUPERBET_ID_TO_NAME, _ligas_prox_check, _ligas_assinatura
    try:
        agora = _time_ligas.monotonic()
        if agora < _ligas_prox_check:
            return
        _ligas_prox_check = agora + _LIGAS_TTL_SEG
        _, assinatura = _ler_json_ligas()
        if assinatura != _ligas_assinatura:
            novo, assinatura = _montar_mapa_ligas()
            if novo:
                SUPERBET_ID_TO_NAME = novo        # rebind atomico
                _ligas_assinatura = assinatura
    except Exception:
        pass                                      # mantem o mapa atual


SUPERBET_ID_TO_NAME, _ligas_assinatura = _montar_mapa_ligas()

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
    """Converte id/codigo bruto -> nome humano. BLINDADO: em qualquer erro
    devolve a liga como veio (nunca levanta, nunca devolve None novo)."""
    if not liga:
        return liga
    try:
        _talvez_recarregar_ligas()   # pega id novo do JSON sem reiniciar a API
        if bookmaker == 'superbet':
            mapa = SUPERBET_ID_TO_NAME
            if liga in mapa:
                return mapa[liga]
            if liga in SUPERBET_ALIASES:
                return SUPERBET_ALIASES[liga]
        if bookmaker == 'bet365' and liga in BET365_CODE_TO_NAME:
            return BET365_CODE_TO_NAME[liga]
    except Exception:
        pass
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


def _esporte_do_id_superbet(liga_id: str, nome: str = '') -> str:
    """Esporte de um id da Superbet. O mapa explicito manda; se o id nao estiver
    la (caso dos ids novos vindos do superbet_ligas.json), INFERE pelo nome —
    senao um id de hoquei/tenis cairia no default 'fifa'."""
    try:
        if liga_id in SUPERBET_ID_TO_ESPORTE:
            return SUPERBET_ID_TO_ESPORTE[liga_id]
        n = (nome or '').lower()
        if any(p in n for p in ('nhl', 'hockey', 'hoquei', 'iihf', 'khl')):
            return 'ehockey'
        if any(p in n for p in ('tennis', 'tênis', 'tenis', 'atp', 'wta')):
            return 'etennis'
        if any(p in n for p in ('nba', 'basket', 'basquete', 'euroliga')):
            return 'nba2k'
    except Exception:
        pass
    return 'fifa'


def _ids_brutos_para_esporte(casa: str, esporte: str) -> list:
    out = []
    try:
        if casa == 'superbet':
            for liga_id, nome in list(SUPERBET_ID_TO_NAME.items()):
                if _esporte_do_id_superbet(liga_id, nome) == esporte:
                    out.append(liga_id)
        elif casa == 'bet365':
            for codigo in list(BET365_CODE_TO_NAME.keys()):
                esp = BET365_CODE_TO_ESPORTE.get(codigo, 'fifa')
                if esp == esporte:
                    out.append(codigo)
    except Exception:
        return out
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


@router.get("/disponiveis")
async def listar_torneios_disponiveis(
    casa: str = Query(...),
    esporte: str = Query(...),
    dias: int = Query(7, ge=1, le=30),
    min_ticks: int = Query(100, ge=0),
):
    """Le SO do catalogo_torneios (instantaneo). Se nao houver catalogo ainda,
    retorna vazio (o job atualizar_catalogo.py preenche em ate ~10min)."""
    async with db() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM catalogo_torneios WHERE casa=$1 AND esporte=$2",
            casa, esporte,
        )
    if not row:
        return {
            "casa": casa, "esporte": esporte, "dias": dias, "min_ticks": min_ticks,
            "total_pais": 0, "torneios": [], "_cache": "vazio",
        }
    import json as _json
    payload = row["payload"]
    if isinstance(payload, str):
        payload = _json.loads(payload)
    # remove jogadores/times do retorno do /disponiveis (eles vao no /participantes)
    out = {k: v for k, v in payload.items() if k not in ("jogadores", "times")}
    return {**out, "_cache": "catalogo"}




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
    esporte: Optional[str] = Query(None),
):
    """Le jogadores/times do catalogo (campo por_grade). Instantaneo, sem query na ticks.

    - sem grades: retorna todos os jogadores/times da casa/esporte.
    - grades + whitelist: junta so as grades escolhidas.
    - grades + blacklist: junta todas MENOS as grades escolhidas.
    Precisa de bookmaker (casa) e esporte pra achar o catalogo.
    """
    casa = bookmaker
    if not casa:
        raise HTTPException(status_code=400, detail="bookmaker (casa) e obrigatorio")

    # se nao passar esporte, tenta achar em qualquer esporte dessa casa
    async with db() as conn:
        if esporte:
            row = await conn.fetchrow(
                "SELECT payload FROM catalogo_torneios WHERE casa=$1 AND esporte=$2",
                casa, esporte)
        else:
            row = await conn.fetchrow(
                "SELECT payload FROM catalogo_torneios WHERE casa=$1 LIMIT 1", casa)

    if not row:
        return {"torneio": torneio_id, "total": 0, "jogadores": [], "times": [], "_cache": "vazio"}

    import json as _json
    payload = row["payload"]
    if isinstance(payload, str):
        payload = _json.loads(payload)
    por_grade = payload.get("por_grade", {})

    # sem filtro de grade: tudo
    if not grades:
        jogadores = payload.get("jogadores", [])
        times = payload.get("times", [])
        return {"torneio": torneio_id, "total": len(jogadores),
                "jogadores": jogadores, "times": times, "_cache": "catalogo"}

    grades_list = [g.strip() for g in grades.split("|") if g.strip()]
    jset, tset = set(), set()

    if grades_modo == "blacklist":
        # todas as grades MENOS as excluidas
        for g, d in por_grade.items():
            if g in grades_list:
                continue
            jset |= set(d.get("jogadores", []))
            tset |= set(d.get("times", []))
    else:
        # whitelist: so as grades escolhidas
        for g in grades_list:
            d = por_grade.get(g)
            if d:
                jset |= set(d.get("jogadores", []))
                tset |= set(d.get("times", []))

    return {
        "torneio": torneio_id,
        "modo": grades_modo,
        "grades_filtradas" if grades_modo == "whitelist" else "grades_excluidas": grades_list,
        "total": len(jset),
        "jogadores": sorted(jset),
        "times": sorted(tset),
        "_cache": "catalogo",
    }


@router.post("/{torneio_id}/cache/invalidar")
async def invalidar_cache(torneio_id: str):
    keys_to_remove = [k for k in _CACHE.keys() if len(k) >= 2 and k[1] == torneio_id]
    for k in keys_to_remove:
        del _CACHE[k]
    return {"removidos": len(keys_to_remove)}
