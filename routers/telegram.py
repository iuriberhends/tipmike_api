"""
routers/telegram.py - Endpoints de gerenciamento de canais Telegram

Endpoints:
- GET    /telegram-canais          → lista todos
- POST   /telegram-canais          → cria novo
- PATCH  /telegram-canais/:id      → edita
- DELETE /telegram-canais/:id      → remove
- POST   /telegram-canais/:id/test → envia mensagem teste pro canal
"""
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import db

# Carrega .env se ainda nao foi carregado
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

router = APIRouter(prefix="/telegram-canais", tags=["telegram"])


# ============================================================
# MODELS
# ============================================================
class CanalCreate(BaseModel):
    nome: str = Field(..., min_length=1, max_length=100)
    chat_id: str = Field(..., min_length=1, max_length=50)
    descricao: Optional[str] = None
    ativo: bool = True


class CanalUpdate(BaseModel):
    nome: Optional[str] = None
    chat_id: Optional[str] = None
    descricao: Optional[str] = None
    ativo: Optional[bool] = None


# ============================================================
# ENDPOINTS
# ============================================================
@router.get("")
async def listar_canais():
    """Lista todos os canais"""
    async with db() as conn:
        rows = await conn.fetch(
            "SELECT * FROM telegram_canais ORDER BY id"
        )
        return [dict(r) for r in rows]


@router.post("", status_code=201)
async def criar_canal(payload: CanalCreate):
    """Cria novo canal"""
    async with db() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO telegram_canais (nome, chat_id, descricao, ativo)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                payload.nome.strip(),
                payload.chat_id.strip(),
                payload.descricao,
                payload.ativo,
            )
            return dict(row)
        except Exception as e:
            if 'duplicate key' in str(e) or 'unique' in str(e).lower():
                raise HTTPException(409, f"chat_id '{payload.chat_id}' já existe")
            raise HTTPException(500, str(e))


@router.patch("/{canal_id}")
async def atualizar_canal(canal_id: int, payload: CanalUpdate):
    """Atualiza canal"""
    fields = []
    values = []
    idx = 1
    data = payload.model_dump(exclude_unset=True)

    if not data:
        raise HTTPException(400, "Nenhum campo informado")

    for k, v in data.items():
        if isinstance(v, str): v = v.strip()
        fields.append(f"{k} = ${idx}")
        values.append(v)
        idx += 1

    values.append(canal_id)

    sql = f"UPDATE telegram_canais SET {', '.join(fields)} WHERE id = ${idx} RETURNING *"

    async with db() as conn:
        try:
            row = await conn.fetchrow(sql, *values)
            if not row:
                raise HTTPException(404, f"Canal {canal_id} não encontrado")
            return dict(row)
        except HTTPException:
            raise
        except Exception as e:
            if 'duplicate key' in str(e) or 'unique' in str(e).lower():
                raise HTTPException(409, "chat_id já está em uso")
            raise HTTPException(500, str(e))


@router.delete("/{canal_id}", status_code=204)
async def deletar_canal(canal_id: int):
    """Remove canal. Bots ligados a ele ficam com telegram_canal_id NULL (ON DELETE SET NULL)"""
    async with db() as conn:
        result = await conn.execute("DELETE FROM telegram_canais WHERE id = $1", canal_id)
        if result.endswith('0'):
            raise HTTPException(404, f"Canal {canal_id} não encontrado")
    return None


@router.post("/{canal_id}/test")
async def testar_canal(canal_id: int):
    """Envia mensagem de teste pro canal"""
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(500, "TELEGRAM_BOT_TOKEN não configurado no .env")

    async with db() as conn:
        canal = await conn.fetchrow(
            "SELECT * FROM telegram_canais WHERE id = $1",
            canal_id
        )

    if not canal:
        raise HTTPException(404, f"Canal {canal_id} não encontrado")

    if canal['chat_id'].startswith('PLACEHOLDER_'):
        raise HTTPException(400, "Canal está com chat_id placeholder. Atualize antes de testar.")

    msg = (
        f"🟢 <b>Teste TipMike</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Canal: <b>{canal['nome']}</b>\n"
        f"chat_id: <code>{canal['chat_id']}</code>\n"
        f"Status: {'✅ ativo' if canal['ativo'] else '⚠️ inativo'}\n\n"
        f"Se você está vendo essa mensagem, a integração está funcionando!"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': canal['chat_id'],
        'text': msg,
        'parse_mode': 'HTML',
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(url, json=payload)
            data = r.json()
        except Exception as e:
            raise HTTPException(500, f"Erro ao chamar Telegram API: {e}")

    if r.status_code == 200 and data.get('ok'):
        return {
            "ok": True,
            "message": f"Mensagem enviada pro canal '{canal['nome']}'",
            "telegram_response": data,
        }

    # Erros comuns
    desc = data.get('description', 'Erro desconhecido')
    if r.status_code == 400:
        # chat not found, chat_id mal formatado etc
        raise HTTPException(400, f"Telegram rejeitou: {desc}. Verifique se o chat_id está correto e o bot é admin do canal.")
    if r.status_code == 403:
        raise HTTPException(403, f"Telegram bloqueou: {desc}. Adicione o bot como admin do canal.")

    raise HTTPException(r.status_code, f"Telegram retornou {r.status_code}: {desc}")
