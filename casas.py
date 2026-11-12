# -*- coding: utf-8 -*-
"""
casas.py — CADASTRO CENTRAL DE PAÍSES E CASAS (11/nov/2026)

Até aqui a casa era texto livre e cada parte do sistema tinha a sua lista fixa
(CASAS_VALIDAS nos bots, COMBINACOES no catálogo, as listas do site). Com a
operação do Peru — e outros países depois — a casa passa a ter um PAÍS, e todo
mundo consulta este módulo.

Fonte: tabelas `paises` e `casas` (migrations/035_paises_casas.sql), relidas a
cada 60 s. Se as tabelas não existirem ou o banco falhar, vale a cópia embutida
abaixo (o mesmo conteúdo do seed), para nada parar.

Regras:
  * casas.codigo = ticks.bookmaker = bots.casa = pasta bookmaker= do parquet.
  * Casa de fora do Brasil leva o sufixo do país (betsson_pe). Brasil sem sufixo.
  * pais None = casa GLOBAL (aparece em todos os países): hoje só a bet365.
  * Sem país informado = Brasil (PAIS_PADRAO): o site antigo segue igual.
  * Casa que não está no cadastro é tratada como do Brasil, como sempre foi.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import Header, HTTPException, Query

PAIS_PADRAO = "BR"
_TTL_S = 60

_PAISES_EMBUTIDOS = {
    "BR": {"codigo": "BR", "nome": "Brasil", "moeda": "BRL", "simbolo_moeda": "R$",
           "fuso": "America/Sao_Paulo", "idioma": "pt-BR", "sufixo": "", "ativo": True, "ordem": 1},
    "PE": {"codigo": "PE", "nome": "Peru", "moeda": "PEN", "simbolo_moeda": "S/",
           "fuso": "America/Lima", "idioma": "es-PE", "sufixo": "_pe", "ativo": True, "ordem": 2},
}


def _c(codigo, nome, pais, plataforma, idioma, cor, ao_vivo, ordem, ativa=True):
    return {"codigo": codigo, "nome": nome, "pais": pais, "plataforma": plataforma, "idioma": idioma,
            "cor": cor, "ativa": ativa, "ao_vivo": ao_vivo, "ordem": ordem}


_CASAS_EMBUTIDAS = {c["codigo"]: c for c in [
    _c("bet365", "Bet365", None, "jbot", "en", "emerald", True, 1),
    _c("superbet", "Superbet", "BR", "superbet", "pt", "amber", True, 2),
    _c("betano", "Betano", "BR", "kaizen", "pt", "blue", True, 3),
    _c("estrelabet", "Estrelabet", "BR", "altenar", "pt", "purple", True, 4),
    _c("kto", "KTO", "BR", "superbet", "pt", None, True, 5),
    _c("apostaganha", "ApostaGanha", "BR", None, "pt", None, True, 6),
    _c("novibet", "Novibet", "BR", None, "pt", None, True, 7),
    _c("vupi", "Vupi", "BR", None, "pt", None, True, 8),
    _c("meridianbet", "Meridianbet", "BR", None, "pt", None, True, 9),
    _c("betnacional", "Betnacional", "BR", None, "pt", None, True, 10),
    _c("betmgm", "BetMGM", "BR", None, "pt", None, True, 11),
    _c("betboom", "Betboom", "BR", None, "pt", None, True, 12),
    _c("starbet", "Starbet", "BR", None, "pt", None, True, 13),
    _c("betsson_pe", "Betsson", "PE", "obg", "es", None, True, 1),
    _c("betsafe_pe", "Betsafe", "PE", "obg", "es", None, True, 2),
    _c("inkabet_pe", "Inkabet", "PE", "obg", "es", None, True, 3),
    _c("jokerbet_pe", "Jokerbet", "PE", "altenar", "es", None, True, 4),
    _c("masterbets365_pe", "Masterbets365", "PE", "altenar", "es", None, True, 5),
    _c("rushbet_pe", "Rushbet", "PE", "kambi", "es", None, True, 6),
    _c("olimpo_pe", "Olimpo", "PE", "kambi", "es", None, True, 7),
    _c("betano_pe", "Betano", "PE", "kaizen", "es", "blue", True, 8),
]}

_estado = {"paises": dict(_PAISES_EMBUTIDOS), "casas": dict(_CASAS_EMBUTIDAS), "lido": 0.0, "fonte": "embutido"}


async def recarregar(conn) -> str:
    """Relê as tabelas. Devolve 'banco' ou 'embutido'. Nunca levanta."""
    try:
        ps = await conn.fetch("SELECT codigo, nome, moeda, simbolo_moeda, fuso, idioma, sufixo, ativo, ordem FROM paises")
        cs = await conn.fetch("SELECT codigo, nome, pais, plataforma, idioma, cor, ativa, ao_vivo, ordem FROM casas")
        if ps and cs:
            _estado["paises"] = {r["codigo"]: dict(r) for r in ps}
            _estado["casas"] = {r["codigo"]: dict(r) for r in cs}
            _estado["fonte"] = "banco"
    except Exception:
        _estado["fonte"] = "embutido"
    _estado["lido"] = time.time()
    return _estado["fonte"]


async def garantir(conn=None) -> None:
    """Relê se o cache venceu. Sem conexão, usa o pool da API (se houver)."""
    if time.time() - _estado["lido"] < _TTL_S:
        return
    if conn is not None:
        await recarregar(conn)
        return
    try:
        from database import db
        async with db() as c:
            await recarregar(c)
    except Exception:
        _estado["lido"] = time.time()   # sem banco: segue no que tem e tenta de novo depois


# ---------------------------------------------------------------- consultas ---
def paises(so_ativos: bool = True) -> list:
    ps = [p for p in _estado["paises"].values() if p.get("ativo", True) or not so_ativos]
    return sorted(ps, key=lambda p: (p.get("ordem") or 0, p["codigo"]))


def pais_existe(codigo: Optional[str]) -> bool:
    return bool(codigo) and codigo.upper() in _estado["paises"]


def casa(codigo: Optional[str]) -> Optional[dict]:
    return _estado["casas"].get((codigo or "").strip().lower())


def pais_da_casa(codigo: Optional[str]) -> Optional[str]:
    """'BR', 'PE'... ; None = global. Casa fora do cadastro conta como Brasil."""
    c = casa(codigo)
    if c is None:
        return PAIS_PADRAO
    return c.get("pais")


def casa_no_pais(codigo: Optional[str], pais: Optional[str]) -> bool:
    """A casa aparece no país? Global aparece em todos."""
    p = pais_da_casa(codigo)
    return p is None or p == (pais or PAIS_PADRAO).upper()


def casas_do_pais(pais: Optional[str], so_ativas: bool = True) -> list:
    """Casas do país + as globais, na ordem do cadastro."""
    pais = (pais or PAIS_PADRAO).upper()
    cs = [c for c in _estado["casas"].values()
          if (c.get("pais") in (None, pais)) and (c.get("ativa", True) or not so_ativas)]
    return sorted(cs, key=lambda c: (c.get("pais") is not None, c.get("ordem") or 0, c["codigo"]))


def codigos_do_pais(pais: Optional[str]) -> list:
    return [c["codigo"] for c in casas_do_pais(pais, so_ativas=False)]


def codigos_ativos() -> list:
    return sorted(c for c, d in _estado["casas"].items() if d.get("ativa", True))


def casa_valida(codigo: Optional[str]) -> bool:
    c = casa(codigo)
    return c is not None and c.get("ativa", True)


def ao_vivo(codigo: Optional[str]) -> bool:
    """Robôs ao vivo podem usar a casa? Fora do cadastro: sim (comportamento antigo)."""
    c = casa(codigo)
    return True if c is None else bool(c.get("ao_vivo", True))


def idioma_da_casa(codigo: Optional[str]) -> str:
    c = casa(codigo)
    return (c or {}).get("idioma") or "pt"


def plataforma_da_casa(codigo: Optional[str]) -> Optional[str]:
    c = casa(codigo)
    return (c or {}).get("plataforma")


def casa_do_nome_de_arquivo(base: str) -> Optional[str]:
    """Casa pelo nome de arquivo ('betsson_pe_BATTLE_...', 'superbet ticks ...').
    Vale o MAIOR código cadastrado que abre o nome (betano_pe ganha de betano).
    None se nenhum casar."""
    b = (base or "").strip().lower().replace(" ", "_")
    melhor = None
    for cod in _estado["casas"]:
        if b == cod or b.startswith(cod + "_") or b.startswith(cod + "-") or b.startswith(cod + "."):
            if melhor is None or len(cod) > len(melhor):
                melhor = cod
    return melhor


def condicao_pais(pais: Optional[str], coluna: str, n: int) -> tuple:
    """Pedaço de WHERE que deixa `coluna` (bookmaker/casa) só com as casas do país.
    Devolve (sql, [parametro]) com o parâmetro em $n.
    Brasil = tudo que NÃO é de outro país: casa fora do cadastro (ou NULL) segue
    aparecendo como sempre. Outro país = as casas dele + as globais."""
    pais = (pais or PAIS_PADRAO).upper()
    if pais == PAIS_PADRAO:
        outras = sorted(c for c, d in _estado["casas"].items() if d.get("pais") not in (None, PAIS_PADRAO))
        return f"NOT (COALESCE({coluna}, '') = ANY(${n}::text[]))", [outras]
    return f"{coluna} = ANY(${n}::text[])", [codigos_do_pais(pais)]


def fonte() -> str:
    return _estado["fonte"]


# ------------------------------------------------------------ dependência ---
async def pais_atual(x_pais: Optional[str] = Header(None, alias="X-Pais"),
                     pais: Optional[str] = Query(None, description="país (BR, PE...). Padrão: BR")) -> str:
    """Dependência FastAPI: país pedido pelo site (query ?pais= ou cabeçalho X-Pais).
    Sem nada = Brasil. País desconhecido = 400."""
    await garantir()
    escolhido = (pais or x_pais or PAIS_PADRAO).strip().upper()
    if not pais_existe(escolhido):
        raise HTTPException(400, f"país desconhecido: {escolhido}. Use: {', '.join(p['codigo'] for p in paises())}")
    return escolhido
