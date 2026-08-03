# -*- coding: utf-8 -*-
"""
atualizar_catalogo.py — preenche a tabela catalogo_torneios (cache persistente).

Reaproveita os tradutores/classificadores do routers/torneios.py (mesma logica
que o frontend espera). Computa torneios+grades+jogadores+times por casa/esporte
e grava como JSONB pronto. O endpoint /disponiveis le SO desse catalogo (instantaneo).

MODOS:
    python atualizar_catalogo.py            # INCREMENTAL: olha so ligas novas (rapido, p/ rodar a cada 10min)
    python atualizar_catalogo.py --full     # COMPLETO: recomputa tudo do zero (rodar 1x/dia no 6h)

Casas e esportes processados estao em COMBINACOES abaixo.
"""
import sys, os, json, asyncio, socket
from datetime import datetime

import asyncpg

# importa a logica ja existente da API (rodar da pasta tipmike_api)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from routers.torneios import (
    ESPORTE_LIGA_PATTERNS, ESPORTE_LIGA_BLACKLIST,
    traduzir_liga, classificar_pai, nome_pai_para_exibicao,
    _ids_brutos_para_esporte, TORNEIO_PATTERNS,
)

DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"

# casas x esportes que o frontend oferece
COMBINACOES = [
    ("superbet", "fifa"), ("superbet", "nba2k"),
    ("betano", "fifa"),   ("betano", "nba2k"),
    ("estrelabet", "fifa"), ("estrelabet", "nba2k"),
    ("bet365", "fifa"),   ("bet365", "nba2k"),
]

DIAS = 7
MIN_TICKS = 100

# ------------------------------------------------------------------
# Blindagem (02/ago): 3 copias deste script rodaram SIMULTANEAS (agenda
# empilhando execucoes mais lentas que o intervalo) e as agregacoes em
# paralelo saturaram o banco — a API afogou junto e o site caiu.
#   1. TRAVA DE INSTANCIA (porta local): a copia seguinte detecta e sai
#      na hora — empilhamento vira impossivel por construcao.
#   2. STATEMENT_TIMEOUT por conexao: no pior caso a query morre sozinha
#      em 120s em vez de segurar o banco (a proxima rodada refaz).
# ------------------------------------------------------------------
PORTA_TRAVA = 47232
STATEMENT_TIMEOUT_MS = 120_000


def travar_instancia() -> socket.socket:
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.bind(("127.0.0.1", PORTA_TRAVA))
        sk.listen(1)
        return sk
    except OSError:
        print(f"[catalogo] JA EXISTE outra instancia (porta {PORTA_TRAVA}). Saindo.")
        sys.exit(0)

# mapa esporte (chave do frontend) -> valor REAL da coluna `sport` na tabela ticks.
# o coletor ja grava o sport certo, entao classificar por aqui e robusto e nao
# depende de pattern de nome de liga (que falhava: ex superbet basket
# 'EAL - NextGen' / 'Battle - NBA 1' / 'H2H - GG League Mixed' nao batiam em
# nenhum pattern de nba2k E ainda vazavam pro fifa via 'EAL%' / '%H2H GG%').
ESPORTE_PARA_SPORT = {
    "fifa":    "E-Football",
    "nba2k":   "E-Basketball",
    "ehockey": "E-Hockey",
    "etennis": "E-Tennis",
}


# agrupamento de torneio pelo COMECO do nome da liga (a "marca").
# casas tipo estrelabet nao usam o separador ' - ', entao o classificar_pai original
# (corta no ' - ') deixava cada liga como torneio proprio. Aqui agrupamos por prefixo.
# ORDEM IMPORTA: marca mais "envolvente" primeiro (FC26/CLA-UA/ESportsBattle/Volta
# precisam vir antes de World Cup/Champions League/International, senao pegam errado).
GRUPOS_PREFIXO = [
    ("FC26",             "FC26"),
    ("CLA-UA",           "CLA-UA"),
    # --- familia GT: junta "GT - X" e "GT League - X" num pai so ---
    ("GT",               "GT "),
    ("GT",               "GT League"),
    # --- familia CLA: junta "CLA - X", o rotulo velho "CLA Copa do Mundo 2x5"
    #     e o "Cyber Live Arena" (que nao tem hifen e virava familia sozinha) ---
    ("CLA",              "CLA "),
    ("CLA",              "Cyber Live Arena"),
    ("CLA",              "Live Arena"),
    ("ESportsBattle",    "ESportsBattle"),
    ("Volta",            "Volta"),
    ("Champions VOLTA",  "Champions VOLTA"),
    ("Valhalla Cup",     "Valhalla Cup"),
    ("Valhalla League",  "Valhalla League"),
    ("Valkyrie Cup",     "Valkyrie Cup"),
    ("H2H GG",           "H2H GG"),
    ("Champions League", "Champions League"),
    ("World Cup",        "World Cup"),
    ("International",     "International"),
]


def classificar_pai_prefixo(liga):
    """Agrupa pelo comeco do nome. Se nao bate marca conhecida, cai no separador
    ' - ' (mantem o comportamento da superbet/betano/bet365), senao a liga inteira."""
    s = (liga or "").strip()
    low = s.lower()
    for nome, pref in GRUPOS_PREFIXO:
        if low.startswith(pref.lower()):
            return nome
    if ' - ' in s:
        return s.split(' - ')[0].strip()
    return s


async def computar_combo(conn, casa, esporte, full=True):
    """Recomputa torneios+grades+jogadores+times de uma casa/esporte. Retorna o payload."""
    sport = ESPORTE_PARA_SPORT.get(esporte)
    if not sport:
        return None

    # 1) torneios + grades (com contagem) — filtra pela coluna `sport` REAL ($2).
    #    TRIM(liga) funde duplicatas com espaco sobrando (ex: ' CLA-UA ...' == 'CLA-UA ...')
    sql = f"""
        SELECT TRIM(liga) AS liga, COUNT(*) AS ticks
        FROM ticks
        WHERE bookmaker = $1 AND sport = $2
          AND liga IS NOT NULL AND TRIM(liga) != ''
          AND ts >= NOW() - INTERVAL '{DIAS} days'
        GROUP BY TRIM(liga)
        HAVING COUNT(*) >= {MIN_TICKS}
        ORDER BY ticks DESC
    """
    rows = await conn.fetch(sql, casa, sport)

    pais_dict = {}
    ligas_brutas = []
    for r in rows:
        liga_bruta = r["liga"]; ticks = r["ticks"]
        ligas_brutas.append(liga_bruta)
        liga = traduzir_liga(casa, liga_bruta)
        pai = classificar_pai_prefixo(liga)
        if not pai:
            continue
        d = pais_dict.setdefault(pai, {"grades": {}, "ticks_total": 0})
        if liga not in d["grades"]:
            d["grades"][liga] = ticks
            d["ticks_total"] += ticks

    torneios = []
    for pai, dados in sorted(pais_dict.items(), key=lambda x: x[1]["ticks_total"], reverse=True):
        grades = [{"nome": n, "ticks": t} for n, t in sorted(dados["grades"].items(), key=lambda g: g[1], reverse=True)]
        torneios.append({
            "nome_pai": nome_pai_para_exibicao(pai),
            "nome_pai_real": pai,
            "ticks_total": dados["ticks_total"],
            "grades": grades,
        })

    # 2) jogadores + times POR GRADE (liga bruta) — pro filtro por grade no form.
    #    Uma query so, agrupada por liga, em vez de N queries.
    por_grade = {}   # liga_bruta -> {"jogadores": [...], "times": [...]}
    jogadores_all, times_all = set(), set()
    if ligas_brutas:
        inp = ",".join(f"${i+3}" for i in range(len(ligas_brutas)))  # $1=casa, $2=sport, $3..=ligas
        pj = [casa, sport] + ligas_brutas
        # jogadores por liga (jogador_a e jogador_b)
        sql_j = f"""
            SELECT liga, jogador FROM (
                SELECT TRIM(liga) AS liga, jogador_a AS jogador FROM ticks
                WHERE bookmaker=$1 AND sport=$2 AND TRIM(liga) IN ({inp})
                  AND ts >= NOW() - INTERVAL '{DIAS} days'
                  AND jogador_a IS NOT NULL AND jogador_a != ''
                UNION
                SELECT TRIM(liga) AS liga, jogador_b AS jogador FROM ticks
                WHERE bookmaker=$1 AND sport=$2 AND TRIM(liga) IN ({inp})
                  AND ts >= NOW() - INTERVAL '{DIAS} days'
                  AND jogador_b IS NOT NULL AND jogador_b != ''
            ) s
        """
        for r in await conn.fetch(sql_j, *pj):
            lb = r["liga"]; jg = r["jogador"]
            por_grade.setdefault(lb, {"jogadores": set(), "times": set()})["jogadores"].add(jg)
            jogadores_all.add(jg)
        # times por liga (time_a e time_b)
        sql_t = f"""
            SELECT liga, time FROM (
                SELECT TRIM(liga) AS liga, time_a AS time FROM ticks
                WHERE bookmaker=$1 AND sport=$2 AND TRIM(liga) IN ({inp})
                  AND ts >= NOW() - INTERVAL '{DIAS} days'
                  AND time_a IS NOT NULL AND time_a != ''
                UNION
                SELECT TRIM(liga) AS liga, time_b AS time FROM ticks
                WHERE bookmaker=$1 AND sport=$2 AND TRIM(liga) IN ({inp})
                  AND ts >= NOW() - INTERVAL '{DIAS} days'
                  AND time_b IS NOT NULL AND time_b != ''
            ) s
        """
        for r in await conn.fetch(sql_t, *pj):
            lb = r["liga"]; tm = r["time"]
            por_grade.setdefault(lb, {"jogadores": set(), "times": set()})["times"].add(tm)
            times_all.add(tm)

    # serializa por_grade: a CHAVE e a liga TRADUZIDA (igual o frontend ve nas grades)
    grades_part = {}
    for lb, d in por_grade.items():
        liga_trad = traduzir_liga(casa, lb)
        g = grades_part.setdefault(liga_trad, {"jogadores": set(), "times": set()})
        g["jogadores"] |= d["jogadores"]
        g["times"] |= d["times"]
    grades_part = {
        k: {"jogadores": sorted(v["jogadores"]), "times": sorted(v["times"])}
        for k, v in grades_part.items()
    }

    payload = {
        "casa": casa, "esporte": esporte,
        "dias": DIAS, "min_ticks": MIN_TICKS,
        "total_pais": len(torneios),
        "torneios": torneios,
        "jogadores": sorted(jogadores_all),   # todos juntos (quando nao filtra grade)
        "times": sorted(times_all),
        "por_grade": grades_part,             # jogadores/times de CADA grade (pro filtro)
    }
    return payload, ligas_brutas


async def main():
    trava = travar_instancia()  # noqa: F841
    full = "--full" in sys.argv
    conn = await asyncpg.connect(DSN, server_settings={'statement_timeout': str(STATEMENT_TIMEOUT_MS)})
    print(f"[{datetime.now():%H:%M:%S}] {'FULL' if full else 'INCREMENTAL'} — atualizando catalogo")

    if not full:
        # INCREMENTAL: so recomputa combos que ganharam liga NOVA nos ultimos minutos
        novas = await conn.fetch("""
            SELECT DISTINCT bookmaker, liga FROM ticks
            WHERE ts >= NOW() - INTERVAL '15 minutes'
              AND liga IS NOT NULL AND liga != ''
        """)
        # quais (casa) tem liga que ainda nao esta em catalogo_visto?
        combos_pra_atualizar = set()
        for r in novas:
            casa = r["bookmaker"]; liga = r["liga"]
            for (c, esp) in COMBINACOES:
                if c != casa:
                    continue
                ja = await conn.fetchval(
                    "SELECT 1 FROM catalogo_visto WHERE casa=$1 AND esporte=$2 AND liga=$3",
                    casa, esp, liga)
                if not ja:
                    combos_pra_atualizar.add((casa, esp))
        if not combos_pra_atualizar:
            print("  nada novo. catalogo ja atualizado.")
            await conn.close(); return
        alvos = list(combos_pra_atualizar)
    else:
        alvos = COMBINACOES

    for casa, esporte in alvos:
        try:
            res = await computar_combo(conn, casa, esporte, full=full)
            if not res:
                continue
            payload, ligas_brutas = res
            await conn.execute("""
                INSERT INTO catalogo_torneios (casa, esporte, payload, atualizado)
                VALUES ($1,$2,$3,NOW())
                ON CONFLICT (casa, esporte) DO UPDATE SET payload=$3, atualizado=NOW()
            """, casa, esporte, json.dumps(payload))
            # marca as ligas como vistas
            for lb in ligas_brutas:
                await conn.execute("""
                    INSERT INTO catalogo_visto (casa, esporte, liga) VALUES ($1,$2,$3)
                    ON CONFLICT DO NOTHING
                """, casa, esporte, lb)
            print(f"  [OK] {casa}/{esporte}: {payload['total_pais']} torneios, "
                  f"{len(payload['jogadores'])} jogadores, {len(payload['times'])} times")
        except Exception as e:
            print(f"  [ERRO] {casa}/{esporte}: {e}")

    await conn.close()
    print(f"[{datetime.now():%H:%M:%S}] concluido.")


if __name__ == "__main__":
    asyncio.run(main())
