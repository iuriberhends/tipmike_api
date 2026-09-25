# -*- coding: utf-8 -*-
"""
testar_extrator.py
==================
Exercita as DECISOES do coletor sem tocar no Gran.

POR QUE EXISTE
--------------
Em 24/09 eu emendei conserto em cima de conserto e mandei cada um direto pra
VPS depois de so conferir que compilava. Compilar nao prova nada: o defeito
que apareceu no log - "bloco 1/149: 5361 questoes - larga demais, ja medida
antes" - era o corte por tamanho REMOVIDO brigando com uma leitura da lista
antiga que ficou. Duas partes certas sozinhas, erradas juntas. Nenhum teste
de sintaxe pegaria isso; este pega.

Cada teste abaixo corresponde a um erro que de fato aconteceu. Se um deles
voltar a falhar, foi uma regressao de verdade, nao teoria.

USO:
    python testar_extrator.py
"""

import sys
import importlib.util
from pathlib import Path

RAIZ = Path(__file__).parent
_sp = importlib.util.spec_from_file_location("g", RAIZ / "gran_extrator.py")
g = importlib.util.module_from_spec(_sp)
_argv, sys.argv = sys.argv, [sys.argv[0]]
_sp.loader.exec_module(g)
sys.argv = _argv

falhas = []


def conferir(rotulo, obtido, esperado):
    ok = obtido == esperado
    print(f"  {'ok  ' if ok else 'FALHA'} {rotulo}")
    if not ok:
        print(f"        esperava {esperado!r}, veio {obtido!r}")
        falhas.append(rotulo)


def testar_estados():
    print("\n  ESTADOS: so a Bahia, e so em Historia/Geografia")
    casos = [
        ("História do Brasil", "História dos Estados Brasileiros", True),
        ("Geografia do Brasil", "Geografia Específica dos Estados e DF", True),
        ("História do Brasil", "Paraná - PR", True),
        ("Geografia do Brasil", "São Paulo - SP", True),
        ("História do Brasil", "Bahia - BA", False),
        ("Geografia do Brasil", "Bahia - BA", False),
        ("História do Brasil", "Salvador - BA", False),
        ("História do Brasil", "Período Regencial - 1831 a 1840", False),
        # municipios: o pai sai, os baianos ficam
        ("História do Brasil", "História dos Municípios", True),
        ("Geografia do Brasil", "Municípios Brasileiros", True),
        ("História do Brasil", "Feira de Santana - BA", False),
        ("História do Brasil", "Vitória da Conquista - BA", False),
        ("História do Brasil", "Campinas - SP", True),
        # a armadilha: 519 questoes de Matematica casam com "- PA"
        ("Matemática", "Progressão Aritmética - PA", False),
        ("Direito Penal", "Paraná - PR", False),
    ]
    for mat, nome, esperado in casos:
        conferir(f"{mat[:11]:11} {nome[:38]:38}",
                 g.e_estado_fora_do_edital(mat, nome), esperado)

    fica, sai = g.podar_estados(
        "História do Brasil",
        ["397244", "419732", "420275", "433737", "397459", "400487"],
        {"397459": "Paraná - PR", "433737": "Período Regencial"})
    conferir("poda mantem as 3 Bahia + o nacional", sorted(fica),
             sorted(["419732", "420275", "433737", "400487"]))
    conferir("poda remove o pai e o Parana", sorted(e for e, _ in sai),
             ["397244", "397459"])


def testar_bancas():
    print("\n  BANCAS: as 14, e a url so monta se o parametro foi aprendido")
    conferir("sao 14 bancas", len(g.BANCAS_PERMITIDAS), 14)
    for nome, bid in (("FCC", 92), ("IBFC", 277), ("UNEB", 1021),
                      ("CONSULTEC", 330)):
        conferir(f"{nome} = {bid} (id que o dono ja sabia)",
                 bid in g.BANCAS_PERMITIDAS, True)
    conferir("IDIB 937 NAO entra (nao e IDECAN)",
             937 in g.BANCAS_PERMITIDAS, False)

    g.PARAM_BANCA = None
    try:
        g.montar_url_tela([403587], ["Médio"], bancas=[92])
        conferir("sem parametro aprendido, recusa montar a url", False, True)
    except g.Parada:
        conferir("sem parametro aprendido, recusa montar a url", True, True)
    g.PARAM_BANCA = "banca"
    u = g.montar_url_tela([403587], ["Médio"], bancas=[92, 27])
    conferir("url leva banca=92,27", "banca=92,27" in u, True)
    conferir("url sem banca continua valida",
             "banca=" not in g.montar_url_tela([403587], ["Médio"]), True)


def testar_conferidor_de_banca():
    print("\n  CONFERIDOR: filtro que nao pegou tem que ser denunciado")
    boas = [{"banca_id": 92} for _ in range(19)] + [{"banca_id": 27}]
    ok, _ = g.conferir_filtro_banca(boas, [92, 27])
    conferir("20/20 das bancas pedidas -> passa", ok, True)
    ruins = [{"banca_id": 999} for _ in range(18)] + [{"banca_id": 92}] * 2
    ok, msg = g.conferir_filtro_banca(ruins, [92, 27])
    conferir("filtro ignorado pelo Gran -> reprova", ok, False)
    conferir("e diz quem veio no lugar", "999" in msg, True)


def testar_suspeitas():
    print("\n  ETIQUETA SUSPEITA: acha a intrusa, sem acusar assunto folha")
    # 60 questoes so da etiqueta 999 (intrusa) e 60 que casam com 2 do filtro
    intrusa = [{"assunto_ids": [999]} for _ in range(60)]
    normais = [{"assunto_ids": [111, 222]} for _ in range(60)]
    sus = g.etiquetas_suspeitas(intrusa + normais, [999, 111, 222])
    conferir("aponta a intrusa", [e for e, _, _ in sus], ["999"])
    poucas = [{"assunto_ids": [777]} for _ in range(5)]
    sus2 = g.etiquetas_suspeitas(poucas + normais, [777, 111, 222])
    conferir("nao acusa quem tem poucas questoes", sus2, [])


def testar_sem_contradicao():
    print("\n  SEM CONTRADICAO: nada mais pula bloco por tamanho")
    fonte = (RAIZ / "gran_extrator.py").read_text(encoding="utf-8")
    conferir("sumiu o corte por tamanho",
             "etiqueta larga demais, PULANDO" in fonte, False)
    conferir("sumiu o pulo pela lista antiga",
             "larga demais, ja medida antes" in fonte, False)
    conferir("padrao continua uma etiqueta por consulta",
             "uma_etiqueta=not args.em_blocos" in fonte, True)


def testar_fiacao():
    """
    A poda so vale se for CHAMADA no caminho que a VPS percorre.

    Os testes de cima conferem a funcao. Mas o defeito real de 24/09 foi
    outro: `podar_estados` estava dentro do `if not g.get("assuntos")`, que
    so roda quando a materia e vista pela 1a vez. Na VPS o filtro de Historia
    ja existia, entao a poda inteira era enfeite - teste verde, maquina
    errada. Este teste olha ONDE a funcao e chamada.
    """
    import ast
    print("\n  FIACAO: a poda roda tambem em materia ja comecada")
    fonte = (RAIZ / "gran_extrator.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    fn = next(n for n in ast.walk(arvore)
              if isinstance(n, ast.FunctionDef) and n.name == "coletar_edital")

    # acha todo `if not g.get("assuntos")` e ve se a poda esta dentro dele
    dentro_do_if_de_montagem = False
    for no in ast.walk(fn):
        if not isinstance(no, ast.If):
            continue
        cond = ast.unparse(no.test)
        if "assuntos" not in cond or "not " not in cond:
            continue
        corpo = "".join(ast.unparse(x) for x in no.body)
        if "podar_estados" in corpo:
            dentro_do_if_de_montagem = True
    conferir("podar_estados NAO esta preso ao 1o encontro da materia",
             dentro_do_if_de_montagem, False)
    conferir("podar_estados e chamado em coletar_edital",
             "podar_estados" in ast.unparse(fn), True)
    conferir("nomear_filtro e chamado antes de coletar",
             "nomear_filtro" in ast.unparse(fn), True)


def testar_assinaturas():
    """
    Toda chamada interna bate com a assinatura de quem ela chama?

    O `nomear_filtro` montava `ListagemPelaTela(pagina, niveis=..., ...)` e
    faltava o `assuntos`, que e posicional obrigatorio. Compilou, passou em
    todos os testes e quebrou na VPS na primeira materia - porque teste
    nenhum CHAMAVA a funcao. Aqui eu confiro as chamadas sem executar nada.
    """
    import ast
    print("\n  ASSINATURAS: chamada bate com quem ela chama")
    fonte = (RAIZ / "gran_extrator.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)

    alvos = {}
    for no in ast.walk(arvore):
        if isinstance(no, ast.FunctionDef):
            alvos[no.name] = no.args
        elif isinstance(no, ast.ClassDef):
            for f in no.body:
                if isinstance(f, ast.FunctionDef) and f.name == "__init__":
                    alvos[no.name] = f.args

    # NOME LOCAL ESCONDE O GLOBAL.
    #
    # A 1a versao acusou 3 chamadas que estavam certas: `pedir` e um lambda
    # local e `ir_para` e uma closure - nenhum e a funcao de mesmo nome do
    # modulo. Verificador que chora lobo e pior que nenhum (ja tinha me
    # custado isso no verificador de SQL), entao aqui eu junto os nomes que
    # cada funcao amarra localmente e nao olho pra eles.
    def locais_de(fn):
        nomes = {a.arg for a in fn.args.args}
        nomes |= {a.arg for a in getattr(fn.args, "kwonlyargs", [])}
        for x in ast.walk(fn):
            if isinstance(x, ast.Assign):
                for alvo in x.targets:
                    if isinstance(alvo, ast.Name):
                        nomes.add(alvo.id)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if x is not fn:
                    nomes.add(x.name)
            elif isinstance(x, ast.NamedExpr) and isinstance(x.target, ast.Name):
                nomes.add(x.target.id)
        return nomes

    # UMA VEZ POR FUNCAO, nao uma vez por chamada.
    #
    # A 1a versao chamava `locais_de(fn)` dentro do laco das chamadas, e
    # `locais_de` percorre a funcao inteira: num arquivo de 5.000 linhas isso
    # virou 7s aqui e travou na VPS, que e mais fraca. O teste tem que ser
    # rapido pra ser rodado sempre - se demora, deixa de ser rodado.
    sombra = {}
    for fn in [n for n in ast.walk(arvore) if isinstance(n, ast.FunctionDef)]:
        meus = locais_de(fn)
        for x in ast.walk(fn):
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Name):
                sombra.setdefault(id(x), set()).update(meus)

    problemas = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call) or not isinstance(no.func, ast.Name):
            continue
        if no.func.id in sombra.get(id(no), set()):
            continue                      # e um nome local, nao o do modulo
        args = alvos.get(no.func.id)
        if args is None:
            continue
        nomes = [a.arg for a in args.args]
        if no.func.id in {c.name for c in ast.walk(arvore)
                          if isinstance(c, ast.ClassDef)}:
            nomes = nomes[1:]              # tira o self
        n_padrao = len(args.defaults)
        obrigatorios = nomes[:len(nomes) - n_padrao] if n_padrao else nomes
        dados = set(nomes[:len(no.args)]) | {k.arg for k in no.keywords}
        faltando = [o for o in obrigatorios if o not in dados]
        if faltando and not any(k.arg is None for k in no.keywords):
            problemas.append(f"{no.func.id}() sem {faltando} "
                             f"(linha {no.lineno})")

    conferir("nenhuma chamada com argumento obrigatorio faltando",
             problemas, [])
    if problemas:
        for p in problemas[:6]:
            print(f"        {p}")


def testar_pagina_um():
    """
    A pagina 1 tem que ser reconhecida mesmo sem `page` na URL.

    O app omite `page` quando pede a primeira. Eu comparava None com "1" e
    descartava a resposta - a pagina 1 nunca era reconhecida, a ida-e-volta
    nao adiantava (na volta ele serve do proprio cache, sem rede) e a coleta
    parava com "a pagina 1 nao veio pelo app", com a pagina na tela e 20
    paginas na barra. Travou Historia em 24/09.
    """
    print("\n  PAGINA 1: reconhecida mesmo sem `page` na url")

    class FalsaResp:
        def __init__(self, url):
            self.url, self.status = url, 200
        def json(self):
            return {"data": {"rows": [1], "total": 1, "pages": 1}}

    class FalsaTela(g.TelaDeQuestoes):
        def __init__(self, urls):
            self.respostas = [{"resp": FalsaResp(u), "status": 200,
                               "retry_after": None} for u in urls]

    base = "https://rota-api.x/v1/elastic/questao?"
    casos = [
        ("sem page, perPage=100 -> pagina 1", [base + "perPage=100"], 1, 100, True),
        ("page=1 explicito", [base + "page=1&perPage=100"], 1, 100, True),
        ("page=2 nao e a 1", [base + "page=2&perPage=100"], 1, 100, False),
        ("sem page mas perPage=20 nao serve", [base + "perPage=20"], 1, 100, False),
        ("pagina 2 ainda exige page=2", [base + "perPage=100"], 2, 100, False),
    ]
    for rot, urls, pag, pp, esperado in casos:
        conferir(rot, FalsaTela(urls).corpo(pag, pp) is not None, esperado)

    fonte = (RAIZ / "gran_extrator.py").read_text(encoding="utf-8")
    conferir("o cache do app tambem trata page ausente como 1",
             "(par.page || '1')" in fonte, True)


def testar_paginacao():
    print("\n  PAGINACAO: numero de pagina sem o tamanho dele nao vale")
    import math
    for total, pp, esperado in ((470, 20, 24), (470, 100, 5), (1630, 100, 17)):
        conferir(f"{total} a {pp}/pagina = {esperado} paginas",
                 math.ceil(total / pp), esperado)
    conferir("a 22 nao existe com 100 por pagina", 22 <= math.ceil(470 / 100),
             False)


def main():
    print("=" * 62)
    print("  TESTE DO EXTRATOR - nenhuma requisicao ao Gran")
    print("=" * 62)
    testar_estados()
    testar_bancas()
    testar_conferidor_de_banca()
    testar_suspeitas()
    testar_sem_contradicao()
    testar_fiacao()
    testar_assinaturas()
    testar_pagina_um()
    testar_paginacao()
    print("\n" + "=" * 62)
    if falhas:
        print(f"  {len(falhas)} FALHA(S):")
        for f in falhas:
            print(f"     {f}")
        return 1
    print("  tudo passou - pode levar pra VPS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
