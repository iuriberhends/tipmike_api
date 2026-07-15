"""
routers/apostas.py
CRUD de apostas registradas pelos bots.

Fase 4 (ownership): usuário comum enxerga apenas apostas de bots
próprios; admin/serviço enxerga tudo. A posse é derivada do bot
(apostas.bot_id -> bots.user_id) — apostas sem bot (manuais/globais)
são visíveis apenas para admin/serviço.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from datetime import datetime
from database import db
from models import ApostaCreate, ApostaUpdate
from security import get_current_user, acesso_total

logger = logging.getLogger("tipmike.apostas")

router = APIRouter(prefix="/apostas", tags=["Apostas"])


async def _dono_da_aposta(conn, aposta_id: int):
    """Busca a aposta com o dono do bot junto (coluna extra _dono_id)."""
    return await conn.fetchrow(
        """
        SELECT a.*, b.user_id AS _dono_id
        FROM apostas a
        LEFT JOIN bots b ON b.id = a.bot_id
        WHERE a.id = $1
        """,
        aposta_id,
    )


def _sem_acesso(usuario: dict, dono_id) -> bool:
    """Aposta de bot alheio (ou sem bot) é invisível pra usuário comum."""
    if acesso_total(usuario):
        return False
    return dono_id is None or dono_id != usuario.get("id")


@router.get("")
async def listar_apostas(
    bot_id: Optional[int] = None,
    modo: Optional[str] = None,
    resultado: Optional[str] = None,
    casa: Optional[str] = None,
    jogador: Optional[str] = None,
    data_inicio: Optional[str] = None,
    data_fim: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    usuario: dict = Depends(get_current_user),
):
    conditions = ["1=1"]
    params = []
    i = 1

    # Ownership: usuário comum só enxerga apostas dos próprios bots.
    if not acesso_total(usuario):
        conditions.append(f"bot_id IN (SELECT id FROM bots WHERE user_id = ${i})")
        params.append(usuario.get("id")); i += 1

    if bot_id:
        conditions.append(f"bot_id = ${i}"); params.append(bot_id); i += 1
    if modo:
        conditions.append(f"modo = ${i}"); params.append(modo); i += 1
    if resultado:
        conditions.append(f"resultado = ${i}"); params.append(resultado); i += 1
    if casa:
        conditions.append(f"casa = ${i}"); params.append(casa); i += 1
    if jogador:
        conditions.append(f"(jogador_a ILIKE ${i} OR jogador_b ILIKE ${i})")
        params.append(f"%{jogador}%"); i += 1
    if data_inicio:
        conditions.append(f"apostado_em >= ${i}"); params.append(data_inicio); i += 1
    if data_fim:
        conditions.append(f"apostado_em <= ${i}"); params.append(data_fim); i += 1

    where = " AND ".join(conditions)
    params += [limit, offset]

    sql = f"""
        SELECT * FROM apostas
        WHERE {where}
        ORDER BY apostado_em DESC
        LIMIT ${i} OFFSET ${i+1}
    """

    try:
        async with db() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception:
        logger.exception("Erro ao listar apostas.")
        raise HTTPException(status_code=500, detail="Erro interno ao listar apostas.")

    return [dict(r) for r in rows]


@router.get("/{aposta_id}")
async def get_aposta(aposta_id: int, usuario: dict = Depends(get_current_user)):
    try:
        async with db() as conn:
            row = await _dono_da_aposta(conn, aposta_id)
    except Exception:
        logger.exception("Erro ao buscar aposta %s.", aposta_id)
        raise HTTPException(status_code=500, detail="Erro interno ao buscar aposta.")

    if not row or _sem_acesso(usuario, row["_dono_id"]):
        # 404 também pra aposta alheia: não vaza existência.
        raise HTTPException(status_code=404, detail="Aposta não encontrada")

    d = dict(row)
    d.pop("_dono_id", None)
    return d


@router.post("", status_code=201)
async def criar_aposta(aposta: ApostaCreate, usuario: dict = Depends(get_current_user)):
    try:
        async with db() as conn:
            # Ownership na criação: bot informado precisa ser seu (ou ser admin/serviço).
            if aposta.bot_id:
                if acesso_total(usuario):
                    dono = await conn.fetchrow("SELECT id FROM bots WHERE id = $1", aposta.bot_id)
                else:
                    dono = await conn.fetchrow(
                        "SELECT id FROM bots WHERE id = $1 AND user_id = $2",
                        aposta.bot_id, usuario.get("id"),
                    )
                if not dono:
                    raise HTTPException(status_code=404, detail=f"Bot {aposta.bot_id} nao encontrado")
            elif not acesso_total(usuario):
                raise HTTPException(
                    status_code=403,
                    detail="Aposta sem bot (manual/global) é restrita a administradores.",
                )

            sql = """
                INSERT INTO apostas (
                    bot_id, modo, casa, esporte, torneio,
                    jogador_a, jogador_b, event_id,
                    mercado, linha, odd, lado,
                    placar_a_entrada, placar_b_entrada,
                    minuto_entrada, periodo_entrada
                ) VALUES (
                    $1,$2,$3,$4,$5,
                    $6,$7,$8,
                    $9,$10,$11,$12,
                    $13,$14,
                    $15,$16
                )
                RETURNING *
            """
            row = await conn.fetchrow(
                sql,
                aposta.bot_id, aposta.modo, aposta.casa, aposta.esporte, aposta.torneio,
                aposta.jogador_a, aposta.jogador_b, aposta.event_id,
                aposta.mercado, aposta.linha, aposta.odd, aposta.lado,
                aposta.placar_a_entrada, aposta.placar_b_entrada,
                aposta.minuto_entrada, aposta.periodo_entrada,
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao criar aposta.")
        raise HTTPException(status_code=500, detail="Erro interno ao criar aposta.")

    return dict(row)


@router.put("/{aposta_id}")
async def atualizar_aposta(aposta_id: int, aposta: ApostaUpdate, usuario: dict = Depends(get_current_user)):
    try:
        async with db() as conn:
            existing = await _dono_da_aposta(conn, aposta_id)
            if not existing or _sem_acesso(usuario, existing["_dono_id"]):
                raise HTTPException(status_code=404, detail="Aposta não encontrada")

            updates = []
            params = []
            i = 1

            fields = aposta.model_dump(exclude_none=True)
            for key, value in fields.items():
                updates.append(f"{key} = ${i}")
                params.append(value)
                i += 1

            if not updates:
                raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

            params.append(aposta_id)
            sql = f"UPDATE apostas SET {', '.join(updates)} WHERE id = ${i} RETURNING *"
            row = await conn.fetchrow(sql, *params)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao atualizar aposta %s.", aposta_id)
        raise HTTPException(status_code=500, detail="Erro interno ao atualizar aposta.")

    return dict(row)


@router.delete("/{aposta_id}")
async def deletar_aposta(aposta_id: int, usuario: dict = Depends(get_current_user)):
    try:
        async with db() as conn:
            existing = await _dono_da_aposta(conn, aposta_id)
            if not existing or _sem_acesso(usuario, existing["_dono_id"]):
                raise HTTPException(status_code=404, detail="Aposta não encontrada")

            row = await conn.fetchrow(
                "DELETE FROM apostas WHERE id = $1 RETURNING id",
                aposta_id,
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao deletar aposta %s.", aposta_id)
        raise HTTPException(status_code=500, detail="Erro interno ao deletar aposta.")

    if not row:
        raise HTTPException(status_code=404, detail="Aposta não encontrada")
    return {"mensagem": "Aposta removida", "id": row["id"]}


@router.post("/manual")
async def aposta_manual(aposta: ApostaCreate, usuario: dict = Depends(get_current_user)):
    aposta.modo = "manual"
    return await criar_aposta(aposta, usuario)
