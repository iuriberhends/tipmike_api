"""
routers/admin.py — Fase 5: gestão de usuários e convites.

TUDO aqui exige role admin (dependencies do router). Regras de segurança:
- Convite: código exibido UMA vez; só o SHA-256 vai pro banco; uso único,
  com validade e revogação. A role da conta criada vem gravada no convite.
- Admin não desativa/rebaixa/exclui a própria conta.
- O último administrador ativo é intocável (não desativa, não rebaixa, não exclui).
- Desativar ou resetar senha revoga TODAS as sessões (refresh tokens) do alvo.
- Excluir usuário só quando ele não tem bots (senão: desativar ou transferir).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from database import db
from security import (
    gerar_codigo_convite,
    get_current_user,
    hash_codigo_convite,
    hash_senha,
    require_admin,
    validar_nome,
    validar_senha,
)

logger = logging.getLogger("tipmike.admin")

router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])

VALIDADE_CONVITE_DIAS_DEFAULT = 7
VALIDADE_CONVITE_DIAS_MAX = 90


# ─────────────────────────── Modelos ────────────────────────────

class UsuarioPatch(BaseModel):
    nome: Optional[str] = None
    role: Optional[str] = None    # 'admin' | 'user'
    ativo: Optional[bool] = None


class ResetSenhaBody(BaseModel):
    senha_nova: str


class ConviteCreate(BaseModel):
    role: str = "user"
    dias_validade: int = Field(VALIDADE_CONVITE_DIAS_DEFAULT, ge=1, le=VALIDADE_CONVITE_DIAS_MAX)
    nota: Optional[str] = Field(None, max_length=200)


# ─────────────────────────── Helpers ────────────────────────────

async def _revogar_sessoes(conn, usuario_id: int) -> None:
    """Derruba todas as sessões do usuário (refresh tokens)."""
    await conn.execute(
        "UPDATE refresh_tokens SET revogado = true WHERE usuario_id = $1 AND NOT revogado",
        usuario_id,
    )


async def _eh_ultimo_admin_ativo(conn, usuario_id: int) -> bool:
    n = await conn.fetchval(
        "SELECT COUNT(*) FROM usuarios WHERE role = 'admin' AND ativo AND id <> $1",
        usuario_id,
    )
    return (n or 0) == 0


# ─────────────────────────── Usuários ───────────────────────────

@router.get("/usuarios")
async def listar_usuarios(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Busca por nome ou e-mail"),
):
    where = ""
    params = []
    if q:
        params.append(f"%{q}%")
        where = "WHERE (u.nome ILIKE $1 OR u.email ILIKE $1)"

    sql = f"""
        SELECT u.id, u.nome, u.email, u.role, u.ativo, u.criado_em, u.ultimo_login,
               COUNT(b.id)::int AS qtd_bots
        FROM usuarios u
        LEFT JOIN bots b ON b.user_id = u.id
        {where}
        GROUP BY u.id
        ORDER BY u.criado_em ASC, u.id ASC
        LIMIT {limit} OFFSET {offset}
    """
    sql_total = f"SELECT COUNT(*) FROM usuarios u {where}"

    try:
        async with db() as conn:
            rows = await conn.fetch(sql, *params)
            total = await conn.fetchval(sql_total, *params)
    except Exception:
        logger.exception("Erro ao listar usuários.")
        raise HTTPException(status_code=500, detail="Erro interno ao listar usuários.")

    return {"total": total, "limit": limit, "offset": offset,
            "items": [dict(r) for r in rows]}


@router.patch("/usuarios/{usuario_id}")
async def atualizar_usuario(
    usuario_id: int,
    body: UsuarioPatch,
    atual: dict = Depends(get_current_user),
):
    if body.role is not None and body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role deve ser 'admin' ou 'user'.")
    if body.nome is not None:
        body.nome = validar_nome(body.nome)
    if body.nome is None and body.role is None and body.ativo is None:
        raise HTTPException(status_code=400, detail="Nenhum campo enviado.")

    mexe_em_si = usuario_id == atual.get("id")
    if mexe_em_si and (body.ativo is False or (body.role is not None and body.role != "admin")):
        raise HTTPException(
            status_code=400,
            detail="Você não pode desativar ou rebaixar a própria conta.",
        )

    try:
        async with db() as conn:
            async with conn.transaction():
                alvo = await conn.fetchrow(
                    "SELECT id, role, ativo FROM usuarios WHERE id = $1 FOR UPDATE",
                    usuario_id,
                )
                if not alvo:
                    raise HTTPException(status_code=404, detail="Usuário não encontrado.")

                vai_perder_admin = (
                    alvo["role"] == "admin" and alvo["ativo"]
                    and (body.ativo is False or (body.role is not None and body.role != "admin"))
                )
                if vai_perder_admin and await _eh_ultimo_admin_ativo(conn, usuario_id):
                    raise HTTPException(
                        status_code=400,
                        detail="Não é possível desativar ou rebaixar o último administrador ativo.",
                    )

                sets, args = [], []
                for campo in ("nome", "role", "ativo"):
                    valor = getattr(body, campo)
                    if valor is not None:
                        args.append(valor)
                        sets.append(f"{campo} = ${len(args)}")
                args.append(usuario_id)
                row = await conn.fetchrow(
                    f"UPDATE usuarios SET {', '.join(sets)} WHERE id = ${len(args)} "
                    "RETURNING id, nome, email, role, ativo, criado_em, ultimo_login",
                    *args,
                )

                if body.ativo is False:
                    # derruba as sessões na hora (o access morre no próximo request:
                    # get_current_user relê 'ativo' do banco a cada chamada)
                    await _revogar_sessoes(conn, usuario_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao atualizar usuário %s.", usuario_id)
        raise HTTPException(status_code=500, detail="Erro interno ao atualizar usuário.")

    logger.info(
        "Admin %s atualizou usuário %s: %s",
        atual.get("id"), usuario_id, body.model_dump(exclude_none=True),
    )
    return dict(row)


@router.post("/usuarios/{usuario_id}/resetar-senha")
async def resetar_senha(
    usuario_id: int,
    body: ResetSenhaBody,
    atual: dict = Depends(get_current_user),
):
    senha = validar_senha(body.senha_nova)
    try:
        novo_hash = hash_senha(senha)
    except Exception:
        logger.exception("Falha ao gerar hash de senha.")
        raise HTTPException(status_code=500, detail="Erro interno ao processar a senha.")

    try:
        async with db() as conn:
            async with conn.transaction():
                resultado = await conn.execute(
                    "UPDATE usuarios SET senha_hash = $1 WHERE id = $2",
                    novo_hash, usuario_id,
                )
                if resultado == "UPDATE 0":
                    raise HTTPException(status_code=404, detail="Usuário não encontrado.")
                await _revogar_sessoes(conn, usuario_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao resetar senha do usuário %s.", usuario_id)
        raise HTTPException(status_code=500, detail="Erro interno ao resetar a senha.")

    logger.info("Admin %s resetou a senha do usuário %s.", atual.get("id"), usuario_id)
    return {"ok": True, "id": usuario_id, "sessoes_revogadas": True}


@router.delete("/usuarios/{usuario_id}")
async def deletar_usuario(usuario_id: int, atual: dict = Depends(get_current_user)):
    if usuario_id == atual.get("id"):
        raise HTTPException(status_code=400, detail="Você não pode excluir a própria conta.")

    try:
        async with db() as conn:
            async with conn.transaction():
                alvo = await conn.fetchrow(
                    "SELECT id, role, ativo FROM usuarios WHERE id = $1 FOR UPDATE",
                    usuario_id,
                )
                if not alvo:
                    raise HTTPException(status_code=404, detail="Usuário não encontrado.")
                if alvo["role"] == "admin" and alvo["ativo"] and await _eh_ultimo_admin_ativo(conn, usuario_id):
                    raise HTTPException(
                        status_code=400,
                        detail="Não é possível excluir o último administrador ativo.",
                    )

                qtd_bots = await conn.fetchval(
                    "SELECT COUNT(*) FROM bots WHERE user_id = $1", usuario_id
                )
                if qtd_bots:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Usuário possui {qtd_bots} bot(s). Exclua/transfira os bots ou apenas desative a conta.",
                    )

                await conn.execute("DELETE FROM usuarios WHERE id = $1", usuario_id)
                # refresh_tokens caem por ON DELETE CASCADE;
                # convites criados/usados por ele ficam com SET NULL.
    except HTTPException:
        raise
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(
            status_code=409,
            detail="Usuário possui dados vinculados. Desative a conta em vez de excluir.",
        )
    except Exception:
        logger.exception("Erro ao excluir usuário %s.", usuario_id)
        raise HTTPException(status_code=500, detail="Erro interno ao excluir usuário.")

    logger.info("Admin %s excluiu o usuário %s.", atual.get("id"), usuario_id)
    return {"deletado": True, "id": usuario_id}


# ─────────────────────────── Convites ───────────────────────────

@router.post("/convites", status_code=201)
async def criar_convite(body: ConviteCreate, atual: dict = Depends(get_current_user)):
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role deve ser 'admin' ou 'user'.")

    codigo = gerar_codigo_convite()
    expira_em = datetime.now(timezone.utc) + timedelta(days=body.dias_validade)

    try:
        async with db() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO convites (codigo_hash, role, nota, criado_por, expira_em)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, role, nota, expira_em, criado_em
                """,
                hash_codigo_convite(codigo), body.role, body.nota,
                atual.get("id"), expira_em,
            )
    except Exception:
        logger.exception("Erro ao criar convite.")
        raise HTTPException(status_code=500, detail="Erro interno ao criar convite.")

    logger.info("Admin %s criou convite %s (role=%s).", atual.get("id"), row["id"], body.role)
    return {
        **dict(row),
        "codigo": codigo,
        "aviso": "Copie o código agora — por segurança ele não é exibido novamente.",
    }


@router.get("/convites")
async def listar_convites(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    sql = f"""
        SELECT c.id, c.role, c.nota, c.revogado, c.expira_em, c.criado_em, c.usado_em,
               cri.nome AS criado_por_nome,
               usa.nome AS usado_por_nome,
               CASE
                   WHEN c.usado_em IS NOT NULL THEN 'usado'
                   WHEN c.revogado THEN 'revogado'
                   WHEN c.expira_em <= now() THEN 'expirado'
                   ELSE 'pendente'
               END AS situacao
        FROM convites c
        LEFT JOIN usuarios cri ON cri.id = c.criado_por
        LEFT JOIN usuarios usa ON usa.id = c.usado_por
        ORDER BY c.criado_em DESC, c.id DESC
        LIMIT {limit} OFFSET {offset}
    """
    try:
        async with db() as conn:
            rows = await conn.fetch(sql)
            total = await conn.fetchval("SELECT COUNT(*) FROM convites")
    except Exception:
        logger.exception("Erro ao listar convites.")
        raise HTTPException(status_code=500, detail="Erro interno ao listar convites.")

    return {"total": total, "limit": limit, "offset": offset,
            "items": [dict(r) for r in rows]}


@router.delete("/convites/{convite_id}")
async def revogar_convite(convite_id: int, atual: dict = Depends(get_current_user)):
    """Revoga um convite ainda não usado (idempotente se já revogado)."""
    try:
        async with db() as conn:
            async with conn.transaction():
                convite = await conn.fetchrow(
                    "SELECT id, usado_em, revogado FROM convites WHERE id = $1 FOR UPDATE",
                    convite_id,
                )
                if not convite:
                    raise HTTPException(status_code=404, detail="Convite não encontrado.")
                if convite["usado_em"] is not None:
                    raise HTTPException(status_code=409, detail="Convite já foi usado — não dá pra revogar.")
                if not convite["revogado"]:
                    await conn.execute(
                        "UPDATE convites SET revogado = true WHERE id = $1", convite_id
                    )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao revogar convite %s.", convite_id)
        raise HTTPException(status_code=500, detail="Erro interno ao revogar convite.")

    logger.info("Admin %s revogou o convite %s.", atual.get("id"), convite_id)
    return {"revogado": True, "id": convite_id}
