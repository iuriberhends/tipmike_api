"""
security.py — Núcleo de autenticação e autorização do TipMike.

Responsabilidades:
- Hash e verificação de senha (Argon2id via argon2-cffi)
- Criação e validação de JWTs (access e service) via PyJWT
- Geração e hash de refresh tokens opacos
- Dependencies FastAPI: get_current_user, get_current_user_opcional, require_admin
- Rate limiting simples em memória para endpoints sensíveis

Dependências novas:
    pip install argon2-cffi PyJWT

Variáveis de ambiente:
    TIPMIKE_JWT_SECRET    obrigatória em produção (mín. 32 chars).
                          Sem ela, um segredo EFÊMERO é gerado (tokens morrem
                          no restart — refresh tokens do banco sobrevivem).
    TIPMIKE_ACCESS_MIN    minutos de vida do access token (default 30)
    TIPMIKE_REFRESH_DIAS  dias de vida do refresh token (default 30)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import jwt  # PyJWT
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import db

logger = logging.getLogger("tipmike.auth")

# ────────────────────────── Configuração ──────────────────────────

JWT_ALG = "HS256"


def _int_env(nome: str, default: int) -> int:
    """Lê um inteiro de env var com fallback seguro — env malformada nunca derruba a API."""
    bruto = os.getenv(nome)
    if bruto is None or not bruto.strip():
        return default
    try:
        valor = int(bruto.strip())
        if valor <= 0:
            raise ValueError
        return valor
    except (ValueError, TypeError):
        logger.warning("%s inválida (%r); usando default %s.", nome, bruto, default)
        return default


ACCESS_TOKEN_MINUTOS = _int_env("TIPMIKE_ACCESS_MIN", 30)
REFRESH_TOKEN_DIAS = _int_env("TIPMIKE_REFRESH_DIAS", 30)


def _carregar_jwt_secret() -> str:
    segredo = (os.getenv("TIPMIKE_JWT_SECRET") or "").strip()
    if segredo:
        if len(segredo) < 32:
            # Falha explícita: segredo fraco é pior que segredo ausente.
            raise RuntimeError(
                "TIPMIKE_JWT_SECRET muito curto (mínimo 32 caracteres). "
                "Gere um com: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        return segredo
    logger.warning(
        "TIPMIKE_JWT_SECRET não definida — usando segredo EFÊMERO. "
        "Defina a variável em produção para as sessões sobreviverem a restarts."
    )
    return secrets.token_urlsafe(64)


JWT_SECRET = _carregar_jwt_secret()

# ─────────────────────────── Senhas ────────────────────────────────

_ph = PasswordHasher()  # Argon2id com parâmetros default (seguros)

# Hash "isca" para equalizar o tempo de resposta quando o e-mail não existe
# (mitiga enumeração de usuários por timing de resposta).
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))


def hash_senha(senha: str) -> str:
    return _ph.hash(senha)


def verificar_senha(senha: str, senha_hash: str) -> bool:
    """Retorna True/False. Nunca levanta exceção — qualquer anomalia nega o acesso."""
    try:
        _ph.verify(senha_hash, senha)
        return True
    except VerifyMismatchError:
        return False
    except (InvalidHashError, ValueError, TypeError):
        logger.error("Hash de senha inválido/corrompido encontrado no banco.")
        return False
    except Exception:
        logger.exception("Erro inesperado ao verificar senha.")
        return False


def queimar_tempo_verificacao() -> None:
    """Verificação contra o hash isca — chamada quando o usuário não existe."""
    try:
        _ph.verify(_DUMMY_HASH, "senha-incorreta")
    except Exception:
        pass


def senha_precisa_rehash(senha_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(senha_hash)
    except Exception:
        return False


# ─────────────────────── Validação de entrada ──────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalizar_e_validar_email(email: object) -> str:
    if not isinstance(email, str):
        raise HTTPException(status_code=422, detail="E-mail inválido.")
    email = email.strip().lower()
    if not (3 <= len(email) <= 255) or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="E-mail inválido.")
    return email


def validar_nome(nome: object) -> str:
    if not isinstance(nome, str):
        raise HTTPException(status_code=422, detail="Nome inválido.")
    nome = " ".join(nome.split())  # colapsa espaços repetidos
    if not (2 <= len(nome) <= 80):
        raise HTTPException(status_code=422, detail="Nome deve ter entre 2 e 80 caracteres.")
    return nome


def validar_senha(senha: object) -> str:
    if not isinstance(senha, str):
        raise HTTPException(status_code=422, detail="Senha inválida.")
    if not (8 <= len(senha) <= 128):
        raise HTTPException(status_code=422, detail="Senha deve ter entre 8 e 128 caracteres.")
    tem_letra = any(c.isalpha() for c in senha)
    tem_numero = any(c.isdigit() for c in senha)
    if not (tem_letra and tem_numero):
        raise HTTPException(
            status_code=422,
            detail="Senha deve conter ao menos uma letra e um número.",
        )
    return senha


# ─────────────────────────── Tokens ─────────────────────────────────

def criar_access_token(usuario_id: int, email: str, role: str) -> tuple[str, int]:
    """Retorna (token, validade_em_segundos)."""
    agora = int(datetime.now(timezone.utc).timestamp())
    expira_s = ACCESS_TOKEN_MINUTOS * 60
    payload = {
        "sub": str(usuario_id),  # PyJWT >= 2.10 exige sub como string
        "email": email,
        "role": role,
        "type": "access",
        "iat": agora,
        "exp": agora + expira_s,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG), expira_s


def criar_token_servico(nome: str, dias: int = 365, role: str = "service") -> str:
    """Token de longa duração para bots/workers internos (type=service)."""
    agora = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": f"svc:{nome}",
        "role": role,
        "type": "service",
        "iat": agora,
        "exp": agora + dias * 86400,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decodificar_token(token: str) -> dict:
    """Valida assinatura, expiração e claims obrigatórias. Levanta 401 se inválido."""
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALG],  # lista fechada: bloqueia downgrade de algoritmo
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def gerar_refresh_token() -> str:
    return secrets.token_urlsafe(48)  # ~288 bits de entropia


def hash_refresh_token(token: str) -> str:
    # SHA-256 é suficiente: o token é aleatório de alta entropia (não é senha humana).
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ───────────────────── Rate limiting (memória) ──────────────────────

class RateLimiter:
    """
    Limitador por chave (ex.: IP, e-mail) com janela deslizante.
    Estado em memória do processo — suficiente para 1 instância uvicorn.
    """

    def __init__(self, max_tentativas: int, janela_segundos: int):
        self.max = max_tentativas
        self.janela = janela_segundos
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def permitir(self, chave: str) -> bool:
        agora = time.monotonic()
        corte = agora - self.janela
        with self._lock:
            fila = [t for t in self._hits.get(chave, []) if t > corte]
            if len(fila) >= self.max:
                self._hits[chave] = fila
                return False
            fila.append(agora)
            self._hits[chave] = fila
            # Higiene: impede crescimento sem limite do dicionário
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > corte}
            return True


limiter_login = RateLimiter(max_tentativas=8, janela_segundos=60)
limiter_registro = RateLimiter(max_tentativas=5, janela_segundos=300)
limiter_refresh = RateLimiter(max_tentativas=30, janela_segundos=60)


def exigir_rate_limit(limiter: RateLimiter, chave: str) -> None:
    if not limiter.permitir(chave):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas. Aguarde um pouco e tente novamente.",
        )


# ──────────────────── Dependencies (FastAPI) ───────────────────────

_bearer = HTTPBearer(auto_error=False)


def _usuario_de_servico(payload: dict) -> dict:
    return {
        "id": None,
        "email": None,
        "nome": str(payload.get("sub", "servico")),
        "role": str(payload.get("role", "service")),
        "ativo": True,
        "service": True,
    }


async def get_current_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """
    Valida o Bearer token e retorna o usuário.
    - type=access  → confere assinatura E consulta o banco (usuário desativado
                     perde acesso na hora, mesmo com JWT ainda válido).
    - type=service → aceito sem consulta ao banco (bots/workers internos).
    """
    if cred is None or not cred.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decodificar_token(cred.credentials)
    tipo = payload.get("type")

    if tipo == "service":
        return _usuario_de_servico(payload)

    if tipo != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tipo de token não aceito neste endpoint.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        usuario_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        async with db() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, email, nome, role, ativo, criado_em, ultimo_login
                FROM usuarios
                WHERE id = $1
                """,
                usuario_id,
            )
    except Exception:
        logger.exception("Falha ao consultar usuário durante autenticação.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Falha temporária ao validar sessão. Tente novamente.",
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão inválida.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    usuario = dict(row)
    if not usuario.get("ativo"):
        raise HTTPException(status_code=403, detail="Usuário desativado.")

    usuario["service"] = False
    return usuario


async def get_current_user_opcional(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[dict]:
    """Como get_current_user, mas retorna None sem credencial (não levanta 401)."""
    if cred is None or not cred.credentials:
        return None
    try:
        return await get_current_user(cred)
    except HTTPException:
        # Token presente porém inválido: trata como anônimo em contextos opcionais.
        return None


async def require_admin(usuario: dict = Depends(get_current_user)) -> dict:
    if usuario.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Requer permissão de administrador.")
    return usuario


# ─────────────────────── Autorização / escopo (Fase 4) ──────────────────────

def acesso_total(usuario: dict) -> bool:
    """
    True para quem enxerga TODOS os dados: admins e tokens de serviço
    (workers internos). Usuário comum enxerga apenas o que é dele.
    Nunca levanta exceção — qualquer anomalia nega o acesso total.
    """
    try:
        return (usuario or {}).get("role") in ("admin", "service")
    except Exception:
        return False
