"""
routers/bot_treinamento.py - Endpoint pra toggle do modo treinamento

Como plugar no main.py:
    from routers import bot_treinamento
    app.include_router(bot_treinamento.router)

Endpoint:
    PATCH /bots/{id}/treinamento  body: {"em_treinamento": true|false}
    -> retorna {"id":, "nome":, "em_treinamento": ...}
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import db

router = APIRouter(prefix="/bots", tags=["bots"])


class TreinamentoToggle(BaseModel):
    em_treinamento: bool


@router.patch("/{bot_id}/treinamento")
async def toggle_treinamento(bot_id: int, payload: TreinamentoToggle):
    """
    Liga/desliga modo treinamento de um bot.

    Em treinamento = bot continua simulando, mas NAO envia Telegram.
    """
    async with db() as conn:
        row = await conn.fetchrow(
            """
            UPDATE bots
            SET em_treinamento = $1, atualizado_em = NOW()
            WHERE id = $2
            RETURNING id, nome, em_treinamento, telegram_canal_id
            """,
            payload.em_treinamento,
            bot_id,
        )

    if not row:
        raise HTTPException(404, f"Bot {bot_id} nao encontrado")

    return {
        "id": row["id"],
        "nome": row["nome"],
        "em_treinamento": row["em_treinamento"],
        "telegram_canal_id": row["telegram_canal_id"],
    }
