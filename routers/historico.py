"""
routers/historico.py
Endpoint que retorna o histórico agregado de um bot:
- Resultados por dia (greens, reds, lucro, total)
- Totais consolidados (WR, ROI, lucro, odd média)
- Tips recentes (últimas N apostas)
- Export CSV (todas as apostas do bot)

Usa modo='simulado' por padrão (apostas geradas pelo bot_executor).
"""

import csv
import io
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from database import db
from security import get_current_user, acesso_total

router = APIRouter(prefix="/bots", tags=["historico"])

# Timezone do Brasil pra formatar timestamps do CSV de forma amigavel
BRT = timezone(timedelta(hours=-3))

# Mapeamento periodo -> dias
PERIODO_DIAS = {
    "dia": 1,
    "3d": 3,
    "7d": 7,
    "15d": 15,
    "30d": 30,
    "todas": 90,
}


@router.get("/{bot_id}/historico")
async def get_historico(
    bot_id: int,
    periodo: str = Query("30d", description="dia | 3d | 7d | 15d | 30d | todas"),
    modo: str = Query("simulado", description="simulado | real"),
    limite_tips: int = Query(60, ge=1, le=500, description="Quantas tips retornar"),
    usuario: dict = Depends(get_current_user),
):
    """
    Retorna histórico completo do bot:
    {
      "bot": {...},
      "resultadosDiarios": [{iso, label, greens, reds, total, lucro}, ...],
      "tips": [{dataHora, confronto, selecao, odd, unidades, status}, ...],
      "totais": {greens, reds, total, lucro, roi, wr, oddMedia, ...},
      "dias": 30
    }
    """
    if periodo not in PERIODO_DIAS:
        raise HTTPException(400, f"periodo invalido. Use: {list(PERIODO_DIAS.keys())}")
    if modo not in ("simulado", "real"):
        raise HTTPException(400, "modo deve ser 'simulado' ou 'real'")

    dias = PERIODO_DIAS[periodo]

    async with db() as conn:
        # 1. Bot existe?
        bot_row = await conn.fetchrow("""
            SELECT id, nome, descricao, casa, esporte, mercado, status,
                   torneios, criado_em, atualizado_em, user_id
            FROM bots
            WHERE id = $1
        """, bot_id)
        if not bot_row:
            raise HTTPException(404, f"Bot {bot_id} nao encontrado")

        bot = dict(bot_row)
        dono_id = bot.pop("user_id", None)
        if not acesso_total(usuario) and dono_id != usuario.get("id"):
            # 404 igual ao inexistente: não vaza existência de bot alheio.
            raise HTTPException(404, f"Bot {bot_id} nao encontrado")
        # Liga: pega 1° torneio do bot (UI espera string única)
        liga_str = ""
        torneios = bot.get("torneios")
        if torneios:
            if isinstance(torneios, list) and torneios:
                liga_str = torneios[0]
            elif isinstance(torneios, str):
                liga_str = torneios

        # 2. Resultados diários (apostas resolvidas + pendentes)
        diarios_rows = await conn.fetch(f"""
            SELECT
                DATE(apostado_em AT TIME ZONE 'America/Sao_Paulo') AS dia,
                TO_CHAR(DATE(apostado_em AT TIME ZONE 'America/Sao_Paulo'), 'DD/MM') AS label,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado = 'green') AS greens,
                COUNT(*) FILTER (WHERE resultado = 'red')   AS reds,
                COUNT(*) FILTER (WHERE resultado = 'void')  AS voids,
                COUNT(*) FILTER (WHERE resultado IS NULL OR status = 'pendente') AS pendentes,
                COALESCE(SUM(pnl), 0)::float AS lucro_reais,
                COALESCE(SUM(lucro_unidades), 0)::float AS lucro_un,
                COALESCE(AVG(odd) FILTER (WHERE odd IS NOT NULL), 0)::float AS odd_media
            FROM apostas
            WHERE bot_id = $1
              AND modo = $2
              AND apostado_em >= NOW() - INTERVAL '{dias} days'
            GROUP BY 1, 2
            ORDER BY 1
        """, bot_id, modo)

        resultados_diarios = []
        for r in diarios_rows:
            d = dict(r)
            resultados_diarios.append({
                "iso": d["dia"].isoformat() if d.get("dia") else "",
                "label": d.get("label") or "",
                "total": int(d["total"]),
                "greens": int(d["greens"]),
                "reds": int(d["reds"]),
                "voids": int(d["voids"]),
                "pendentes": int(d["pendentes"]),
                "meiosGreens": 0,
                "meiosReds": 0,
                "devolvidas": int(d["voids"]),
                "canceladas": 0,
                # `lucro` que a UI usa: lucro em unidades (lucro_un)
                "lucro": round(d["lucro_un"], 2),
                "lucroReais": round(d["lucro_reais"], 2),
            })

        # 3. Totais consolidados
        totais_row = await conn.fetchrow(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado = 'green') AS greens,
                COUNT(*) FILTER (WHERE resultado = 'red')   AS reds,
                COUNT(*) FILTER (WHERE resultado = 'void')  AS voids,
                COUNT(*) FILTER (WHERE resultado IS NULL OR status = 'pendente') AS pendentes,
                COALESCE(SUM(pnl), 0)::float AS lucro_reais,
                COALESCE(SUM(lucro_unidades), 0)::float AS lucro_un,
                COALESCE(AVG(odd) FILTER (WHERE odd IS NOT NULL), 0)::float AS odd_media,
                COALESCE(SUM(stake), 0)::float AS stake_total
            FROM apostas
            WHERE bot_id = $1
              AND modo = $2
              AND apostado_em >= NOW() - INTERVAL '{dias} days'
        """, bot_id, modo)

        t = dict(totais_row) if totais_row else {}
        total = int(t.get("total") or 0)
        greens = int(t.get("greens") or 0)
        reds = int(t.get("reds") or 0)
        voids = int(t.get("voids") or 0)
        pendentes = int(t.get("pendentes") or 0)
        resolvidas = greens + reds + voids
        lucro_un = round(t.get("lucro_un") or 0, 2)
        lucro_reais = round(t.get("lucro_reais") or 0, 2)
        odd_media = round(t.get("odd_media") or 0, 2)

        # ROI = lucro / stake total (em unidades, stake=1u)
        # WR = greens / (greens + reds) (ignora voids)
        wr = round((greens / (greens + reds)) * 100, 1) if (greens + reds) > 0 else 0
        roi = round((lucro_un / resolvidas) * 100, 2) if resolvidas > 0 else 0

        totais = {
            "total": total,
            "greens": greens,
            "reds": reds,
            "voids": voids,
            "pendentes": pendentes,
            "resolvidas": resolvidas,
            "meiosGreens": 0,
            "meiosReds": 0,
            "devolvidas": voids,
            "canceladas": 0,
            "lucro": lucro_un,           # unidades (UI usa)
            "lucroReais": lucro_reais,    # R$ absoluto
            "roi": roi,                   # %
            "wr": wr,                     # %
            "oddMedia": odd_media,
            "stakeTotal": round(t.get("stake_total") or 0, 2),
        }

        # 4. Tips recentes
        tips_rows = await conn.fetch("""
            SELECT
                id, apostado_em,
                jogador_a, jogador_b,
                mercado, linha, selecao, lado,
                odd, stake,
                resultado, lucro_unidades, pnl, status,
                placar_a_entrada, placar_b_entrada,
                placar_final_a, placar_final_b,
                live_time
            FROM apostas
            WHERE bot_id = $1 AND modo = $2
            ORDER BY apostado_em DESC
            LIMIT $3
        """, bot_id, modo, limite_tips)

        tips = []
        for r in tips_rows:
            d = dict(r)
            ts = d.get("apostado_em")
            data_hora = ""
            if ts:
                # Formato: "DD/MM HH:MM"
                from datetime import timezone, timedelta
                # Converte UTC pra BRT (-3)
                ts_brt = ts.astimezone(timezone(timedelta(hours=-3))) if ts.tzinfo else ts
                data_hora = ts_brt.strftime("%d/%m %H:%M")

            ja = d.get("jogador_a") or ""
            jb = d.get("jogador_b") or ""
            confronto = f"{ja} x {jb}" if ja or jb else "—"

            sel = d.get("selecao") or d.get("lado") or ""
            linha = d.get("linha")
            if linha is not None:
                try:
                    linha_str = f"({float(linha)})"
                    selecao_full = f"{sel} {linha_str}".strip()
                except Exception:
                    selecao_full = sel
            else:
                selecao_full = sel

            unidades = d.get("lucro_unidades")
            status_resultado = d.get("resultado") or ("pendente" if d.get("status") == "pendente" else "void")
            status_norm = "green" if status_resultado == "green" else ("red" if status_resultado == "red" else "pendente")

            tips.append({
                "id": d["id"],
                "dataHora": data_hora,
                "confronto": confronto,
                "selecao": selecao_full,
                "odd": float(d["odd"]) if d.get("odd") else 0,
                "unidades": float(unidades) if unidades is not None else 0,
                "status": status_norm,
                "placar_entrada": f"{d.get('placar_a_entrada','-')}x{d.get('placar_b_entrada','-')}",
                "placar_final": f"{d.get('placar_final_a','-')}x{d.get('placar_final_b','-')}" if d.get("placar_final_a") is not None else None,
                "live_time": d.get("live_time"),
            })

        return {
            "bot": {
                "id": bot["id"],
                "nome": bot["nome"],
                "descricao": bot.get("descricao") or "",
                "casa": (bot.get("casa") or "").upper(),
                "esporte": bot.get("esporte"),
                "mercado": bot.get("mercado"),
                "status": bot.get("status"),
                "liga": liga_str,
            },
            "resultadosDiarios": resultados_diarios,
            "tips": tips,
            "totais": totais,
            "dias": dias,
            "periodo": periodo,
            "modo": modo,
        }


@router.get("/{bot_id}/stats")
async def get_stats_bot(
    bot_id: int,
    modo: str = Query("simulado"),
    usuario: dict = Depends(get_current_user),
):
    """
    Stats resumidos do bot pra mostrar inline no card da tela /bots:
    { tips, lucro, greens, reds, roi, wr }
    """
    if modo not in ("simulado", "real"):
        raise HTTPException(400, "modo deve ser 'simulado' ou 'real'")

    async with db() as conn:
        dono = await conn.fetchrow("SELECT user_id FROM bots WHERE id = $1", bot_id)
        if not dono or (not acesso_total(usuario) and dono["user_id"] != usuario.get("id")):
            raise HTTPException(404, f"Bot {bot_id} nao encontrado")

        row = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado = 'green') AS greens,
                COUNT(*) FILTER (WHERE resultado = 'red')   AS reds,
                COUNT(*) FILTER (WHERE resultado IS NULL OR status = 'pendente') AS pendentes,
                COALESCE(SUM(lucro_unidades), 0)::float AS lucro_un,
                COALESCE(AVG(odd) FILTER (WHERE odd IS NOT NULL), 0)::float AS odd_media
            FROM apostas
            WHERE bot_id = $1 AND modo = $2
        """, bot_id, modo)

        d = dict(row) if row else {}
        total = int(d.get("total") or 0)
        greens = int(d.get("greens") or 0)
        reds = int(d.get("reds") or 0)
        resolvidas = greens + reds
        lucro = round(d.get("lucro_un") or 0, 2)
        wr = round((greens / resolvidas) * 100, 1) if resolvidas > 0 else 0
        roi = round((lucro / resolvidas) * 100, 2) if resolvidas > 0 else 0

        return {
            "tips": total,
            "greens": greens,
            "reds": reds,
            "pendentes": int(d.get("pendentes") or 0),
            "lucro": lucro,
            "roi": roi,
            "wr": wr,
            "oddMedia": round(d.get("odd_media") or 0, 2),
        }


# ============================================================
# EXPORT CSV
# ============================================================

CSV_COLUNAS = [
    'id', 'apostado_em', 'resolvido_em', 'modo',
    'bookmaker', 'casa', 'esporte', 'torneio', 'liga', 'event_id',
    'jogador_a', 'jogador_b',
    'mercado', 'mercado_tipo', 'linha', 'selecao', 'lado',
    'odd', 'stake',
    'placar_a_entrada', 'placar_b_entrada',
    'placar_final_a', 'placar_final_b',
    'minuto_entrada', 'periodo_entrada', 'live_time',
    'status', 'resultado', 'pnl', 'lucro_unidades', 'motivo',
    'stats_h2h_json',
]


def _csv_safe(v):
    """Converte valor pra string segura pro CSV (None -> '').
    Datetimes sao convertidos pra BRT e formatados sem timezone suffix
    (Excel pt-BR nao parseia timezone bem).
    """
    if v is None:
        return ''
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(BRT)
        return v.strftime('%Y-%m-%d %H:%M:%S')
    return str(v)


def _slug_filename(s: str, max_len: int = 50) -> str:
    """Slug ASCII pra usar em filename (sem caracter problematico)."""
    out = []
    for c in (s or ''):
        if c.isalnum():
            out.append(c)
        elif c in (' ', '-', '_'):
            out.append('_')
    slug = ''.join(out).strip('_')[:max_len]
    return slug or 'bot'


@router.get("/{bot_id}/export.csv")
async def export_apostas_csv(
    bot_id: int,
    modo: str = Query("simulado", description="simulado | real"),
    excel: bool = Query(True, description="True: UTF-8 BOM + separator ;  False: UTF-8 + ,"),
    periodo: str = Query("todas", description="dia | 3d | 7d | 15d | 30d | todas (todas = sem filtro de data)"),
    resolvidas: bool = Query(False, description="True: só apostas com resultado definido"),
    limit: int = Query(50000, ge=1, le=200000),
    usuario: dict = Depends(get_current_user),
):
    """
    Exporta apostas de um bot pra CSV.
    Cada bot tem seu CSV próprio (rota independente por bot_id).
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

        # 'todas' = sem filtro de data; outras chaves filtram por janela
        if periodo != 'todas':
            dias = PERIODO_DIAS[periodo]
            where.append(f"apostado_em >= NOW() - INTERVAL '{dias} days'")

        if resolvidas:
            where.append("resultado IS NOT NULL")

        sql = f"""
            SELECT
                id, apostado_em, resolvido_em, modo,
                bookmaker, casa, esporte, torneio, liga, event_id,
                jogador_a, jogador_b,
                mercado, mercado_tipo, linha, selecao, lado,
                odd, stake,
                placar_a_entrada, placar_b_entrada,
                placar_final_a, placar_final_b,
                minuto_entrada, periodo_entrada, live_time,
                status, resultado, pnl, lucro_unidades, motivo,
                stats_h2h
            FROM apostas
            WHERE {' AND '.join(where)}
            ORDER BY apostado_em DESC
            LIMIT {limit}
        """
        rows = await conn.fetch(sql, *args)

    # Monta CSV em memoria
    delimiter = ';' if excel else ','
    buf = io.StringIO()
    if excel:
        buf.write('\ufeff')  # BOM pro Excel pt-BR abrir UTF-8 corretamente

    writer = csv.writer(buf, delimiter=delimiter, lineterminator='\n', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_COLUNAS)

    for r in rows:
        d = dict(r)

        # stats_h2h vira string JSON
        stats = d.get('stats_h2h')
        if stats is None:
            stats_str = ''
        elif isinstance(stats, str):
            stats_str = stats
        else:
            stats_str = json.dumps(stats, separators=(',', ':'), ensure_ascii=False)

        row_out = []
        for col in CSV_COLUNAS:
            if col == 'stats_h2h_json':
                row_out.append(stats_str)
            else:
                row_out.append(_csv_safe(d.get(col)))
        writer.writerow(row_out)

    csv_content = buf.getvalue()
    buf.close()

    # Filename: bot_<id>_<slug_nome>_<YYYYMMDD>.csv
    nome_slug = _slug_filename(bot_row['nome'] or 'bot')
    data_str = datetime.now().strftime('%Y%m%d')
    filename = f"bot_{bot_id}_{nome_slug}_{data_str}.csv"

    return Response(
        content=csv_content.encode('utf-8'),
        media_type="text/csv; charset=utf-8",
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            # CORS: expor o header pra fetch() do frontend conseguir ler
            'Access-Control-Expose-Headers': 'Content-Disposition',
        },
    )
