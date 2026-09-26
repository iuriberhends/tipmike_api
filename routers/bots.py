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
    # v50: organizacao do usuario logado (so' vem na listagem)
    if 'favorito' in d:
        base['favorito'] = bool(d.get('favorito'))
    if 'grupo_id' in d:
        base['grupo_id'] = d.get('grupo_id')

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
    # v50 — organizacao por usuario: so' favoritos / so' um grupo / sem grupo
    favoritos: bool = Query(False, description="true = so' os bots favoritados"),
    grupo: Optional[str] = Query(None, pattern=r"^(sem|\d{1,9})$",
                                 description="id do grupo, ou 'sem' (bots sem grupo)"),
    escopo: str = Query("todos", pattern="^(todos|meus)$",
                        description="admin: 'todos' (padrao) ou 'meus' (so os "
                                    "bots dele). Usuario comum ve so os dele "
                                    "de qualquer jeito."),
    usuario: dict = Depends(get_current_user),
):
    """
    Lista bots paginada. Inclui em_treinamento + telegram_canal_id
    pra o frontend mostrar corretamente o estado do botao Treinamento.

    Fase 4 (ownership): usuário comum vê só os próprios bots;
    admin/serviço vê todos (com user_id + dono_nome em cada item).

    v2 (11/ago): admin ganhou ESCOLHA. `escopo=meus` filtra pelos bots do
    próprio admin; `escopo=todos` (padrão) mantém o comportamento de sempre.
    Pro usuário comum o parâmetro é inócuo — o filtro por dono continua
    aplicado de qualquer forma (a permissão NÃO vem do parâmetro, vem do
    acesso_total; senão bastaria mandar escopo=todos pra ver bot alheio).
    """
    # o escopo entra na CHAVE do cache: sem isso, "todos" e "meus" se
    # atropelariam (o segundo pedido receberia a lista do primeiro).
    # v50: favorito/grupo sao POR USUARIO -> o id do usuario entra na chave
    # (dois admins nao podem dividir o cache da lista).
    cache_key = (f"list:{_escopo(usuario)}:u{usuario.get('id')}:{escopo}:{limit}:"
                 f"{offset}:{status}:{casa}:{esporte}:{q}:{favoritos}:{grupo}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    where = []
    # v50: $1 e' SEMPRE o usuario logado (usado nos JOINs de favorito/grupo)
    params = [usuario.get("id")]
    # usuario comum: sempre so os dele. Admin: so filtra se PEDIR (escopo=meus).
    if not acesso_total(usuario) or escopo == "meus":
        where.append("b.user_id = $1")
    if favoritos:
        where.append("f.bot_id IS NOT NULL")
    if grupo == "sem":
        where.append("gm.grupo_id IS NULL")
    elif grupo:
        params.append(int(grupo))
        where.append(f"gm.grupo_id = ${len(params)}")
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

    # v50: favorito e grupo do USUARIO LOGADO ($1). Favoritos sobem pro topo.
    _joins_org = """
        LEFT JOIN bot_favoritos f ON f.bot_id = b.id AND f.user_id = $1
        LEFT JOIN bot_grupo_membros gm ON gm.bot_id = b.id AND gm.user_id = $1
    """
    sql = f"""
        SELECT b.id, b.nome, b.casa, b.esporte, b.mercado, b.status,
               b.em_treinamento, b.telegram_canal_id,
               b.criado_em, b.atualizado_em,
               b.user_id, ud.nome AS dono_nome,
               (f.bot_id IS NOT NULL) AS favorito, gm.grupo_id
        FROM bots b
        LEFT JOIN usuarios ud ON ud.id = b.user_id
        {_joins_org}
        {where_clause}
        ORDER BY (f.bot_id IS NOT NULL) DESC, b.atualizado_em DESC NULLS LAST, b.id DESC
        LIMIT {limit} OFFSET {offset}
    """
    sql_count = f"SELECT COUNT(*) FROM bots b {_joins_org} {where_clause}"

    try:
        async with db() as conn:
            try:
                rows = await conn.fetch(sql, *params)
                total = await conn.fetchval(sql_count, *params)
            except Exception as e_org:
                # v50 BLINDAGEM: migration 034 ainda nao aplicada -> a lista
                # continua funcionando como antes (sem favorito/grupo) em vez
                # de derrubar a tela de Bots.
                if 'bot_favoritos' not in str(e_org) and 'bot_grupo_membros' not in str(e_org):
                    raise
                logger.warning("list_bots: tabelas de organizacao ausentes "
                               "(rode migrations/034_bots_org.sql) — lista sem favoritos/grupos.")
                # remonta o WHERE do jeito antigo (numeracao propria dos $n)
                where_leg, params_leg = [], []
                if not acesso_total(usuario) or escopo == "meus":
                    params_leg.append(usuario.get("id"))
                    where_leg.append(f"b.user_id = ${len(params_leg)}")
                for _col, _val in (("status", status), ("casa", casa), ("esporte", esporte)):
                    if _val:
                        params_leg.append(_val)
                        where_leg.append(f"b.{_col} = ${len(params_leg)}")
                if q:
                    params_leg.append(f"%{q}%")
                    where_leg.append(f"b.nome ILIKE ${len(params_leg)}")
                wc_leg = ("WHERE " + " AND ".join(where_leg)) if where_leg else ""
                if favoritos or grupo:
                    rows, total = [], 0   # pediu favoritos/grupo e eles nao existem
                else:
                    rows = await conn.fetch(f"""
                        SELECT b.id, b.nome, b.casa, b.esporte, b.mercado, b.status,
                               b.em_treinamento, b.telegram_canal_id,
                               b.criado_em, b.atualizado_em,
                               b.user_id, ud.nome AS dono_nome
                        FROM bots b
                        LEFT JOIN usuarios ud ON ud.id = b.user_id
                        {wc_leg}
                        ORDER BY b.atualizado_em DESC NULLS LAST, b.id DESC
                        LIMIT {limit} OFFSET {offset}
                    """, *params_leg)
                    total = await conn.fetchval(f"SELECT COUNT(*) FROM bots b {wc_leg}", *params_leg)
    except Exception:
        logger.exception("Erro ao listar bots.")
        raise HTTPException(status_code=500, detail="Erro interno ao listar bots.")

    resultado = {
        "total": total,
        "limit": limit,
        "offset": offset,
        # eco do que foi de fato aplicado (admin=True diz ao front se vale a
        # pena mostrar o seletor)
        "escopo": ("meus" if (not acesso_total(usuario) or escopo == "meus")
                   else "todos"),
        "admin": bool(acesso_total(usuario)),
        "items": [_row_to_dict(r, full=False) for r in rows],
    }
    _cache_set(cache_key, resultado)
    return {**resultado, "_cache": "miss"}


# ============================================================
# v50 — ORGANIZACAO: FAVORITOS E GRUPOS (por usuario)
# ============================================================
# Ficam em tabelas proprias (bot_favoritos, bot_grupos, bot_grupo_membros —
# migrations/034_bots_org.sql). A tabela `bots` NAO muda: o executor, o
# backtest e os clones continuam iguais. Cada usuario tem a SUA organizacao
# (admin favoritar bot alheio nao mexe na tela do dono). Um bot fica em no
# maximo UM grupo por usuario ("mover" = tirar do anterior e por no novo).
# Estas rotas vem ANTES de /{bot_id}, senao GET /bots/org cairia no get_bot.
import re as _re_org

_ORG_MAX_GRUPOS = 50
_ORG_MAX_MOVER = 200
_RE_COR = _re_org.compile(r'^#[0-9a-fA-F]{6}$')


class FavoritoToggle(BaseModel):
    favorito: bool


class GrupoCriar(BaseModel):
    nome: str = Field(..., min_length=1, max_length=60)
    cor: Optional[str] = Field(None, max_length=7)

    @field_validator('nome')
    @classmethod
    def _nome_ok(cls, v):
        v = (v or '').strip()
        if not v:
            raise ValueError('nome vazio')
        return v

    @field_validator('cor')
    @classmethod
    def _cor_ok(cls, v):
        if v in (None, ''):
            return None
        if not _RE_COR.match(v):
            raise ValueError('cor invalida (use #RRGGBB)')
        return v


class GrupoEditar(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=60)
    cor: Optional[str] = Field(None, max_length=7)
    ordem: Optional[int] = Field(None, ge=0, le=10000)

    @field_validator('nome')
    @classmethod
    def _nome_ok(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError('nome vazio')
        return v

    @field_validator('cor')
    @classmethod
    def _cor_ok(cls, v):
        if v in (None, ''):
            return None
        if not _RE_COR.match(v):
            raise ValueError('cor invalida (use #RRGGBB)')
        return v


class MoverBots(BaseModel):
    bot_ids: List[int] = Field(..., min_length=1, max_length=_ORG_MAX_MOVER)
    grupo_id: Optional[int] = None   # None = tirar do grupo


async def _bots_acessiveis(conn, usuario: dict, ids) -> List[int]:
    """Dos ids pedidos, devolve so' os que o usuario pode ver (dono ou admin)."""
    ids = sorted({int(i) for i in ids if isinstance(i, int) and i > 0})
    if not ids:
        return []
    if acesso_total(usuario):
        rows = await conn.fetch("SELECT id FROM bots WHERE id = ANY($1::int[])", ids)
    else:
        rows = await conn.fetch(
            "SELECT id FROM bots WHERE id = ANY($1::int[]) AND user_id = $2",
            ids, usuario.get("id"))
    return [r["id"] for r in rows]


async def _grupo_do_usuario(conn, usuario: dict, grupo_id: int):
    return await conn.fetchrow(
        "SELECT id, nome, cor, ordem FROM bot_grupos WHERE id = $1 AND user_id = $2",
        grupo_id, usuario.get("id"))


@router.get("/org")
async def org_listar(usuario: dict = Depends(get_current_user)):
    """Grupos do usuario (com quantos bots VISIVEIS pra ele cada um tem) +
    ids dos favoritos. Nao usa cache: e' leve e muda a cada clique."""
    uid = usuario.get("id")
    filtro_dono = "" if acesso_total(usuario) else " AND b.user_id = $1"
    try:
        async with db() as conn:
            grupos = await conn.fetch(f"""
                SELECT g.id, g.nome, g.cor, g.ordem,
                       COUNT(b.id) AS total
                FROM bot_grupos g
                LEFT JOIN bot_grupo_membros gm
                       ON gm.grupo_id = g.id AND gm.user_id = $1
                LEFT JOIN bots b ON b.id = gm.bot_id{filtro_dono}
                WHERE g.user_id = $1
                GROUP BY g.id
                ORDER BY g.ordem, lower(g.nome), g.id
            """, uid)
            favs = await conn.fetch(f"""
                SELECT f.bot_id FROM bot_favoritos f
                JOIN bots b ON b.id = f.bot_id{filtro_dono}
                WHERE f.user_id = $1
            """, uid)
            dono_sem = "" if acesso_total(usuario) else "AND b.user_id = $1"
            sem = await conn.fetchval(f"""
                SELECT COUNT(*) FROM bots b
                WHERE NOT EXISTS (SELECT 1 FROM bot_grupo_membros gm
                                  WHERE gm.bot_id = b.id AND gm.user_id = $1)
                {dono_sem}
            """, uid)
    except Exception:
        logger.exception("Erro ao listar organizacao dos bots.")
        raise HTTPException(status_code=500,
                            detail="Erro interno ao carregar favoritos/grupos. "
                                   "A migration 034_bots_org.sql foi aplicada?")
    return {
        "grupos": [{"id": g["id"], "nome": g["nome"], "cor": g["cor"],
                    "ordem": g["ordem"], "total": g["total"]} for g in grupos],
        "favoritos": [r["bot_id"] for r in favs],
        "sem_grupo": sem or 0,
    }


@router.put("/org/favoritos/{bot_id}")
async def org_favoritar(bot_id: int, payload: FavoritoToggle,
                        usuario: dict = Depends(get_current_user)):
    uid = usuario.get("id")
    try:
        async with db() as conn:
            ok = await _bots_acessiveis(conn, usuario, [bot_id])
            if not ok:
                raise HTTPException(status_code=404, detail=f"Bot #{bot_id} não encontrado")
            if payload.favorito:
                await conn.execute("""
                    INSERT INTO bot_favoritos (user_id, bot_id) VALUES ($1, $2)
                    ON CONFLICT (user_id, bot_id) DO NOTHING
                """, uid, bot_id)
            else:
                await conn.execute(
                    "DELETE FROM bot_favoritos WHERE user_id = $1 AND bot_id = $2",
                    uid, bot_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao favoritar bot %s.", bot_id)
        raise HTTPException(status_code=500, detail="Erro interno ao favoritar.")
    _cache_invalidate_all()
    return {"id": bot_id, "favorito": payload.favorito}


@router.post("/org/grupos", status_code=201)
async def org_criar_grupo(payload: GrupoCriar, usuario: dict = Depends(get_current_user)):
    uid = usuario.get("id")
    try:
        async with db() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM bot_grupos WHERE user_id = $1", uid)
            if n >= _ORG_MAX_GRUPOS:
                raise HTTPException(status_code=400,
                                    detail=f"Limite de {_ORG_MAX_GRUPOS} grupos atingido")
            existe = await conn.fetchval(
                "SELECT 1 FROM bot_grupos WHERE user_id = $1 AND lower(nome) = lower($2)",
                uid, payload.nome)
            if existe:
                raise HTTPException(status_code=409,
                                    detail=f'Já existe um grupo "{payload.nome}"')
            row = await conn.fetchrow("""
                INSERT INTO bot_grupos (user_id, nome, cor, ordem)
                VALUES ($1, $2, $3,
                        COALESCE((SELECT MAX(ordem) + 1 FROM bot_grupos WHERE user_id = $1), 0))
                RETURNING id, nome, cor, ordem
            """, uid, payload.nome, payload.cor)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao criar grupo de bots.")
        raise HTTPException(status_code=500, detail="Erro interno ao criar grupo.")
    _cache_invalidate_all()
    return {"id": row["id"], "nome": row["nome"], "cor": row["cor"],
            "ordem": row["ordem"], "total": 0}


@router.patch("/org/grupos/{grupo_id}")
async def org_editar_grupo(grupo_id: int, payload: GrupoEditar,
                           usuario: dict = Depends(get_current_user)):
    uid = usuario.get("id")
    campos = payload.model_dump(exclude_unset=True)
    if not campos:
        raise HTTPException(status_code=400, detail="Nada pra alterar")
    try:
        async with db() as conn:
            if not await _grupo_do_usuario(conn, usuario, grupo_id):
                raise HTTPException(status_code=404, detail="Grupo não encontrado")
            if campos.get("nome"):
                dup = await conn.fetchval("""
                    SELECT 1 FROM bot_grupos
                    WHERE user_id = $1 AND lower(nome) = lower($2) AND id <> $3
                """, uid, campos["nome"], grupo_id)
                if dup:
                    raise HTTPException(status_code=409,
                                        detail=f'Já existe um grupo "{campos["nome"]}"')
            sets, args = [], []
            for col in ("nome", "cor", "ordem"):
                if col in campos:
                    args.append(campos[col])
                    sets.append(f"{col} = ${len(args)}")
            args += [grupo_id, uid]
            row = await conn.fetchrow(f"""
                UPDATE bot_grupos SET {", ".join(sets)}
                WHERE id = ${len(args) - 1} AND user_id = ${len(args)}
                RETURNING id, nome, cor, ordem
            """, *args)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao editar grupo %s.", grupo_id)
        raise HTTPException(status_code=500, detail="Erro interno ao editar grupo.")
    _cache_invalidate_all()
    return dict(row)


@router.delete("/org/grupos/{grupo_id}")
async def org_excluir_grupo(grupo_id: int, usuario: dict = Depends(get_current_user)):
    """Apaga o GRUPO. Os bots NAO sao apagados: voltam pra 'Sem grupo'."""
    try:
        async with db() as conn:
            r = await conn.execute(
                "DELETE FROM bot_grupos WHERE id = $1 AND user_id = $2",
                grupo_id, usuario.get("id"))
    except Exception:
        logger.exception("Erro ao excluir grupo %s.", grupo_id)
        raise HTTPException(status_code=500, detail="Erro interno ao excluir grupo.")
    if r == "DELETE 0":
        raise HTTPException(status_code=404, detail="Grupo não encontrado")
    _cache_invalidate_all()
    return {"excluido": True, "id": grupo_id}


@router.post("/org/mover")
async def org_mover(payload: MoverBots, usuario: dict = Depends(get_current_user)):
    """Move N bots pra um grupo (grupo_id) ou tira do grupo (grupo_id=null)."""
    uid = usuario.get("id")
    try:
        async with db() as conn:
            async with conn.transaction():
                if payload.grupo_id is not None:
                    if not await _grupo_do_usuario(conn, usuario, payload.grupo_id):
                        raise HTTPException(status_code=404, detail="Grupo não encontrado")
                ids = await _bots_acessiveis(conn, usuario, payload.bot_ids)
                if not ids:
                    raise HTTPException(status_code=404, detail="Nenhum bot válido na seleção")
                if payload.grupo_id is None:
                    await conn.execute("""
                        DELETE FROM bot_grupo_membros
                        WHERE user_id = $1 AND bot_id = ANY($2::int[])
                    """, uid, ids)
                else:
                    await conn.execute("""
                        INSERT INTO bot_grupo_membros (user_id, bot_id, grupo_id)
                        SELECT $1, x, $3 FROM unnest($2::int[]) AS x
                        ON CONFLICT (user_id, bot_id) DO UPDATE SET grupo_id = EXCLUDED.grupo_id
                    """, uid, ids, payload.grupo_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Erro ao mover bots de grupo.")
        raise HTTPException(status_code=500, detail="Erro interno ao mover bots.")
    _cache_invalidate_all()
    return {"movidos": len(ids), "ignorados": len(set(payload.bot_ids)) - len(ids),
            "grupo_id": payload.grupo_id}


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
