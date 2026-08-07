# -*- coding: utf-8 -*-
r"""
aplicar_atropelo_front.py — poe o campo ATROPELO no painel
===========================================================
Edita DOIS arquivos, so acrescentando (nenhuma linha e' removida):

  src/screens/BacktestAvulso.jsx   -> estado, bloco visual, validacao, payload
  routers/backtest_upload.py       -> campos do Pydantic + montagem do filtros

Por que script e nao arquivo pronto: assim o patch entra NO TEU arquivo, com
as tuas mudancas locais preservadas. Cada edicao e' ancorada num trecho unico;
se a ancora nao bater (arquivo diferente do esperado), ele AVISA e nao mexe.

Uso — rode de onde estao as duas pastas, ou passe os caminhos:
    python aplicar_atropelo_front.py
    python aplicar_atropelo_front.py --jsx C:\...\tipmike\src\screens\BacktestAvulso.jsx ^
                                     --router C:\...\tipmike_api\routers\backtest_upload.py
    python aplicar_atropelo_front.py --desfazer      (volta os .bak)

Faz backup .bak-atropelo antes de escrever. Rodar 2x nao duplica nada.
"""
import argparse
import os
import shutil
import sys

JSX_PADRAO = os.path.join("src", "screens", "BacktestAvulso.jsx")
ROUTER_PADRAO = os.path.join("routers", "backtest_upload.py")
SUFIXO_BAK = ".bak-atropelo"

# ---------------------------------------------------------------- JSX --------
JSX_EDICOES = [
    # 1) estado
    ("""  const [momentoAtivo, setMomentoAtivo] = useState(false);
  const [momentoMax, setMomentoMax] = useState('2');""",
     """  const [momentoAtivo, setMomentoAtivo] = useState(false);
  const [momentoMax, setMomentoMax] = useState('2');
  // v16 — ATROPELO: % dos jogos ANTERIORES de cada jogador que terminaram com
  // 15+ de diferenca. Vale o PIOR dos dois. Jogo que desanda mata aposta de
  // almofada, e o efeito bate nos DOIS lados — e' propriedade do JOGO.
  const [atropeloAtivo, setAtropeloAtivo] = useState(false);
  const [atropeloMax, setAtropeloMax] = useState('22');
  const [atropeloMargem, setAtropeloMargem] = useState('15');
  const [atropeloMinJogos, setAtropeloMinJogos] = useState('6');"""),

    # 2) bloco visual (entra logo apos o bloco do Momento)
    ("""Só funciona onde o coletor marca o período (hoje: Superbet).</div>
                </div>
                </Grupo>""",
     """Só funciona onde o coletor marca o período (hoje: Superbet).</div>
                </div>
                {/* ATROPELO (v16): propriedade do JOGADOR, calculada dos jogos
                    anteriores dele. Medido no handicap; no Over PIORA (jogo
                    desandado ainda estoura a linha). */}
                <div className="mt-3">
                  <div className="text-[10px] uppercase tracking-wider text-[--mike-fg-muted] font-bold mb-1.5">Atropelo (jogo que desanda)</div>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input type="checkbox" checked={atropeloAtivo} onChange={(e) => setAtropeloAtivo(e.target.checked)} className="accent-cyan-500" />
                    <span className="text-[11px] text-[--mike-fg-soft]">Evitar jogadores cujos jogos viram massacre</span>
                  </label>
                  {atropeloAtivo && (
                    <div className="grid grid-cols-3 gap-3 mt-2">
                      <Campo label="Atropelo máx. (%)" hint="corta se o PIOR dos dois passar disso">
                        <Input type="number" min="0" value={atropeloMax} onChange={setAtropeloMax} placeholder="ex: 22" />
                      </Campo>
                      <Campo label="Margem (pontos)" hint="diferença que conta como atropelo">
                        <Input type="number" min="1" value={atropeloMargem} onChange={setAtropeloMargem} placeholder="15" />
                      </Campo>
                      <Campo label="Mín. jogos" hint="abaixo disso reprova (fail closed)">
                        <Input type="number" min="1" value={atropeloMinJogos} onChange={setAtropeloMinJogos} placeholder="6" />
                      </Campo>
                    </div>
                  )}
                  <div className="text-[10px] text-[--mike-fg-muted] mt-1.5">
                    Taxa = % dos jogos ANTERIORES do jogador que terminaram com <b>{atropeloMargem || 15}+</b> de
                    diferença; vale o pior dos dois. Mediana da Blitz ≈ <b>11%</b>. Medido fora da amostra no HC:
                    cortar acima de <b>22%</b> melhorou o ROI nas 14 configs testadas (+4 a +12 pontos) e as
                    unidades subiram em 13 delas. <b>No Over piora.</b>
                  </div>
                </div>
                </Grupo>"""),

    # 3) validacao
    ("""      if (fmin != null && fmax != null && fmin > fmax) return 'Folga mín não pode ser maior que a máx.';
    }
    return null;""",
     """      if (fmin != null && fmax != null && fmin > fmax) return 'Folga mín não pode ser maior que a máx.';
    }
    if (atropeloAtivo) {
      const a = numOuNull(atropeloMax);
      if (a == null) return 'Atropelo ligado: informe o atropelo máximo (%).';
      if (a <= 0 || a > 100) return 'Atropelo máximo: use um valor entre 1 e 100.';
      const mg = numOuNull(atropeloMargem);
      if (mg != null && mg <= 0) return 'Margem do atropelo deve ser maior que zero.';
      const mj = numOuNull(atropeloMinJogos);
      if (mj != null && mj < 1) return 'Mín. de jogos do atropelo deve ser pelo menos 1.';
    }
    return null;"""),

    # 4a) payload
    ("""      momento_ativo: momentoAtivo,
      momento_max: momentoAtivo ? numOuNull(momentoMax) : null,""",
     """      momento_ativo: momentoAtivo,
      momento_max: momentoAtivo ? numOuNull(momentoMax) : null,
      atropelo_ativo: atropeloAtivo,
      atropelo_max: atropeloAtivo ? numOuNull(atropeloMax) : null,
      atropelo_margem: atropeloAtivo ? numOuNull(atropeloMargem) : null,
      atropelo_min_jogos: atropeloAtivo ? numOuNull(atropeloMinJogos) : null,"""),

    # 4b) deps do validarFiltros
    ("""  }, [escadaLinhas, folgaAtiva, folgaMin, folgaMax, momentoAtivo, momentoMax, maxPorJogo, uploadId, mercado, linhaMin""",
     """  }, [escadaLinhas, folgaAtiva, folgaMin, folgaMax, momentoAtivo, momentoMax, atropeloAtivo, atropeloMax, atropeloMargem, atropeloMinJogos, maxPorJogo, uploadId, mercado, linhaMin"""),

    # 4c) deps do handleRodar
    ("""  }, [validarFiltros, escadaLinhas, folgaAtiva, folgaMin, folgaMax, momentoAtivo, momentoMax, maxPorJogo, uploadId, mercado, lado, casa, esporte, filtrosHist,""",
     """  }, [validarFiltros, escadaLinhas, folgaAtiva, folgaMin, folgaMax, momentoAtivo, momentoMax, atropeloAtivo, atropeloMax, atropeloMargem, atropeloMinJogos, maxPorJogo, uploadId, mercado, lado, casa, esporte, filtrosHist,"""),
]

# ------------------------------------------------------------- ROUTER --------
ROUTER_EDICOES = [
    ("""    folga_ativo: bool = False""",
     """    # v16 — ATROPELO (taxa de jogos do jogador que terminam com 15+ de
    # diferenca). Ausente = filtro desligado; o worker ignora sem as chaves.
    atropelo_ativo: bool = False
    atropelo_max: float | None = None
    atropelo_min: float | None = None
    atropelo_margem: float | None = None
    atropelo_min_jogos: int | None = None
    folga_ativo: bool = False"""),

    ("""    if bool(req.folga_ativo) and (req.folga_min is not None or req.folga_max is not None):
        filtros["folgaAtivo"] = True""",
     """    # ATROPELO (v16): mesmo formato do bot ao vivo. So liga se houver borda —
    # ativo sem min nem max nao filtra nada e o worker desliga sozinho.
    if bool(getattr(req, "atropelo_ativo", False)) and (
            getattr(req, "atropelo_min", None) is not None
            or getattr(req, "atropelo_max", None) is not None):
        filtros["atropeloAtivo"] = True
        if getattr(req, "atropelo_min", None) is not None:
            filtros["atropeloMin"] = float(req.atropelo_min)
        if getattr(req, "atropelo_max", None) is not None:
            filtros["atropeloMax"] = float(req.atropelo_max)
        if getattr(req, "atropelo_margem", None):
            filtros["atropeloMargem"] = float(req.atropelo_margem)
        if getattr(req, "atropelo_min_jogos", None):
            filtros["atropeloMinJogos"] = int(req.atropelo_min_jogos)

    if bool(req.folga_ativo) and (req.folga_min is not None or req.folga_max is not None):
        filtros["folgaAtivo"] = True"""),
]


def achar(caminho, padrao):
    if caminho and os.path.isfile(caminho):
        return caminho
    if os.path.isfile(padrao):
        return padrao
    for raiz in (".", "..", os.path.join("..", "tipmike"),
                 os.path.join("..", "tipmike_api")):
        alvo = os.path.join(raiz, padrao)
        if os.path.isfile(alvo):
            return alvo
    return None


def aplicar(caminho, edicoes, rotulo):
    if not caminho:
        print(f"  {rotulo}: NAO ENCONTRADO — passe o caminho na linha de comando")
        return False
    with open(caminho, "r", encoding="utf-8") as f:
        src = f.read()
    if "atropeloAtivo" in src or "atropelo_ativo" in src:
        print(f"  {rotulo}: JA TEM atropelo — nada a fazer")
        return True
    novo = src
    problemas = []
    for i, (velho, troca) in enumerate(edicoes, 1):
        n = novo.count(velho)
        if n != 1:
            problemas.append(f"    ediçao {i}: ancora aparece {n}x (esperado 1)")
            continue
        novo = novo.replace(velho, troca)
    if problemas:
        print(f"  {rotulo}: NAO APLIQUEI — o arquivo difere do esperado:")
        for p in problemas:
            print(p)
        print("    (me mande o arquivo que eu ajusto as ancoras)")
        return False
    shutil.copyfile(caminho, caminho + SUFIXO_BAK)
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(novo)
    ganho = len(novo.splitlines()) - len(src.splitlines())
    print(f"  {rotulo}: OK — +{ganho} linhas | backup em {os.path.basename(caminho)}{SUFIXO_BAK}")
    return True


def desfazer(caminho, rotulo):
    if caminho and os.path.isfile(caminho + SUFIXO_BAK):
        shutil.copyfile(caminho + SUFIXO_BAK, caminho)
        print(f"  {rotulo}: revertido do backup")
    else:
        print(f"  {rotulo}: sem backup pra reverter")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jsx", default=None)
    p.add_argument("--router", default=None)
    p.add_argument("--desfazer", action="store_true")
    a = p.parse_args()

    jsx = achar(a.jsx, JSX_PADRAO)
    router = achar(a.router, ROUTER_PADRAO)
    print("=" * 70)
    print(" ATROPELO no painel")
    print("=" * 70)
    print(f"  jsx    : {jsx or '(nao achei)'}")
    print(f"  router : {router or '(nao achei)'}")
    print()
    if a.desfazer:
        desfazer(jsx, "BacktestAvulso.jsx")
        desfazer(router, "backtest_upload.py")
        return
    ok1 = aplicar(jsx, JSX_EDICOES, "BacktestAvulso.jsx")
    ok2 = aplicar(router, ROUTER_EDICOES, "backtest_upload.py")
    print()
    if ok1 and ok2:
        print(" PRONTO. Agora:")
        print("   1. reinicie a API      -> nssm restart TipMikeAPI")
        print("   2. rebuild do front    -> npm run build   (ou npm run dev)")
        print("   3. TESTE DE ACEITE: rode um backtest SEM marcar o atropelo —")
        print("      tem que dar o MESMO numero de antes (a mudanca e' aditiva).")
        print("   4. depois marque atropelo max 22 na familia")
        print("      Todas>=70 & L>=5,5 & folga>=2,5 -> ROI deve ir de ~14% pra ~20%")
    else:
        print(" Alguma parte nao foi aplicada — veja acima. Nada foi quebrado.")
    print(" Reverter tudo: python aplicar_atropelo_front.py --desfazer")


if __name__ == "__main__":
    main()
