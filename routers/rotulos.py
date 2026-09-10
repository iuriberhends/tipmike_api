# -*- coding: utf-8 -*-
"""
routers/rotulos.py — APELIDOS (so' organizacao)

Guarda um nome livre por objeto (parquet, backtest, varredura, esteira) pra
tela parar de exigir que o usuario decore `4b2e..._mikedb_estrelabet_2026-05-19
_2026-09-07.parquet`. NENHUM worker le' daqui: e' rotulo, nao configuracao.

Endpoints
  GET  /rotulos?escopo=parquet          -> {"itens": {chave: nome, ...}}
  GET  /rotulos/{escopo}/{chave}        -> {"nome": "..."} (404 se nao tem)
  PUT  /rotulos                         -> define (nome vazio = apaga)

Chave: pro parquet e' o caminho/upload_id; pros jobs e' o id em texto.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rotulos", tags=["rotulos"])

ESCOPOS = ("parquet", "backtest", "varredura", "esteira")


class RotuloIn(BaseModel):
    escopo: str = Field(..., max_length=20)
    chave: str = Field(..., max_length=500)
    nome: str = Field(default="", max_length=120)


def _valida_escopo(escopo: str) -> str:
    e = (escopo or "").strip().lower()
    if e not in ESCOPOS:
        raise HTTPException(400, f"escopo invalido: use um de {list(ESCOPOS)}")
    return e


async def apelidos(escopo: str, chaves=None) -> dict:
    """Helper pros outros routers: {chave: nome} do escopo. Nunca levanta —
    apelido e' enfeite, lista sem apelido e' melhor que lista quebrada."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            if chaves:
                rows = await conn.fetch(
                    "SELECT chave, nome FROM rotulos WHERE escopo = $1 "
                    "AND chave = ANY($2::text[])",
                    escopo, [str(c) for c in chaves])
            else:
                rows = await conn.fetch(
                    "SELECT chave, nome FROM rotulos WHERE escopo = $1", escopo)
        return {r["chave"]: r["nome"] for r in rows}
    except Exception as e:                       # tabela ainda nao migrada etc
        logger.warning(f"[rotulos] leitura falhou (segue sem apelido): {e}")
        return {}


@router.get("")
async def listar(escopo: str = Query(..., description="parquet|backtest|varredura|esteira")):
    return {"escopo": _valida_escopo(escopo), "itens": await apelidos(_valida_escopo(escopo))}


@router.get("/{escopo}/{chave:path}")
async def um(escopo: str, chave: str):
    itens = await apelidos(_valida_escopo(escopo), [chave])
    if chave not in itens:
        raise HTTPException(404, "sem apelido")
    return {"escopo": escopo, "chave": chave, "nome": itens[chave]}


@router.put("")
async def definir(body: RotuloIn):
    escopo = _valida_escopo(body.escopo)
    chave = (body.chave or "").strip()
    if not chave:
        raise HTTPException(400, "chave vazia")
    nome = (body.nome or "").strip()
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            if not nome:                          # nome vazio = apagar
                await conn.execute(
                    "DELETE FROM rotulos WHERE escopo = $1 AND chave = $2",
                    escopo, chave)
                return {"ok": True, "apagado": True}
            await conn.execute(
                """INSERT INTO rotulos (escopo, chave, nome)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (escopo, chave)
                   DO UPDATE SET nome = EXCLUDED.nome, atualizado_em = now()""",
                escopo, chave, nome)
        return {"ok": True, "escopo": escopo, "chave": chave, "nome": nome}
    except Exception as e:
        msg = str(e).lower()
        if "rotulos" in msg and ("does not exist" in msg or "relation" in msg):
            raise HTTPException(
                500, "Tabela `rotulos` ausente. Rode migrations/029_rotulos.sql")
        raise HTTPException(500, f"falha salvando apelido: {e}")
