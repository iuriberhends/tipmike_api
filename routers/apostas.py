"""
routers/apostas.py
CRUD de apostas registradas pelos bots.
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime
from database import db
from models import ApostaCreate, ApostaUpdate

router = APIRouter(prefix="/apostas", tags=["Apostas"])


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
):
    conditions = ["1=1"]
    params = []
    i = 1

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

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]


@router.get("/{aposta_id}")
async def get_aposta(aposta_id: int):
    async with db() as conn:
        row = await conn.fetchrow("SELECT * FROM apostas WHERE id = $1", aposta_id)

    if not row:
        raise HTTPException(status_code=404, detail="Aposta não encontrada")

    return dict(row)


@router.post("", status_code=201)
async def criar_aposta(aposta: ApostaCreate):
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
    async with db() as conn:
        row = await conn.fetchrow(
            sql,
            aposta.bot_id, aposta.modo, aposta.casa, aposta.esporte, aposta.torneio,
            aposta.jogador_a, aposta.jogador_b, aposta.event_id,
            aposta.mercado, aposta.linha, aposta.odd, aposta.lado,
            aposta.placar_a_entrada, aposta.placar_b_entrada,
            aposta.minuto_entrada, aposta.periodo_entrada,
        )

    return dict(row)


@router.put("/{aposta_id}")
async def atualizar_aposta(aposta_id: int, aposta: ApostaUpdate):
    async with db() as conn:
        existing = await conn.fetchrow("SELECT id FROM apostas WHERE id = $1", aposta_id)
        if not existing:
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

    return dict(row)


@router.delete("/{aposta_id}")
async def deletar_aposta(aposta_id: int):
    async with db() as conn:
        row = await conn.fetchrow(
            "DELETE FROM apostas WHERE id = $1 RETURNING id",
            aposta_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Aposta não encontrada")
    return {"mensagem": "Aposta removida", "id": row["id"]}


@router.post("/manual")
async def aposta_manual(aposta: ApostaCreate):
    aposta.modo = "manual"
    return await criar_aposta(aposta)
