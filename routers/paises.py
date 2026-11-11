# -*- coding: utf-8 -*-
"""
routers/paises.py — países e casas para o site (11/nov/2026).

    GET /paises                -> países ativos (moeda, fuso, idioma)
    GET /casas?pais=PE         -> casas do país + globais (bet365)
    GET /casas?pais=PE&todas=1 -> inclui as inativas

O site troca as listas fixas (CriarBot, Bots, BacktestAvulso, Live) por estas.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

import casas as cadastro

router = APIRouter(tags=["paises"])


@router.get("/paises")
async def listar_paises():
    await cadastro.garantir()
    return {"paises": cadastro.paises(), "padrao": cadastro.PAIS_PADRAO, "fonte": cadastro.fonte()}


@router.get("/casas")
async def listar_casas(pais: str = Depends(cadastro.pais_atual),
                       todas: Optional[bool] = Query(False, description="inclui casas inativas")):
    lista = cadastro.casas_do_pais(pais, so_ativas=not todas)
    return {"pais": pais,
            "casas": [{**c, "global": c.get("pais") is None} for c in lista],
            "fonte": cadastro.fonte()}
