"""
models.py
Schemas Pydantic para request/response da API.
"""

from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime, date


# ── Sistema ────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    versao: str
    db: str
    ticks_total: int


# ── Ticks ─────────────────────────────────────────────────────────

class Tick(BaseModel):
    id: int
    ts: datetime
    bookmaker: str
    sport: Optional[str]
    liga: Optional[str]
    event_id: str
    evento: Optional[str]
    jogador_a: Optional[str]
    jogador_b: Optional[str]
    score_home: Optional[int]
    score_away: Optional[int]
    live_time: Optional[str]
    mercado: Optional[str]
    mercado_tipo: Optional[str]
    linha: Optional[str]
    selecao: Optional[str]
    odds: Optional[float]
    odd_status: Optional[int]


# ── Eventos ────────────────────────────────────────────────────────

class Evento(BaseModel):
    event_id: str
    bookmaker: str
    sport: Optional[str]
    liga: Optional[str]
    jogador_a: Optional[str]
    jogador_b: Optional[str]
    score_home: Optional[int]
    score_away: Optional[int]
    live_time: Optional[str]
    ultimo_tick: Optional[datetime]


# ── H2H ───────────────────────────────────────────────────────────

class H2HStats(BaseModel):
    jogador_a: str
    jogador_b: str
    total_jogos: int
    vitorias_a: int
    vitorias_b: int
    empates: int
    media_gols_ft: Optional[float]
    media_gols_ht: Optional[float]
    ultimo_jogo: Optional[datetime]


class H2HJogo(BaseModel):
    ts: datetime
    bookmaker: str
    liga: Optional[str]
    score_home: Optional[int]
    score_away: Optional[int]
    event_id: str


# ── Bots ───────────────────────────────────────────────────────────

class BotCreate(BaseModel):
    nome: str
    descricao: Optional[str] = None
    casa: str
    esporte: str
    mercado: str
    filtros: dict = {}
    linha_min: Optional[float] = None
    linha_max: Optional[float] = None
    odd_min: Optional[float] = None
    odd_max: Optional[float] = None
    max_apostas_dia: Optional[int] = None
    max_apostas_partida: Optional[int] = None
    blacklist_jogadores: Optional[list] = None
    whitelist_jogadores: Optional[list] = None
    torneios: Optional[list] = None
    torneios_excluir: Optional[list] = None
    horario_inicio: Optional[str] = None
    horario_fim: Optional[str] = None
    status: str = "pausado"


class BotUpdate(BotCreate):
    nome: Optional[str] = None
    casa: Optional[str] = None
    esporte: Optional[str] = None
    mercado: Optional[str] = None


class Bot(BaseModel):
    id: int
    nome: str
    descricao: Optional[str]
    status: str
    casa: str
    esporte: str
    mercado: str
    filtros: Optional[dict]
    linha_min: Optional[float]
    linha_max: Optional[float]
    odd_min: Optional[float]
    odd_max: Optional[float]
    max_apostas_dia: Optional[int]
    max_apostas_partida: Optional[int]
    blacklist_jogadores: Optional[list]
    whitelist_jogadores: Optional[list]
    torneios: Optional[list]
    torneios_excluir: Optional[list]
    criado_em: Optional[datetime]
    atualizado_em: Optional[datetime]


# ── Apostas ────────────────────────────────────────────────────────

class ApostaCreate(BaseModel):
    bot_id: Optional[int] = None
    modo: str = "real"
    casa: Optional[str] = None
    esporte: Optional[str] = None
    torneio: Optional[str] = None
    jogador_a: Optional[str] = None
    jogador_b: Optional[str] = None
    event_id: Optional[str] = None
    mercado: Optional[str] = None
    linha: Optional[float] = None
    odd: Optional[float] = None
    lado: Optional[str] = None
    placar_a_entrada: Optional[int] = None
    placar_b_entrada: Optional[int] = None
    minuto_entrada: Optional[int] = None
    periodo_entrada: Optional[str] = None


class ApostaUpdate(BaseModel):
    resultado: Optional[str] = None
    placar_final_a: Optional[int] = None
    placar_final_b: Optional[int] = None
    lucro_unidades: Optional[float] = None
    resolvido_em: Optional[datetime] = None


class Aposta(BaseModel):
    id: int
    bot_id: Optional[int]
    modo: str
    casa: Optional[str]
    esporte: Optional[str]
    torneio: Optional[str]
    jogador_a: Optional[str]
    jogador_b: Optional[str]
    event_id: Optional[str]
    mercado: Optional[str]
    linha: Optional[float]
    odd: Optional[float]
    lado: Optional[str]
    placar_a_entrada: Optional[int]
    placar_b_entrada: Optional[int]
    resultado: Optional[str]
    lucro_unidades: Optional[float]
    apostado_em: datetime
    resolvido_em: Optional[datetime]


# ── Stats ──────────────────────────────────────────────────────────

class StatsDashboard(BaseModel):
    bots_ativos: int
    bots_total: int
    apostas_hoje: int
    lucro_hoje: Optional[float]
    win_rate_hoje: Optional[float]
    apostas_pendentes: int
    ticks_ultima_hora: int
    bookmakers_ativos: list


# ── Auth ───────────────────────────────────────────────────────────
# Validações de conteúdo (formato de e-mail, força da senha) ficam em
# security.py — compatível com Pydantic v1 e v2.

class RegistroRequest(BaseModel):
    email: str
    nome: str
    senha: str
    role: Optional[str] = None  # só é honrado se o solicitante for admin


class LoginRequest(BaseModel):
    email: str
    senha: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UsuarioOut(BaseModel):
    id: Optional[int] = None          # None quando for token de serviço
    email: Optional[str] = None
    nome: str
    role: str
    ativo: bool = True
    criado_em: Optional[datetime] = None
    ultimo_login: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expira_em_segundos: int
    usuario: UsuarioOut
