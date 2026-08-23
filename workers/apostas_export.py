# -*- coding: utf-8 -*-
r"""
workers/apostas_export.py — UMA fonte de verdade pro formato do export.

POR QUE EXISTE: o varredor precisa ver EXATAMENTE a mesma planilha que voce
baixa pelo painel. Se as duas montagens divergirem (uma coluna com nome
diferente, um chip faltando), o garimpo passa a minerar um dado que nao e' o
que o backtest produziu — e o erro so aparece dias depois, num numero que nao
fecha. Entao o formato mora aqui e quem precisar importa.

Espelha `baixar_planilha_apostas` do routers/backtest_upload.py.
Se um dia mexer nas colunas de la, mexe AQUI e o endpoint passa a chamar esta
funcao (o patch opcional esta no README).
"""
from datetime import datetime

__all__ = ["montar_linhas_apostas", "df_apostas", "COLUNAS_MINIMAS"]

_RES_MAP = {"green": "Green", "red": "Red", "void": "Void"}

# o varredor precisa destas pra funcionar; a checagem acontece no worker
COLUNAS_MINIMAS = ("Tip", "Linha", "Data", "Hora", "Confronto",
                   "Jogador A", "Jogador B", "Odd", "Placar Envio",
                   "Placar Final", "Resultado", "Lucro/Prej.")


def _fmt_dt(ts_iso):
    try:
        d = datetime.fromisoformat(str(ts_iso).replace("Z", ""))
        return d.strftime("%d/%m/%Y"), d.strftime("%H:%M:%S")
    except Exception:
        return str(ts_iso), ""


def montar_linhas_apostas(detalhe):
    """apostas_detalhe (lista de dicts do job) -> lista de linhas do export.

    BLINDADO: item torto nao derruba o lote — e' pulado e contabilizado pelo
    chamador (len da entrada vs len da saida).
    """
    linhas = []
    for a in (detalhe or []):
        if not isinstance(a, dict):
            continue
        try:
            data, hora = _fmt_dt(a.get("ts"))
            linha = {
                "Torneio": a.get("torneio", ""),
                "Campeonato": a.get("liga", ""),
                "Confronto": f"{a.get('jogador_a', '')} x {a.get('jogador_b', '')}",
                "Jogador A": a.get("jogador_a", ""),
                "Time A": a.get("time_a", ""),
                "Jogador B": a.get("jogador_b", ""),
                "Time B": a.get("time_b", ""),
                "Data": data,
                "Hora": hora,
                "Mercado": a.get("mercado", ""),
                "Tip": a.get("tip", ""),
                "Linha": a.get("linha"),
            }
            # colunas de WR dinamicas (wr_cols) com dedup de rotulo repetido —
            # mesmo tratamento do endpoint, inclusive o fallback legado
            wr_cols = a.get("wr_cols")
            if isinstance(wr_cols, list) and wr_cols:
                vistos = {}
                for wc in wr_cols:
                    if not isinstance(wc, dict):
                        continue
                    lbl = str(wc.get("l") or "").strip() or "WR"
                    vistos[lbl] = vistos.get(lbl, 0) + 1
                    if vistos[lbl] > 1:
                        lbl = f"{lbl} #{vistos[lbl]}"
                    linha[lbl] = wc.get("v")
                if a.get("qtd_ind_a") is not None:
                    linha["Qtd Ind A"] = a.get("qtd_ind_a")
                if a.get("qtd_ind_b") is not None:
                    linha["Qtd Ind B"] = a.get("qtd_ind_b")
            else:
                linha["Janela 1"] = a.get("janela_1", "")
                linha["Winrate 1"] = a.get("winrate_1")
                linha["Janela 2"] = a.get("janela_2", "")
                linha["Winrate 2"] = a.get("winrate_2")
            linha.update({
                "Odd": a.get("odd"),
                "Placar Envio": a.get("placar_envio", ""),
                "Placar Final": a.get("score_final", ""),
                "Resultado": _RES_MAP.get(a.get("resultado"), a.get("resultado", "")),
                "Lucro/Prej.": a.get("lucro_unidades"),
                # event_id: ja existe no apostas_detalhe desde sempre. Com ele o
                # varredor sabe o que e' uma partida em vez de ESTIMAR por
                # (Confronto + 45min) — e o eixo TETO depende disso: trocar a
                # unidade de jogo muda o resultado de ~1/3 das configs com teto.
                "event_id": a.get("event_id"),
            })
            # v23: coluna Err — so quando o job computou (errAtivo ou
            # errAnotar). Mesma regra do endpoint do painel: job sem err
            # continua saindo byte a byte identico.
            if a.get("err") is not None:
                linha["Err"] = a.get("err")
            linhas.append(linha)
        except Exception:
            continue
    return linhas


def df_apostas(detalhe):
    """Mesmo que montar_linhas_apostas, ja em DataFrame."""
    import pandas as pd
    return pd.DataFrame(montar_linhas_apostas(detalhe))
