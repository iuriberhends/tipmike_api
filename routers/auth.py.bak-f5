"""
routers/auth.py — Registro, login, refresh e sessão do usuário.

Endpoints:
    POST /auth/registro      cria usuário (controlado por TIPMIKE_REGISTRO_ABERTO)
    POST /auth/login         emite access + refresh token
    POST /auth/refresh       rotaciona o refresh e emite novo access
    POST /auth/logout        revoga o refresh token informado (idempotente)
    POST /auth/logout-todas  revoga todas as sessões do usuário autenticado
    GET  /auth/me            dados do usuário autenticado

Env:
    TIPMIKE_REGISTRO_ABERTO  "1" abre o registro público (default: fechado —
                             admins autenticados sempre podem criar usuários).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request

from database import db
from models import (
    LoginRequest,
    RefreshRequest,
    RegistroRequest,
    TokenResponse,
    UsuarioOut,
)
from security import (
    REFRESH_TOKEN_DIAS,
    criar_access_token,
    exigir_rate_limit,
    gerar_refresh_token,
    get_current_user,
    get_current_user_opcional,
    hash_refresh_token,
    hash_senha,
    limiter_login,
    limiter_refresh,
    limiter_registro,
    normalizar_e_validar_email,
    queimar_tempo_verificacao,
    senha_precisa_rehash,
    validar_nome,
    validar_senha,
    verificar_senha,
)

logger = logging.getLogger("tipmike.auth")

router = APIRouter(prefix="/auth", tags=["Auth"])

# Mensagem única pra qualquer falha de credencial — não revela se o e-mail
# existe, se a senha errou ou se a conta está desativada.
MSG_CREDENCIAIS = "E-mail ou senha inválidos."


def _registro_aberto() -> bool:
    return os.getenv("TIPMIKE_REGISTRO_ABERTO", "0").strip().lower() in ("1", "true", "sim", "yes")


def _ip_cliente(request: Request) -> str:
    """IP real do cliente. Atrás de reverse proxy (Caddy/nginx) vem no X-Forwarded-For."""
    try:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()[:64] or "desconhecido"
        return (request.client.host if request.client else "desconhecido")[:64]
    except Exception:
        return "desconhecido"


async def _emitir_tokens(conn, usuario: dict, ip: str) -> TokenResponse:
    """Cria o par access+refresh e persiste o refresh (hasheado) no banco."""
    access, expira_s = criar_access_token(usuario["id"], usuario["email"], usuario["role"])
    refresh = gerar_refresh_token()
    expira_em = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_DIAS)

    await conn.execute(
        """
        INSERT INTO refresh_tokens (usuario_id, token_hash, expira_em, criado_ip)
        VALUES ($1, $2, $3, $4)
        """,
        usuario["id"], hash_refresh_token(refresh), expira_em, ip,
    )

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expira_em_segundos=expira_s,
        usuario=UsuarioOut(
            id=usuario["id"],
            email=usuario["email"],
            nome=usuario["nome"],
            role=usuario["role"],
            ativo=usuario["ativo"],
            criado_em=usuario.get("criado_em"),
            ultimo_login=usuario.get("ultimo_login"),
        ),
    )


# ─────────────────────────── Registro ──────────────────────────────

@router.post("/registro", response_model=UsuarioOut, status_code=201)
async def registro(
    body: RegistroRequest,
    request: Request,
    solicitante: Optional[dict] = Depends(get_current_user_opcional),
):
    ip = _ip_cliente(request)
    exigir_rate_limit(limiter_registro, f"registro:{ip}")

    eh_admin = bool(solicitante and solicitante.get("role") == "admin")
    if not _registro_aberto() and not eh_admin:
        raise HTTPException(status_code=403, detail="Registro fechado. Contate um administrador.")

    email = normalizar_e_validar_email(body.email)
    nome = validar_nome(body.nome)
    senha = validar_senha(body.senha)

    # role só é honrada quando quem cria é admin; caso contrário é sempre 'user'.
    role = "user"
    if eh_admin and body.role in ("admin", "user"):
        role = body.role

    try:
        senha_hash = hash_senha(senha)
    except Exception:
        logger.exception("Falha ao gerar hash de senha.")
        raise HTTPException(status_code=500, detail="Erro interno ao processar o cadastro.")

    try:
        async with db() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO usuarios (email, nome, senha_hash, role)
                VALUES ($1, $2, $3, $4)
                RETURNING id, email, nome, role, ativo, criado_em, ultimo_login
                """,
                email, nome, senha_hash, role,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="E-mail já cadastrado.")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Falha ao inserir usuário.")
        raise HTTPException(status_code=500, detail="Erro interno ao processar o cadastro.")

    logger.info("Usuário criado: id=%s role=%s por_admin=%s ip=%s", row["id"], role, eh_admin, ip)
    return dict(row)


# ─────────────────────────── Login ──────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    ip = _ip_cliente(request)
    email_chave = (body.email or "").strip().lower()[:255]
    exigir_rate_limit(limiter_login, f"login-ip:{ip}")
    exigir_rate_limit(limiter_login, f"login-email:{email_chave}")

    try:
        email = normalizar_e_validar_email(body.email)
    except HTTPException:
        # E-mail malformado: resposta genérica pra não vazar informação.
        queimar_tempo_verificacao()
        raise HTTPException(status_code=401, detail=MSG_CREDENCIAIS)

    try:
        async with db() as conn:
            usuario = await conn.fetchrow(
                """
                SELECT id, email, nome, senha_hash, role, ativo, criado_em, ultimo_login
                FROM usuarios
                WHERE lower(email) = $1
                """,
                email,
            )

            if usuario is None:
                queimar_tempo_verificacao()
                raise HTTPException(status_code=401, detail=MSG_CREDENCIAIS)

            usuario = dict(usuario)

            if not verificar_senha(body.senha or "", usuario["senha_hash"]):
                logger.warning("Login falhou (senha) usuario_id=%s ip=%s", usuario["id"], ip)
                raise HTTPException(status_code=401, detail=MSG_CREDENCIAIS)

            if not usuario["ativo"]:
                # Resposta genérica; o motivo real fica só no log.
                logger.warning("Login negado: usuário %s desativado (ip=%s).", usuario["id"], ip)
                raise HTTPException(status_code=401, detail=MSG_CREDENCIAIS)

            # Atualiza o hash se os parâmetros do Argon2 evoluíram desde o cadastro
            if senha_precisa_rehash(usuario["senha_hash"]):
                try:
                    await conn.execute(
                        "UPDATE usuarios SET senha_hash = $1 WHERE id = $2",
                        hash_senha(body.senha), usuario["id"],
                    )
                except Exception:
                    logger.exception("Falha no rehash de senha (não bloqueia o login).")

            await conn.execute(
                "UPDATE usuarios SET ultimo_login = now() WHERE id = $1",
                usuario["id"],
            )

            # Higiene oportunista: remove tokens expirados há mais de 7 dias
            try:
                await conn.execute(
                    "DELETE FROM refresh_tokens WHERE expira_em < now() - interval '7 days'"
                )
            except Exception:
                logger.exception("Falha na limpeza de refresh tokens (não bloqueia o login).")

            resposta = await _emitir_tokens(conn, usuario, ip)

    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro inesperado no login.")
        raise HTTPException(status_code=500, detail="Erro interno ao efetuar login.")

    logger.info("Login OK: usuario_id=%s ip=%s", usuario["id"], ip)
    return resposta


# ─────────────────────────── Refresh ────────────────────────────────

@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request):
    ip = _ip_cliente(request)
    exigir_rate_limit(limiter_refresh, f"refresh:{ip}")

    token = (body.refresh_token or "").strip()
    if not token or len(token) > 512:
        raise HTTPException(status_code=401, detail="Sessão inválida. Faça login novamente.")

    token_hash = hash_refresh_token(token)

    try:
        async with db() as conn:
            row = await conn.fetchrow(
                """
                SELECT rt.id AS rt_id, rt.usuario_id, rt.expira_em, rt.revogado,
                       u.id, u.email, u.nome, u.role, u.ativo, u.criado_em, u.ultimo_login
                FROM refresh_tokens rt
                JOIN usuarios u ON u.id = rt.usuario_id
                WHERE rt.token_hash = $1
                """,
                token_hash,
            )

            if row is None:
                raise HTTPException(status_code=401, detail="Sessão inválida. Faça login novamente.")

            if row["revogado"]:
                # Reuso de token já rotacionado = possível roubo → derruba TODAS as sessões.
                logger.warning(
                    "REUSO de refresh token detectado (usuario_id=%s ip=%s). Revogando todas as sessões.",
                    row["usuario_id"], ip,
                )
                await conn.execute(
                    "UPDATE refresh_tokens SET revogado = true WHERE usuario_id = $1",
                    row["usuario_id"],
                )
                raise HTTPException(status_code=401, detail="Sessão inválida. Faça login novamente.")

            if row["expira_em"] <= datetime.now(timezone.utc):
                raise HTTPException(status_code=401, detail="Sessão expirada. Faça login novamente.")

            if not row["ativo"]:
                raise HTTPException(status_code=403, detail="Usuário desativado.")

            # Rotação: invalida o token usado e emite um par novo.
            await conn.execute(
                "UPDATE refresh_tokens SET revogado = true WHERE id = $1",
                row["rt_id"],
            )

            usuario = {
                k: row[k]
                for k in ("id", "email", "nome", "role", "ativo", "criado_em", "ultimo_login")
            }
            resposta = await _emitir_tokens(conn, usuario, ip)

    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro inesperado no refresh.")
        raise HTTPException(status_code=500, detail="Erro interno ao renovar sessão.")

    return resposta


# ─────────────────────────── Logout ─────────────────────────────────

@router.post("/logout")
async def logout(body: RefreshRequest):
    """Revoga o refresh token informado. Idempotente: sempre responde ok."""
    token = (body.refresh_token or "").strip()
    if token and len(token) <= 512:
        try:
            async with db() as conn:
                await conn.execute(
                    "UPDATE refresh_tokens SET revogado = true WHERE token_hash = $1",
                    hash_refresh_token(token),
                )
        except Exception:
            logger.exception("Erro ao revogar refresh token no logout.")
    return {"ok": True}


@router.post("/logout-todas")
async def logout_todas(usuario: dict = Depends(get_current_user)):
    """Revoga todas as sessões (refresh tokens) do usuário autenticado."""
    if usuario.get("service") or usuario.get("id") is None:
        raise HTTPException(status_code=400, detail="Endpoint disponível apenas para usuários.")
    try:
        async with db() as conn:
            await conn.execute(
                "UPDATE refresh_tokens SET revogado = true WHERE usuario_id = $1 AND revogado = false",
                usuario["id"],
            )
    except Exception:
        logger.exception("Erro ao revogar sessões do usuário %s.", usuario.get("id"))
        raise HTTPException(status_code=500, detail="Erro interno ao encerrar sessões.")
    return {"ok": True}


# ─────────────────────────── Sessão ─────────────────────────────────

@router.get("/me", response_model=UsuarioOut)
async def me(usuario: dict = Depends(get_current_user)):
    return {
        "id": usuario.get("id"),
        "email": usuario.get("email"),
        "nome": usuario.get("nome"),
        "role": usuario.get("role"),
        "ativo": bool(usuario.get("ativo", True)),
        "criado_em": usuario.get("criado_em"),
        "ultimo_login": usuario.get("ultimo_login"),
    }
