"""routers/bots.py - CRUD de bots otimizado

v2 - inclui em_treinamento + telegram_canal_id na listagem e no detalhe.
     Endpoint PATCH /bots/:id/treinamento integrado aqui (invalida cache certo).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, time as time_t
from decimal import Decimal
import json
import asyncio
import logging

from database import db
from security import get_current_user, acesso_total

logger = logging.getLogger("tipmike.bots")

router = APIRouter(prefix="/bots", tags=["Bots"])


def _escopo(usuario: dict) -> str:
    """Chave de escopo pro cache: dados de um usuário nunca servem outro."""
    return "all" if acesso_total(usuario) else f"u{usuario.get('id')}"

# ============================================================
# CACHE EM MEMÓRIA (TTL curto, invalida em mutações)
# ============================================================
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = timedelta(seconds=5)

def _cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if datetime.now() - ts > _CACHE_TTL:
        del _CACHE[key]
        return None
    return data

def _cache_set(key: str, data):
    _CACHE[key] = (datetime.now(), data)

def _cache_invalidate_all():
    _CACHE.clear()


# ============================================================
# MODELOS PYDANTIC
# ============================================================
ESPORTES_VALIDOS = {'fifa', 'nba2k', 'ehockey', 'etennis'}
CASAS_VALIDAS = {'betano', 'superbet', 'bet365', 'estrelabet', 'novibet', 'vupi'}
STATUS_VALIDOS = {'ativo', 'pausado', 'erro'}


class BotCreate(BaseModel):
    nome: str = Field(..., min_length=4, max_length=100)
    descricao: Optional[str] = Field(None, max_length=2000)
    casa: str = Field(...)
    esporte: str = Field(...)
    mercado: str = Field(..., max_length=50)
    torneios: Optional[List[str]] = None
    torneios_excluir: Optional[List[str]] = None
    linha_min: Optional[float] = None
    linha_max: Optional[float] = None
    odd_min: Optional[float] = None
    odd_max: Optional[float] = None
    whitelist_pares: Optional[List[Dict[str, Any]]] = None
    blacklist_pares: Optional[List[Dict[str, Any]]] = None
    whitelist_cenarios: Optional[List[str]] = None
    max_apostas_partida: Optional[int] = None
    filtros: Dict[str, Any] = Field(default_factory=dict)

    @field_validator('casa')
    @classmethod
    def valida_casa(cls, v):
        if v not in CASAS_VALIDAS:
            raise ValueError(f"Casa inválida. Use: {', '.join(CASAS_VALIDAS)}")
        return v

    @field_validator('esporte')
    @classmethod
    def valida_esporte(cls, v):
        if v not in ESPORTES_VALIDOS:
            raise ValueError(f"Esporte inválido. Use: {', '.join(ESPORTES_VALIDOS)}")
        return v

    @field_validator('linha_max')
    @classmethod
    def valida_linhas(cls, v, info):
        lmin = info.data.get('linha_min')
        if v is not None and lmin is not None and v < lmin:
            raise ValueError("linha_max deve ser >= linha_min")
        return v

    @field_validator('odd_max')
    @classmethod
    def valida_odds(cls, v, info):
        omin = info.data.get('odd_min')
        if v is not None and omin is not None and v < omin:
            raise ValueError("odd_max deve ser >= odd_min")
        return v


class BotPatch(BaseModel):
    nome: Optional[str] = Field(None, min_length=4, max_length=100)
    descricao: Optional[str] = None
    status: Optional[str] = None
    casa: Optional[str] = None
    esporte: Optional[str] = None
    mercado: Optional[str] = None
    torneios: Optional[List[str]] = None
    torneios_excluir: Optional[List[str]] = None
    linha_min: Optional[float] = None
    linha_max: Optional[float] = None
    odd_min: Optional[float] = None
    odd_max: Optional[float] = None
    whitelist_pares: Optional[List[Dict[str, Any]]] = None
    blacklist_pares: Optional[List[Dict[str, Any]]] = None
    whitelist_cenarios: Optional[List[str]] = None
    max_apostas_partida: Optional[int] = None
    filtros: Optional[Dict[str, Any]] = None
    em_treinamento: Optional[bool] = None
    telegram_canal_id: Optional[int] = None

    @field_validator('status')
    @classmethod
    def valida_status(cls, v):
        if v is not None and v not in STATUS_VALIDOS:
            raise ValueError(f"Status inválido. Use: {', '.join(STATUS_VALIDOS)}")
        return v


class TreinamentoToggle(BaseModel):
    em_treinamento: bool


# ============================================================
# HELPERS DE SERIALIZAÇÃO
# ============================================================
def _to_jsonb(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)) and len(value) == 0:
        return None
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


def _row_to_dict(row, full=True):
    """Converte row asyncpg pra dict serializável.

    full=False: campos leves (pra listagem)
    full=True: tudo, incluindo JSONB

    EM AMBOS modos retorna em_treinamento + telegram_canal_id.
    """
    if row is None:
        return None
    d = dict(row)

    base = {
        'id': d.get('id'),
        'nome': d.get('nome'),
        'casa': d.get('casa'),
        'esporte': d.get('esporte'),
        'mercado': d.get('mercado'),
        'status': d.get('status'),
        'em_treinamento': bool(d.get('em_treinamento')) if d.get('em_treinamento') is not None else False,
        'telegram_canal_id': d.get('telegram_canal_id'),
        'criado_em': d['criado_em'].isoformat() if d.get('criado_em') else None,
        'atualizado_em': d['atualizado_em'].isoformat() if d.get('atualizado_em') else None,
        'user_id': d.get('user_id'),
    }
    if 'dono_nome' in d:
        base['dono_nome'] = d.get('dono_nome')

    if not full:
        return base

    def parse_json(field):
        v = d.get(field)
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v

    def num(v):
        if v is None:
            return None
        if isinstance(v, Decimal):
            return float(v)
        return v

    return {
        **base,
        'descricao': d.get('descricao'),
        'torneios': parse_json('torneios'),
        'torneios_excluir': parse_json('torneios_excluir'),
        'tipo_hc': d.get('tipo_hc'),
        'periodo_hc': d.get('periodo_hc'),
        'linha_min': num(d.get('linha_min')),
        'linha_max': num(d.get('linha_max')),
        'odd_min': num(d.get('odd_min')),
        'odd_max': num(d.get('odd_max')),
        'spread_max': num(d.get('spread_max')),
        'movimento_linha': d.get('movimento_linha'),
        'movimento_odd': d.get('movimento_odd'),
        'whitelist_jogadores': parse_json('whitelist_jogadores'),
        'blacklist_jogadores': parse_json('blacklist_jogadores'),
        'whitelist_pares': parse_json('whitelist_pares'),
        'blacklist_pares': parse_json('blacklist_pares'),
        'whitelist_cenarios': parse_json('whitelist_cenarios'),
        'max_apostas_dia': d.get('max_apostas_dia'),
        'max_apostas_simult': d.get('max_apostas_simult'),
        'max_apostas_partida': d.get('max_apostas_partida'),
        'max_apostas_torneio': d.get('max_apostas_torneio'),
        'horario_inicio': d['horario_inicio'].isoformat() if d.get('horario_inicio') else None,
        'horario_fim': d['horario_fim'].isoformat() if d.get('horario_fim') else None,
        'dias_semana': parse_json('dias_semana'),
        'cooldown_segundos': d.get('cooldown_segundos'),
        'filtros': parse_json('filtros') or {},
    }


# ============================================================
# ENDPOINTS
# ============================================================
@router.get("")
async def list_bots(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    casa: Optional[str] = Query(None),
    esporte: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Busca por nome (LIKE)"),
    usuario: dict = Depends(get_current_user),
):
    """
    Lista bots paginada. Inclui em_treinamento + telegram_canal_id
    pra o frontend mostrar corretamente o estado do botao Treinamento.

    Fase 4 (ownership): usuário comum vê só os próprios bots;
    admin/serviço vê todos (com user_id + dono_nome em cada item).
    """
    cache_key = f"list:{_escopo(usuario)}:{limit}:{offset}:{status}:{casa}:{esporte}:{q}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    where = []
    params = []
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        where.append(f"b.user_id = ${len(params)}")
    if status:
        params.append(status)
        where.append(f"b.status = ${len(params)}")
    if casa:
        params.append(casa)
        where.append(f"b.casa = ${len(params)}")
    if esporte:
        params.append(esporte)
        where.append(f"b.esporte = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"b.nome ILIKE ${len(params)}")

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT b.id, b.nome, b.casa, b.esporte, b.mercado, b.status,
               b.em_treinamento, b.telegram_canal_id,
               b.criado_em, b.atualizado_em,
               b.user_id, ud.nome AS dono_nome
        FROM bots b
        LEFT JOIN usuarios ud ON ud.id = b.user_id
        {where_clause}
        ORDER BY b.atualizado_em DESC NULLS LAST, b.id DESC
        LIMIT {limit} OFFSET {offset}
    """
    sql_count = f"SELECT COUNT(*) FROM bots b {where_clause}"

    try:
        async with db() as conn:
            rows = await conn.fetch(sql, *params)
            total = await conn.fetchval(sql_count, *params)
    except Exception:
        logger.exception("Erro ao listar bots.")
        raise HTTPException(status_code=500, detail="Erro interno ao listar bots.")

    resultado = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_row_to_dict(r, full=False) for r in rows],
    }
    _cache_set(cache_key, resultado)
    return {**resultado, "_cache": "miss"}


@router.get("/{bot_id}")
async def get_bot(bot_id: int, usuario: dict = Depends(get_current_user)):
    """Retorna bot completo (com JSONB) pra edição. Bot alheio -> 404 (não vaza existência)."""
    cache_key = f"get:{_escopo(usuario)}:{bot_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    guarda = ""
    params = [bot_id]
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        guarda = " AND b.user_id = $2"

    try:
        async with db() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT b.*, ud.nome AS dono_nome
                FROM bots b
                LEFT JOIN usuarios ud ON ud.id = b.user_id
                WHERE b.id = $1{guarda}
                """,
                *params,
            )
    except Exception:
        logger.exception("Erro ao buscar bot %s.", bot_id)
        raise HTTPException(status_code=500, detail="Erro interno ao buscar bot.")

    if not row:
        raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")

    resultado = _row_to_dict(row, full=True)
    _cache_set(cache_key, resultado)
    return {**resultado, "_cache": "miss"}


@router.post("", status_code=201)
async def create_bot(payload: BotCreate, usuario: dict = Depends(get_current_user)):
    """Cria bot novo. Status default 'pausado'. O dono é sempre quem cria (vem do token, nunca do payload)."""
    if usuario.get("id") is None:
        raise HTTPException(status_code=400, detail="Token de serviço não pode criar bots.")
    sql = """
        INSERT INTO bots (
            nome, descricao, status, casa, esporte, mercado,
            torneios, torneios_excluir,
            linha_min, linha_max, odd_min, odd_max,
            whitelist_pares, blacklist_pares, whitelist_cenarios,
            max_apostas_partida, filtros, user_id
        ) VALUES (
            $1, $2, 'pausado', $3, $4, $5,
            $6::jsonb, $7::jsonb,
            $8, $9, $10, $11,
            $12::jsonb, $13::jsonb, $14::jsonb,
            $15, $16::jsonb, $17
        )
        RETURNING *
    """
    args = [
        payload.nome.strip(),
        payload.descricao,
        payload.casa,
        payload.esporte,
        payload.mercado,
        _to_jsonb(payload.torneios),
        _to_jsonb(payload.torneios_excluir),
        payload.linha_min,
        payload.linha_max,
        payload.odd_min,
        payload.odd_max,
        _to_jsonb(payload.whitelist_pares),
        _to_jsonb(payload.blacklist_pares),
        _to_jsonb(payload.whitelist_cenarios),
        payload.max_apostas_partida,
        json.dumps(payload.filtros or {}, separators=(',', ':'), ensure_ascii=False),
        usuario.get("id"),
    ]

    try:
        async with db() as conn:
            row = await conn.fetchrow(sql, *args)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao criar bot: {str(e)[:200]}")

    _cache_invalidate_all()
    resultado = _row_to_dict(row, full=True)
    resultado["dono_nome"] = usuario.get("nome")
    return resultado


@router.patch("/{bot_id}")
async def patch_bot(bot_id: int, payload: BotPatch, usuario: dict = Depends(get_current_user)):
    """Update parcial. Só atualiza os campos enviados."""
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="Nenhum campo enviado")

    JSONB_FIELDS = {
        'torneios', 'torneios_excluir', 'whitelist_pares',
        'blacklist_pares', 'whitelist_cenarios', 'filtros',
    }

    set_clauses = []
    args = []
    for field, value in data.items():
        args.append(_to_jsonb(value) if field in JSONB_FIELDS else value)
        cast = "::jsonb" if field in JSONB_FIELDS else ""
        set_clauses.append(f"{field} = ${len(args)}{cast}")

    set_clauses.append("atualizado_em = NOW()")

    args.append(bot_id)
    cond = f"id = ${len(args)}"
    if not acesso_total(usuario):
        args.append(usuario.get("id"))
        cond += f" AND user_id = ${len(args)}"
    sql = f"""
        UPDATE bots SET {', '.join(set_clauses)}
        WHERE {cond}
        RETURNING *
    """

    try:
        async with db() as conn:
            row = await conn.fetchrow(sql, *args)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao atualizar: {str(e)[:200]}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")

    _cache_invalidate_all()
    return _row_to_dict(row, full=True)


@router.delete("/{bot_id}")
async def delete_bot(bot_id: int, usuario: dict = Depends(get_current_user)):
    """Deleta bot. CASCADE remove apostas/backtest_execucoes vinculadas. Bot alheio -> 404."""
    params = [bot_id]
    cond = "id = $1"
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        cond += " AND user_id = $2"
    try:
        async with db() as conn:
            result = await conn.execute(f"DELETE FROM bots WHERE {cond}", *params)
    except Exception:
        logger.exception("Erro ao deletar bot %s.", bot_id)
        raise HTTPException(status_code=500, detail="Erro interno ao deletar bot.")

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")

    _cache_invalidate_all()
    return {"deletado": True, "id": bot_id}


@router.post("/{bot_id}/start")
async def start_bot(bot_id: int, usuario: dict = Depends(get_current_user)):
    """Liga bot (status='ativo'). Bot alheio -> 404."""
    params = [bot_id]
    cond = "id=$1"
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        cond += " AND user_id=$2"
    try:
        async with db() as conn:
            row = await conn.fetchrow(
                f"UPDATE bots SET status='ativo', atualizado_em=NOW() WHERE {cond} RETURNING id, nome, status",
                *params,
            )
    except Exception:
        logger.exception("Erro ao ligar bot %s.", bot_id)
        raise HTTPException(status_code=500, detail="Erro interno ao ligar bot.")
    if not row:
        raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")
    _cache_invalidate_all()
    return dict(row)


@router.post("/{bot_id}/stop")
async def stop_bot(bot_id: int, usuario: dict = Depends(get_current_user)):
    """Pausa bot (status='pausado'). Bot alheio -> 404."""
    params = [bot_id]
    cond = "id=$1"
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        cond += " AND user_id=$2"
    try:
        async with db() as conn:
            row = await conn.fetchrow(
                f"UPDATE bots SET status='pausado', atualizado_em=NOW() WHERE {cond} RETURNING id, nome, status",
                *params,
            )
    except Exception:
        logger.exception("Erro ao pausar bot %s.", bot_id)
        raise HTTPException(status_code=500, detail="Erro interno ao pausar bot.")
    if not row:
        raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")
    _cache_invalidate_all()
    return dict(row)


@router.patch("/{bot_id}/treinamento")
async def toggle_treinamento(bot_id: int, payload: TreinamentoToggle, usuario: dict = Depends(get_current_user)):
    """
    Liga/desliga modo treinamento.
    em_treinamento=true: bot continua simulando mas NAO envia Telegram.

    Integrado no proprio router pra invalidar o cache de listagem
    (caso contrario o GET /bots fica retornando estado antigo por ate 5s).
    """
    params = [payload.em_treinamento, bot_id]
    cond = "id = $2"
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        cond += " AND user_id = $3"
    try:
        async with db() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE bots
                SET em_treinamento = $1, atualizado_em = NOW()
                WHERE {cond}
                RETURNING id, nome, em_treinamento, telegram_canal_id, status
                """,
                *params,
            )
    except Exception:
        logger.exception("Erro no toggle treinamento do bot %s.", bot_id)
        raise HTTPException(status_code=500, detail="Erro interno ao alternar treinamento.")
    if not row:
        raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")

    _cache_invalidate_all()
    return {
        "id": row["id"],
        "nome": row["nome"],
        "em_treinamento": row["em_treinamento"],
        "telegram_canal_id": row["telegram_canal_id"],
        "status": row["status"],
    }


@router.post("/{bot_id}/clone")
async def clone_bot(bot_id: int, usuario: dict = Depends(get_current_user)):
    """Clona bot. Novo bot fica pausado, nome com sufixo (cópia). A cópia pertence a quem clona."""
    if usuario.get("id") is None:
        raise HTTPException(status_code=400, detail="Token de serviço não pode clonar bots.")
    guarda = ""
    params = [bot_id]
    if not acesso_total(usuario):
        params.append(usuario.get("id"))
        guarda = " AND user_id=$2"
    async with db() as conn:
        async with conn.transaction():
            orig = await conn.fetchrow(f"SELECT * FROM bots WHERE id=$1{guarda}", *params)
            if not orig:
                raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")
            row = await conn.fetchrow("""
                INSERT INTO bots (
                    nome, descricao, status, casa, esporte, mercado,
                    torneios, torneios_excluir, tipo_hc, periodo_hc,
                    linha_min, linha_max, odd_min, odd_max, spread_max,
                    movimento_linha, movimento_odd,
                    whitelist_jogadores, blacklist_jogadores,
                    whitelist_pares, blacklist_pares, whitelist_cenarios,
                    max_apostas_dia, max_apostas_simult, max_apostas_partida, max_apostas_torneio,
                    horario_inicio, horario_fim, dias_semana, cooldown_segundos, filtros, user_id
                ) VALUES (
                    LEFT($1 || ' (cópia)', 100), $2, 'pausado', $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13, $14,
                    $15, $16,
                    $17, $18,
                    $19, $20, $21,
                    $22, $23, $24, $25,
                    $26, $27, $28, $29, $30, $31
                ) RETURNING *
            """,
                orig['nome'], orig['descricao'], orig['casa'], orig['esporte'], orig['mercado'],
                orig['torneios'], orig['torneios_excluir'], orig['tipo_hc'], orig['periodo_hc'],
                orig['linha_min'], orig['linha_max'], orig['odd_min'], orig['odd_max'], orig['spread_max'],
                orig['movimento_linha'], orig['movimento_odd'],
                orig['whitelist_jogadores'], orig['blacklist_jogadores'],
                orig['whitelist_pares'], orig['blacklist_pares'], orig['whitelist_cenarios'],
                orig['max_apostas_dia'], orig['max_apostas_simult'], orig['max_apostas_partida'], orig['max_apostas_torneio'],
                orig['horario_inicio'], orig['horario_fim'], orig['dias_semana'], orig['cooldown_segundos'],
                orig['filtros'],
                usuario.get("id"),
            )

    _cache_invalidate_all()
    resultado = _row_to_dict(row, full=True)
    resultado["dono_nome"] = usuario.get("nome")
    return resultado
