# -*- coding: utf-8 -*-
"""
gran_extrator.py
================
Extrai do Gran Cursos Online usando a SUA sessao ja logada no Chrome.

Nunca pede senha. Voce loga uma vez na janela do abrir_chrome_gran.bat e o
extrator conversa com aquela janela pela porta 9380.

MODOS
-----
  Teste (Fase 0) - valida o formato antes de soltar a varredura:
      python gran_extrator.py --aula          (a aula aberta na aba)
      python gran_extrator.py --questoes      (o filtro aberto na aba)

  Automatico (Fase 1 e 6) - varre sozinho:
      python gran_extrator.py --curso --disciplina "Direito Penal"
      python gran_extrator.py --todas-questoes --disciplina "Direito Penal"
      python gran_extrator.py --curso                 (TODAS as disciplinas)
      python gran_extrator.py --todas-questoes        (TODAS as disciplinas)

  Questoes do edital (Mike Mentor) - materia por materia, retomavel:
      python gran_extrator.py --coletar-edital
      python gran_extrator.py --coletar-edital --horas 6
      python gran_extrator.py --coletar-edital --disciplina "Língua Portuguesa"
      (filtro = cadernos das aulas + etiquetas de prova de Soldado; grava
       saida/questoes/edital <materia>/ e o resumo em saida/contagem_edital.json)

  Contagem (Mike Mentor, spec v3 §3.1) - so conta, nao coleta:
      python gran_extrator.py --contar filtros_edital.json
      (uma listagem por materia; grava saida/contagem_edital.json)

REGRAS
------
  - Pausa de 2 a 3 segundos entre acoes, sem paralelismo.
    Nas AULAS (--curso/--aula) da pra pedir menos com --pausa-aulas SEG:
    comeca em 1,0s e desce 25% a cada 30 requisicoes limpas ate SEG.
    Um 429 para tudo (como sempre) e o progresso fica gravado.
  - Captcha, bloqueio ou "acessos simultaneos" -> PARA e avisa. Nunca contorna,
    nunca repete em laco.
  - Retomavel: grava saida/_progresso.json. Aula ja coletada nao baixa de novo.
  - Nunca responde questao (isso sujaria sua estatistica no Gran).
  - Antes de uma varredura grande, mostra a conta de requisicoes e PERGUNTA.

IMPORTANTE: nao use o Gran em outra janela/aparelho enquanto isto roda. O site
derruba sessao por acesso simultaneo.

SAIDA
-----
    saida/aulas/<disciplina>/<ordem>_<codigo_aula>_<titulo>.json   (+ .pdf ao lado)
        (aulas coletadas antes de 23/09 ficam com o nome antigo, sem o codigo -
         o _progresso.json aponta o arquivo certo de cada uma)
    saida/contagem_edital.json   (modo --contar)
    saida/questoes/<disciplina>/<disciplina>.json
    saida/_progresso.json
"""

import sys
import io
import re
import html
import json
import time
import random
import argparse
from pathlib import Path
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote, quote

# Console do Windows e cp1252 e engasga com acento. Forca UTF-8.
#
# line_buffering=True NAO e detalhe: sem ele o wrapper novo nasce com buffer
# de BLOCO (~8 KB) em vez do buffer de LINHA que o terminal usa. O efeito e
# cruel numa coleta longa - o progresso e escrito mas fica preso, e a tela
# parece travada por minutos ate despejar tudo de uma vez.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[ERRO] Playwright nao instalado. Rode:  pip install playwright")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constantes descobertas na inspecao (ver gran_inspecionar.py)
# ---------------------------------------------------------------------------
PORTA_CDP = 9380
RAIZ = Path(__file__).parent
DIR_SAIDA = RAIZ / "saida"
ARQ_PROGRESSO = DIR_SAIDA / "_progresso.json"

HOST_ALUNO = "https://www.grancursosonline.com.br"
HOST_API_Q = "https://rota-api.grancursosonline.com.br"

# Identificador PUBLICO do app de questoes (nao e credencial sua; e igual pra
# todo mundo). Sem ele a API de questoes recusa.
CLIENT_ID = "2u8s7nrp2bthg4pdv8lvbh54b8"

# Os 6 artefatos da "Central de revisao" e como viram campo aqui
ARTEFATOS = {
    "transcript": "transcricao",
    "summary": "resumo",
    "review": "resumo_bolso",
    "mindmap": "mapa_mental",
    "flashcards": "flashcards",
    "quiz": "questoes_fixacao",
}

PAUSA_MIN, PAUSA_MAX = 2.0, 3.0
# A listagem de questoes devolve ~130 KB por chamada. Quando elas vem em
# sequencia (releitura pra completar os assuntos), 2,5s dispara 429.
# Na coleta normal isso nao aparece porque as estatisticas as espacam.
PAUSA_LISTAGEM = 8.0
NIVEL_PADRAO = "Médio"          # escolaridade do concurso de Soldado
POR_PAGINA_PADRAO = 20          # o mesmo que o app usa

SINAIS_BLOQUEIO = [
    "verifique que voce e humano", "verifique que você é humano",
    "confirme que voce nao e um robo", "confirme que você não é um robô",
    "acesso negado", "access denied", "403 forbidden",
    "muitas requisicoes", "muitas requisições", "too many requests",
    "sua conexao foi bloqueada", "sua conexão foi bloqueada",
    "unusual traffic", "trafego incomum", "tráfego incomum",
]


class Parada(Exception):
    """
    Motivo pra parar tudo: captcha, bloqueio, sessao derrubada.

    Carrega o 'status' HTTP quando veio de uma requisicao, pra quem chamou
    poder distinguir 401 (autorizacao) de 429 (ritmo) - tratar os dois igual
    fazia o extrator dizer "token recusado" quando era so limite de taxa.
    """

    def __init__(self, mensagem, status=None):
        super().__init__(mensagem)
        self.status = status


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

# --pausa-aulas (so nos modos de AULA): rampa de 1,0s ate o piso pedido.
# Os 429 de 23/09 vieram todos da API de QUESTOES; os endpoints de aula nunca
# foram testados abaixo de 2s. Por isso nao salta direto pro piso: desce aos
# poucos, e qualquer 429 para a execucao (o main registra e sai).
PAUSA_AULAS_PISO = None
PAUSA_AULAS_ATUAL = 1.0
PAUSA_AULAS_DEGRAU = 30          # requisicoes limpas antes de acelerar
_pausas_limpas = 0


def pausa(pagina=None, motivo=""):
    """
    Pausa de 2 a 3 segundos entre acoes (ou a rampa do --pausa-aulas).

    Usa wait_for_timeout quando tem pagina: na API sincrona do Playwright o
    time.sleep NAO entrega os eventos do navegador (ficam na fila).
    """
    global PAUSA_AULAS_ATUAL, _pausas_limpas
    if PAUSA_AULAS_PISO is not None:
        _pausas_limpas += 1
        if _pausas_limpas >= PAUSA_AULAS_DEGRAU and PAUSA_AULAS_ATUAL > PAUSA_AULAS_PISO:
            _pausas_limpas = 0
            novo = max(PAUSA_AULAS_PISO, PAUSA_AULAS_ATUAL * 0.75)
            print(f"      >> {PAUSA_AULAS_DEGRAU} requisicoes limpas: pausa "
                  f"{PAUSA_AULAS_ATUAL:.2f}s -> {novo:.2f}s")
            PAUSA_AULAS_ATUAL = novo
        s = random.uniform(PAUSA_AULAS_ATUAL, PAUSA_AULAS_ATUAL * 1.25)
    else:
        s = random.uniform(PAUSA_MIN, PAUSA_MAX)
    if motivo:
        print(f"      . {motivo} ({s:.1f}s)")
    if pagina is not None:
        pagina.wait_for_timeout(int(s * 1000))
    else:
        time.sleep(s)


def pausa_longa(pagina=None, motivo=""):
    """Pausa maior, pras chamadas pesadas de listagem (ver PAUSA_LISTAGEM)."""
    s = random.uniform(PAUSA_LISTAGEM, PAUSA_LISTAGEM + 2)
    if motivo:
        print(f"      . {motivo} ({s:.1f}s)")
    if pagina is not None:
        pagina.wait_for_timeout(int(s * 1000))
    else:
        time.sleep(s)


class Tempos:
    """
    Mede o ida-e-volta real de cada tipo de chamada.

    Serve pra saber se ainda vale baixar a pausa: quando a pausa fica menor
    que a latencia, o ganho vira migalha. Separa listagem (pesada) das leves
    porque sao ordens de grandeza diferentes.
    """

    def __init__(self):
        self.amostras = {}

    def registrar(self, url, segundos):
        if "elastic/questao" in url:
            tipo = "listagem"
        elif "/estatisticas" in url:
            tipo = "estatistica"
        elif "/comentario/" in url:
            tipo = "comentario"
        elif "ltp.infra" in url:
            tipo = "artefato"
        else:
            tipo = "outras"
        self.amostras.setdefault(tipo, []).append(segundos)

    def relatorio(self):
        if not self.amostras:
            return []
        linhas = ["  TEMPO DE REDE (ida-e-volta, sem contar a pausa):"]
        for tipo, vs in sorted(self.amostras.items()):
            vs_ord = sorted(vs)
            mediana = vs_ord[len(vs_ord) // 2]
            p90 = vs_ord[int(len(vs_ord) * 0.9)]
            linhas.append(f"     {tipo:12} {len(vs):>5}x  "
                          f"mediana {mediana*1000:>5.0f}ms  p90 {p90*1000:>5.0f}ms")
        return linhas


TEMPOS = Tempos()


def duracao_legivel(segundos):
    """3725 -> '1h02'   ·   420 -> '7min'   ·   45 -> '45s'"""
    if segundos is None or segundos < 0:
        return "?"
    s = int(segundos)
    if s < 90:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


class Andamento:
    """
    Diz onde a coleta esta e quanto falta.

    A estimativa vem do ritmo MEDIDO nesta execucao, nao da conta teorica de
    2,5s por requisicao: o ritmo real varia (questao sem comentario gasta uma
    chamada a menos, o ritmo adaptativo muda a pausa). Quando se retoma uma
    coleta, o que ja estava pronto conta no total mas nao na media - senao a
    previsao sai errada logo no comeco.
    """

    def __init__(self, total, ja_feitos=0, unidade="questoes"):
        self.total = total or 0
        self.base = ja_feitos or 0
        self.feitos_agora = 0
        self.unidade = unidade
        self.inicio = time.time()

    def marcar(self, n=1):
        self.feitos_agora += n

    @property
    def feitos(self):
        return self.base + self.feitos_agora

    def linha(self, prefixo=""):
        decorrido = time.time() - self.inicio
        pct = (100.0 * self.feitos / self.total) if self.total else 0.0
        faltam = max(0, self.total - self.feitos)
        if self.feitos_agora >= 3 and decorrido > 5:
            por_item = decorrido / self.feitos_agora
            eta = duracao_legivel(faltam * por_item)
            ritmo = f"{self.feitos_agora / (decorrido / 60):.0f}/min"
        else:
            eta, ritmo = "calculando", "-"
        barra_cheia = int(pct / 5)
        barra = "#" * barra_cheia + "." * (20 - barra_cheia)
        return (f"  [{barra}] {pct:5.1f}%  {self.feitos}/{self.total} {self.unidade}"
                f"  ·  {duracao_legivel(decorrido)} corridos"
                f"  ·  faltam ~{eta} ({ritmo})" + (f"  ·  {prefixo}" if prefixo else ""))


class Ritmo:
    """
    Acha o ritmo seguro TRABALHANDO - nao num teste separado.

    Por que existe: fixar uma pausa grande "por segurança" custa dezenas de
    horas quando sao 13 disciplinas. E fixar uma pequena leva 429. O ritmo
    certo ninguem mede sem tentar, entao mede-se durante o trabalho util:
    as paginas da calibracao coletam de verdade.

    Como funciona:
      - comeca NO PISO (o mais rapido que se admite) e deixa o servidor dizer
        onde e o limite. Achar o teto na 1a pagina e melhor que descer devagar
        por 60 questoes - e o recuo existe justamente pra isso.
      - no 429: respeita o Retry-After, dobra a pausa e TRAVA ali
      - se comecar acima do piso, acelera 25% a cada N acertos
      - depois de max_429 tropecos, desiste e para

    Distincao que importa: 429 e o servidor pedindo pra ir mais devagar -
    desacelerar e obedecer. Diferente de captcha ou 401, onde a unica resposta
    certa e parar. Por isso aqui tem recuo, e nos outros nao.
    """

    def __init__(self, inicial=None, piso=8.0, teto=300.0,
                 acelera_a_cada=4, max_429=3, unidade="paginas"):
        self.unidade = unidade
        self.atual = float(inicial if inicial is not None else piso)
        self.piso = float(piso)
        self.teto = float(teto)
        self.acelera_a_cada = acelera_a_cada
        self.max_429 = max_429
        self.seguidas = 0
        self.tropecos = 0
        self.ultimo_bom = None
        self.trava = None        # depois de um 429, nao desce mais daqui

    def _fmt(self, s):
        """0.4 -> '0,4s' · 34 -> '34s'. Sem isso, sub-segundo virava '0s'."""
        return f"{s:.1f}s" if s < 10 else f"{s:.0f}s"

    def esperar(self, pagina, motivo=""):
        s = random.uniform(self.atual, self.atual * 1.15)
        print(f"      . {motivo} ({self._fmt(s)})")
        if pagina is not None:
            pagina.wait_for_timeout(int(s * 1000))
        else:
            time.sleep(s)

    def deu_certo(self):
        self.ultimo_bom = self.atual
        self.seguidas += 1
        if self.seguidas >= self.acelera_a_cada:
            self.seguidas = 0
            novo = max(self.piso, self.atual * 0.75)
            if self.trava:
                novo = max(novo, self.trava)
            if novo < self.atual:
                print(f"      >> {self.acelera_a_cada} {self.unidade} limpas: "
                      f"acelerando {self._fmt(self.atual)} -> {self._fmt(novo)}")
                self.atual = novo

    def levou_429(self, pagina, retry_after=None):
        """Devolve True se vale continuar (ja desacelerado), False se e hora de parar."""
        self.tropecos += 1
        self.seguidas = 0
        # o ritmo que falhou nao pode mais ser usado; travo acima dele
        self.trava = min(self.teto, self.atual * 2)
        anterior = self.atual
        self.atual = self.trava
        if self.tropecos > self.max_429:
            print(f"      >> {self.tropecos}o 429. Ja desacelerei ate "
                  f"{self._fmt(self.atual)} e nao adiantou - parando.")
            return False
        espera = float(retry_after) if retry_after else max(600.0, anterior * 10)
        print(f"      >> 429 no ritmo de {self._fmt(anterior)} "
              f"({self.tropecos}/{self.max_429}).")
        print(f"         Esperando {duracao_legivel(espera)} e voltando a "
              f"{self._fmt(self.atual)}.")
        if pagina is not None:
            pagina.wait_for_timeout(int(espera * 1000))
        else:
            time.sleep(espera)
        return True

    def resumo(self, nome="ritmo"):
        return (f"{nome} final {self._fmt(self.atual)}"
                + (f", melhor que funcionou {self._fmt(self.ultimo_bom)}"
                   if self.ultimo_bom else "")
                + f", {self.tropecos} tropeco(s)")


def nome_de_arquivo(texto, limite=90):
    """Nome de arquivo seguro no Windows."""
    t = (texto or "sem-titulo").strip().replace("/", "-").replace("\\", "-")
    t = re.sub(r'[<>:"|?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:limite].strip() or "sem-titulo"


def checar_bloqueio(pagina):
    """Levanta Parada se tiver captcha VISIVEL ou bloqueio na tela."""
    try:
        d = pagina.evaluate("""() => {
            const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
            const ifr = [...document.querySelectorAll('iframe')]
                .filter(f => /recaptcha|hcaptcha|turnstile/i.test(f.src||''));
            return {
              desafio: ifr.some(f => vis(f) && f.getBoundingClientRect().height > 80),
              widget: [...document.querySelectorAll('.g-recaptcha,.h-captcha,#challenge-form')].some(vis),
              texto: (document.body.innerText||'').slice(0,3000).toLowerCase(),
            };
        }""")
    except Exception:
        return
    # O reCAPTCHA v3 invisivel roda em TODA pagina do Gran - nao e bloqueio.
    if d.get("desafio"):
        raise Parada("tem um captcha aberto na tela")
    if d.get("widget"):
        raise Parada("tem um widget de captcha visivel")
    for s in SINAIS_BLOQUEIO:
        if s in d.get("texto", ""):
            raise Parada(f"a pagina esta mostrando '{s}'")


def buscar(pagina, url, headers=None, metodo="GET", corpo=None, binario=False):
    """
    Requisicao usando a sessao do navegador.

    page.request (e nao fetch dentro da pagina) de proposito: ele leva os
    cookies do seu login e NAO passa pelo CORS, que era o que fazia a chamada
    direta falhar com "Failed to fetch".
    """
    t0 = time.time()
    try:
        if metodo == "POST":
            r = pagina.request.post(url, headers=headers or {}, data=corpo, timeout=60000)
        else:
            r = pagina.request.get(url, headers=headers or {}, timeout=60000)
    except Exception as e:
        raise Parada(f"a requisicao falhou ({str(e)[:160]})")
    # Quanto do tempo por questao e REDE e quanto e pausa minha? Sem isso nao
    # da pra saber se baixar a pausa ainda compensa: se o ida-e-volta ja for
    # 400ms, cortar a pausa de 0,4s pela metade ganha 25%, nao 50%.
    TEMPOS.registrar(url, time.time() - t0)

    if r.status == 401:
        texto = ""
        try:
            texto = r.text()[:300]
        except Exception:
            pass
        if "simult" in texto.lower():
            raise Parada(
                "o Gran derrubou a sessao por ACESSOS SIMULTANEOS.\n"
                "     Feche o Gran nas outras janelas/aparelhos, recarregue a pagina\n"
                "     no Chrome do extrator e rode de novo (ele retoma de onde parou).",
                status=401,
            )
        raise Parada(f"sessao sem autorizacao (401). Refaca o login na janela do Chrome.\n     {texto}")
    if r.status == 429:
        espere, ra = "", None
        try:
            bruto = (r.headers or {}).get("retry-after")
            if bruto and str(bruto).strip().isdigit():
                ra = int(str(bruto).strip())
                espere = f"\n     O servidor pede pra esperar {ra}s."
        except Exception:
            pass
        e = Parada(
            "o Gran respondeu 429 (requisicoes demais)." + espere +
            "\n     Espere uns minutos e rode de novo - ele retoma de onde parou.",
            status=429,
        )
        # o ritmo adaptativo usa isso pra esperar o que o servidor mandou,
        # em vez de chutar. Sem o atributo, ele cai num fallback de 10 min.
        e.retry_after = ra
        raise e
    if r.status >= 400:
        raise Parada(f"o Gran respondeu {r.status} em {url[:90]}", status=r.status)

    if binario:
        return r.body()
    # .md vem SEM charset declarado e seria lido como cp1252 ("VocÃª").
    # Pego os bytes e decodifico como UTF-8 na mao.
    return r.body().decode("utf-8", errors="replace")


def buscar_json(pagina, url, headers=None, metodo="GET", corpo=None):
    txt = buscar(pagina, url, headers, metodo, corpo)
    try:
        return json.loads(txt)
    except Exception as e:
        inicio = re.sub(r"\s+", " ", (txt or ""))[:200]
        raise Parada(
            f"resposta nao era JSON em:\n     {url[:110]}\n"
            f"     ({e})\n     voltou isto: {inicio!r}"
        )


def salvar_json(caminho, dados):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    return caminho


# Em 23/09 a penalidade durou HORAS, nao minutos: 20 min depois do ultimo
# 429 a primeira chamada ainda era recusada. E cada tentativa dentro da
# janela parece estende-la - inclusive uma sondagem. Entao o numero aqui
# e deliberadamente generoso: esperar demais custa tempo ocioso, sondar
# cedo demais custa a janela inteira.
ESPERA_APOS_429_MIN = 180


def registrar_429(progresso):
    progresso["ultimo_429"] = datetime.now().isoformat(timespec="seconds")
    progresso["n_429"] = int(progresso.get("n_429") or 0) + 1
    gravar_progresso(progresso)


def checar_castigo(progresso, forcar=False):
    """
    Recusa começar se o ultimo 429 foi ha pouco.

    A penalidade do Gran ACUMULA: no dia 23/09 ela comecou custando minutos e
    terminou derrubando a primeira chamada de qualquer execucao. Cada nova
    tentativa dentro da janela parece estende-la - ou seja, insistir e o que
    mais atrasa. Como nem eu nem voce lembramos disso no calor da hora, fica
    escrito aqui.
    """
    quando = progresso.get("ultimo_429")
    if not quando:
        return
    try:
        t = datetime.fromisoformat(quando)
    except Exception:
        return
    faltam = ESPERA_APOS_429_MIN * 60 - (datetime.now() - t).total_seconds()
    if faltam <= 0:
        return
    print("\n" + "!" * 70)
    print(f"  O Gran deu 429 ha {int((datetime.now()-t).total_seconds()/60)} min "
          f"(foram {progresso.get('n_429')} hoje).")
    print(f"  A penalidade acumula: tentar agora provavelmente a estende.")
    print(f"  Espere mais ~{int(faltam/60)} min. Nada se perde - o progresso esta gravado.")
    if forcar:
        print("  (--forcar: seguindo assim mesmo, por sua conta)")
        print("!" * 70)
        return
    print("\n  Se tiver certeza de que quer tentar agora: acrescente --forcar")
    print("!" * 70)
    raise SystemExit(3)


def ler_progresso():
    if ARQ_PROGRESSO.exists():
        try:
            return json.loads(ARQ_PROGRESSO.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"aulas": {}, "questoes": {}}


def gravar_progresso(p):
    ARQ_PROGRESSO.parent.mkdir(parents=True, exist_ok=True)
    ARQ_PROGRESSO.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


SEMPRE_SIM = False      # ligado por --sim, pra rodar sem ninguem na frente


def confirmar(pergunta, padrao_sim=False):
    if SEMPRE_SIM:
        print(f"{pergunta} -> sim (--sim)")
        return True
    sufixo = "[S/n]" if padrao_sim else "[s/N]"
    try:
        r = input(f"{pergunta} {sufixo} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not r:
        return padrao_sim
    return r in ("s", "sim", "y", "yes")


def cod(ident):
    """
    Os ids do Gran sao base64 e vem com '=', '+' e '/'.

    Na URL eles aparecem codificados  ("jMysMmHHepM%3D"), mas a API devolve e
    espera eles CRUS ("jMysMmHHepM=") dentro do JSON. Misturar os dois faz a
    comparacao falhar sem erro nenhum - a aula simplesmente "nao existe".
    Regra: guardo sempre o CRU e chamo cod() quando monto endereco.
    """
    return quote(ident or "", safe="")


def limpar_html(t):
    """
    Tira as marcacoes e resolve as entidades HTML.

    Uso html.unescape e NAO uma lista de substituicoes: os comentarios dos
    professores vem cheios de entidades nomeadas (&ccedil; &atilde; &eacute;),
    e tratar so &amp;/&lt;/&gt; deixava "espa&ccedil;o" no texto final.
    """
    if not t:
        return t
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    # Quebro tanto no FECHA quanto no ABRE. Os comentarios vem com HTML
    # malformado de verdade - ex: "<p><strong>GABARITO: D</strong><p>Alternativa"
    # abre um segundo <p> sem fechar o primeiro. So olhando o </p> saia
    # "GABARITO: DAlternativa A", tudo grudado.
    t = re.sub(r"</?(p|div|li|tr|h[1-6]|section|blockquote)\b[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)                      # &ccedil; -> ç, &nbsp; -> espaco
    t = t.replace("\xa0", " ")                # o nbsp vira espaco normal
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ---------------------------------------------------------------------------
# Arvore do curso (disciplinas -> topicos -> aulas), com cache
# ---------------------------------------------------------------------------

class Curso:
    """Guarda o que ja foi buscado pra nao repetir requisicao."""

    def __init__(self, pagina, curso_id):
        self.pg = pagina
        self.id = curso_id
        self._curso = None
        self._disciplinas = None
        self._topicos = {}
        self._aulas = {}

    def curso(self):
        if self._curso is None:
            pausa(self.pg, "buscando o curso")
            self._curso = buscar_json(self.pg, f"{HOST_ALUNO}/aluno/sala-de-aula/curso/co/{cod(self.id)}")
        return self._curso

    def disciplinas(self):
        if self._disciplinas is None:
            pausa(self.pg, "buscando as disciplinas")
            self._disciplinas = buscar_json(
                self.pg,
                f"{HOST_ALUNO}/aluno/curso/listar-conteudo-aula/codigo/{cod(self.id)}/tipo/video")
        return self._disciplinas

    def topicos(self, disc_id):
        if disc_id not in self._topicos:
            pausa(self.pg, f"buscando os modulos da disciplina {disc_id}")
            self._topicos[disc_id] = buscar_json(
                self.pg,
                f"{HOST_ALUNO}/aluno/curso/listar-conteudo-aula/codigo/{cod(self.id)}"
                f"/tipo/video/disciplina/{disc_id}")
        return self._topicos[disc_id]

    def aulas(self, disc_id, topico_id):
        chave = (disc_id, topico_id)
        if chave not in self._aulas:
            pausa(self.pg, f"buscando as aulas do modulo {topico_id}")
            self._aulas[chave] = buscar_json(
                self.pg,
                f"{HOST_ALUNO}/aluno/curso/listar-conteudo-aula/codigo/{cod(self.id)}"
                f"/tipo/video/disciplina/{disc_id}/conteudo/{topico_id}")
        return self._aulas[chave]


def escolher_disciplinas(curso, filtro):
    """
    Filtra por id ou por nome.

    O nome EXATO ganha do parcial. Sem isso, pedir "Direito Penal" tambem
    trazia "Direito Penal Militar" (o nome esta contido no outro) - e a
    varredura coletava uma disciplina a mais sem avisar.
    Pra pegar as duas de proposito, use --disciplina "Penal".
    """
    todas = curso.disciplinas()
    if not filtro:
        return todas
    f = filtro.strip().lower()

    por_id = [d for d in todas if f == str(d.get("id"))]
    if por_id:
        return por_id
    exatas = [d for d in todas if f == (d.get("nome") or "").strip().lower()]
    if exatas:
        return exatas
    parciais = [d for d in todas if f in (d.get("nome") or "").lower()]
    if not parciais:
        nomes = ", ".join((d.get("nome") or "?") for d in todas)
        raise Parada(f"nao achei a disciplina '{filtro}'.\n     Disponiveis: {nomes}")
    if len(parciais) > 1:
        print(f"  [!] '{filtro}' casou com {len(parciais)} disciplinas: "
              f"{', '.join((d.get('nome') or '?') for d in parciais)}")
    return parciais


# ---------------------------------------------------------------------------
# MODO AULA
# ---------------------------------------------------------------------------

_ARTEFATO_400_SEGUIDOS = 0      # 3 seguidos = parar (nao e mais uma aula so)
AULAS_400 = []                  # aulas salvas sem artefatos nesta execucao

def canario_artefatos(curso, progresso):
    """
    Teste de sessao com UMA aula que ja funcionou: pede a lista de artefatos
    dela de novo. Se voltar RETURNED_EXISTING, a sessao esta boa e os 400
    eram das aulas; se der erro, o problema e geral.
    Custa 1 requisicao (2 se ainda nao houver canario guardado).
    """
    pg = curso.pg
    corpo = progresso.get("canario_artefatos")
    if not corpo:
        # ainda nao guardei nenhum: monto a partir de uma aula ja coletada
        # que tem transcricao (ou seja, cujos artefatos existem)
        for reg in progresso.get("aulas", {}).values():
            arq = RAIZ / reg.get("arquivo", "")
            if not arq.exists() or reg.get("artefatos_400"):
                continue
            try:
                d = json.loads(arq.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not d.get("transcricao") or not d.get("video_id"):
                continue
            pausa(pg, "canario: buscando uma aula que ja funcionou")
            try:
                video = buscar_json(pg, f"{HOST_ALUNO}/aluno/sala-de-aula/video/co/{cod(curso.id)}/a/{cod(d['video_id'])}")
            except Parada:
                return False
            leg = ((video.get("player") or {}).get("caption") or {}).get("src") or ""
            corpo = {"type": "VIDEO", "disciplinaNome": d.get("disciplina"), "aulaNome": d.get("titulo"),
                     "cursoId": curso.id, "aulaId": d["video_id"], "disciplinaId": str(d.get("disciplina_id")),
                     "arquivos": {"legenda": leg}, "artifactTypes": list(ARTEFATOS)}
            break
    if not corpo:
        return False
    pausa(pg, "canario: testando a sessao com uma aula que ja funcionou")
    try:
        r = buscar_json(pg, f"{HOST_ALUNO}/aluno/sala-de-aula/artefatos",
                        headers={"Content-Type": "application/json"},
                        metodo="POST", corpo=json.dumps(corpo))
    except Parada:
        return False
    acao = (r or {}).get("action")
    if acao and acao != "RETURNED_EXISTING":
        raise Parada(f"o canario respondeu action='{acao}' (geracao por IA?). Parei de proposito.")
    progresso["canario_artefatos"] = corpo
    gravar_progresso(progresso)
    return True


def extrair_aula(curso, disc, topico, detalhe, progresso, forcar=False):
    """
    Extrai UMA aula. Tudo vem de API - nao precisa navegar ate a pagina dela.
    'detalhe' e o registro da aula na listagem do modulo.
    """
    pg = curso.pg
    aula_id = detalhe.get("id_video")
    codigo_aula = str(detalhe.get("codigo") or "")
    titulo = (detalhe.get("st_titulo_novo") or "").strip()
    disciplina = disc.get("nome") or "Sem disciplina"

    ja = progresso["aulas"].get(codigo_aula)
    if ja and not forcar:
        arq_ant = RAIZ / ja.get("arquivo", "")
        if arq_ant.exists():
            print(f"    [ja tenho] {titulo[:55]}")
            return None

    print(f"    -> {titulo[:60]}")
    pausa(pg, "buscando a aula")
    video = buscar_json(pg, f"{HOST_ALUNO}/aluno/sala-de-aula/video/co/{cod(curso.id)}/a/{cod(aula_id)}")

    # o titulo vem "7 - Dolo - Crime Doloso II": o numero e a posicao no modulo
    mt = re.match(r"^\s*(\d+)\s*-\s*(.+)$", video.get("titulo") or "")
    ordem = int(mt.group(1)) if mt else 0
    if mt:
        titulo = mt.group(2).strip()
    legenda = ((video.get("player") or {}).get("caption") or {}).get("src") or ""

    # --- artefatos ---
    pausa(pg, "pedindo a lista de artefatos")
    corpo = {
        "type": "VIDEO",
        "disciplinaNome": disciplina,
        "aulaNome": titulo,
        "cursoId": curso.id,
        "aulaId": aula_id,
        "disciplinaId": str(disc.get("id")),
        "arquivos": {"legenda": legenda},
        "artifactTypes": list(ARTEFATOS),
    }
    # 400 aqui e problema DAQUELA aula (pedido que o Gran nao aceita - ex.:
    # video sem legenda), nao bloqueio nem limite de taxa. Parar a varredura
    # inteira por isso faria a retomada bater na mesma aula e parar de novo.
    # Entao: anoto, salvo a aula sem os artefatos e sigo. Se vierem 3 aulas
    # seguidas com 400, ai ja nao e uma aula so - paro pra voce me mostrar.
    global _ARTEFATO_400_SEGUIDOS
    artefatos_400 = False
    try:
        resp = buscar_json(pg, f"{HOST_ALUNO}/aluno/sala-de-aula/artefatos",
                           headers={"Content-Type": "application/json"},
                           metodo="POST", corpo=json.dumps(corpo))
        _ARTEFATO_400_SEGUIDOS = 0
        # guarda o pedido desta aula como "canario": uma aula que SABIDAMENTE
        # funciona, pra testar a sessao se vierem varios 400 seguidos
        if (resp or {}).get("action") in (None, "RETURNED_EXISTING"):
            progresso["canario_artefatos"] = corpo
    except Parada as e:
        if e.status != 400:
            raise
        # Sem legenda no video, o Gran nao tem de onde tirar transcricao e
        # resumo - o 400 e esperado e nao conta como sinal de problema.
        # Com legenda, conta; no 3o seguido, testo uma aula que funciona.
        if legenda:
            _ARTEFATO_400_SEGUIDOS += 1
        if _ARTEFATO_400_SEGUIDOS >= 3:
            if canario_artefatos(curso, progresso):
                print("       (3 aulas seguidas com 400, mas uma aula que ja funcionava continua"
                      " funcionando: o problema e destas aulas, nao da sessao - sigo)")
                _ARTEFATO_400_SEGUIDOS = 0
            else:
                raise Parada(
                    "o Gran respondeu 400 em 3 aulas seguidas E numa aula que ja funcionava.\n"
                    "     Agora parece ser a sessao ou o Gran, nao as aulas. Parei - me mande esta saida.",
                    status=400)
        print(f"       (o Gran respondeu 400 nos artefatos desta aula - "
              f"{'sem legenda no video; ' if not legenda else ''}salvo sem transcricao/resumo/fixacao e sigo)")
        AULAS_400.append(f"{disciplina} / {titulo}" + ("" if legenda else " (sem legenda)"))
        artefatos_400 = True
        resp = {}

    acao = resp.get("action")
    # "RETURNED_EXISTING" = ja existiam. Outra coisa sugere que a chamada
    # MANDOU o Gran gerar artefato por IA - custa dinheiro pra eles e,
    # repetido em centenas de aulas, e um padrao feio. Paro e te pergunto.
    if acao and acao != "RETURNED_EXISTING":
        raise Parada(
            f"o Gran respondeu action='{acao}' em vez de 'RETURNED_EXISTING'.\n"
            "     Isso indica que a chamada disparou GERACAO de artefato por IA,\n"
            "     e nao apenas leitura. Parei de proposito - me diga se quer seguir."
        )

    artefatos = resp.get("artefatos") or {}
    conteudos = {}
    for tipo, campo in ARTEFATOS.items():
        a = artefatos.get(tipo)
        if not a or not a.get("url") or a.get("status") != "completed":
            conteudos[campo] = None
            continue
        pausa(pg, f"baixando {campo}")
        bruto = buscar(pg, a["url"])
        if a["url"].split("?")[0].endswith(".json"):
            try:
                j = json.loads(bruto)
            except Exception:
                conteudos[campo] = None
                continue
            if tipo == "transcript":
                tons = j.get("tones") or {}
                tom = (j.get("metadata") or {}).get("defaultTone") or next(iter(tons), None)
                conteudos[campo] = (tons.get(tom) or {}).get("content")
                conteudos["transcricao_meta"] = j.get("metadata")
            elif tipo == "flashcards":
                conteudos[campo] = j.get("decks")
            elif tipo == "quiz":
                conteudos[campo] = j.get("questions")
            else:
                conteudos[campo] = j
        else:
            conteudos[campo] = bruto      # .md ja decodificado como UTF-8

    faltando = [c for c in ARTEFATOS.values() if not conteudos.get(c)]
    if faltando:
        print(f"       (sem: {', '.join(faltando)})")

    # --- monta o JSON ---
    pasta = DIR_SAIDA / "aulas" / nome_de_arquivo(disciplina, 60)
    # O codigo da aula entra no nome (spec v3 §3.1). Sem ele, duas aulas com o
    # mesmo numero e o mesmo titulo em modulos diferentes da MESMA disciplina
    # ("1 - Exercicios" no modulo 2 e no modulo 5) caiam no mesmo arquivo: a
    # segunda sobrescrevia a primeira e, na retomada, o "[ja tenho]" pulava as
    # duas - perda calada. As aulas antigas continuam achadas pelo progresso.
    base = f"{ordem:02d}_{codigo_aula or 'semcodigo'}_{nome_de_arquivo(titulo, 80)}"
    dados = {
        "curso": curso.curso().get("nome"),
        "curso_id": curso.id,
        "disciplina": disciplina,
        "disciplina_id": str(disc.get("id")),
        "materia": detalhe.get("Materia"),
        "modulo": {"id": topico.get("id"), "titulo": (topico.get("desc") or "").strip()},
        "ordem": ordem,
        "ordem_no_curso": detalhe.get("nr_ordem"),
        "titulo": titulo,
        "codigo_aula": codigo_aula,
        "video_id": aula_id,
        "professor": detalhe.get("professor") or detalhe.get("Nome_Grade"),
        "duracao": detalhe.get("st_tempo_duracao"),
        "url_aula": f"{HOST_ALUNO}/aluno/curso/video/codigo/{cod(curso.id)}/v/{cod(aula_id)}",
        "caderno_questoes": detalhe.get("caderno_questoes"),
        "transcricao": conteudos.get("transcricao"),
        "transcricao_meta": conteudos.get("transcricao_meta"),
        "resumo": conteudos.get("resumo"),
        "resumo_bolso": conteudos.get("resumo_bolso"),
        "mapa_mental": conteudos.get("mapa_mental"),
        "flashcards": conteudos.get("flashcards"),
        "questoes_fixacao": conteudos.get("questoes_fixacao"),
        "pdf_degravacao": None,
        "tinha_legenda": bool(legenda),
        "artefatos_400": artefatos_400,
        "coletado_em": datetime.now().isoformat(timespec="seconds"),
    }

    # --- PDF da degravacao ---
    fk = detalhe.get("fk_material_resumo")
    if fk:
        pausa(pg, "baixando o PDF da degravacao")
        try:
            conteudo = buscar(
                pg, f"{HOST_ALUNO}/aluno/espaco/download-resumo/codigo/{cod(curso.id)}/c/{cod(fk)}",
                binario=True)
            if conteudo[:4] == b"%PDF":
                pasta.mkdir(parents=True, exist_ok=True)
                arq_pdf = pasta / f"{base}.pdf"
                arq_pdf.write_bytes(conteudo)
                dados["pdf_degravacao"] = arq_pdf.name
            else:
                print("       (o download do PDF nao voltou um PDF)")
        except Parada as e:
            print(f"       (PDF: {e})")

    arq = salvar_json(pasta / f"{base}.json", dados)
    progresso["aulas"][codigo_aula] = {
        "quando": dados["coletado_em"],
        "arquivo": str(arq.relative_to(RAIZ)),
        "titulo": titulo, "disciplina": disciplina,
        **({"artefatos_400": True} if artefatos_400 else {}),
    }
    gravar_progresso(progresso)
    return dados


def achar_aula_da_aba(curso, pagina):
    """Localiza, na arvore, a aula que esta aberta na aba."""
    m = re.search(r"/codigo/([^/]+)/v/([^/?#]+)", pagina.url)
    if not m:
        raise Parada(
            "esta aba nao e uma aula.\n"
            "     Abra a aula (a pagina do video) no Chrome e rode de novo.\n"
            f"     Aba atual: {pagina.url[:100]}"
        )
    # a URL traz "jMysMmHHepM%3D"; a API responde "jMysMmHHepM=".
    # Guardo sempre o CRU - senao a comparacao com id_video nunca bate.
    aula_id = unquote(m.group(2))
    pausa(pagina, "descobrindo a que modulo esta aula pertence")
    video = buscar_json(pagina, f"{HOST_ALUNO}/aluno/sala-de-aula/video/co/{cod(curso.id)}/a/{cod(aula_id)}")
    # CUIDADO: o endpoint tem DOIS campos parecidos.
    #   video["disciplina_id"]             = 574  -> e o id da MATERIA
    #                                               ("Direito Penal - Parte Geral")
    #   video["conteudo"]["disciplina_id"] = 3392 -> e o que as URLs usam
    # Usar o de cima faz o /listar-conteudo-aula devolver HTML de erro.
    conteudo = video.get("conteudo") or {}
    disc_id = str(conteudo.get("disciplina_id") or video.get("disciplina_id") or "")
    top_id = conteudo.get("conteudo_id")
    disc = next((d for d in curso.disciplinas() if str(d.get("id")) == disc_id), {})
    topico = next((t for t in curso.topicos(disc_id) if t.get("id") == top_id), {})
    detalhe = next((a for a in curso.aulas(disc_id, top_id) if a.get("id_video") == aula_id), None)
    if not detalhe:
        raise Parada("nao encontrei esta aula na listagem do modulo")
    return disc, topico, detalhe


def varrer_curso(pagina, curso_id, progresso, filtro_disc, forcar):
    curso = Curso(pagina, curso_id)
    print(f"  Curso: {curso.curso().get('nome')}")
    discs = escolher_disciplinas(curso, filtro_disc)

    # --- levanta o plano antes de baixar nada ---
    plano = []
    for d in discs:
        for t in curso.topicos(str(d.get("id"))):
            for a in curso.aulas(str(d.get("id")), t.get("id")):
                plano.append((d, t, a))
    ja_tenho = sum(1 for _, _, a in plano
                   if str(a.get("codigo")) in progresso["aulas"] and not forcar)
    faltam = len(plano) - ja_tenho

    print(f"\n  Disciplinas: {', '.join((d.get('nome') or '?') for d in discs)}")
    print(f"  Aulas no plano: {len(plano)}   ja coletadas: {ja_tenho}   a fazer: {faltam}")
    # ~9 requisicoes por aula (video + artefatos + 6 arquivos + pdf); cada uma
    # custa a pausa + ~0,5s de rede. Com --pausa-aulas, conta pelo piso.
    por_req = ((PAUSA_AULAS_PISO * 1.12) if PAUSA_AULAS_PISO is not None else 2.5) + 0.5
    seg = faltam * 9 * por_req
    print(f"  Estimativa: ~{faltam*9} requisicoes, ~{seg/3600:.1f} horas")
    print("\n  Lembre: nao use o Gran em outra janela/aparelho enquanto isto roda.")
    if faltam and not confirmar("\n  Comecar a varredura?"):
        print("  Ok, nao comecei.")
        return 0

    feitas = 0
    andamento = Andamento(len(plano), ja_feitos=ja_tenho, unidade="aulas")
    for i, (d, t, a) in enumerate(plano, 1):
        print(f"\n  [{i}/{len(plano)}] {d.get('nome')} / modulo {t.get('id')}")
        checar_bloqueio(pagina)
        if extrair_aula(curso, d, t, a, progresso, forcar):
            feitas += 1
            andamento.marcar()
            print(andamento.linha())
    if AULAS_400:
        print(f"\n  [i] {len(AULAS_400)} aula(s) salvas SEM artefatos (o Gran respondeu 400):")
        for nome in AULAS_400:
            print(f"      - {nome}")
    return feitas


# ---------------------------------------------------------------------------
# MODO QUESTOES
# ---------------------------------------------------------------------------

def desembrulhar_token(bruto):
    """
    O valor guardado pode vir de tres jeitos:
      eyJhbGc...        -> o token cru
      "eyJhbGc..."      -> texto JSON, COM aspas (json.loads devolve str)
      {"token":"eyJ..."} -> objeto JSON
    Deixar as aspas faz virar  Authorization: Bearer "eyJ..."  e o Gran recusa
    com 401 "Nao Autorizado" - exatamente o erro que apareceu.
    """
    t = (bruto or "").strip()
    if t[:1] in ("{", "["):
        try:
            j = json.loads(t)
            if isinstance(j, dict):
                for k in ("token", "access_token", "accessToken", "value", "jwt"):
                    if j.get(k):
                        return str(j[k]).strip().strip('"')
        except Exception:
            pass
    elif t[:1] == '"':
        try:
            j = json.loads(t)
            if isinstance(j, str):
                return j.strip()
        except Exception:
            pass
        return t.strip('"')
    return t


def formato_do_token(t):
    """Descreve o token SEM mostrar o valor (pra diagnostico)."""
    if not t:
        return "vazio"
    partes = t.count(".") + 1
    return (f"{len(t)} chars, comeca com {t[:4]!r}, "
            f"{'parece JWT' if t.startswith('eyJ') and partes == 3 else f'{partes} parte(s)'}")


def cabecalhos_do_navegador(pagina):
    """
    Origin/Referer/User-Agent iguais aos que o app do Gran manda.

    Por que isso importa: page.request e um cliente HTTP separado do
    navegador - leva os cookies, mas nao os cabecalhos de contexto. Uma
    chamada autenticada SEM Origin nem Referer tem cara de robo e parece cair
    numa faixa de limite bem mais apertada: o inspetor (que so observa o app
    chamando) recebia 200 enquanto o extrator levava 429 no primeiro pedido.
    """
    try:
        ua = pagina.evaluate("() => navigator.userAgent")
    except Exception:
        ua = ""
    origem = "https://questoes.grancursosonline.com.br"
    cab = {"Origin": origem, "Referer": origem + "/"}
    if ua:
        cab["User-Agent"] = ua
    return cab


def cabecalhos_api_questoes(pagina, com_bearer=True):
    """A API de questoes exige o token do app + o X-Client-Id."""
    bruto = pagina.evaluate("() => localStorage.getItem('auth/token') || ''")
    if not bruto:
        raise Parada(
            "nao achei o token da sua sessao de questoes em localStorage['auth/token'].\n"
            "     Abra o Gran Questoes na janela do Chrome (e faca login) e rode de novo."
        )
    token = desembrulhar_token(bruto)
    cab = dict(cabecalhos_do_navegador(pagina))
    cab.update({
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}" if com_bearer and not token.startswith("Bearer") else token,
        "X-Client-Id": CLIENT_ID,
    })
    return cab


JS_BUSCAR = r"""
async ({ url, token, clientId }) => {
  // Roda DENTRO da pagina: quem faz a requisicao e o proprio Chrome que voce
  // deixou aberto e logado, com a conexao, os cookies e os cabecalhos dele.
  // Nao e imitacao de navegador - e o navegador.
  //
  // Por que importa: o page.request do Playwright e um cliente HTTP separado.
  // O Gran passou a recusar ele com 429 enquanto a MESMA API respondia
  // normalmente quando a pagina pedia (o usuario clicou na pagina 2 e
  // funcionou). Eram dois clientes distintos aos olhos do servidor.
  try {
    const r = await fetch(url, {
      credentials: "include",
      headers: {
        "Accept": "application/json, text/plain, */*",
        "Authorization": token,
        "X-Client-Id": clientId,
      },
    });
    return { status: r.status, corpo: await r.text() };
  } catch (e) {
    // erro de CORS cai aqui sem status; resposta de erro do servidor
    // costuma vir sem cabecalho de CORS e tambem cai neste ponto
    return { status: 0, erro: String((e && e.message) || e) };
  }
}
"""


def buscar_json_na_pagina(pagina, url, cab):
    """Faz a chamada pelo JavaScript da propria pagina (ver JS_BUSCAR)."""
    t0 = time.time()
    try:
        r = pagina.evaluate(JS_BUSCAR, {
            "url": url,
            "token": cab.get("Authorization", ""),
            "clientId": cab.get("X-Client-Id", CLIENT_ID),
        })
    except Exception as e:
        raise Parada(f"nao consegui rodar a busca na pagina ({str(e)[:140]})")
    TEMPOS.registrar(url, time.time() - t0)

    status = r.get("status")
    if status == 429:
        e = Parada("o Gran respondeu 429 (requisicoes demais).", status=429)
        e.retry_after = None
        raise e
    if status == 401:
        raise Parada("sessao sem autorizacao (401).", status=401)
    if status == 0:
        # "Failed to fetch" e ambiguo de dentro do navegador: quando a API
        # responde erro, ela nao manda os cabecalhos de CORS junto, e o
        # navegador esconde o status. Um 429 fica indistinguivel de falha de
        # CORS. Trato como 429 - e a hipotese mais provavel aqui e a mais
        # conservadora, porque aciona o recuo em vez de insistir.
        e = Parada(
            'a chamada pela pagina falhou ("Failed to fetch").\n'
            "     Quase sempre isso e o 429 escondido: a resposta de erro vem\n"
            "     sem cabecalho de CORS e o navegador nao deixa ler o status.\n"
            "     Trato como limite de taxa - espere e tente mais tarde.",
            status=429)
        e.retry_after = None
        raise e
    if status >= 400:
        raise Parada(f"o Gran respondeu {status}", status=status)
    try:
        return json.loads(r.get("corpo") or "")
    except Exception as e:
        raise Parada(f"resposta nao era JSON ({e}): {(r.get('corpo') or '')[:160]}")


SEM_BEARER = False      # descoberto na marra, so se um 401 acontecer
VIA_PAGINA = True       # usar o fetch do navegador em vez do cliente proprio


def pedir_api_q(pagina, url, cab):
    """Toda chamada a API de questoes passa por aqui, pelo caminho escolhido."""
    if VIA_PAGINA:
        return buscar_json_na_pagina(pagina, url, cab)
    return buscar_json(pagina, url, headers=cab)


def buscar_json_q(pagina, url):
    """
    Chamada a API de questoes, resolvendo o formato do token PREGUICOSAMENTE.

    A versao anterior gastava 1 a 2 requisicoes so pra "conferir o token"
    antes de qualquer trabalho util - e foi justamente isso que estourou o
    limite de taxa. Pior: tratava 429 como token recusado e mandava o usuario
    refazer o login a toa.
    Agora: tenta do jeito normal; SO se vier 401 e que testa o token cru.
    Qualquer outro erro (429, 500...) sobe na hora, com o motivo certo.
    """
    global SEM_BEARER
    cab = cabecalhos_api_questoes(pagina, com_bearer=not SEM_BEARER)
    pedir = lambda u, c: pedir_api_q(pagina, u, c)
    try:
        return pedir(url, cab)
    except Parada as e:
        if e.status != 401:
            raise                       # 429 e outros nao sao problema de token
        # 401: pode ser o formato do token. Tenta a outra forma, UMA vez.
        pausa(pagina, "401 - testando o token no outro formato")
        try:
            r = pedir(url, cabecalhos_api_questoes(pagina, com_bearer=SEM_BEARER))
            SEM_BEARER = not SEM_BEARER
            print(f"      (o Gran quis o token {'SEM' if SEM_BEARER else 'COM'} o prefixo 'Bearer')")
            return r
        except Parada as e2:
            if e2.status != 401:
                raise
        bruto = pagina.evaluate("() => localStorage.getItem('auth/token') || ''")
        chaves = pagina.evaluate(
            "() => Object.keys(localStorage).filter(k => /token|auth|key/i.test(k))")
        raise Parada(
            "o Gran recusou o token das questoes nas duas formas (401).\n"
            f"     formato do token: {formato_do_token(desembrulhar_token(bruto))}\n"
            f"     chaves no navegador: {chaves}\n"
            "     Abra o Gran Questoes na janela do Chrome, recarregue (F5) e rode de novo.",
            status=401)


# A ordenacao que o app manda em TODA listagem. Nao e enfeite: sem 'sort', o
# Elasticsearch monta outra busca, e um limitador que pesa custo de consulta
# trata isso diferente. Foi a unica diferenca real entre a chamada do app
# (200, oito vezes seguidas) e a minha (429) - mesmos cabecalhos, mesmo token,
# mesmo navegador. Copiado da requisicao capturada, nao inventado.
SORT_PADRAO = '[{"anos":"desc"},{"_score":"desc"}]'


def montar_url_questoes(assuntos, niveis, pagina_num, por_pagina, orgaos=None):
    from urllib.parse import quote as _q
    partes = [f"perPage={por_pagina}", f"page={pagina_num}",
              "marcarResolvidas=1",          # o app sempre manda
              f"sort={_q(SORT_PADRAO, safe='[]{},:')}"]
    partes += [f"assunto[]={a}" for a in (assuntos or [])]
    partes += [f"nivel[]={_q(str(n))}" for n in (niveis or [])]
    # Na tela o filtro de instituicao sai como "orgao=544"; a API usa o mesmo
    # padrao de array dos outros ("assunto[]", "nivel[]").
    partes += [f"orgao[]={o}" for o in (orgaos or [])]
    partes += ["resolucao=TODAS", "anulada=0", "desatualizada=0"]
    return f"{HOST_API_Q}/v1/elastic/questao?" + "&".join(partes)


def conferir_filtro_orgao(linhas, orgaos):
    """
    Confere se o filtro de orgao PEGOU mesmo.

    'orgao[]' e deducao minha a partir do que a tela escreve. Se o nome do
    parametro estiver errado, a API ignora e devolve o acervo INTEIRO - e eu
    sairia varrendo centenas de milhares de questoes achando que sao da PM-BA.
    Entao olho as questoes que voltaram: se quase nenhuma e do orgao pedido,
    o filtro nao pegou.
    """
    if not linhas:
        return True, ""
    pedidos = {str(o) for o in orgaos}
    def ids_da_questao(q):
        return {str((o or {}).get("id")) for o in (q.get("orgaos") or [])}
    batem = sum(1 for q in linhas if ids_da_questao(q) & pedidos)
    fracao = batem / len(linhas)
    if fracao >= 0.9:
        return True, f"{batem}/{len(linhas)} da primeira pagina sao do orgao pedido"
    exemplos = [((q.get("orgaos") or [{}])[0].get("sigla")
                 or (q.get("orgaos") or [{}])[0].get("nome")) for q in linhas[:5]]
    return False, (f"so {batem}/{len(linhas)} sao do orgao {orgaos}. "
                   f"Vieram de: {exemplos}")


def assuntos_do_caderno(url_caderno):
    """Tira os ids de assunto da URL 'caderno_questoes' que a API do curso da."""
    if not url_caderno:
        return []
    q = parse_qs(urlparse(url_caderno).query)
    ids = []
    for chave in ("assunto", "a"):
        for v in q.get(chave, []):
            ids.extend([x.strip() for x in str(v).split(",") if x.strip().isdigit()])
    return sorted(set(ids), key=int)


def letra_declarada_no_comentario(texto):
    """
    Acha a letra que o professor declara no comentario.

    Os professores escrevem de dois jeitos:
        "Letra b. Assunto abordado: ..."
        "GABARITO: D"
    Serve pra CONFERIR o campo 'resposta' da API por um caminho independente.
    """
    if not texto:
        return None
    m = re.search(r"^\s*(?:GABARITO\s*:?\s*|Letra\s+)([A-Ea-e])\b", texto.strip())
    if m:
        return m.group(1).upper()
    m = re.search(r"\bGABARITO\s*:?\s*([A-Ea-e])\b", texto[:200])
    return m.group(1).upper() if m else None


def montar_questao(q):
    """
    Monta a questao a partir da linha da listagem. SEM TOCAR NA REDE.

    Separado de propor das duas chamadas por questao (estatistica e
    comentario) porque a listagem ja vem inteira - enunciado, alternativas e
    ate o gabarito. Assim da pra coletar uma disciplina inteira sem gastar
    cota nenhuma e deixar as leves pra depois.

    O gabarito vem de 'resposta' = id do item correto.
    """
    itens = q.get("itens") or []
    resposta_id = q.get("resposta")
    alternativas, gabarito = [], None
    for it in itens:
        letra = (it.get("rotulo") or "").strip()
        correta = (it.get("id") == resposta_id)
        if correta:
            gabarito = letra
        alternativas.append({
            "letra": letra,
            "texto": it.get("corpo_clean") or limpar_html(it.get("corpo")),
            "id": it.get("id"),
            "correta": correta,
        })

    qid = q.get("id")
    dados = {
        "id": qid,
        "codigo": f"Q{qid}",
        "enunciado": q.get("enunciado_clean") or limpar_html(q.get("enunciado")),
        "alternativas": alternativas,
        "gabarito": gabarito,
        # Guardo o ID de banca/orgao/cargo, nao so o nome. Ja me mordeu com o
        # assunto: o nome serve pra ler, o id serve pra juntar. A tabela
        # prova_real da spec (§3.7) amarra por orgao+cargo+ano+banca, e o
        # filtro de orgao da API (passada PM-BA/CBM-BA) tambem e por id.
        "banca": (q.get("bancas") or [{}])[0].get("descricao"),
        "banca_id": (q.get("bancas") or [{}])[0].get("id"),
        "banca_sigla": (q.get("bancas") or [{}])[0].get("sigla"),
        "ano": (q.get("anos") or [None])[0],
        "orgao": (q.get("orgaos") or [{}])[0].get("nome"),
        "orgao_sigla": (q.get("orgaos") or [{}])[0].get("sigla"),
        "orgao_id": (q.get("orgaos") or [{}])[0].get("id"),
        "orgao_uf": (q.get("orgaos") or [{}])[0].get("uf"),
        "orgao_esfera": (q.get("orgaos") or [{}])[0].get("esfera"),
        "prova": (q.get("provas") or [{}])[0].get("nome"),
        "prova_ano": (q.get("provas") or [{}])[0].get("ano"),
        "cargo": (q.get("cargos") or [{}])[0].get("descricao"),
        "cargo_id": (q.get("cargos") or [{}])[0].get("id"),
        # Guardo o ID, nao so o nome. O 'caderno_questoes' de cada aula amarra
        # por ID de assunto - e e dele que sai o vinculo questao->aula da spec
        # (3.3.1). So com o nome nao da pra fazer essa juncao.
        "assuntos": [{
            "id": a.get("id"),
            "nome": a.get("nome_clean") or a.get("nome") or a.get("descricao"),
            "raiz": a.get("assunto_raiz"),
            "pai": a.get("pai"),
        } for a in (q.get("assuntos") or [])],
        "assunto_ids": [a.get("id") for a in (q.get("assuntos") or []) if a.get("id")],
        "tipo": q.get("tipo"),
        "dificuldade": q.get("dificuldade"),
        # Sinalizadores que dizem se a questao da pra usar SO COMO TEXTO.
        # O bot do Telegram (spec 3.5) manda a questao escrita: se o enunciado
        # for imagem, ou se ela pertencer a um grupo cujo texto comum fica
        # fora dela, chega quebrada. Descobri isso com 6 questoes sem
        # enunciado nenhum - e sem estes campos nao havia como identifica-las.
        "tem_imagem": q.get("hasImage"),
        "tem_imagem_nas_alternativas": q.get("hasImageItens"),
        "grupo_questao": q.get("grupoQuestao"),
        "discursiva": q.get("questaoDiscursiva"),
        "so_texto": bool(q.get("enunciado_clean") or q.get("enunciado"))
                    and not q.get("hasImage") and not q.get("hasImageItens"),
        "anulada": q.get("anulada"),
        "desatualizada": q.get("desatualizada"),
        "tem_comentario_professor": bool((q.get("comentarios") or {}).get("professor")),
        "indice_acerto": None,
        "comentario_professor": None,
    }
    return dados


def completar_leves(pagina, dados, cab, com_comentarios, ritmo=None):
    """
    As duas chamadas POR QUESTAO: estatistica e comentario do professor.
    'ritmo' controla a pausa delas.

    A estatistica devolve ~160 bytes; a listagem, ~130 KB. Tratar as duas com
    a mesma pausa de 2-3s era interpretacao minha, nao o que a spec pede
    ("pausa entre PAGINAS"). Com 50 mil questoes pela frente na Fase 6, a
    diferenca entre 2,5s e 1s por chamada e de dezenas de horas.
    """
    # Defensivo: sem cabecalho, a chamada de estatistica volta 401. Ja
    # aconteceu - a passada por orgao passava None aqui e morria na primeira
    # questao, depois da listagem ter funcionado.
    if not cab:
        cab = cabecalhos_api_questoes(pagina)

    def esperar(motivo):
        if ritmo is not None:
            ritmo.esperar(pagina, motivo)
        else:
            pausa(pagina, motivo)

    qid = dados["id"]
    # estatistica: obrigatoria (e a dificuldade REAL, que ordena tudo depois)
    esperar(f"estatistica da Q{qid}")
    est = pedir_api_q(pagina, f"{HOST_API_Q}/v1/questao/{qid}/estatisticas", cab)
    e = (est.get("data") or {}).get("estatisticas") or {}
    total = e.get("total") or 0
    dados["indice_acerto"] = {
        "acertos": e.get("acertos"), "erros": e.get("erros"),
        "brancos": e.get("brancos"), "total": total,
        "percentual": round(100.0 * (e.get("acertos") or 0) / total, 1) if total else None,
    }

    if com_comentarios and dados["tem_comentario_professor"]:
        esperar(f"comentario da Q{qid}")
        c = pedir_api_q(pagina, f"{HOST_API_Q}/v1/comentario/questao/{qid}?professor=1", cab)
        linhas = c.get("data")
        if isinstance(linhas, dict):
            linhas = linhas.get("rows") or []
        if linhas:
            p = linhas[0]
            texto = limpar_html(p.get("descricao"))
            dados["comentario_professor"] = {
                "professor": p.get("professor"),
                "texto": texto,
                "html": p.get("descricao"),
                "data": p.get("dataComentario"),
            }
            # Confere o gabarito por um caminho INDEPENDENTE: o campo
            # 'resposta' da API contra a letra que o professor escreveu.
            # Se divergirem, e sinal de que entendi a API errado - e melhor
            # descobrir na primeira questao do que depois de 6.818.
            letra = letra_declarada_no_comentario(texto)
            if letra:
                dados["gabarito_confere_com_comentario"] = (letra == dados["gabarito"])
                if letra != dados["gabarito"]:
                    print(f"      [!] Q{qid}: a API diz gabarito {dados['gabarito']}, "
                          f"mas o professor escreveu {letra}")
    dados.pop("leves_pendentes", None)
    return dados


def extrair_questao(pagina, q, cab, com_comentarios, ritmo=None):
    """A questao inteira: monta da listagem e completa com as duas leves."""
    return completar_leves(pagina, montar_questao(q), cab, com_comentarios,
                           ritmo=ritmo)


# NOTA: existe /v3/materia/arvore (o seletor de Assunto do filtro usa) que
# devolve a taxonomia com id+nome numa chamada so. Cheguei a implementar o
# cruzamento por NOME com ele pra fugir do 429, mas descartei: dos 791 nomes
# usados nas questoes a arvore cobria poucos, e ha nomes repetidos em ramos
# diferentes ("Conceito", "Principio da Subsidiariedade") - viraria id errado.
# A releitura da listagem e exata. Fica o registro caso seja util noutra coisa.


# A TELA fala um vocabulario diferente da API: onde a API quer nivel[]=Médio,
# a tela escreve nivel=2. Mapear errado aqui nao da erro - so devolve o acervo
# inteiro calado, que e o jeito mais caro de descobrir o engano.
NIVEL_NA_TELA = {"fundamental": "1", "médio": "2", "medio": "2",
                 "superior": "3"}
HOST_TELA_Q = "https://questoes.grancursosonline.com.br"
# perfil proprio do Firefox: o login sobrevive entre execucoes e nao se
# mistura com o Firefox pessoal
PERFIL_FIREFOX = Path("C:/firefox_gran")

# AS 14 BANCAS QUE O EDITAL MANDA COLETAR (decisao do dono da spec, 24/09).
# Os ids sairam das 50.885 questoes ja no disco - nao precisei perguntar ao
# Gran. Os quatro que ele ja sabia (FCC 92, IBFC 277, UNEB 1021, CONSULTEC
# 330) bateram, o que da confianca nos outros dez.
# CUIDADO: IDECAN (409) nao e IDIB (937). AOCP e Consulplan tem DUAS entradas
# cada no acervo, e as duas contam.
BANCAS_PERMITIDAS = {
    92: "FCC", 277: "IBFC", 102: "FGV", 252: "VUNESP", 409: "IDECAN",
    982: "AOCP (Instituto)", 13: "AOCP (Assessoria)", 949: "SELECON",
    930: "IBADE", 979: "Consulplan (Instituto)", 45: "Consulplan (Consultoria)",
    1021: "UNEB", 330: "CONSULTEC", 27: "CEBRASPE/CESPE",
}

# Preenchido por `descobrir_param_banca` na 1a vez e guardado no progresso.
PARAM_BANCA = None

# Os candidatos vem do padrao que a propria tela usa nos outros filtros
# (assunto=, nivel=, anos=) mais os nomes que o app mostra na interface.
CANDIDATOS_PARAM_BANCA = ("banca", "bancas", "banca_id", "instituicao",
                          "organizadora", "bancaId")


def descobrir_param_banca(pagina, etiqueta_teste, nivel_teste=None):
    """
    Descobre COMO a tela nomeia o filtro de banca, provando que pegou.

    Nao da pra chutar: com o nome errado a tela ignora o parametro CALADA e
    devolve o acervo inteiro - foi por isso que o filtro de orgao ganhou um
    conferidor. Entao eu testo: abro a etiqueta sem filtro, guardo o total,
    e abro de novo com cada candidato. O nome certo e o que DIMINUI o total.
    Um nome ignorado deixa o total igual, e isso e a prova de que esta errado.

    Nao gasta a API de questoes: a tela de filtro vem renderizada no servidor.
    """
    global PARAM_BANCA
    base = montar_url_tela(etiqueta_teste, nivel_teste)

    def total_da_tela(url):
        pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            pagina.wait_for_selector(".pagination-bar__item", timeout=45000)
        except Exception:
            pass
        esperar_ate(pagina, lambda: bool(
            (pagina.evaluate(JS_ESTADO_TELA) or {}).get("contagem")), teto_s=20)
        est = pagina.evaluate(JS_ESTADO_TELA) or {}
        bruto = re.sub(r"\D", "", (est.get("contagem") or "").split("quest")[0])
        return int(bruto) if bruto else None

    sem = total_da_tela(base)
    if not sem:
        print("  [!] nao consegui ler o total sem filtro - nao da pra provar nada")
        return None
    print(f"  aprendendo o filtro de banca: a etiqueta {etiqueta_teste} tem "
          f"{sem:,} questoes sem filtro".replace(",", "."))

    for nome in CANDIDATOS_PARAM_BANCA:
        # uma banca grande so, pra diferenca ser obvia
        com = total_da_tela(f"{base}&{nome}=27")
        if com is None:
            continue
        if com < sem:
            print(f"     '{nome}=' FUNCIONA: {sem:,} -> {com:,} "
                  f"(so CEBRASPE)".replace(",", "."))
            PARAM_BANCA = nome
            return nome
        print(f"     '{nome}=' ignorado (total nao mudou: {com:,})".replace(",", "."))
    print("  [!] nenhum candidato filtrou. NAO vou coletar achando que filtrei.")
    return None


def montar_url_tela(assuntos, niveis=None, anos=None, bancas=None):
    """
    Monta o filtro do jeito que a TELA entende.

    Aceita um id so (a RAIZ, que ja traz os filhos: 406223 sozinho devolve as
    mesmas 6.818 que os 50 ids) ou uma LISTA de etiquetas - o caso dos blocos
    do edital.

    Testado: a tela ignora `assunto[]=` calada e devolve o acervo inteiro, e
    colapsa `assunto=` repetido no ultimo valor. O unico formato de lista que
    ela entende e separado por VIRGULA - o mesmo que o proprio Gran usa nos
    cadernos das aulas. Ano tambem e lista, nao faixa: `anos=2024,2025`.
    """
    lista = assuntos if isinstance(assuntos, (list, tuple, set)) else [assuntos]
    partes = ["assunto=" + ",".join(str(a) for a in lista)]
    for n in (niveis or []):
        cod_nivel = NIVEL_NA_TELA.get(str(n).strip().lower())
        if cod_nivel:
            partes.append(f"nivel={cod_nivel}")
        else:
            print(f"  [!] nivel '{n}' nao tem equivalente na tela - ignorado")
    if anos:
        partes.append("anos=" + ",".join(str(a) for a in anos))
    if bancas:
        # O NOME DO PARAMETRO E APRENDIDO, NAO CHUTADO.
        #
        # `descobrir_param_banca` prova qual e, olhando se o total DIMINUI.
        # Sem essa prova eu prefiro estourar aqui: coletar sem filtrar seria
        # varrer 122 mil questoes de bancas que o edital manda descartar, e o
        # erro so apareceria no fim, com a cota ja gasta.
        if not PARAM_BANCA:
            raise Parada(
                "pediram filtro de banca mas eu ainda nao aprendi como a "
                "tela nomeia esse parametro.\n"
                "     Rode a descoberta antes - chutar aqui faria o Gran\n"
                "     ignorar calado e devolver o acervo inteiro.")
        partes.append(f"{PARAM_BANCA}=" + ",".join(str(b) for b in bancas))
    partes += ["desatualizada=0", "anulada=0"]
    return f"{HOST_TELA_Q}/aluno/filtro/concursos?" + "&".join(partes)


def conferir_filtro_banca(linhas, bancas):
    """
    O filtro de banca PEGOU mesmo?

    Irmao do `conferir_filtro_orgao`, e pela mesma razao: parametro com nome
    errado nao da erro, da o acervo inteiro. Aqui a checagem e ainda mais
    barata porque cada questao ja diz de que banca e.
    """
    if not linhas:
        return True, ""
    pedidas = {str(b) for b in bancas}
    def id_da(q):
        # Duas formas convivem: a listagem crua traz `banca` como objeto, o
        # JSON ja gravado traz `banca_id` solto. A 1a versao fazia
        # `(q.get("banca") or {}).get("id")` e devolvia "None" pro segundo
        # caso - reprovava um filtro que tinha pegado certo. O teste offline
        # pegou isso antes de ir pra VPS.
        b = q.get("banca")
        if isinstance(b, dict) and b.get("id") is not None:
            return str(b["id"])
        return str(q.get("banca_id"))
    batem = sum(1 for q in linhas if id_da(q) in pedidas)
    fracao = batem / len(linhas)
    if fracao >= 0.9:
        return True, f"{batem}/{len(linhas)} da 1a pagina sao das bancas pedidas"
    fora = Counter(id_da(q) for q in linhas if id_da(q) not in pedidas)
    return False, (f"so {batem}/{len(linhas)} da 1a pagina sao das bancas "
                   f"pedidas; vieram tambem {dict(fora.most_common(4))}")


# --- JS da tela de questoes. Sem regex e sem "\n": o literal de barra invertida
# --- nao sobrevive ao caminho ate o navegador, e regex ali quebra o evaluate.
JS_ESTADO_TELA = """() => {
    const c = document.querySelector('.page__options__count');
    const nums = [];
    for (const b of document.querySelectorAll('.pagination-bar__item')) {
        const t = b.innerText.trim();
        if (t.length && !isNaN(Number(t))) nums.push(t);
    }
    return {contagem: (c ? c.innerText : '').split(String.fromCharCode(10)).join(' ').trim(),
            ultima_pagina: nums.length ? Number(nums[nums.length-1]) : null,
            cards: document.querySelectorAll('.ds-question').length};
}"""

# O id do assunto esta no href da trilha do card: /direito-penal-legislacao-
# especial-406351 -> 406351. Ler daqui nao custa requisicao nenhuma.
# Medido contra 307 questoes que ja tinham o dado da API: 283 identicas (92%) e
# 24 em que o DOM e SUBCONJUNTO - nunca o contrario. Os que faltam estao atras
# do botao "+" da trilha, que so renderiza quando clicado. Por isso a API, quando
# aparece, tem preferencia; e por isso cada questao guarda de onde veio.
JS_COLHER_CARDS = """() => {
    const out = [];
    for (const c of document.querySelectorAll('.ds-question')) {
        const qid = c.querySelector('[data-question]');
        const trilha = c.querySelector('.ds-question__header__top__subject');
        const ids = [], nomes = [];
        if (trilha) {
            for (const a of trilha.querySelectorAll('a')) {
                const partes = (a.getAttribute('href') || '').split('-');
                const ult = partes[partes.length - 1];
                if (ult && ult.length && !isNaN(Number(ult))) {
                    ids.push(Number(ult));
                    nomes.push(a.innerText.trim());
                }
            }
        }
        if (qid && ids.length) out.push({id: qid.getAttribute('data-question'),
                                         ids: ids, nomes: nomes});
    }
    return out;
}"""

# O app guarda TODA listagem no localStorage, com a URL inteira na chave:
#   cache.1.41.0/https://rota-api.../v1/elastic/questao?perPage=100&page=2&...
# e o valor e {expires, data:{data:<corpo da API>, status, headers}}.
# Ler daqui e melhor que escutar a rede por dois motivos: pega tambem as
# paginas que o app NAO pediu (a 1a vem embutida na carga e mesmo assim e
# gravada aqui), e nao depende de acertar a janela do evento de rede.
JS_CACHE_LISTAGEM = """([pag, porPag, assunto]) => {
    for (const k of Object.keys(localStorage)) {
        if (k.indexOf('elastic/questao') < 0) continue;
        const q = k.split('?')[1] || '';
        const par = {};
        for (const kv of q.split('&')) {
            const i = kv.indexOf('=');
            if (i > 0) par[kv.slice(0, i)] = kv.slice(i + 1);
        }
        // pagina ausente na chave E a pagina 1 - o app omite `page` quando
        // pede a primeira. Comparando cru, a pagina 1 nunca era achada.
        if ((par.page || '1') !== String(pag)) continue;
        if (par.perPage !== String(porPag)) continue;
        // Comparar a lista de etiquetas como CONJUNTO, nao como texto.
        // Dois motivos, os dois medidos: a virgula vem como %2C na chave, e
        // o app REORDENA a lista antes de pedir (mandei 407084,407085,... e
        // ele gravou 407090,407085,407288,...). Comparando cru, o cache
        // parecia vazio em toda pagina e a coleta caia no meu cliente - que
        // e exatamente o que este caminho existe pra evitar.
        if (assunto) {
            var vist = par.assunto || '';
            try { vist = decodeURIComponent(vist); } catch (e) {}
            var a = vist.split(',').sort().join(',');
            var b = String(assunto).split(',').sort().join(',');
            if (a !== b) continue;
        }
        return localStorage.getItem(k);
    }
    return null;
}"""

JS_FILTRO_DO_APP = """() => {
    // Qual lista de etiquetas o app REALMENTE consultou.
    //
    // Pedir `assunto=407070` nao quer dizer que ele consulte 407070: o Gran
    // EXPANDE a etiqueta nos filhos e pede
    // `assunto=407070,407071,407072,407073,407074,407075`. Comparar com o que
    // eu pedi nunca casa - e a colheita cai no meu cliente em toda pagina.
    // Entao eu pergunto ao app, em vez de supor.
    let melhor = null, quando = -1;
    for (const k of Object.keys(localStorage)) {
        if (k.indexOf('elastic/questao') < 0) continue;
        let exp = 0;
        try { exp = (JSON.parse(localStorage[k]) || {}).expires || 0; } catch (e) {}
        if (exp > quando) {
            quando = exp;
            const q = k.split('?')[1] || '';
            for (const kv of q.split('&')) {
                const i = kv.indexOf('=');
                if (i > 0 && kv.slice(0, i) === 'assunto') {
                    try { melhor = decodeURIComponent(kv.slice(i + 1)); }
                    catch (e) { melhor = kv.slice(i + 1); }
                }
            }
        }
    }
    return melhor;
}"""

JS_LISTAR_CACHE = """() => Object.keys(localStorage)
    .filter(k => k.indexOf('elastic/questao') >= 0)
    .map(k => {
        const q = k.split('?')[1] || '';
        const p = {};
        for (const kv of q.split('&')) {
            const i = kv.indexOf('=');
            if (i > 0) p[kv.slice(0, i)] = kv.slice(i + 1);
        }
        let a = p.assunto || '';
        try { a = decodeURIComponent(a); } catch (e) {}
        return 'page=' + p.page + ' perPage=' + p.perPage + ' assunto=' + a;
    })"""

JS_PODAR_CACHE = """(tetoKb) => {
    let bytes = 0;
    const chaves = [];
    for (const k of Object.keys(localStorage)) {
        if (k.indexOf('elastic/questao') < 0) continue;   // so listagens
        chaves.push(k);
        bytes += localStorage[k].length;
    }
    if (bytes / 1024 < tetoKb) return 0;
    for (const k of chaves) localStorage.removeItem(k);
    return chaves.length;
}"""

JS_PAGINA_ATIVA = """() => {
    // So item ATIVO que tenha NUMERO. A versao antiga fazia
    // Number(innerText) direto e, quando o ativo era um chevron sem texto,
    // devolvia 0 - ai o codigo achava que nunca estava na pagina pedida,
    // tentava clicar num botao inexistente e morria com "a pagina 1 nao esta
    // na barra".
    for (const b of document.querySelectorAll('.pagination-bar__item.active')) {
        const t = (b.innerText || '').trim();
        if (t.length && !isNaN(Number(t))) return Number(t);
    }
    return null;
}"""

JS_ORDINAL_DO_PRIMEIRO = """() => {
    // O numero de ordem da 1a questao da tela ("Questao 21" -> 21).
    // E o sinal CONFIAVEL de em que pagina se esta. A barra nao serve: o
    // botao da pagina atual some da lista de clicaveis (medido: com 10
    // paginas, estando na 2, a barra listava [1,3,4,...,10]) e o item
    // `.active` nem sempre tem texto. Com isso pagina_ativa devolvia None,
    // o codigo achava que precisava navegar, tentava clicar num botao que
    // nao existe e a colheita morria - ou pior, caia no meu cliente.
    const e = document.querySelector('.ds-question__header__top__order');
    if (!e) return null;
    const t = (e.innerText || '').replace(/[^0-9]/g, '');
    return t.length ? Number(t) : null;
}"""

JS_PAGINAS_NA_BARRA = """() => {
    const n = [];
    for (const b of document.querySelectorAll('.pagination-bar__item')) {
        const t = (b.innerText || '').trim();
        if (t.length && !isNaN(Number(t))) n.push(Number(t));
    }
    return n;
}"""

# A tela tem ~12 .virtual-select (Disciplina, Assunto, Banca...). Acho cada um
# pelo TEXTO que ele mostra, nao pela posicao: posicao fixa quebraria no dia em
# que o Gran acrescentasse um filtro, e calada.
JS_ACHAR_SELECT = """(querNumero) => {
    const vs = [...document.querySelectorAll('.virtual-select')];
    for (let i = 0; i < vs.length; i++) {
        const t = ((vs[i].querySelector('.virtual-select__box__selected-one')||{}).innerText||'').trim();
        const ehNumero = t.length > 0 && !isNaN(Number(t));
        const ehOrdem = t.toLowerCase().includes('recent')
                     || t.toLowerCase().includes('antig')
                     || t.toLowerCase().includes('relevant');
        if (querNumero ? ehNumero : ehOrdem) return {indice: i, atual: t};
    }
    return null;
}"""

ORDENS = {"recentes": "Mais recentes", "antigas": "Mais antigas",
          "relevantes": "Mais relevantes"}


def esperar_ate(pagina, condicao, teto_s=15.0, passo_ms=300):
    """
    Espera a condicao virar verdade, olhando de 300 em 300ms.

    Trocar relogio fixo por sondagem nao acelera nenhuma requisicao - so para
    de ficar parado depois que a coisa ja aconteceu. Eram 5s apos trocar o
    'por pagina' e 2,5s apos carregar o filtro: com 902 etiquetas isso dava
    quase 2h de espera morta na passada inteira.
    """
    for _ in range(int(teto_s * 1000 / passo_ms)):
        pagina.wait_for_timeout(passo_ms)
        try:
            if condicao():
                return True
        except Exception:
            pass
    return False


def escolher_no_select(pagina, indice, texto):
    """Abre um virtual-select e escolhe a opcao pelo texto exato."""
    caixa = pagina.locator(".virtual-select").nth(indice)
    caixa.locator(".virtual-select__box").click(timeout=8000)
    esperar_ate(pagina, lambda: pagina.locator(".virtual-select-item").count() > 0,
                teto_s=6)
    # SO DENTRO DO MENU ABERTO.
    #
    # Aqui era `pagina.get_by_text(texto).last`, que varre a PAGINA INTEIRA.
    # Funcionou em Portugues por sorte: o ultimo "100" da tela era mesmo a
    # opcao. Em Matematica o enunciado das questoes e cheio de numeros, entao
    # o ".last" caia no texto de uma questao e o seletor nunca mudava - a
    # materia inteira ia falhando etiqueta por etiqueta.
    opcao = pagina.locator(".virtual-select-item").filter(
        has_text=re.compile(rf"^\s*{re.escape(texto)}\s*$"))
    if not opcao.count():
        opcao = pagina.locator(".virtual-select-item").get_by_text(
            texto, exact=True)
    opcao.last.click(timeout=8000)
    # pronto quando a escolha aparece na caixa E a tela ja tem cards
    esperar_ate(pagina, lambda: (
        texto in (pagina.locator(".virtual-select").nth(indice)
                  .locator(".virtual-select__box__selected-one")
                  .inner_text() or "")
        and pagina.locator(".ds-question").count() > 0), teto_s=15)


def preparar_tela(pagina, url_filtro=None, por_pagina=100, ordem=None):
    """
    Deixa a aba pronta: filtro certo, 100 por pagina e a ordenacao pedida.

    Abrir o filtro pela URL nao custa chamada de API - a lista vem renderizada
    no servidor. E 100 por pagina corta 341 paginas para 69.
    """
    if url_filtro:
        print("  abrindo o filtro (carga do servidor, nao gasta API)")
        pagina.goto(url_filtro, wait_until="domcontentloaded", timeout=60000)
        # Esperar pelo RELOGIO e frágil: a lista chega renderizada mas a barra
        # de paginacao so aparece depois de hidratar, e ai eu lia "None
        # paginas" e parava achando que o filtro estava vazio. Espero o
        # elemento, nao os segundos.
        try:
            pagina.wait_for_selector(".pagination-bar__item", timeout=45000)
        except Exception:
            print("  [!] a barra de paginacao nao apareceu em 45s")
        # pronto quando a contagem aparece - antes eram 2,5s cravados
        esperar_ate(pagina, lambda: bool(
            pagina.locator(".page__options__count").count()
            and pagina.locator(".ds-question").count()), teto_s=10)

    def mostrar():
        est = pagina.evaluate(JS_ESTADO_TELA)
        print(f"  tela: {est['contagem']} · {est['ultima_pagina']} paginas · "
              f"{est['cards']} na tela")
        return est

    est = mostrar()

    if ordem:
        alvo = ORDENS.get(str(ordem).lower(), ordem)
        sel = pagina.evaluate(JS_ACHAR_SELECT, False)
        if not sel:
            print("  [!] nao achei o seletor de ordenacao - seguindo assim")
        elif sel["atual"].lower() != alvo.lower():
            print(f"  ordenacao: '{sel['atual']}' -> '{alvo}'")
            escolher_no_select(pagina, sel["indice"], alvo)
            est = mostrar()

    sel = pagina.evaluate(JS_ACHAR_SELECT, True)
    if not sel:
        print("  [!] nao achei o seletor de questoes por pagina - seguindo assim")
    elif int(sel["atual"]) != por_pagina:
        print(f"  mudando de {sel['atual']} para {por_pagina} por pagina")
        antes = est.get("ultima_pagina")
        # INSISTIR ATE VALER.
        #
        # O clique falha calado quando a tela ainda esta montando: o seletor
        # continua em 20 e a barra continua com o numero de paginas antigo
        # (1.982 questoes / 100 paginas = ainda 20 por pagina). Seguir assim
        # faz o coletor procurar respostas de perPage=100 que nunca existirao,
        # e concluir "a pagina nao veio" com a pagina na tela.
        for tentativa in range(3):
            try:
                escolher_no_select(pagina, sel["indice"], str(por_pagina))
            except Exception as e:
                # o clique estourar E uma das formas de nao pegar; sem este
                # try o retry nunca rodava e a etiqueta inteira se perdia
                print(f"      . o clique no seletor estourou "
                      f"({str(e).splitlines()[0][:44]})")
            agora = pagina.evaluate(JS_ACHAR_SELECT, True)
            if agora and int(agora["atual"]) == por_pagina:
                break
            if tentativa < 2:
                print(f"      . o seletor nao pegou (esta em "
                      f"{agora['atual'] if agora else '?'}); tentando de novo "
                      f"({tentativa + 2}/3)")
            pagina.wait_for_timeout(2000)
            sel = agora or sel
        else:
            print(f"  [!] nao consegui por {por_pagina} por pagina - seguindo "
                  f"com o que a tela tem")
        # ESPERAR A PAGINACAO REFLETIR O NOVO TAMANHO.
        #
        # A lista e virtual: mostra ~25 cards mesmo com 100 carregados, e
        # mostra 2 enquanto ainda monta. Contar card nao diz se terminou. O
        # sinal honesto e a BARRA: com 1.982 questoes, 100 paginas quer dizer
        # que ainda esta em 20 por pagina; 20 paginas quer dizer que o tamanho
        # novo valeu. Sem isto eu seguia com a tela pela metade e concluia que
        # a pagina nao tinha vindo - com ela na tela.
        esperar_ate(pagina, lambda: (
            pagina.evaluate(JS_ESTADO_TELA).get("ultima_pagina") != antes),
            teto_s=20)
        est = mostrar()
    return est


class TelaDeQuestoes:
    """
    A tela de questoes dirigida pelo script: o APP pede, eu escuto pelo CDP.

    Por que existe: a listagem (`/v1/elastic/questao`, ~130 KB) e a UNICA
    chamada que leva 429 - e e justamente a que o app faz sozinho ao paginar.
    As leves (estatistica, comentario, ~160 B) nunca levaram 429 nem a 0,4s.
    Dividindo assim, a coleta deixa de depender da cota que eu nao controlo.

    Cheguei aqui depois de esgotar as diferencas entre a minha requisicao e a
    do app (token, cabecalhos, X-Client-Id, a query inteira): eram identicas, e
    ainda assim ele recebia 200 e eu 429. Parei de imitar o app e passei a
    usa-lo.
    """

    def __init__(self, pagina):
        self.pagina = pagina
        self.respostas = []
        # ESCUTA PELO PLAYWRIGHT, NAO PELO CDP.
        #
        # Era `new_cdp_session` + `Network.getResponseBody`, que so existe no
        # Chromium. Em 24/09 o Cloudflare passou a barrar
        # `/v1/elastic/questao` em TODO Chrome desta maquina - com e sem
        # automacao, perfil novo, cache limpo - e a liberar no Firefox. O
        # coletor teve que rodar noutro motor, e `page.on("response")` e API
        # do Playwright: funciona nos dois. De quebra sumiu o controle manual
        # de requestId, que era metade do codigo daqui.
        pagina.on("response", self._ao_responder)

    def _ao_responder(self, resp):
        try:
            if "/v1/elastic/questao" not in resp.url:
                return
            # o OPTIONS de preflight nao leva Authorization por desenho - ja me
            # fez concluir "token diferente" uma vez. Ignorar.
            if (resp.request.method or "").upper() == "OPTIONS":
                return
            try:
                cab = {k.lower(): v for k, v in resp.headers.items()}
            except Exception:
                cab = {}
            self.respostas.append({"resp": resp, "status": resp.status,
                                   "retry_after": cab.get("retry-after")})
            # teto pra lista nao crescer sem fim numa rodada de horas
            if len(self.respostas) > 60:
                del self.respostas[:-30]
        except Exception:
            pass

    def limpar(self):
        self.respostas.clear()

    def corpo(self, pagina_n=None, por_pagina=None):
        """
        O corpo da ultima resposta 200 QUE BATE com a pagina e o tamanho
        pedidos.

        Conferir isso nao e zelo: sem essa checagem eu devolvia a resposta da
        carga inicial (perPage=20) enquanto a paginacao estava calculada para
        100 - lia 20 de cada 100 e pulava 80 ao avancar. Apareceu na 1a
        rodada longa como "pagina 1/1226" com "20/20 da pagina".
        """
        for r in reversed(self.respostas):
            if r["status"] != 200:
                continue
            if pagina_n is not None or por_pagina is not None:
                try:
                    q = r["resp"].url.split("?", 1)[1]
                    par = dict(kv.split("=", 1) for kv in q.split("&")
                               if "=" in kv)
                except Exception:
                    continue
                # PAGINA AUSENTE E PAGINA 1.
                #
                # O app omite `page` quando pede a primeira - e eu comparava
                # None com "1" e descartava. Resultado: a pagina 1 nunca era
                # reconhecida, a ida-e-volta nao adiantava (na volta ele serve
                # do proprio cache, sem rede) e a coleta parava com "a pagina
                # 1 nao veio pelo app" - com a pagina na tela, 20 paginas na
                # barra. Foi o que travou Historia em 24/09.
                pag_resp = par.get("page") or "1"
                if pagina_n is not None and pag_resp != str(pagina_n):
                    continue
                if por_pagina is not None and par.get("perPage") != str(por_pagina):
                    continue
            try:
                return r["resp"].json()
            except Exception:
                continue
        return None

    def erro(self):
        """A ultima resposta que NAO foi 200, se houver."""
        ruins = [r for r in self.respostas if r["status"] != 200]
        return ruins[-1] if ruins else None

    def pagina_ativa(self, por_pagina=None):
        """
        Em que pagina a tela esta.

        Primeiro pelo numero de ordem da 1a questao (confiavel); a barra e so
        desempate, porque o botao da pagina atual some dela.
        """
        if por_pagina:
            try:
                ordinal = self.pagina.evaluate(JS_ORDINAL_DO_PRIMEIRO)
            except Exception:
                ordinal = None
            if ordinal:
                return (ordinal - 1) // int(por_pagina) + 1
        return self.pagina.evaluate(JS_PAGINA_ATIVA)

    def podar_cache(self, teto_kb=2000):
        """
        Apaga as listagens guardadas no localStorage quando elas crescem
        demais. SO as listagens - `auth/*` nunca, que e onde mora o token.

        Por que: o localStorage tem ~5 MB. Medido numa passada real, 21
        listagens ja ocupavam 3,1 MB e o app comecou a EVICTAR. Pagina
        evictada nao esta no cache e tambem nao gera requisicao (o app achava
        que tinha), entao a colheita caia no meu cliente - justo o que este
        caminho existe pra evitar. Podando, o app pede de novo e eu pego pela
        rede. Confirmado: depois da poda ele pediu, e o token sobreviveu.
        """
        try:
            n = self.pagina.evaluate(JS_PODAR_CACHE, teto_kb)
        except Exception:
            return 0
        if not n:
            return 0
        # RECARREGAR DEPOIS DE PODAR, sempre.
        #
        # A biblioteca de cache do app monta um indice EM MEMORIA quando a
        # pagina carrega. Apagar as chaves por baixo dela nao avisa ninguem:
        # ela continua achando que tem a pagina, nao pede, e a tela fica sem
        # nada - do lado de fora parece que o Gran bloqueou. Foi o que deixou
        # esta aba sem paginar a noite inteira enquanto o Firefox, recem
        # aberto, paginava normal. O reload reconstroi o indice a partir do
        # que existe de verdade.
        try:
            self.pagina.reload(wait_until="domcontentloaded", timeout=60000)
            self.pagina.wait_for_selector(".pagination-bar__item", timeout=45000)
            self.pagina.wait_for_timeout(2000)
        except Exception as e:
            print(f"      [!] recarreguei apos podar e a barra nao voltou: "
                  f"{str(e)[:80]}")
        return n

    def do_cache(self, n, por_pagina, assunto=None):
        """
        A listagem da pagina n como o app a guardou no localStorage.

        Devolve o MESMO formato que buscar_json_q devolveria, pra quem chama
        nao precisar saber de onde veio.
        """
        try:
            bruto = self.pagina.evaluate(JS_CACHE_LISTAGEM,
                                         [n, por_pagina, assunto])
        except Exception:
            return None
        if not bruto:
            return None
        try:
            env = json.loads(bruto)
        except Exception:
            return None
        # {expires, data:{data:<corpo>, status}} - o corpo e o que a API
        # devolveria. Se um dia mudarem o embrulho, e melhor devolver None
        # e cair pro caminho de rede do que entregar coisa errada calado.
        interno = (env.get("data") or {}).get("data")
        if isinstance(interno, dict) and "data" in interno:
            return interno
        return None

    def cards(self):
        """As questoes visiveis, lidas do DOM. Nao custa requisicao."""
        return self.pagina.evaluate(JS_COLHER_CARDS)

    def preparar(self, url_filtro=None, por_pagina=100, ordem=None):
        return preparar_tela(self.pagina, url_filtro=url_filtro,
                             por_pagina=por_pagina, ordem=ordem)

    def _clicar_setinha(self):
        """
        Clica na setinha de proxima pagina, reresolvendo o elemento a cada
        tentativa.

        O Vue redesenha a barra inteira a cada troca de pagina, entao guardar
        o locator e reusar da "Element is not attached to the DOM" - que foi
        o erro que matou a 1a rodada noturna.
        """
        for tentativa in range(3):
            try:
                prox = self.pagina.locator(".pagination-bar__item").filter(
                    has=self.pagina.locator("i.fa-chevron-right")).last
                if not prox.count():
                    return False
                prox.scroll_into_view_if_needed(timeout=8000)
                prox.click(timeout=8000)
                return True
            except Exception:
                if tentativa == 2:
                    return False
                self.pagina.wait_for_timeout(1500)
        return False

    def ir_para(self, n, por_pagina=None):
        """
        Campo 'Ir para a pagina' + OK quando existe; senao, o botao numerado.

        Os dois caminhos sao necessarios: com muitas paginas o numero some da
        barra (vira "..."), e com poucas paginas o Gran nem desenha o campo de
        ir-para. Um so dos dois deixa metade dos filtros de fora.
        """
        p = self.pagina
        # A barra e re-renderizada pelo Vue a cada troca de pagina, entao o
        # elemento que eu acabei de achar pode sair do DOM entre o achar e o
        # clicar ("Element is not attached to the DOM"). Reresolver e tentar
        # de novo e o certo - foi isso que derrubou a 1a rodada noturna.
        for tentativa in range(3):
            try:
                campo = p.locator(".pagination-bar__navigation-input input").first
                if not campo.count():
                    break
                campo.scroll_into_view_if_needed(timeout=8000)
                campo.click(timeout=8000)
                campo.fill("")
                campo.type(str(n), delay=80)   # fill() seco o Vue nao registra
                p.locator(".pagination-bar__navigation-submit").first.click(
                    timeout=8000)
                return
            except Exception as e:
                if tentativa == 2:
                    raise
                print(f"      . a barra se redesenhou no meio do clique; "
                      f"tentando de novo ({tentativa + 2}/3)")
                p.wait_for_timeout(1500)
        # 2) a SETINHA de proxima, quando o destino e a pagina seguinte.
        #
        # Nao da pra confiar no rotulo dos botoes: o Gran renderiza o botao da
        # pagina corrente SEM TEXTO e marcado como `disabled active` - medido,
        # com a tela na pagina 1 a barra saia ['1','','3','4',...]. Procurar o
        # botao "2" nunca acha, e a colheita morre na 2a pagina de todo
        # filtro. A setinha nao tem rotulo pra errar.
        ativa = self.pagina_ativa(por_pagina)
        if ativa is not None and n == ativa + 1:
            if self._clicar_setinha():
                return

        botao = p.locator(".pagination-bar__item", has_text=re.compile(rf"^\s*{n}\s*$"))
        if not botao.count():
            # Filtro de uma pagina so: o Gran nao desenha nem campo de ir-para
            # nem botao clicavel, e a pagina pedida JA e a que esta na tela.
            # Sem esta saida, uma materia pequena morre antes de ler a 1a
            # pagina - que era o unico conteudo dela.
            na_barra = p.evaluate(JS_PAGINAS_NA_BARRA)
            if len(na_barra) <= 1 and n in (1, *(na_barra or [])):
                return
            # Ultimo recurso: a setinha. O rotulo da pagina corrente vem
            # vazio, entao "nao esta na barra" muitas vezes quer dizer "e a
            # proxima" - e avancar de um em um e o que a colheita faz mesmo.
            if True:
                # CAMINHAR ate a pagina pedida, de um em um.
                #
                # A setinha so anda um passo. Quando a coleta RETOMA no meio
                # (progresso gravado na pagina 15, por exemplo), um passo so
                # nunca chega - e o codigo caia no meu cliente. Andar e de
                # graca: cada passo e o app pedindo, nao eu.
                atual = self.pagina_ativa(por_pagina)
                passos = 0
                print(f"      . a pagina {n} nao tem rotulo na barra; "
                      f"caminhando da {atual} ate ela")
                while passos < 80:
                    if not self._clicar_setinha():
                        return
                    p.wait_for_timeout(1200)
                    passos += 1
                    agora = self.pagina_ativa(por_pagina)
                    if agora is None or agora >= n:
                        return
                    if agora == atual:        # travou, nao adianta insistir
                        return
                    atual = agora
                return
            raise Parada(f"a pagina {n} nao esta na barra ({na_barra}) e nao "
                         f"ha campo de ir-para nem setinha")
        botao.first.scroll_into_view_if_needed(timeout=8000)
        botao.first.click(timeout=8000)

    def fechar(self):
        try:
            self.pagina.remove_listener("response", self._ao_responder)
        except Exception:
            pass
        self.respostas.clear()


def descobrir_raiz(pagina, assuntos, niveis):
    """
    Descobre o id do assunto RAIZ da disciplina - SEM chamada de API.

    A primeira versao disto pedia uma listagem com perPage=1. Era incoerente:
    eu blindava a coleta contra o 429 e deixava a descoberta exposta a ele -
    e foi exatamente onde parou no primeiro teste. A raiz esta no DOM: o 1o
    link da trilha do card e sempre ela (`/direito-penal-406223`), e carregar
    o filtro vem renderizado do servidor.

    Nao chuto 'assuntos[0]': calhou de ser a raiz em Penal e em Penal Militar,
    mas o caderno da aula nao promete ordem nenhuma - e errar aqui devolve o
    acervo inteiro calado, que e o jeito mais caro de descobrir o engano.

    Devolve None se nao der; quem chama decide o que fazer (e nao deve cair
    calado no caminho antigo).
    """
    if not assuntos:
        return None
    try:
        preparar_tela(pagina, url_filtro=montar_url_tela(assuntos[0], niveis),
                      por_pagina=POR_PAGINA_PADRAO)
        cartas = pagina.evaluate(JS_COLHER_CARDS)
    except Exception as e:
        print(f"  [!] nao consegui abrir o filtro pra achar a raiz: {str(e)[:110]}")
        return None

    contagem = Counter(str(c["ids"][0]) for c in cartas if c.get("ids"))
    if not contagem:
        print("  [!] nenhum card trouxe trilha de assunto")
        return None
    raiz, quantos = contagem.most_common(1)[0]
    if quantos < len(cartas) * 0.6:
        print(f"  [!] a trilha nao concorda numa raiz so: {contagem.most_common(3)}")
        return None
    if str(assuntos[0]) != raiz:
        print(f"  [i] raiz e {raiz} (o 1o do caderno era {assuntos[0]})")
    return raiz


class ListagemPelaTela:
    """
    Entrega a listagem de uma pagina fazendo o APP pedir.

    Quando o app nao pede - as primeiras paginas ja vem embutidas na carga e
    ele guarda ~40 do lado do cliente - cai para o meu proprio cliente. Isso e
    seguro porque o 429 vem de RAJADA, nao de chamada isolada: 69 listagens
    seguidas derrubam, duas espacadas por minutos nao. O tombo antigo era pedir
    todas as 69 por aqui.
    """

    def __init__(self, pagina, assuntos, niveis, por_pagina, orgaos=None,
                 assunto_raiz=None, espera_resposta=3000, sem_cliente=False):
        self.tela = TelaDeQuestoes(pagina)
        self.pagina = pagina
        self.assuntos, self.niveis, self.orgaos = assuntos, niveis, orgaos
        self.raiz = assunto_raiz
        self.por_pagina = por_pagina
        self.espera = espera_resposta
        self.pelo_app = 0
        self.pelo_cliente = []
        self._dando_volta = False     # evita ida-e-volta dentro de ida-e-volta
        # Com sem_cliente, a queda pro meu cliente deixa de existir: em vez de
        # pedir eu, PARO. Numa rodada longa sem ninguem olhando esse recurso
        # silencioso viraria exatamente o que derrubou a conta em 24/09 - a
        # pessoa acha que esta rodando "de graca" e ha horas nao esta.
        self.sem_cliente = sem_cliente

    def preparar(self, url_filtro, ordem=None):
        est = self.tela.preparar(url_filtro=url_filtro,
                                 por_pagina=self.por_pagina, ordem=ordem)

        def tamanho_da_tela():
            try:
                r = self.pagina.evaluate(JS_ACHAR_SELECT, True)
                return int(r["atual"]) if r else None
            except Exception:
                return None

        # SE O SELETOR NAO PEGOU, RECARREGAR - NUNCA PAGINAR DE 20 EM 20.
        #
        # A 1a versao disto caia pra 20 "pra nao travar". Mas 330 questoes a
        # 20 por pagina sao 17 requisicoes no lugar de 4, e requisicao e
        # exatamente o que esta escasso: foi o excesso delas que bloqueou o PC
        # em 24/09. Recarregar o filtro NAO gasta a API de questoes (e carga
        # do servidor), entao recarregar sai muito mais barato que quadruplicar
        # a paginacao. Se nem assim pegar, a etiqueta fica pra proxima rodada.
        for tentativa in range(2):
            atual = tamanho_da_tela()
            if atual == self.por_pagina:
                break
            if atual is None:
                # NAO E O SELETOR: A PAGINA NAO CARREGOU.
                #
                # Sem barra de paginacao, sem cards e sem contagem, nao existe
                # seletor pra pegar. Recarregar 3x aqui so gasta tempo e
                # esconde o que houve. Largo a etiqueta com a causa certa - o
                # laco de fora ja sabe pular uma e seguir.
                raise Parada(
                    "a tela nao carregou: sem barra, sem cards, sem "
                    "contagem.\n"
                    "     Nao e o seletor de tamanho - nao ha o que "
                    "selecionar.\n"
                    "     Esta etiqueta fica pra proxima rodada.")
            print(f"      . a tela ficou em {atual} por pagina "
                  f"(pedi {self.por_pagina}); recarregando o filtro "
                  f"({tentativa + 2}/3)")
            self.pagina.wait_for_timeout(2500)
            est = self.tela.preparar(url_filtro=url_filtro,
                                     por_pagina=self.por_pagina, ordem=ordem)
        else:
            if tamanho_da_tela() != self.por_pagina:
                raise Parada(
                    f"a tela insiste em {tamanho_da_tela()} questoes por "
                    f"pagina (pedi {self.por_pagina}).\n"
                    "     Nao sigo assim: seria 5x mais requisicao pela mesma\n"
                    "     questao, e requisicao e o que esta escasso.\n"
                    "     Esta etiqueta fica pra proxima rodada.")
        # Descobrir o filtro que o app de fato usou (ele expande a etiqueta
        # nos filhos). Sem isto, o casamento do cache falha em toda pagina.
        try:
            real = self.pagina.evaluate(JS_FILTRO_DO_APP)
        except Exception:
            real = None
        if real and real != self.raiz:
            n_meu = len((self.raiz or "").split(","))
            n_app = len(real.split(","))
            if n_app != n_meu:
                print(f"      . o app expandiu {n_meu} etiqueta(s) em {n_app}")
            self.raiz = real
        return est

    def _evidencia(self, n):
        """
        O que o app pediu e o que ele guardou - pra parar de adivinhar.

        Imprime os parametros de cada resposta capturada e as chaves de
        listagem do localStorage. Sem isto a mensagem de falha so diz "nao
        veio", e eu fico deduzindo o formato do pedido - ja deduzi errado
        duas vezes nesta mesma falha.
        """
        linhas = ["     --- o que o app pediu nesta tela ---\n"]
        if not self.tela.respostas:
            linhas.append("     (nenhuma resposta de listagem capturada)\n")
        for r in self.tela.respostas[-8:]:
            try:
                q = r["resp"].url.split("?", 1)[1]
                par = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
                resumo = {k: par.get(k) for k in ("page", "perPage")}
                ass = (par.get("assunto") or par.get("assunto[]") or "")[:44]
                linhas.append(f"     {r['status']}  {resumo}  assunto={ass}\n")
            except Exception:
                linhas.append(f"     {r['status']}  (url sem parametros)\n")
        try:
            chaves = self.pagina.evaluate("""() => Object.keys(localStorage)
                .filter(k => k.indexOf('elastic/questao') >= 0)
                .map(k => { const q = k.split('?')[1] || ''; const p = {};
                    for (const kv of q.split('&')) { const i = kv.indexOf('=');
                        if (i > 0) p[kv.slice(0, i)] = kv.slice(i + 1); }
                    return p.page + '|' + p.perPage; }).slice(0, 10)""")
            linhas.append(f"     cache do app (page|perPage): {chaves}\n")
        except Exception:
            pass
        linhas.append(f"     eu procurava: page={n} perPage={self.por_pagina}"
                      f" assunto={str(self.raiz)[:40]}\n")
        return "".join(linhas)

    def buscar(self, n):
        """Devolve o JSON da pagina n. Levanta Parada se o APP levou erro."""
        # NAO LIMPO AS RESPOSTAS AQUI.
        #
        # A resposta da pagina 1 chega durante o `preparar` (quando troco pra
        # 100 por pagina). O `limpar()` que ficava aqui a jogava fora, e logo
        # depois eu concluia "a pagina nao veio" - com a pagina na tela.
        # Limpar fazia sentido quando `corpo()` devolvia a ultima resposta
        # qualquer; desde que ele confere pagina E perPage, resposta velha nao
        # atrapalha: ou bate, ou e ignorada.
        if self.tela.pagina_ativa(self.por_pagina) != n:
            # A poda so pode acontecer QUANDO SE VAI NAVEGAR. Podando antes de
            # saber que a pagina pedida ja e a da tela, some o cache dela e
            # nao ha navegacao pra gerar requisicao - fica sem os dois, e a
            # colheita cai no meu cliente. Aqui e seguro: logo depois da poda
            # vem o clique, o app pede, e eu pego pela rede.
            # NAO PODO MAIS O CACHE AQUI.
            #
            # A poda existia porque a captura pelo CDP perdia a pagina quando
            # o app a servia do proprio cache: eu apagava as chaves pra
            # forcar um pedido novo. So que apagar corrompe o indice em
            # memoria do app, entao a poda TINHA que recarregar a pagina - e
            # recarregar no meio da paginacao e lento e cria corrida (foi o
            # que derrubou a 1a rodada na VPS, na pagina 2).
            # Com a escuta pela rede isso perdeu o sentido: se o app evicta
            # sozinho, ele pede de novo e eu pego o pedido. Eviction deixou de
            # ser problema e virou aliada.
            self.tela.ir_para(n, self.por_pagina)
            # ESPERA COM SONDAGEM, nao relogio fixo.
            #
            # Eram 3s cravados. Quando o Gran esta lento (e ele fica, depois
            # de um dia de uso pesado), a resposta chega depois disso: eu
            # concluia "o app nao pediu" e caia no meu cliente - justo o que
            # este caminho existe pra evitar. Agora olho a cada 500ms ate
            # aparecer, com teto.
            j = self._esperar_resposta(n)
            if j is not None:
                self.pelo_app += 1
                return j

        # 1) o proprio cache do app. Cobre a pagina 1, que vem embutida na
        #    carga (o app a grava com perPage=20) e nao gera evento de rede.
        j = self.tela.do_cache(n, self.por_pagina, self.raiz)
        if j is not None:
            self.pelo_app += 1
            return j

        # 2) o que eu escutei passar na rede (so o que bate com a pagina
        #    e o tamanho pedidos - ver corpo())
        j = self.tela.corpo(n, self.por_pagina)
        if j is not None:
            self.pelo_app += 1
            return j

        erro = self.tela.erro()
        if erro:
            raise Parada(f"o app recebeu {erro['status']} na pagina {n}")

        # 3) a IDA E VOLTA.
        #
        # A pagina 1 vem renderizada no servidor: o app a mostra sem pedir, e
        # por isso ela nao esta no cache nem passa pela rede. Sair dela e
        # voltar obriga o app a pedir - medido: com o cache limpo, ir de 3 pra
        # 19 gerou 2 requisicoes. Isso vale pra qualquer pagina que o app ache
        # que ja tem.
        if not self._dando_volta:
            self._dando_volta = True
            try:
                vizinha = n + 1 if n == 1 else n - 1
                print(f"      . a pagina {n} nao gerou pedido; indo na "
                      f"{vizinha} e voltando pra forcar")
                self.tela.ir_para(vizinha)
                self.pagina.wait_for_timeout(self.espera)
                self.tela.ir_para(n)
                self.pagina.wait_for_timeout(self.espera)
                j = (self.tela.do_cache(n, self.por_pagina, self.raiz)
                     or self.tela.corpo(n, self.por_pagina))
                if j is not None:
                    self.pelo_app += 1
                    return j
            except Parada:
                pass
            finally:
                self._dando_volta = False

        # 4) ultimo recurso: peco eu. Deveria ser raro - se aparecer muito,
        #    e sinal de que o cache mudou de formato e eu nao percebi.
        if self.sem_cliente:
            # MOSTRAR O QUE O APP DE FATO PEDIU.
            #
            # Eu ja errei duas vezes o diagnostico desta falha deduzindo o
            # formato do pedido (primeiro achei que era o `limpar()`, depois
            # que era o `page` ausente). Deduzir de novo seria o terceiro
            # chute. Aqui embaixo vai a evidencia: os parametros de cada
            # resposta capturada e as chaves de listagem do cache do app.
            raise Parada(
                f"a pagina {n} nao veio pelo app e eu nao vou pedir por fora "
                f"(--adiar-leves).\n"
                f"{self._evidencia(n)}"
                "     O progresso esta gravado - rode de novo e ele continua.\n"
                "     Se repetir sempre na mesma pagina, rode o "
                "gran_diagnostico.py: pode ser bloqueio do endpoint.")
        self.pelo_cliente.append(n)
        print(f"      (nao achei a pagina {n} no cache do app; pedindo eu - "
              f"{len(self.pelo_cliente)}a vez)")
        # Dizer O QUE tem no cache, nao so que faltou. Sem isto, cada falha de
        # casamento vira uma rodada de adivinhacao - e ja foram tres (virgula
        # codificada, ordem trocada, tamanho de pagina).
        try:
            tem = self.pagina.evaluate(JS_LISTAR_CACHE)
            print(f"         cache tem {len(tem)}: {tem[:6]}")
            print(f"         eu procurava: page={n} perPage={self.por_pagina} "
                  f"assunto={self.raiz}")
        except Exception:
            pass
        return buscar_json_q(self.pagina, montar_url_questoes(
            self.assuntos, self.niveis, n, self.por_pagina, self.orgaos))

    def _esperar_resposta(self, n, teto_s=None):
        """
        Olha a cada 500ms se a pagina n ja chegou (cache ou rede).

        Eram 18s cravados e nao bastou: na VPS a pagina 2 chegou "so um pouco
        depois" e eu ja tinha desistido. Esperar de menos custa uma rodada
        inteira; esperar de mais custa segundos.
        """
        teto_s = ESPERA_PAGINA_S if teto_s is None else teto_s
        passos = int(teto_s * 1000 / 500)
        for i in range(passos):
            self.pagina.wait_for_timeout(500)
            j = self.tela.do_cache(n, self.por_pagina, self.raiz)
            if j is None:
                j = self.tela.corpo(n, self.por_pagina)
            if j is not None:
                if i > 5:
                    print(f"      . a pagina {n} levou ~{(i + 1) * 0.5:.0f}s")
                return j
            if self.tela.erro():
                return None
        return None

    def resumo(self):
        return (f"{self.pelo_app} pagina(s) pelo app, "
                f"{len(self.pelo_cliente)} pelo meu cliente "
                f"{self.pelo_cliente if self.pelo_cliente else ''}")

    def fechar(self):
        self.tela.fechar()


def coletar_pela_tela(pagina, rotulo, assuntos, niveis, assunto_raiz,
                      por_pagina=100, max_paginas=None, pausa_pagina=2.5):
    """
    Coleta uma disciplina INTEIRA sem gastar uma requisicao minha.

    A listagem ja traz tudo o que a questao precisa pra existir - enunciado,
    alternativas e o gabarito (campo 'resposta'). Faltam so as duas leves
    (estatistica e comentario), que ficam marcadas como pendentes e saem
    depois, com --completar-leves.

    Serve pra dois casos: coletar durante uma penalidade de 429 (que barra o
    meu cliente mas nao o app), e separar o trabalho caro do barato.
    """
    pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
    arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"
    por_id = {}
    if arq.exists():
        try:
            velho = json.loads(arq.read_text(encoding="utf-8"))
            por_id = {x.get("id"): x for x in (velho.get("questoes") or [])}
            print(f"  {len(por_id)} questoes ja no arquivo (serao atualizadas, "
                  f"nunca apagadas)")
        except Exception:
            pass

    fonte = ListagemPelaTela(pagina, assuntos, niveis, por_pagina,
                             assunto_raiz=assunto_raiz)
    novas = atualizadas = 0
    total_geral = None
    try:
        est = fonte.preparar(montar_url_tela(assunto_raiz, niveis))
        total_pags = est.get("ultima_pagina")
        if not total_pags:
            raise Parada("a tela nao mostrou paginacao - confira o --assunto")
        if max_paginas:
            total_pags = min(total_pags, max_paginas)

        for n in range(1, total_pags + 1):
            checar_bloqueio(pagina)
            j = fonte.buscar(n)
            d = j.get("data") or {}
            total_geral = d.get("total") or total_geral
            linhas = d.get("rows") or []
            if not linhas:
                print(f"  pagina {n} veio vazia - parando")
                break
            for q in linhas:
                item = montar_questao(q)
                antigo = por_id.get(item["id"])
                if antigo:
                    # nao apagar o que ja foi conquistado com cota
                    for campo in ("indice_acerto", "comentario_professor",
                                  "gabarito_confere_com_comentario"):
                        if antigo.get(campo) is not None:
                            item[campo] = antigo[campo]
                    atualizadas += 1
                else:
                    novas += 1
                item["leves_pendentes"] = item.get("indice_acerto") is None
                por_id[item["id"]] = item

            pendentes = sum(1 for x in por_id.values() if x.get("leves_pendentes"))
            salvar_json(arq, {
                "rotulo": rotulo, "assuntos": assuntos, "niveis": niveis,
                "assunto_raiz": assunto_raiz,
                "total_no_gran": total_geral, "paginas_no_gran": d.get("pages"),
                "origem_listagem": "tela",
                "questoes": list(por_id.values()),
                "coletado_em": datetime.now().isoformat(timespec="seconds"),
            })
            print(f"    pagina {n}/{total_pags}: {len(por_id)} questoes · "
                  f"{pendentes} esperando as leves")
            pagina.wait_for_timeout(int(pausa_pagina * 1000))
    finally:
        fonte.fechar()

    pendentes = sum(1 for x in por_id.values() if x.get("leves_pendentes"))
    print(f"\n  {len(por_id)} questoes ({novas} novas, {atualizadas} atualizadas)")
    print(f"  {fonte.resumo()}")
    if pendentes:
        print(f"\n  {pendentes} ainda sem indice de acerto. A spec exige esse campo,")
        print("  entao falta o segundo passo (esse SIM gasta a minha cota):")
        print(f'      python gran_extrator.py --completar-leves "{rotulo}"')
    return arq, len(por_id)


def completar_leves_do_arquivo(pagina, rotulo, com_comentarios=True, ritmo=None,
                               limite=None):
    """
    O segundo passo: as duas chamadas por questao, so nas que faltam.

    Grava a CADA questao. Uma queda no meio nao custa nada do que ja veio -
    licao de quando um 429 na 5a pagina jogou fora 400 questoes prontas.
    """
    pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
    arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"
    if not arq.exists():
        raise Parada(f"nao achei {arq}")
    d = json.loads(arq.read_text(encoding="utf-8"))
    qs = d.get("questoes") or []
    faltando = [q for q in qs if q.get("indice_acerto") is None]
    if limite:
        faltando = faltando[:limite]
    if not faltando:
        print("  Nada pendente - todas ja tem indice de acerto.")
        return arq, 0

    print(f"  {len(faltando)} de {len(qs)} questoes esperando as leves")
    ritmo = ritmo or Ritmo(piso=0.4, teto=60.0, acelera_a_cada=40,
                           unidade="questoes")
    cab = cabecalhos_api_questoes(pagina)
    andamento = Andamento(len(qs), ja_feitos=len(qs) - len(faltando))
    feitas = 0
    for i, q in enumerate(faltando, 1):
        checar_bloqueio(pagina)
        if i % 50 == 0:
            try:
                cab = cabecalhos_api_questoes(pagina)   # o token expira
            except Parada:
                pass
        completar_leves(pagina, q, cab, com_comentarios, ritmo=ritmo)
        q.pop("leves_pendentes", None)
        feitas += 1
        andamento.marcar()
        d["questoes"] = qs
        d["leves_em"] = datetime.now().isoformat(timespec="seconds")
        salvar_json(arq, d)
        if i % 10 == 0 or i == len(faltando):
            print(andamento.linha(f"{feitas}/{len(faltando)}"))
    return arq, feitas


def colher_da_tela(pagina, rotulo, ritmo=None, max_paginas=None, pagina_inicial=1,
                   url_filtro=None, por_pagina=100, ordem=None):
    """
    Completa um arquivo JA coletado com o id do assunto, sem fazer requisicao:
    o script clica na paginacao, o app pede, eu escuto - e o que o app nao
    pedir eu leio da trilha do card.

    Casa por id da questao, entao a ordem das paginas nao importa e rodar de
    novo nunca duplica.
    """
    ritmo = ritmo or Ritmo(piso=4.0, teto=120.0, acelera_a_cada=6, unidade="paginas")
    pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
    arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"
    if not arq.exists():
        raise Parada(f"nao existe {arq.name} pra completar (procurei em {pasta})")
    d = json.loads(arq.read_text(encoding="utf-8"))
    por_id = {q.get("id"): q for q in (d.get("questoes") or [])}
    ja_tinham = sum(1 for q in por_id.values() if q.get("assunto_ids"))
    print(f"  {len(por_id)} questoes no arquivo · {ja_tinham} ja tem id de assunto")

    tela = TelaDeQuestoes(pagina)
    respostas = tela.respostas
    corpo_da_ultima = tela.corpo
    ir_para = tela.ir_para

    def gravar():
        d["questoes"] = list(por_id.values())
        d["colhido_da_tela_em"] = datetime.now().isoformat(timespec="seconds")
        salvar_json(arq, d)          # grava A CADA pagina: um tropeco no meio
                                     # ja nao joga fora o que veio antes

    def absorver_api(j):
        """Dado completo, quando o app resolve pedir. Casa por id da questao,
        entao repetir pagina nao duplica."""
        novas = 0
        for q in (j.get("data") or {}).get("rows") or []:
            alvo = por_id.get(q.get("id"))
            if not alvo:
                continue
            brutos = q.get("assuntos") or []
            alvo["assuntos"] = [{
                "id": a.get("id"),
                "nome": a.get("nome_clean") or a.get("nome") or a.get("descricao"),
                "raiz": a.get("assunto_raiz"), "pai": a.get("pai"),
            } for a in brutos]
            alvo["assunto_ids"] = [a.get("id") for a in brutos if a.get("id")]
            alvo["assuntos_origem"] = "api"
            novas += 1
        gravar()
        return novas

    def absorver_tela(linhas):
        """Le a trilha do card. De graca, mas as vezes incompleta - por isso
        nunca sobrescreve o que ja veio da API."""
        novas = 0
        for l in linhas:
            # o DOM devolve o id como texto e o JSON guarda numero - casar so
            # num dos dois formatos faria a colheita achar zero, calada.
            bruto = l.get("id")
            alvo = por_id.get(bruto)
            if alvo is None:
                try:
                    alvo = por_id.get(int(bruto))
                except (TypeError, ValueError):
                    alvo = None
            if alvo is None:
                alvo = por_id.get(str(bruto))
            if not alvo or alvo.get("assuntos_origem") == "api":
                continue
            ids = [i for i in (l.get("ids") or []) if i]
            if not ids:
                continue
            nomes = l.get("nomes") or []
            alvo["assuntos"] = [{"id": i, "nome": nomes[k] if k < len(nomes) else None}
                                for k, i in enumerate(ids)]
            alvo["assunto_ids"] = ids
            alvo["assuntos_origem"] = "tela"
            novas += 1
        if novas:
            gravar()
        return novas

    atualizadas, total_pags = 0, None
    try:
        # o CDP ja escutando enquanto preparo a tela: a chamada que o app faz
        # ao trocar para 100 por pagina JA E a pagina 1 - vem de graca.
        est = preparar_tela(pagina, url_filtro=url_filtro, por_pagina=por_pagina,
                            ordem=ordem)
        total_pags = est.get("ultima_pagina")

        if not total_pags:
            raise Parada("nao consegui ler quantas paginas o filtro tem")

        # As primeiras paginas vem embutidas na carga do servidor: o app as
        # mostra sem pedir nada, e nao ha o que escutar. Nao da pra saber
        # quantas sao de antemao - o laco descobre andando, e a inversao da
        # ordenacao existe pra alcancar justamente esse bloco.
        fila = list(range(max(1, pagina_inicial), total_pags + 1))
        if max_paginas:
            fila = fila[:max_paginas]
        print(f"  {len(fila)} pagina(s) a percorrer, de {total_pags}\n")

        travadas, do_app = 0, []
        while fila:
            pag = fila[0]
            respostas.clear()
            checar_bloqueio(pagina)
            # Ja estar na pagina e o caso da 1a: o botao dela vem 'disabled' e
            # clicar nele estoura o timeout. Nao ha o que pedir - so ler.
            if pagina.evaluate(JS_PAGINA_ATIVA) != pag:
                ritmo.esperar(pagina, f"pagina {pag}/{total_pags}")
                try:
                    ir_para(pag)
                except Exception as e:
                    print(f"  [!] nao consegui pedir a pagina {pag}: {str(e)[:110]}")
                    break
                pagina.wait_for_timeout(3000)  # deixa a resposta chegar

            ativa = pagina.evaluate(JS_PAGINA_ATIVA)
            if ativa != pag:
                ruins = [r for r in respostas if r["status"] != 200]
                if ruins and ruins[-1]["status"] == 429:
                    # 429 no proprio app e a cota da CONTA. Desacelerar e
                    # obedecer; repito a MESMA pagina, nao pulo.
                    print(f"  [!] o APP recebeu 429 na pagina {pag}")
                    if not ritmo.levou_429(pagina, ruins[-1].get("retry_after")):
                        break
                    continue
                if ruins:
                    print(f"  [!] o APP recebeu {ruins[-1]['status']} - parando.")
                    break
                travadas += 1
                print(f"  [!] a tela nao foi para a {pag} (esta na {ativa}) "
                      f"({travadas}/3)")
                if travadas >= 3:
                    print("      desisto - a tela nao responde ao clique")
                    break
                continue

            travadas = 0
            # A tela esta na pagina certa. A trilha do card ja tem o id do
            # assunto, entao leio SEMPRE - e de graca. Se por cima disso o app
            # tiver pedido a listagem, aproveito: ela vem completa e manda.
            j = corpo_da_ultima()
            de_onde = "tela"
            if j:
                ritmo.deu_certo()
                atualizadas += absorver_api(j)
                total_pags = (j.get("data") or {}).get("pages") or total_pags
                de_onde = "api"
            else:
                do_app.append(pag)
                atualizadas += absorver_tela(pagina.evaluate(JS_COLHER_CARDS))

            com_id = sum(1 for x in por_id.values() if x.get("assunto_ids"))
            print(f"    pagina {pag}/{total_pags} [{de_onde}]: "
                  f"{com_id}/{len(por_id)} com id  ·  faltam {len(fila) - 1}")
            fila.pop(0)

        if do_app:
            print(f"\n  {len(do_app)} pagina(s) vieram da tela, sem nenhuma "
                  f"requisicao (o app ja as tinha).")
    finally:
        tela.fechar()

    com_id = sum(1 for x in por_id.values() if x.get("assunto_ids"))
    da_api = sum(1 for x in por_id.values() if x.get("assuntos_origem") == "api")
    da_tela = sum(1 for x in por_id.values() if x.get("assuntos_origem") == "tela")
    faltando = len(por_id) - com_id
    print(f"\n  {com_id}/{len(por_id)} questoes com id de assunto")
    print(f"     {da_api} da API (lista completa) · {da_tela} da tela "
          f"(trilha principal; ~8% podem ter assunto secundario a menos)")
    if faltando:
        print(f"  {faltando} ainda sem id.")
        print("  Rode de novo - casa por id, nada se perde nem duplica.")
    return arq, atualizadas


def reenriquecer_filtro(pagina, rotulo, assuntos, niveis, progresso, por_pagina=100,
                        ritmo=None):
    """
    Completa um arquivo JA coletado com os campos que faltaram, relendo SO a
    listagem - sem refazer estatistica nem comentario, que sao o caro.

    Existe porque a 1a coleta guardou o NOME do assunto e jogou fora o ID, e
    o vinculo questao->aula (spec 3.3.1) depende do ID. Reler a listagem custa
    ~70 requisicoes por disciplina em vez das ~6.800 da coleta inteira.
    """
    pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
    arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"
    if not arq.exists():
        print(f"  [!] nao existe {arq.name} pra completar - pule ou colete primeiro")
        return None, 0

    d = json.loads(arq.read_text(encoding="utf-8"))
    por_id = {q.get("id"): q for q in (d.get("questoes") or [])}
    print(f"  {len(por_id)} questoes no arquivo; relendo so a listagem...")

    # Retoma de onde parou. Sem isso, um 429 no meio jogava fora tudo que ja
    # tinha sido atualizado - foi o que aconteceu na 1a tentativa.
    ritmo = ritmo or Ritmo()
    chave = f"reenriquecer|{rotulo}"
    ant = progresso["questoes"].get(chave) or {}
    pag = int(ant.get("proxima_pagina", 1))
    # "pagina 6" so quer dizer alguma coisa junto do TAMANHO da pagina: com
    # perPage=100 e a questao 501; com perPage=20 e a 101. Gravar o numero
    # sem o tamanho fez a retomada reprocessar 20 paginas ja feitas, com o
    # contador parado. Converto pela posicao do item, que nao depende disso.
    ant_por_pagina = int(ant.get("por_pagina") or por_pagina)
    if pag > 1 and ant_por_pagina != por_pagina:
        item = (pag - 1) * ant_por_pagina
        pag = item // por_pagina + 1
        print(f"  [i] a rodada anterior usava {ant_por_pagina} por pagina; "
              f"convertendo para {por_pagina} -> pagina {pag}")

    # Cinto de seguranca: o contador pode estar errado (foi gravado sem o
    # tamanho da pagina). O DADO nao mente - conto quantas ja tem id e
    # retomo dali. Fico com a MENOR das duas: repetir pagina e desperdicio,
    # pular pagina e questao que nunca recebe o vinculo.
    ja_com_id = sum(1 for x in por_id.values() if x.get("assunto_ids"))
    pag_pelo_dado = ja_com_id // por_pagina + 1
    if pag_pelo_dado != pag:
        print(f"  [i] o contador diz pagina {pag}, mas {ja_com_id} questoes ja tem id "
              f"(= pagina {pag_pelo_dado}). Fico na menor.")
        pag = min(pag, pag_pelo_dado)
    if pag > 1:
        print(f"  [i] retomando da pagina {pag} (questao ~{(pag-1)*por_pagina})")
    # comeca do ritmo que a rodada anterior tinha alcancado, nao do zero.
    # Nunca abaixo do piso: se a rodada passada rodava em 10s e o piso hoje e
    # 20s (porque medimos que 10s falha), o piso manda.
    if ant.get("ritmo_s"):
        restaurado = max(float(ant["ritmo_s"]), ritmo.piso)
        if restaurado != float(ant["ritmo_s"]):
            print(f"  [i] a vez passada rodava em {ant['ritmo_s']}s, abaixo do piso "
                  f"de {ritmo.piso:.0f}s - subindo para o piso")
        ritmo.atual = restaurado
        print(f"  [i] retomando o ritmo de {ritmo.atual:.0f}s")

    atualizadas, nao_achadas = 0, 0

    def gravar():
        d["questoes"] = list(por_id.values())
        d["reenriquecido_em"] = datetime.now().isoformat(timespec="seconds")
        salvar_json(arq, d)

    def marcar_progresso(proxima):
        progresso["questoes"][chave] = {
            "proxima_pagina": proxima,
            "por_pagina": por_pagina,      # sem isso o numero da pagina e ambiguo
            "rotulo": rotulo,
            "ritmo_s": round(ritmo.atual),
            "quando": datetime.now().isoformat(timespec="seconds"),
        }
        gravar_progresso(progresso)

    print(f"  Ritmo inicial {ritmo.atual:.0f}s; acelera a cada "
          f"{ritmo.acelera_a_cada} paginas limpas, recua no 429.")
    andamento = None

    while True:
        checar_bloqueio(pagina)
        try:
            cab = cabecalhos_api_questoes(pagina)      # token pode ter renovado
        except Parada:
            pass
        ritmo.esperar(pagina, f"pagina {pag}")
        try:
            j = buscar_json_q(pagina, montar_url_questoes(assuntos, niveis, pag, por_pagina))
            ritmo.deu_certo()
        except Parada as e:
            gravar()                       # nao perde o que ja foi atualizado
            marcar_progresso(pag)          # salva ja, caso voce interrompa na espera
            print(f"  (gravei as {atualizadas} ja atualizadas; retomo da pagina {pag})")
            if e.status == 429 and ritmo.levou_429(pagina, getattr(e, "retry_after", None)):
                # Regravar DEPOIS do recuo. Salvando so antes, o progresso
                # guardava o ritmo que acabou de falhar - e ao religar o
                # extrator voltava exatamente na velocidade que deu 429.
                marcar_progresso(pag)
                continue                   # desacelerou; tenta a MESMA pagina
            raise
        dd = j.get("data") or {}
        linhas = dd.get("rows") or []
        if not linhas:
            break
        for q in linhas:
            alvo = por_id.get(q.get("id"))
            if not alvo:
                nao_achadas += 1
                continue
            alvo["assuntos"] = [{
                "id": a.get("id"),
                "nome": a.get("nome_clean") or a.get("nome") or a.get("descricao"),
                "raiz": a.get("assunto_raiz"),
                "pai": a.get("pai"),
            } for a in (q.get("assuntos") or [])]
            alvo["assunto_ids"] = [a.get("id") for a in (q.get("assuntos") or []) if a.get("id")]
            atualizadas += 1

        gravar()              # grava a CADA pagina
        marcar_progresso(pag + 1)
        com_id = sum(1 for x in por_id.values() if x.get("assunto_ids"))
        if andamento is None:
            # o total aqui sao as questoes DO ARQUIVO que ainda faltam ganhar
            # o id, nao o acervo do Gran
            andamento = Andamento(len(por_id), ja_feitos=com_id, unidade="com id")
        else:
            andamento.feitos_agora = com_id - andamento.base
        print(andamento.linha(f"pagina {pag}/{dd.get('pages')} · ritmo {ritmo.atual:.0f}s"))

        if dd.get("pages") and pag >= dd["pages"]:
            break
        pag += 1

    com_id = sum(1 for x in por_id.values() if x.get("assunto_ids"))
    print(f"  {com_id}/{len(por_id)} questoes agora tem id de assunto"
          f"{f'; {nao_achadas} da listagem nao estavam no arquivo' if nao_achadas else ''}")
    return arq, atualizadas


def varrer_orgao(pagina, orgao_id, progresso, com_comentarios, por_pagina,
                 niveis=None, max_paginas=None, ritmo_questao=None):
    """
    Passada por ORGAO (spec §3.7): tudo que caiu na PM-BA / CBM-BA.

    Sem filtro de assunto nem de nivel - e o conjunto "caiu na prova real",
    que sustenta o tier S. Roda no mesmo ritmo da varredura por disciplina
    (uma listagem a cada ~100 estatisticas), que e o padrao que nunca deu 429.
    """
    chave = f"orgao|{orgao_id}|{','.join(niveis or [])}"
    anterior = progresso["questoes"].get(chave, {})
    pag = int(anterior.get("proxima_pagina", 1))
    if pag > 1:
        print(f"  [i] retomando da pagina {pag}")

    coletadas, vistos, arq, rotulo = [], set(), None, f"orgao {orgao_id}"
    total_geral = pags_total = None
    andamento = None
    # Ritmo das chamadas LEVES (estatistica/comentario, ~160 bytes cada).
    # Comeca em 1,5s e acelera a cada 20 questoes limpas ate 0,4s. Separado
    # do ritmo das listagens de proposito: sao cargas muito diferentes.
    ritmo_q = ritmo_questao or Ritmo(piso=0.4, acelera_a_cada=20, unidade="questoes")

    while True:
        checar_bloqueio(pagina)
        pausa(pagina, f"pagina {pag}")
        j = buscar_json_q(pagina, montar_url_questoes(
            None, niveis, pag, por_pagina, orgaos=[orgao_id]))
        d = j.get("data") or {}
        linhas = d.get("rows") or []

        if total_geral is None:
            # Antes de varrer: o filtro pegou mesmo?
            ok, motivo = conferir_filtro_orgao(linhas, [orgao_id])
            if not ok:
                raise Parada(
                    "o filtro de orgao NAO pegou.\n"
                    f"     {motivo}\n"
                    "     O parametro 'orgao[]' deve estar errado. Parei antes de\n"
                    "     sair varrendo o acervo inteiro achando que era da PM-BA."
                )
            print(f"  [ok] {motivo}")
            total_geral, pags_total = d.get("total"), d.get("pages")
            sigla = ((linhas[0].get("orgaos") or [{}])[0].get("sigla")
                     or (linhas[0].get("orgaos") or [{}])[0].get("nome") or rotulo)
            rotulo = f"orgao {sigla}".strip()
            porq = 2 if com_comentarios else 1
            print(f"  {sigla}: {total_geral} questoes / {pags_total} paginas")
            print(f"  ~{(total_geral or 0)*porq + (pags_total or 0)} requisicoes, "
                  f"~{((total_geral or 0)*porq + (pags_total or 0))*2.5/3600:.1f} horas")

            pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
            arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"
            if arq.exists():
                try:
                    velho = json.loads(arq.read_text(encoding="utf-8"))
                    coletadas = velho.get("questoes") or []
                    vistos = {x.get("id") for x in coletadas}
                    print(f"  [i] ja tinha {len(coletadas)} questoes neste arquivo")
                except Exception:
                    pass
            andamento = Andamento(total_geral, ja_feitos=len(coletadas))
            print(andamento.linha("comecando"))

        if not linhas:
            print("  Acabaram as questoes.")
            break

        # relido a cada pagina: numa varredura de horas o token expira e o
        # app renova sozinho no navegador (ler localStorage nao custa requisicao)
        try:
            cab = cabecalhos_api_questoes(pagina)
        except Parada:
            cab = None

        print(f"\n  --- pagina {pag}/{pags_total} ({len(linhas)} questoes) ---")
        for i, q in enumerate(linhas, 1):
            if q.get("id") in vistos:
                continue
            vistos.add(q.get("id"))
            try:
                item = extrair_questao(pagina, q, cab, com_comentarios, ritmo=ritmo_q)
                ritmo_q.deu_certo()
            except Parada as e:
                # 429 numa chamada leve: desacelera ESTE ritmo e refaz a
                # questao. E GET, entao repetir nao tem efeito colateral.
                if e.status == 429 and ritmo_q.levou_429(pagina, getattr(e, "retry_after", None)):
                    vistos.discard(q.get("id"))
                    item = extrair_questao(pagina, q, cab, com_comentarios, ritmo=ritmo_q)
                    vistos.add(q.get("id"))
                else:
                    raise
            coletadas.append(item)
            andamento.marcar()
            print(f"    {i:3d}. Q{item['id']}  {item.get('orgao_sigla')}  "
                  f"{item.get('ano')}  gab={item['gabarito'] or '?'}")
            # a cada 5, e nao so no fim da pagina: uma pagina leva ~2 min e
            # ficar esse tempo sem sinal de quanto falta e ruim de acompanhar
            if i % 5 == 0 and i < len(linhas):
                print(andamento.linha(f"pagina {pag}/{pags_total}"))

        salvar_json(arq, {
            "rotulo": rotulo,
            "tipo": "orgao",
            "orgao_id": orgao_id,
            "niveis": niveis or [],
            "total_no_gran": total_geral,
            "paginas_no_gran": pags_total,
            "paginas_coletadas": pag,
            "com_comentarios": com_comentarios,
            "questoes": coletadas,
            "coletado_em": datetime.now().isoformat(timespec="seconds"),
        })
        progresso["questoes"][chave] = {
            "proxima_pagina": pag + 1, "rotulo": rotulo,
            "arquivo": str(arq.relative_to(RAIZ)), "coletadas": len(coletadas),
            "quando": datetime.now().isoformat(timespec="seconds"),
        }
        gravar_progresso(progresso)
        print(andamento.linha(f"pagina {pag}/{pags_total}"))

        if pags_total and pag >= pags_total:
            print("  Era a ultima pagina.")
            break
        if max_paginas and (pag + 1 - int(anterior.get("proxima_pagina", 1))) >= max_paginas:
            print(f"  Parei no limite de {max_paginas} pagina(s).")
            break
        pag += 1

    print(f"  {ritmo_q.resumo('pausa por chamada')}")
    return arq, len(coletadas)


def varrer_filtro(pagina, rotulo, assuntos, niveis, progresso, com_comentarios,
                  por_pagina, perguntar_por_pagina, max_paginas=None, recomecar=False,
                  assunto_raiz=None):
    """
    Percorre um filtro de questoes, pagina a pagina, retomando de onde parou.

    Com 'assunto_raiz', a LISTAGEM vem pela tela (o app pede, eu escuto) e so
    as chamadas leves saem do meu cliente. E a divisao que importa: a listagem
    e a unica que leva 429, e as leves nunca levaram nem a 0,4s.
    """
    cab = cabecalhos_api_questoes(pagina)
    fonte = None
    if assunto_raiz:
        fonte = ListagemPelaTela(pagina, assuntos, niveis, por_pagina,
                                 assunto_raiz=assunto_raiz)
        est = fonte.preparar(montar_url_tela(assunto_raiz, niveis))
        if not est.get("ultima_pagina"):
            fonte.fechar()
            raise Parada("a tela nao mostrou paginacao - confira o --assunto")
        print(f"  listagem pela TELA (o app pede, eu escuto)")
    chave = f"{rotulo}|{','.join(assuntos)}|{','.join(niveis)}"
    anterior = {} if recomecar else progresso["questoes"].get(chave, {})
    if recomecar:
        print("  [i] --recomecar: ignorando o que ja tinha e comecando da pagina 1")

    pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
    arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"

    # Carrega o que ja existe. No modo normal, serve pra pular o que ja tem.
    # No --recomecar, serve pra NAO PERDER: as antigas ficam no arquivo e vao
    # sendo substituidas conforme chegam as novas. Antes eu zerava a lista, e
    # uma queda na pagina 30 deixava o arquivo com 3.000 de 6.821.
    por_id_exist, vistos = {}, set()
    if arq.exists():
        try:
            velho = json.loads(arq.read_text(encoding="utf-8"))
            por_id_exist = {x.get("id"): x for x in (velho.get("questoes") or [])}
            if recomecar:
                print(f"  [i] {len(por_id_exist)} questoes no arquivo serao ATUALIZADAS "
                      f"(nenhuma e apagada)")
            else:
                vistos = set(por_id_exist)
                print(f"  [i] ja tinha {len(por_id_exist)} questoes neste arquivo")
        except Exception:
            pass
    coletadas = list(por_id_exist.values())

    pag = int(anterior.get("proxima_pagina", 1))
    if pag > 1:
        print(f"  [i] retomando da pagina {pag}")

    total_geral = pags_total = None
    andamento = None
    while True:
        checar_bloqueio(pagina)
        # Reler o token a cada pagina. Numa varredura de horas ele expira, e
        # o app renova sozinho no navegador - se eu guardar o de 5h atras,
        # a coleta morre de 401 no meio da noite. Ler localStorage nao custa
        # requisicao nenhuma.
        try:
            cab = cabecalhos_api_questoes(pagina)
        except Parada:
            pass          # mantem o anterior se nao conseguir reler
        if fonte:
            # sem pausa longa aqui: quem pede e o app, e as questoes da pagina
            # anterior ja espacaram esta chamada por minutos
            j = fonte.buscar(pag)
        else:
            pausa(pagina, f"pagina {pag}")
            j = buscar_json_q(pagina,
                              montar_url_questoes(assuntos, niveis, pag, por_pagina))
        d = j.get("data") or {}
        linhas = d.get("rows") or []
        if total_geral is None:
            total_geral, pags_total = d.get("total"), d.get("pages")
            porq = 2 if com_comentarios else 1
            print(f"  {total_geral} questoes / {pags_total} paginas")
            print(f"  ~{(total_geral or 0) * porq + (pags_total or 0)} requisicoes, "
                  f"~{((total_geral or 0)*porq + (pags_total or 0))*2.5/3600:.1f} horas")
            andamento = Andamento(total_geral, ja_feitos=len(coletadas))
            print(andamento.linha("comecando"))
        if not linhas:
            print("  Acabaram as questoes.")
            break

        print(f"\n  --- pagina {pag}/{pags_total} ({len(linhas)} questoes) ---")
        for i, q in enumerate(linhas, 1):
            if q.get("id") in vistos:
                continue
            vistos.add(q.get("id"))
            item = extrair_questao(pagina, q, cab, com_comentarios)
            # substitui a versao antiga se ja existir; senao acrescenta
            if q.get("id") in por_id_exist:
                coletadas[coletadas.index(por_id_exist[q["id"]])] = item
            else:
                coletadas.append(item)
            por_id_exist[q.get("id")] = item
            if andamento:
                andamento.marcar()
            print(f"    {i:2d}. Q{item['id']}  gab={item['gabarito'] or '?'}  "
                  f"acerto={(item['indice_acerto'] or {}).get('percentual')}%")
            if andamento and i % 5 == 0 and i < len(linhas):
                print(andamento.linha(f"pagina {pag}/{pags_total}"))

        # grava a cada pagina: se cair, nao perde o que ja veio
        salvar_json(arq, {
            "rotulo": rotulo,
            "assuntos": assuntos,
            "niveis": niveis,
            "total_no_gran": total_geral,
            "paginas_no_gran": pags_total,
            "paginas_coletadas": pag,
            "com_comentarios": com_comentarios,
            "questoes": coletadas,
            "coletado_em": datetime.now().isoformat(timespec="seconds"),
        })
        progresso["questoes"][chave] = {
            "proxima_pagina": pag + 1, "rotulo": rotulo,
            "arquivo": str(arq.relative_to(RAIZ)), "coletadas": len(coletadas),
            "quando": datetime.now().isoformat(timespec="seconds"),
        }
        gravar_progresso(progresso)
        if andamento:
            print(andamento.linha(f"pagina {pag}/{pags_total}"))
        else:
            print(f"  salvo: {arq.name}  ({len(coletadas)} questoes)")

        if pags_total and pag >= pags_total:
            print("  Era a ultima pagina.")
            break
        # o limite tem que ser checado ANTES de te perguntar, senao ele
        # pergunta "ir para a pagina 2?" e so depois percebe que ja acabou
        if max_paginas and (pag + 1 - int(anterior.get("proxima_pagina", 1))) >= max_paginas:
            print(f"  Parei no limite de {max_paginas} pagina(s) que voce pediu.")
            break
        if perguntar_por_pagina and not confirmar(f"\n  Ir para a pagina {pag+1}?"):
            print("  Parando por sua conta. O progresso ficou gravado.")
            break
        pag += 1

    if fonte:
        print(f"\n  listagem: {fonte.resumo()}")
        fonte.fechar()
    return arq, len(coletadas)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# MODO CONTAR (Mike Mentor, spec v3 §3.1): quantas questoes cada filtro da
# ---------------------------------------------------------------------------

ARQ_CONTAGEM = DIR_SAIDA / "contagem_edital.json"


def _ler_filtros(arquivo):
    """Le o arquivo de filtros do edital. Formato:
        {"materias": [{"rotulo": "Língua Portuguesa",
                       "assuntos": [123, 456, ...],
                       "niveis": ["Médio"]}, ...]}
    """
    caminho = Path(arquivo)
    if not caminho.is_absolute():
        caminho = RAIZ / caminho
    if not caminho.exists():
        raise Parada(f"nao achei o arquivo de filtros: {caminho}")
    try:
        cfg = json.loads(caminho.read_text(encoding="utf-8"))
    except Exception as e:
        raise Parada(f"o arquivo de filtros nao e um JSON valido ({e})")
    materias = cfg.get("materias") if isinstance(cfg, dict) else None
    if not isinstance(materias, list) or not materias:
        raise Parada("o arquivo de filtros nao tem a lista 'materias'")
    limpas, avisos = [], []
    for m in materias:
        if not isinstance(m, dict):
            avisos.append("item que nao e objeto - ignorado")
            continue
        rot = str(m.get("rotulo") or "").strip()
        ass = sorted({str(a).strip() for a in (m.get("assuntos") or [])
                      if str(a).strip().isdigit()}, key=int)
        niv = [str(n).strip() for n in (m.get("niveis") or [NIVEL_PADRAO]) if str(n).strip()]
        if not rot or not ass:
            avisos.append(f"'{rot or '?'}' sem rotulo ou sem etiquetas - ignorado")
            continue
        limpas.append({"rotulo": rot, "assuntos": ass, "niveis": niv})
    for a in avisos:
        print(f"  [!] {a}")
    if not limpas:
        raise Parada("nenhuma materia valida no arquivo de filtros")
    return limpas


def contar_filtros(pagina, arquivo, progresso, pausa_min=20.0):
    """
    Conta quantas questoes cada filtro do edital devolve - UMA listagem por
    materia, perPage=20 (a mesma chamada que o app faz e que o --testar usa).
    Nao baixa questao nenhuma, nao pede estatistica, nao pede comentario.

    Conferencia antes de confiar no numero: pede primeiro o total SO com o
    filtro de nivel (sem etiqueta). Se o total de uma materia der igual a
    esse, o filtro de etiqueta nao pegou - paro em vez de gravar lixo.

    Retomavel: materia ja contada com as MESMAS etiquetas nao e pedida de novo.
    No 429 para na hora (regra da spec: bloqueio -> para), grava o que tem.
    """
    materias = _ler_filtros(arquivo)
    anteriores = {}
    if ARQ_CONTAGEM.exists():
        try:
            anteriores = json.loads(ARQ_CONTAGEM.read_text(encoding="utf-8")).get("materias") or {}
        except Exception:
            anteriores = {}
    resultado = dict(anteriores)

    def assinatura(m):
        return ",".join(m["assuntos"]) + "|" + ",".join(m["niveis"])

    fazer = [m for m in materias
             if (anteriores.get(m["rotulo"]) or {}).get("assinatura") != assinatura(m)]
    print(f"  Materias no arquivo: {len(materias)}   ja contadas: {len(materias) - len(fazer)}"
          f"   a contar: {len(fazer)}")
    if not fazer:
        print("  Nada a fazer - todas ja contadas com esses filtros.")
        return resultado
    espera = max(20.0, float(pausa_min or 20.0))
    print(f"  ~{len(fazer) + 1} listagens, uma a cada ~{espera:.0f}s "
          f"(~{(len(fazer) + 1) * espera / 60:.0f} min)")
    print("\n  Lembre: nao use o Gran em outra janela enquanto isto roda.")
    if not confirmar("\n  Comecar a contagem?"):
        print("  Ok, nao comecei.")
        return resultado

    def pedir(ass, niv, motivo):
        checar_bloqueio(pagina)
        pausa_espera = random.uniform(espera, espera * 1.15)
        print(f"      . {motivo} ({pausa_espera:.0f}s)")
        pagina.wait_for_timeout(int(pausa_espera * 1000))
        try:
            j = buscar_json_q(pagina, montar_url_questoes(ass, niv, 1, 20))
        except Parada as e:
            if e.status == 429:
                registrar_429(progresso)
                salvar_json(ARQ_CONTAGEM, {"gerado_em": datetime.now().isoformat(timespec="seconds"),
                                           "materias": resultado})
                raise Parada(
                    "o Gran respondeu 429 na contagem. Parei (regra: bloqueio -> para).\n"
                    f"     O que ja foi contado esta salvo em {ARQ_CONTAGEM.name}.\n"
                    "     Espere as 3 horas e rode o mesmo comando: ele continua de onde parou.",
                    status=429)
            raise
        d = (j or {}).get("data") or {}
        tot = d.get("total")
        if not isinstance(tot, int):
            raise Parada(f"a listagem nao trouxe o total ({motivo}); resposta: {str(j)[:160]!r}")
        return tot

    # 1) referencia: so o nivel, sem etiqueta
    niveis_ref = fazer[0]["niveis"]
    total_ref = pedir([], niveis_ref, f"total de referencia (so nivel {','.join(niveis_ref)})")
    print(f"  Referencia (sem etiqueta): {total_ref} questoes")

    soma = 0
    for i, m in enumerate(fazer, 1):
        tot = pedir(m["assuntos"], m["niveis"], f"[{i}/{len(fazer)}] {m['rotulo']}")
        if m["niveis"] == niveis_ref and tot >= total_ref:
            raise Parada(
                f"'{m['rotulo']}' deu {tot}, o mesmo que o acervo inteiro do nivel.\n"
                "     O filtro de etiqueta nao pegou. Parei antes de gravar um numero errado.")
        resultado[m["rotulo"]] = {
            "total": tot,
            "paginas_de_100": -(-tot // 100),
            "n_etiquetas": len(m["assuntos"]),
            "niveis": m["niveis"],
            "assinatura": assinatura(m),
            "contado_em": datetime.now().isoformat(timespec="seconds"),
        }
        salvar_json(ARQ_CONTAGEM, {"gerado_em": datetime.now().isoformat(timespec="seconds"),
                                   "referencia_nivel": {"niveis": niveis_ref, "total": total_ref},
                                   "materias": resultado})
        soma += tot
        print(f"    {m['rotulo']}: {tot} questoes ({len(m['assuntos'])} etiquetas)")

    print(f"\n  Contadas agora: {len(fazer)} materia(s), {soma} questoes no total.")
    print(f"  SALVO: {ARQ_CONTAGEM}")
    return resultado


# ---------------------------------------------------------------------------
# MODO COLETAR EDITAL (Mike Mentor, spec v3 §3.1): questoes de cada materia
# ---------------------------------------------------------------------------
#
# Filtro de cada materia, montado AQUI MESMO a partir do que ja esta no PC:
#   (a) as etiquetas dos cadernos de questoes de todas as aulas da materia
#       (saida/aulas/<materia>/*.json -> campo caderno_questoes);
#   (b) as etiquetas das questoes de prova de SOLDADO da passada por orgao
#       (saida/questoes/orgao */*.json), da mesma raiz da materia.
# A raiz da materia (etiqueta generica) sai do filtro: com ela, o filtro vira
# a materia inteira. A contagem sai de graca na 1a listagem de cada materia.
#
# Cortes (spec): etiqueta gigante (> 5.000 no filtro) -> so os ultimos 8
# anos; Atualidades -> so o ultimo ano e o corrente. A listagem vem do mais
# novo pro mais velho, entao o corte e so parar de paginar.
#
# Ritmo: o mesmo da passada por orgao, que rodou limpa - listagem de 20
# (a chamada que o app faz) e as chamadas leves no ritmo adaptativo ate 0,4s.
# Mais uma trava: nunca duas listagens a menos de LISTAGEM_MIN_S segundos
# (paginas cheias de questao ja coletada nao podem virar listagem em rajada).

MATERIAS_FORA_DA_COLETA = {"Direito Penal", "Direito Penal Militar",
                           "Redação Discursiva", "Treinamento Intensivo"}
ORDEM_MATERIAS = ["Língua Portuguesa", "Matemática", "História do Brasil",
                  "Geografia do Brasil", "Atualidades", "Informática",
                  "Direito Constitucional", "Direitos Humanos",
                  "Direito Administrativo", "Igualdade Racial e de Gênero"]
CORTE_GIGANTE = 5000
ANOS_GIGANTE = 8
LISTAGEM_MIN_S = 25.0
# Ritmo do caminho pela TELA. Cada pagina e um pedido do app ao Gran - o fato
# de nao sair do meu cliente nao a torna gratuita para a CONTA.
#
# A metrica que o servidor conta e REQUISICAO POR MINUTO, nao questao por
# minuto. Com 100 por pagina, 3s de pausa ainda dao ~11 req/min - parecia
# seguro e nao e. As referencias medidas:
#     ~55 req/min  -> 24/09, sem freio: 20x 429 e o Cloudflare barrando
#                     /v1/elastic/questao em duas sessoes diferentes
#     ~11 req/min  -> 3s entre paginas (a leitura literal da spec §3.1)
#      ~3 req/min  -> a passada PM-BA: 89 listagens seguidas, ZERO 429
# Ajustado pelo dono em 24/09 para ~7 req/min, que e o ritmo EXATO da rodada
# que colheu Direito Penal inteiro - 6.740 questoes, 69 paginas seguidas, zero
# 429. Eu tinha sugerido 8s (~5 req/min) por prudencia depois dos bloqueios do
# dia; ele preferiu o numero que ja tem prova de ter funcionado, e a prova
# existe mesmo. Se aparecer 429 nesta faixa, subir de volta pra 8.
#
# A CONTA: o ciclo de uma pagina e pausa*1,15 (jitter) + ~2s de
# espera da resposta. 5,5s -> ~7 req/min. Cheguei a por 4,0s
# chamando de '7 req/min' e era 9: esqueci a espera de resposta na
# conta. Numero com rotulo errado e pior que numero alto.
# Etiqueta que sozinha traz mais que isto e larga demais pra ligar questao a
# aula - e ruido, igual a raiz da materia. Medido em 24/09: a 1a etiqueta de
# Lingua Portuguesa trazia 122.587 questoes, o acervo quase inteiro da
# materia. Coletar isso custa horas e nao melhora o vinculo.
LIMITE_ETIQUETA = 3000
# quanto esperar uma pagina chegar antes de desistir dela
ESPERA_PAGINA_S = 45.0
PAUSA_PAGINA_TELA = 5.5
PAUSA_BLOCO_TELA = 14.0
_ULTIMA_LISTAGEM = [0.0]


def _listar_com_trava(pagina, url, motivo):
    """Uma listagem, respeitando o intervalo minimo desde a anterior."""
    falta = LISTAGEM_MIN_S - (time.monotonic() - _ULTIMA_LISTAGEM[0])
    if falta > 0:
        s = falta + random.uniform(0.5, 3.0)
        print(f"      . {motivo} (esperando {s:.0f}s: intervalo minimo entre listagens)")
        pagina.wait_for_timeout(int(s * 1000))
    else:
        pausa(pagina, motivo)
    checar_bloqueio(pagina)
    try:
        return buscar_json_q(pagina, url)
    finally:
        _ULTIMA_LISTAGEM[0] = time.monotonic()


def _amostra_pela_tela(pagina, etiquetas, niveis, por_pagina, motivo):
    """
    Uma pagina de amostra do filtro, pela tela - zero requisicao minha.

    Serve pro passo que descobre a raiz da materia. Devolve o mesmo formato
    que `buscar_json_q` devolveria, pra quem chama nao precisar saber de onde
    veio.
    """
    print(f"      . {motivo} (pela tela)")
    fonte = ListagemPelaTela(pagina, list(etiquetas), niveis, por_pagina,
                             assunto_raiz=",".join(str(e) for e in etiquetas))
    try:
        fonte.preparar(montar_url_tela(list(etiquetas), niveis))
        return fonte.buscar(1)
    finally:
        fonte.fechar()


def _e_bloqueio(e):
    """
    Distingue 'nao consegui esta pagina' de 'o Gran esta me barrando'.

    So o segundo justifica parar tudo. Misturar os dois faz o coletor ou
    desistir a toa, ou insistir contra um bloqueio - os dois custam caro.
    """
    if getattr(e, "status", None) in (401, 403, 429):
        return True
    t = str(e).lower()
    return any(x in t for x in ("captcha", "bloqueio", "bloquead",
                                "desconect", "simultane", "limite de taxa",
                                "sessao"))


def _ler_pacote(caminho, so_disciplina=None):
    """
    Le o pacote_coleta.json (gerado por preparar_coleta_vps.py).

    Devolve o mesmo trio que as tres funcoes que varrem as pastas:
    ({materia: set(etiquetas)}, {raiz: set(etiquetas)}, set(ids ja coletados)).
    """
    arq = Path(caminho)
    if not arq.is_absolute():
        arq = RAIZ / arq
    if not arq.exists():
        raise Parada(f"nao achei o pacote: {arq}\n"
                     "     gere no PC com: python preparar_coleta_vps.py")
    d = json.loads(arq.read_text(encoding="utf-8"))
    por_mat = {}
    for m in d.get("materias") or []:
        rot = str(m.get("rotulo") or "").strip()
        if not rot or rot in MATERIAS_FORA_DA_COLETA:
            continue
        if so_disciplina and so_disciplina.lower() not in rot.lower():
            continue
        ids = {int(a) for a in (m.get("assuntos") or [])
               if str(a).strip().isdigit()}
        if ids:
            por_mat[rot] = ids
    soldado = {int(k): {int(x) for x in v}
               for k, v in (d.get("soldado_por_raiz") or {}).items()}
    ja = {int(x) for x in (d.get("ja_coletadas") or [])}
    print(f"  pacote de {arq.name}: {len(por_mat)} materia(s), "
          f"{len(ja)} questoes ja coletadas (gerado em {d.get('gerado_em')})")
    return por_mat, soldado, ja


_NOMES_DE_ASSUNTO = {}
# materia -> nomes de disciplina que as AULAS declaram. E a lista contra a
# qual eu reconheco a "etiqueta que e a disciplina inteira" (a 77 = Historia).
_DISCIPLINAS_DAS_AULAS = {}


def _carregar_disciplinas():
    """Le o campo `disciplina` das aulas, agrupado por materia."""
    global _DISCIPLINAS_DAS_AULAS
    if _DISCIPLINAS_DAS_AULAS:
        return _DISCIPLINAS_DAS_AULAS
    base = DIR_SAIDA / "aulas"
    if base.exists():
        for arq in base.glob("*/*.json"):
            try:
                d = json.loads(arq.read_text(encoding="utf-8"))
            except Exception:
                continue
            mat = d.get("materia")
            if not mat:
                continue
            # SO `disciplina`, NUNCA `materia`.
            #
            # Com `materia` na lista, a etiqueta "Historia do Brasil" (397241)
            # casava com o nome da propria materia e era removida como se
            # fosse generica - e ela e a etiqueta PRINCIPAL do conteudo. A
            # regra existe pra tirar a DISCIPLINA ("Historia", id 77), que e
            # um nivel acima e traz tudo, inclusive Historia Geral.
            alvo = _DISCIPLINAS_DAS_AULAS.setdefault(mat, set())
            if d.get("disciplina") and d.get("disciplina") != d.get("materia"):
                alvo.add(d["disciplina"])
    return _DISCIPLINAS_DAS_AULAS


def _carregar_nomes_de_assunto():
    """
    Mapa id -> nome, montado das questoes que ja estao no disco.

    E o que permite podar os estados sem pedir a arvore ao Gran (que custaria
    ~5h de requisicao). Cobre so o que ja apareceu em questao coletada - o
    resto e pego em tempo de coleta.
    """
    global _NOMES_DE_ASSUNTO
    if _NOMES_DE_ASSUNTO:
        return _NOMES_DE_ASSUNTO
    # o que ficou guardado de rodadas anteriores vem primeiro: assim a
    # nomeacao nao se repete a cada reinicio
    try:
        _NOMES_DE_ASSUNTO.update(ler_progresso().get("nomes_de_assunto") or {})
    except Exception:
        pass
    base = DIR_SAIDA / "questoes"
    if base.exists():
        for arq in base.glob("*/*.json"):
            try:
                d = json.loads(arq.read_text(encoding="utf-8"))
            except Exception:
                continue
            for q in d.get("questoes") or []:
                for a in (q.get("assuntos") or []):
                    if isinstance(a, dict) and a.get("id") is not None:
                        _NOMES_DE_ASSUNTO.setdefault(
                            str(a["id"]), a.get("titulo") or a.get("nome"))
    return _NOMES_DE_ASSUNTO


def _ids_ja_coletados():
    """Ids de todas as questoes que ja estao em saida/questoes (qualquer arquivo)."""
    ids = set()
    base = DIR_SAIDA / "questoes"
    if not base.exists():
        return ids
    for arq in base.rglob("*.json"):
        try:
            d = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        for q in (d.get("questoes") or []):
            if q.get("id") is not None:
                ids.add(q.get("id"))
    return ids


def _filtros_das_aulas(so_disciplina=None):
    """{materia: set(ids)} a partir dos cadernos das aulas ja coletadas."""
    base = DIR_SAIDA / "aulas"
    if not base.exists():
        raise Parada("nao achei saida/aulas - rode o --curso antes")
    por_mat = {}
    for arq in base.rglob("*.json"):
        try:
            d = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        mat = str(d.get("disciplina") or "").strip()
        if not mat or mat in MATERIAS_FORA_DA_COLETA:
            continue
        if so_disciplina and so_disciplina.lower() not in mat.lower():
            continue
        ids = assuntos_do_caderno(d.get("caderno_questoes"))
        if ids:
            por_mat.setdefault(mat, set()).update(ids)
    return por_mat


def _etiquetas_de_soldado():
    """{raiz: set(ids)} das questoes de prova de Soldado das passadas por orgao."""
    por_raiz = {}
    base = DIR_SAIDA / "questoes"
    if not base.exists():
        return por_raiz
    for pasta in base.iterdir():
        if not pasta.is_dir() or not pasta.name.lower().startswith("orgao"):
            continue
        for arq in pasta.glob("*.json"):
            try:
                d = json.loads(arq.read_text(encoding="utf-8"))
            except Exception:
                continue
            for q in (d.get("questoes") or []):
                if "soldado" not in str(q.get("cargo") or "").lower():
                    continue
                for a in (q.get("assuntos") or []):
                    aid, raiz = a.get("id"), a.get("raiz")
                    if aid and raiz and str(aid) != str(raiz):
                        por_raiz.setdefault(str(raiz), set()).add(str(aid))
    return por_raiz


def _raiz_dominante(linhas, filtro):
    """A raiz mais comum entre as etiquetas do filtro que aparecem nas questoes."""
    c = Counter()
    for q in linhas:
        for a in (q.get("assuntos") or []):
            if str(a.get("id")) in filtro and a.get("assunto_raiz"):
                c[str(a.get("assunto_raiz"))] += 1
    return c.most_common(1)[0][0] if c else None


# Tamanho da consulta: o 1o teste real (25/09) morreu na PRIMEIRA listagem de
# Portugues com "Failed to fetch". Nao era 429 - era a 1a chamada da sessao.
# Com 118 etiquetas a query passa de ~2.100 bytes, e firewall de nuvem (o
# conjunto padrao da AWS, por exemplo) barra query acima de 2.048 bytes com
# uma resposta sem CORS, que o navegador mostra como "Failed to fetch".
# Entao a materia vai em BLOCOS de etiquetas, cada consulta bem abaixo disso.
ETIQUETAS_POR_BLOCO = 50
QUERY_MAX_BYTES = 1500


def _blocos(etiquetas, niveis, por_pagina):
    """Divide as etiquetas em blocos cuja consulta fica abaixo de QUERY_MAX_BYTES."""
    ets = sorted({str(e) for e in etiquetas}, key=int)
    blocos, atual = [], []
    for e in ets:
        teste = atual + [e]
        q = montar_url_questoes(teste, niveis, 99999, por_pagina).split("?", 1)[1]
        if atual and (len(teste) > ETIQUETAS_POR_BLOCO or len(q.encode("utf-8")) > QUERY_MAX_BYTES):
            blocos.append(atual)
            atual = [e]
        else:
            atual = teste
    if atual:
        blocos.append(atual)
    return blocos


# Quantas etiquetas antes de trocar a aba. Medido em 24/09: o ciclo por
# pagina subiu de 8,0s (bloco 45) para 14,0s (bloco 75) - 75% mais lento, com
# o freio FIXO em 3s. Ou o navegador esta pesado de DOM e cache do app, ou o
# servidor esta estrangulando sem dizer. As duas explicam o numero; so uma e
# consertavel daqui. `_medir_ritmo` mede antes e depois pra decidir qual e.
RENOVAR_ABA_A_CADA = 25

# segundos por pagina desde a ultima troca de aba
_CICLOS = []


def _medir_ritmo(limpar=True):
    """Media de segundos por pagina desde a ultima medida."""
    global _CICLOS
    if not _CICLOS:
        return None
    m = sum(_CICLOS) / len(_CICLOS)
    n = len(_CICLOS)
    if limpar:
        _CICLOS = []
    return m, n


def renovar_aba(pagina):
    """
    Fecha a aba e abre outra, no MESMO navegador.

    Nao mexe em login nem na conexao: a sessao vive no perfil, nao na aba. O
    que morre e o que pesa - DOM de milhares de cards, heap do Vue e o cache
    em memoria do app, que so cresce filtro apos filtro.
    """
    medida = _medir_ritmo()
    if medida:
        print(f"      . ritmo das ultimas {medida[1]} paginas: "
              f"{medida[0]:.1f}s por pagina")
    try:
        nova = pagina.context.new_page()
    except Exception as e:
        print(f"      [!] nao consegui abrir aba nova ({str(e)[:50]}); sigo nesta")
        return pagina
    try:
        pagina.close()
    except Exception:
        pass
    print("      . aba trocada (DOM e cache do app zerados)")
    return nova


def _coletar_bloco(pagina, mat, k, n_blocos, bloco, niveis, por_pagina, progresso,
                   g, arq, coletadas, vistos, ritmo_q, ano_atual, prazo, horas,
                   adiar_leves=False):
    """Pagina um bloco de etiquetas. Devolve True se terminou o bloco."""
    # O TAMANHO DA PAGINA ENTRA NA CHAVE. Sem ele, trocar --por-pagina faz o
    # progresso retomar num numero que nao quer mais dizer nada: com 20 por
    # pagina eram 64 paginas, com 100 sao 13, e a retomada na 19 morreu com
    # "a pagina 19 nao esta na barra". Mesmo erro ja cometido hoje de manha no
    # colher_da_tela - guardar pagina sem guardar o tamanho dela.
    chave = f"edital|{mat}|pp{por_pagina}|{','.join(bloco)}"
    anterior = progresso["questoes"].get(chave, {})
    if anterior.get("completo"):
        return True
    # `puladas` NAO pula mais nada.
    #
    # Essa lista nasceu do corte por tamanho (>3.000), que o dono mandou tirar
    # em 24/09: os blocos grandes sao os assuntos CENTRAIS e tem que vir. Eu
    # tirei o corte mas deixei esta leitura aqui, e as duas se anulavam - o
    # log mostrava "bloco 1/149: 5361 questoes - larga demais, ja medida
    # antes" justamente nos blocos que ele mandou coletar. A lista vira so
    # historico; quem limita agora e o filtro de banca e o corte de ano.
    pag = int(anterior.get("proxima_pagina", 1))
    total = pags = ano_min = None
    andamento = None
    rotulo = f"edital {mat}"

    # A LISTAGEM VEM PELA TELA, nao pelo meu cliente.
    #
    # Antes era `_listar_com_trava` -> buscar_json_q, com 25s de intervalo
    # minimo entre paginas. Duas consequencias ruins: a passada inteira ficava
    # refem da cota que leva 429 (8 no dia em que isto foi trocado, cada um
    # custando 3h de castigo), e 300+ paginas viravam 2h so de espera.
    # Pela tela, quem pede e o app - eu leio a resposta do cache dele no
    # localStorage. Medido: 426 questoes em 5 paginas, ZERO requisicoes
    # minhas, rodando DENTRO de uma penalidade ativa.
    # O que ainda passa pelo meu cliente e a estatistica por questao (leve,
    # 0,4s), que e o que `extrair_questao` faz logo abaixo.
    fonte = ListagemPelaTela(pagina, bloco, niveis, por_pagina,
                             assunto_raiz=",".join(str(e) for e in bloco),
                             sem_cliente=adiar_leves)
    try:
        if PAUSA_BLOCO_TELA:
            e = random.uniform(PAUSA_BLOCO_TELA, PAUSA_BLOCO_TELA * 1.3)
            print(f"      . trocando de etiqueta em {e:.0f}s")
            pagina.wait_for_timeout(int(e * 1000))
        est = fonte.preparar(montar_url_tela(
            bloco, niveis, bancas=sorted(BANCAS_PERMITIDAS)))
        na_tela = int(re.sub(r"\D", "",
                             str(est.get("contagem") or "").split("quest")[0]) or 0)
        # o corte de ano e decidido ANTES de paginar, pelo numero que a tela
        # mostra - assim nao preciso comecar, descobrir e recomecar
        if "atualidade" in mat.lower():
            ano_min = ano_atual - 1
        elif na_tela > CORTE_GIGANTE:
            ano_min = ano_atual - ANOS_GIGANTE
        # NADA MAIS E PULADO POR TAMANHO.
        #
        # O corte de 3.000 existia porque sem filtro de banca a etiqueta
        # central de cada materia trazia o acervo inteiro (403587 tinha
        # 122.561). Com as 14 bancas do edital o bloco encolhe, e esses eram
        # justamente os assuntos CENTRAIS - deixa-los de fora era o pior
        # recorte possivel. O corte de ano (>5.000 -> 2018+) continua, e
        # agora vale sobre o total JA FILTRADO, que e o que interessa.
        if ",".join(bloco) in (g.get("puladas") or {}):
            # sobra de quando o corte por tamanho existia
            g["puladas"].pop(",".join(bloco), None)
            gravar_progresso(progresso)
        if ano_min:
            est = fonte.preparar(montar_url_tela(
                bloco, niveis, anos=list(range(ano_min, ano_atual + 1)),
                bancas=sorted(BANCAS_PERMITIDAS)))

        # A PAGINA RETOMADA TEM QUE EXISTIR NESTA PAGINACAO.
        #
        # A chave do progresso carrega o tamanho que eu PEDI (pp100), mas
        # houve rodada em que a tela ficou em 20 e o numero gravado nasceu de
        # 470/20 = 24 paginas. Retomar na 22 com 5 paginas na tela mandava o
        # coletor "caminhar da 1 ate a 22" atras de uma pagina que nao existe.
        # A tela sabe quantas sao; pergunto a ela em vez de confiar no que
        # ficou gravado.
        ultima = est.get("ultima_pagina") if isinstance(est, dict) else None
        if isinstance(ultima, int) and ultima > 0 and pag > ultima:
            print(f"      . o progresso dizia pagina {pag}, mas esta "
                  f"paginacao tem {ultima} - recomeco o bloco "
                  f"(sobra de quando a tela caiu pra 20 por pagina)")
            pag = 1
            progresso["questoes"].pop(chave, None)

        # O NOME SO APARECE NA 1a PAGINA.
        #
        # Das 157 etiquetas de Historia/Geografia eu so conhecia 47 nomes; as
        # outras nunca tinham aparecido em questao coletada. Em vez de pedir a
        # arvore ao Gran (~5h), leio o nome aqui: se for estado que nao e
        # Bahia, largo a etiqueta agora e sigo. Custa 1 pagina por estado,
        # uma unica vez - depois fica gravado.
        if len(bloco) == 1 and any(t in (mat or "").lower()
                                   for t in MATERIAS_COM_ESTADO):
            _carregar_nomes_de_assunto()
            nome_etq = _NOMES_DE_ASSUNTO.get(str(bloco[0]))
            if nome_etq is None:
                nome_etq = _nome_da_etiqueta_na_tela(fonte, bloco[0])
            if e_estado_fora_do_edital(mat, nome_etq):
                print(f"    bloco {k}/{n_blocos}: '{nome_etq}' - estado fora "
                      f"do edital, PULANDO")
                g.setdefault("estados_removidos", {})[str(bloco[0])] = nome_etq
                gravar_progresso(progresso)
                return True

        terminou = _coletar_paginas(
            pagina, mat, k, n_blocos, bloco, niveis, por_pagina, progresso, g,
            arq, coletadas, vistos, ritmo_q, ano_atual, prazo, horas, fonte,
            chave, pag, ano_min, rotulo, adiar_leves)
    finally:
        fonte.fechar()
    if not terminou:          # parou pelo limite de horas: NAO marcar completo
        return False
    progresso["questoes"][chave] = {**progresso["questoes"].get(chave, {}),
                                    "completo": True}
    gravar_progresso(progresso)
    return True


def _coletar_paginas(pagina, mat, k, n_blocos, bloco, niveis, por_pagina,
                     progresso, g, arq, coletadas, vistos, ritmo_q, ano_atual,
                     prazo, horas, fonte, chave, pag, ano_min, rotulo,
                     adiar_leves=False):
    """O laco de paginas de um bloco. Separado so pra caber o `finally` que
    fecha a sessao de CDP da tela sem aninhar o corpo inteiro."""
    total = pags = None
    andamento = None
    while True:
        t_pagina = time.monotonic()
        checar_bloqueio(pagina)
        j = fonte.buscar(pag)
        d = j.get("data") or {}
        linhas = d.get("rows") or []
        if total is None:
            total, pags = d.get("total"), d.get("pages")
            if not isinstance(total, int):
                raise Parada(f"a listagem de '{mat}' nao trouxe o total: {str(d)[:160]!r}")
            g.setdefault("blocos", {})[str(k)] = {"etiquetas": len(bloco),
                                                  "total_no_gran": total,
                                                  "ano_min": ano_min}
            gravar_progresso(progresso)
            print(f"    bloco {k}/{n_blocos}: {total} questoes ({len(bloco)} etiquetas)"
                  + (f" -> corte: so de {ano_min} em diante" if ano_min else ""))
            # ja_feitos: numa retomada as paginas anteriores JA estao no
            # arquivo (gravo a cada pagina). Sem isto a barra recomeca do
            # zero e parece que o trabalho anterior se perdeu - o dono
            # perguntou "ue, coletou as 271?" justamente por isso.
            feitas = max(0, (pag - 1) * por_pagina)
            andamento = Andamento(total, ja_feitos=min(feitas, total))
        if not linhas:
            break
        # TRAVA DE TAMANHO: numa pagina que nao e a ultima, o numero de linhas
        # tem que ser o por_pagina pedido. Quando nao e, a paginacao esta
        # contando 100 e eu estou lendo 20 - de cada 100 questoes eu levaria
        # 20 e pularia 80, sem nenhum erro aparecendo. Foi o que aconteceu na
        # 1a rodada longa (24/09): "pagina 1/1226" com "20/20 da pagina".
        # O tamanho que vale e o que a FONTE esta usando: quando o seletor nao
        # pega, ela se adapta ao tamanho da tela, e comparar com o que eu PEDI
        # faria esta trava disparar num caso correto.
        pp_real = getattr(fonte, "por_pagina", por_pagina)
        if pags and pag < pags and len(linhas) != pp_real:
            raise Parada(
                f"a pagina {pag} veio com {len(linhas)} questoes, mas a "
                f"paginacao esta em {pp_real} por pagina.\n"
                "     Parei: seguir assim pularia o resto de cada pagina.\n"
                "     O progresso esta gravado.")
        try:
            cab = cabecalhos_api_questoes(pagina)
        except Parada:
            cab = None
        velhas = 0
        print(f"\n  --- {mat} bloco {k}/{n_blocos}: pagina {pag}/{pags} ---")
        for i, q in enumerate(linhas, 1):
            ano = (q.get("anos") or [None])[0]
            if ano_min and isinstance(ano, int) and ano < ano_min:
                velhas += 1
                continue
            andamento.marcar()
            if q.get("id") in vistos:
                continue
            vistos.add(q.get("id"))
            if adiar_leves:
                # so o que a listagem ja traz (enunciado, alternativas,
                # gabarito, etiquetas). A estatistica - unica chamada que
                # ainda sairia do meu cliente - fica pra --completar-leves.
                # Com isto a passada inteira roda durante uma penalidade.
                item = montar_questao(q)
                item["leves_pendentes"] = True
                coletadas.append(item)
                if i % 20 == 0 or i == len(linhas):
                    print(f"    {i:2d}/{len(linhas)} da pagina  "
                          f"(leves adiadas)")
                continue
            try:
                item = extrair_questao(pagina, q, cab, False, ritmo=ritmo_q)
                ritmo_q.deu_certo()
            except Parada as e:
                if e.status == 429 and ritmo_q.levou_429(pagina, getattr(e, "retry_after", None)):
                    item = extrair_questao(pagina, q, cab, False, ritmo=ritmo_q)
                else:
                    raise
            coletadas.append(item)
            print(f"    {i:2d}. Q{item['id']}  {item.get('ano')}  gab={item['gabarito'] or '?'}")

        salvar_json(arq, {
            "rotulo": rotulo, "tipo": "edital", "materia": mat,
            "raiz": g.get("raiz"), "assuntos": g.get("assuntos"), "niveis": niveis,
            "blocos": g.get("blocos"), "com_comentarios": False,
            "questoes": coletadas,
            "coletado_em": datetime.now().isoformat(timespec="seconds"),
        })
        progresso["questoes"][chave] = {
            "proxima_pagina": pag + 1, "rotulo": rotulo,
            "arquivo": str(arq.relative_to(RAIZ)), "coletadas": len(coletadas),
            "quando": datetime.now().isoformat(timespec="seconds"),
        }
        g["coletadas"] = len(coletadas)
        gravar_progresso(progresso)
        print(andamento.linha(f"bloco {k}/{n_blocos}, pagina {pag}/{pags}"))

        if velhas and velhas == len(linhas):
            print(f"    Pagina inteira antes de {ano_min}: o resto do bloco e mais velho. Fim do bloco {k}.")
            break
        if pags and pag >= pags:
            break
        if prazo and time.monotonic() > prazo:
            print(f"\n  Limite de {horas:g}h atingido em {mat}, bloco {k}. "
                  "Rode o mesmo comando pra continuar dali.")
            return False
        pag += 1
        # FREIO ENTRE PAGINAS.
        #
        # Ao trocar a listagem do meu cliente pela tela, levei junto o
        # `_listar_com_trava` (25s entre listagens) e nao pus nada no lugar -
        # a coleta foi pra 1.100 questoes/min. Cada pagina continua sendo um
        # pedido ao Gran mesmo vindo pelo app: "de graca pra minha cota" nao e
        # "de graca pra conta". O resultado foi o Cloudflare barrar
        # /v1/elastic/questao, primeiro no Chrome e depois no Firefox.
        # A spec §3.1 e clara: pausa entre PAGINAS, sem paralelismo.
        _CICLOS.append(time.monotonic() - t_pagina)
        espera = random.uniform(PAUSA_PAGINA_TELA, PAUSA_PAGINA_TELA * 1.3)
        print(f"      . proxima pagina em {espera:.1f}s")
        pagina.wait_for_timeout(int(espera * 1000))
    return True


def preparar_firefox(ctx, paginas, minutos=10):
    """
    Deixa o Firefox pronto: aba do Gran Questoes aberta e sessao valida.

    No Chrome era VOCE quem deixava a janela aberta e logada, e eu so me
    conectava. No Firefox eu e que lanco o navegador, entao a janela fecha
    quando o script termina - o login precisa ficar no PERFIL, e a primeira
    vez tem que esperar voce digitar. Eu nunca recebo a senha: so olho se o
    token apareceu.
    """
    pg = paginas[0] if paginas else ctx.new_page()
    if "questoes.grancursosonline" not in pg.url:
        pg.goto(f"{HOST_TELA_Q}/aluno/filtro/concursos",
                wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(3000)

    def logado():
        try:
            return bool(pg.evaluate("() => localStorage.getItem('auth/token')"))
        except Exception:
            return False

    if not logado():
        print("\n" + "!" * 70)
        print("  PRIMEIRA VEZ NESTE PERFIL: faca login na janela do Firefox.")
        print("  Eu nao recebo a senha - so espero o token aparecer.")
        print(f"  Espero ate {minutos} min. O login fica salvo em {PERFIL_FIREFOX}.")
        print("!" * 70 + "\n")
        for i in range(minutos * 12):
            pg.wait_for_timeout(5000)
            if logado():
                print("  Login detectado. Seguindo.")
                break
            if i and i % 12 == 0:
                print(f"     ... esperando o login ({i // 12} min)")
        else:
            raise Parada("nao detectei login no Firefox dentro do tempo")

    if "questoes.grancursosonline" not in pg.url:
        pg.goto(f"{HOST_TELA_Q}/aluno/filtro/concursos",
                wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(3000)
    return [x for x in ctx.pages if x.url.startswith("http")] or [pg]


def etiquetas_suspeitas(coletadas, filtro, minimo=40, limite=0.55):
    """
    Acha a etiqueta que arrastou disciplina alheia pra dentro do filtro.

    Isto existe para SUBSTITUIR a vigilancia manual. Uma etiqueta por consulta
    deixava ver a olho nu quando o Gran pendurou a tag errada no caderno (a
    407288 puxou "Direito Sanitario e Saude" inteiro pra Direitos Humanos),
    mas custava 2,2x mais requisicao e +2,3h so trocando de filtro. O dado ja
    denuncia sozinho: se as questoes que vieram por uma etiqueta quase nunca
    casam com NENHUMA outra do mesmo filtro, ela nao pertence a esta materia -
    esta sozinha num canto, puxando outro assunto.

    Nao gasta requisicao: le o que ja esta na memoria.
    """
    filtro = {str(f) for f in filtro}
    por_etq = {}
    for q in coletadas:
        ids = {str(x) for x in (q.get("assunto_ids") or [])}
        casadas = ids & filtro
        for e in casadas:
            d = por_etq.setdefault(e, {"n": 0, "sozinhas": 0})
            d["n"] += 1
            if len(casadas) == 1:
                d["sozinhas"] += 1
    suspeitas = []
    for e, d in por_etq.items():
        if d["n"] < minimo:
            continue
        fracao = d["sozinhas"] / d["n"]
        if fracao >= limite:
            suspeitas.append((e, d["n"], fracao))
    return sorted(suspeitas, key=lambda x: -x[2])


def _nome_e_vizinhos(coletadas, etq):
    """Como a etiqueta se chama e com que assuntos ela costuma vir."""
    nome, viz = None, Counter()
    for q in coletadas:
        if etq not in [str(x) for x in (q.get("assunto_ids") or [])]:
            continue
        for a in (q.get("assuntos") or []):
            if not isinstance(a, dict):
                continue
            t = a.get("titulo") or a.get("nome")
            if str(a.get("id")) == etq:
                nome = nome or t
            elif t:
                viz[t] += 1
    return nome, [k for k, _ in viz.most_common(3)]


def _avisar_suspeitas(mat, coletadas, filtro):
    """
    Avisa com NOME e vizinhanca - so "isolada" nao decide nada.

    A 1a versao imprimia id e porcentagem, e dos 4 avisos 3 eram falso
    positivo: "Declaracao Universal dos Direitos Humanos" aparece isolada
    porque e assunto folha, nao porque esta no lugar errado. Com o nome ao
    lado o caso verdadeiro salta aos olhos - foi assim que apareceu a 406410,
    "Lei Maria da Penha", com 811 questoes DENTRO de Lingua Portuguesa.
    """
    sus = etiquetas_suspeitas(coletadas, filtro)
    if not sus:
        return
    print(f"\n  [?] {len(sus)} etiqueta(s) de '{mat}' vieram isoladas - "
          f"confira se pertencem a materia:")
    for e, n, fr in sus[:8]:
        nome, viz = _nome_e_vizinhos(coletadas, e)
        print(f"      {e}  {str(nome)[:54]}")
        print(f"         {n} questoes, {fr*100:.0f}% isoladas; vem com: "
              f"{', '.join(v[:28] for v in viz) or '-'}")
    print("      (nada foi descartado - assunto folha tambem aparece isolado)")


# ESTADOS: SO A BAHIA (decisao do dono, 24/09).
#
# Em "Historia dos Estados Brasileiros" e no ramo equivalente de Geografia so
# interessa a Bahia - o edital da PM-BA nao pede os outros, e nas provas de
# Soldado so aparece etiqueta baiana. As revoltas regenciais (Farroupilha,
# Cabanagem, Balaiada) nao se perdem: vem por "Periodo Regencial" [433737],
# que e nacional.
PAI_DOS_ESTADOS = {"397244"}          # "Historia dos Estados Brasileiros"
# Sao DOIS pais, um em cada materia - os prints do dono mostraram a arvore:
#     Historia dos Estados Brasileiros      -> 4.21.5. Bahia - BA
#     Geografia Especifica dos Estados e DF -> 1.11.5. Bahia - BA
# Do segundo eu nao tenho o id, so o nome. Cortar por NOME resolve os dois e
# ainda pega qualquer outro ramo de estado que apareca depois. Isso importa
# porque incluir o pai arrasta TODOS os filhos: o app expande pai em filhos
# antes de consultar.
# Os PAIS dos ramos regionais. Sao varios e eu so conheco o id de um, mas o
# nome basta - e mais seguro, porque pega ramo novo que apareca depois.
# Cortar o pai e o que importa: incluido, ele expande em TODOS os filhos (os
# 27 estados, ou os milhares de municipios do pais inteiro).
# Os filhos da Bahia nao dependem disto: "Salvador - BA", "Feira de Santana -
# BA" e qualquer outro municipio baiano ficam pelo sufixo BA.
RE_PAI_ESTADOS = re.compile(
    r"Estados\s+Brasileiros"
    r"|Estados\s+e\s+DF"
    r"|dos\s+Munic[ií]pios"
    r"|Munic[ií]pios\s+Brasileiros", re.I)
BAHIA_FICA = {
    "419732": "Bahia - BA (Historia)",
    "400487": "Bahia - BA (Geografia)",   # a lista do dono nao tinha esta:
    "420275": "Salvador - BA",            # descoberta pelos assuntos vizinhos
}
# So aplico o corte nestas materias. Fora daqui o sufixo "- UF" e perigoso:
# "Progressao Aritmetica - PA" sao 519 questoes de Matematica que casariam
# com Para.
MATERIAS_COM_ESTADO = ("hist", "geogr")
RE_UF = re.compile(r"\s-\s(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|"
                   r"PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)$")


def _sem_acento(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(t or ""))
                   if not unicodedata.combining(c)).strip().lower()


def e_raiz_generica(mat, nome, disciplinas=None):
    """
    Esta etiqueta e um NIVEL ACIMA da materia?

    O caso concreto: a materia e "Historia do Brasil" e existe uma etiqueta
    chamada so "Historia" (id 77), que e a DISCIPLINA - traz Historia Geral
    junto e, dentro dela, todos os estados e municipios do pais (foi o que o
    dono viu no filtro: dezenas de "Caxambu do Sul - SC", "Campinas - SP").

    O sinal e o nome ser um prefixo ESTRITO do nome da materia: mais curto e
    contido nele. "Historia" < "Historia do Brasil" -> e um nivel acima.
    "Historia do Brasil" == "Historia do Brasil" -> e a materia, fica.

    Tentei antes comparar com o campo `disciplina` das aulas, mas em Historia
    `disciplina` e `materia` sao a mesma palavra, e a regra removia a 397241
    ("Historia do Brasil"), que e a etiqueta PRINCIPAL do conteudo.
    """
    if not nome:
        return False
    n, m = _sem_acento(nome), _sem_acento(mat)
    if n == m:
        return False
    if len(n) < len(m) and m.startswith(n):
        return True
    return any(n == _sem_acento(d) for d in (disciplinas or ()) if d)


def _e_raiz_generica_antiga(mat, nome, disciplinas):
    """
    Esta etiqueta e a DISCIPLINA inteira, disfarcada de assunto?

    A 77 se chama "Historia" e traz a disciplina toda: 1.944 questoes com o
    filtro de bancas, contra 1 assunto de verdade que traz dezenas. Na tela
    ela nem aparece como Assunto - aparece como Disciplina, e o chip de
    Assuntos fica vazio (foi assim que o dono viu).

    Incluir uma dessas anula qualquer recorte fino: dentro de "Historia"
    cabem todos os estados que acabei de excluir, e as outras 115 etiquetas
    da materia viram repeticao do que ela ja trouxe.

    `disciplinas` sao os nomes que as AULAS declaram (campo `disciplina`),
    entao a regra nao depende de eu adivinhar nome nenhum.
    """
    if not nome:
        return False
    n = _sem_acento(nome)
    return any(n == _sem_acento(d) for d in (disciplinas or ()))


def e_estado_fora_do_edital(mat, nome):
    """
    Esta etiqueta deve sair? Vale para o PAI do ramo e para estado != Bahia.

    O pai tambem sai: incluido, ele arrasta os 27 estados de uma vez, porque
    o app expande pai em filhos antes de consultar.
    """
    if not nome or not any(t in (mat or "").lower() for t in MATERIAS_COM_ESTADO):
        return False
    nome = nome.strip()
    if RE_PAI_ESTADOS.search(nome):
        return True
    m = RE_UF.search(nome)
    return bool(m) and m.group(1) != "BA"


def _nome_da_etiqueta_na_tela(fonte, etiqueta):
    """
    Como esta etiqueta se chama, lido da 1a pagina que a fonte ja carregou.

    Nao faz requisicao nova: a listagem ja esta na mao, e cada questao traz
    seus assuntos com id e nome.
    """
    try:
        j = fonte.buscar(1)
    except Exception:
        return None
    alvo = str(etiqueta)
    for q in (j.get("data") or {}).get("rows") or []:
        for a in (q.get("assuntos") or []):
            if isinstance(a, dict) and str(a.get("id")) == alvo:
                return a.get("titulo") or a.get("nome")
    return None


def podar_estados(mat, assuntos, nomes):
    """
    Tira do filtro o pai dos estados e os estados que nao sao Bahia.

    `nomes` e o mapa id->nome montado das questoes ja coletadas. Para as
    etiquetas cujo nome eu ainda nao conheco, quem corta e a checagem em
    tempo de coleta (`_coletar_bloco`), que ve o nome na 1a pagina.
    """
    disciplinas = _DISCIPLINAS_DAS_AULAS.get(mat) or ()
    so_estado = any(t in (mat or "").lower() for t in MATERIAS_COM_ESTADO)
    fica, sai = [], []
    for e in assuntos:
        e = str(e)
        # a raiz da disciplina sai em QUALQUER materia, nao so nas regionais
        if e_raiz_generica(mat, nomes.get(e), disciplinas):
            sai.append((e, f"{nomes.get(e)} (a disciplina inteira)"))
            continue
        if not so_estado:
            fica.append(e)
            continue
        if e in BAHIA_FICA:
            fica.append(e)
            continue
        if e in PAI_DOS_ESTADOS:
            sai.append((e, "Historia dos Estados Brasileiros (pai)"))
            continue
        if e_estado_fora_do_edital(mat, nomes.get(e)):
            sai.append((e, nomes.get(e)))
            continue
        fica.append(e)
    return fica, sai


def nomear_filtro(pagina, mat, assuntos, niveis, por_pagina=100, paginas=2):
    """
    Descobre o NOME das etiquetas do filtro, de uma vez, antes de coletar.

    POR QUE
    -------
    Sem nome eu nao sei quais sao estado, e acabava descobrindo etiqueta por
    etiqueta em tempo de coleta - cada descoberta custava um filtro aberto,
    um seletor trocado e uns 10s, pra no fim jogar fora. O dono foi direto ao
    ponto: "nao e nem pra olhar pra eles".

    COMO, BARATO
    ------------
    Consulta as etiquetas em BLOCO e le 1-2 paginas. Cada questao traz seus
    assuntos com id e nome, entao um punhado de paginas nomeia quase tudo.
    Historia (149 etiquetas) sai em ~6 requisicoes, contra ~30 filtros
    abertos pra jogar fora.

    O que nao aparecer na amostra fica sem nome e cai na checagem em tempo de
    coleta - que continua existindo, so deixou de ser o caminho normal.
    """
    faltam = [str(e) for e in assuntos if str(e) not in _NOMES_DE_ASSUNTO]
    if not faltam:
        return {}
    achados = {}
    blocos = _blocos(faltam, niveis, por_pagina)
    print(f"    nomeando {len(faltam)} etiqueta(s) sem nome, "
          f"{len(blocos)} consulta(s)...")
    for bloco in blocos:
        fonte = ListagemPelaTela(pagina, bloco, niveis, por_pagina,
                                 sem_cliente=True)
        try:
            fonte.preparar(montar_url_tela(
                bloco, niveis, bancas=sorted(BANCAS_PERMITIDAS)))
            for pg in range(1, paginas + 1):
                try:
                    j = fonte.buscar(pg)
                except Exception:
                    break
                for q in (j.get("data") or {}).get("rows") or []:
                    for a in (q.get("assuntos") or []):
                        if isinstance(a, dict) and a.get("id") is not None:
                            n = a.get("titulo") or a.get("nome")
                            if n:
                                achados[str(a["id"])] = n
                if pg < paginas:
                    pausa(pagina, "nomeando")
        except Exception as e:
            print(f"      (bloco de nomeacao falhou: "
                  f"{str(e).splitlines()[0][:60]})")
        finally:
            fonte.fechar()
        pausa(pagina, "nomeando")
    _NOMES_DE_ASSUNTO.update(achados)
    nomeadas = sum(1 for e in faltam if e in achados)
    print(f"    nomeei {nomeadas} de {len(faltam)}")
    return achados


def _relatorio_dos_pulados(pagina, ordem, guardados, niveis, por_pagina):
    """
    Mede quanto sobra dos blocos que tinham sido pulados, DEPOIS do filtro de
    banca, e grava a tabela em disco.

    O dono pediu ver isto antes de comecar, e com razao: esses blocos sao os
    assuntos CENTRAIS de cada materia (403587 = a raiz de Portugues, 122.561
    questoes). Corta-los por tamanho era o pior recorte possivel; mas entrar
    neles as cegas tambem e ruim. A tabela diz se o filtro de banca resolve
    ou se ainda vai precisar do corte de ano.

    Custa uma carga de tela por bloco - nao gasta a API de questoes.
    """
    pulados = [(m, e, n) for m in ordem
               for e, n in sorted((guardados.get(m, {}).get("puladas") or {}).items(),
                                  key=lambda x: -x[1])]
    if not pulados:
        print("\n  (nenhum bloco pulado pendente)")
        return []

    # O QUE JA FOI MEDIDO NAO SE MEDE DE NOVO.
    #
    # Esta tabela custa um filtro aberto por bloco: eram ~2 min e 12 filtros a
    # cada reinicio pra chegar no mesmo numero ja gravado em disco. Releio o
    # arquivo e so meco o que e novo.
    alvo = DIR_SAIDA / "blocos_pulados.json"
    antes_medidos = {}
    if alvo.exists():
        try:
            for l in json.loads(alvo.read_text(encoding="utf-8")):
                if l.get("depois") is not None:
                    antes_medidos[(l.get("materia"), l.get("etiquetas"))] = l
        except Exception:
            pass
    novos = [(m, e, n) for m, e, n in pulados if (m, e) not in antes_medidos]
    if not novos:
        linhas = [antes_medidos[(m, e)] for m, e, _ in pulados
                  if (m, e) in antes_medidos]
        ta = sum(l["antes"] for l in linhas) or 1
        td = sum(l["depois"] for l in linhas)
        print(f"\n  os {len(linhas)} blocos pulados ja foram medidos: "
              f"{ta:,} -> {td:,} ({100*td/ta:.0f}% do original)"
              .replace(",", "."))
        print(f"     detalhe em {alvo.name} - nao vou remedir")
        return linhas
    if antes_medidos:
        print(f"\n  {len(antes_medidos)} bloco(s) ja medidos; meco so os "
              f"{len(novos)} novos")
    pulados = novos

    print(f"\n  OS {len(pulados)} BLOCOS QUE TINHAM SIDO PULADOS")
    print("  medindo quanto sobra com as 14 bancas do edital...\n")
    print(f"     {'materia':22} {'etiqueta':12} {'antes':>9} {'depois':>9}  {'corte':>6}")
    linhas = []
    for mat, etq, antes in pulados:
        try:
            est = preparar_tela(pagina, url_filtro=montar_url_tela(
                etq.split(","), niveis, bancas=sorted(BANCAS_PERMITIDAS)),
                por_pagina=por_pagina)
            depois = int(re.sub(r"\D", "", str(est.get("contagem") or "")
                                .split("quest")[0]) or 0)
        except Exception as e:
            print(f"     {mat[:22]:22} {etq:12} {antes:>9,} {'ERRO':>9}  "
                  f"{str(e).splitlines()[0][:26]}".replace(",", "."))
            linhas.append({"materia": mat, "etiquetas": etq, "antes": antes,
                           "depois": None, "erro": str(e).splitlines()[0][:80]})
            continue
        corte = f"{datetime.now().year - ANOS_GIGANTE}+" if depois > CORTE_GIGANTE else "-"
        print(f"     {mat[:22]:22} {etq:12} {antes:>9,} {depois:>9,}  {corte:>6}"
              .replace(",", "."))
        linhas.append({"materia": mat, "etiquetas": etq, "antes": antes,
                       "depois": depois, "corte_ano": corte})
        pausa(pagina, "medindo")

    medidos = [l for l in linhas if l.get("depois") is not None]
    if medidos:
        ta, td = sum(l["antes"] for l in medidos), sum(l["depois"] for l in medidos)
        print(f"\n     {'TOTAL':22} {'':12} {ta:>9,} {td:>9,}   "
              f"({100*td/ta:.0f}% do original)".replace(",", "."))
    alvo.parent.mkdir(parents=True, exist_ok=True)
    juntas = list(antes_medidos.values()) + linhas
    alvo.write_text(json.dumps(juntas, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"     gravado em {alvo}")
    return linhas


ARQ_FILTROS_EDITAL = RAIZ / "filtros_edital.json"


def filtro_do_edital(so_disciplina=None):
    """
    O filtro sai do `filtros_edital.json` - nao mais dos cadernos das aulas.

    POR QUE MUDOU (24/09)
    ---------------------
    O filtro nascia das etiquetas que o professor pendurou no caderno de cada
    aula. Isso trazia muito do que o edital nao pede - 811 questoes de Lei
    Maria da Penha dentro de Lingua Portuguesa, os 27 estados em Historia, uma
    aula com 95 etiquetas que despejou o pais inteiro. E, ao mesmo tempo,
    deixava faltando: das 38.426 de Portugues no disco, so 4.974 batiam com os
    itens do edital.

    O dono cortou o no: "a coleta tem que focar no edital". Agora cada item do
    edital aponta as etiquetas dele, e o que nao esta na lista nao entra.

    Devolve (por_materia, nao_coletar) com os ids ja como texto.
    """
    if not ARQ_FILTROS_EDITAL.exists():
        raise Parada(
            f"nao achei {ARQ_FILTROS_EDITAL.name}.\n"
            "     E ele que diz o que coletar - sem ele eu voltaria a varrer\n"
            "     os cadernos das aulas, que e o que o dono mandou parar.")
    d = json.loads(ARQ_FILTROS_EDITAL.read_text(encoding="utf-8"))
    por_mat, proibidas = {}, {}
    for m in d.get("materias") or []:
        mat = m.get("materia")
        if not mat or m.get("coletar") is False:
            continue
        if so_disciplina and so_disciplina.lower() not in mat.lower():
            continue
        etqs = {}
        for it in (m.get("itens") or []):
            for e in (it.get("etiquetas") or []):
                if e.get("id"):
                    etqs[str(e["id"])] = e.get("nome")
        # regra 2: evidencia_soldado entra junto
        for e in (m.get("evidencia_soldado") or []):
            if e.get("id"):
                etqs.setdefault(str(e["id"]), e.get("nome"))
        # regra 4: as proibidas saem, mesmo que apareçam em algum item
        fora = {str(e.get("id")) for e in (m.get("nao_coletar") or [])
                if e.get("id")}
        for i in fora:
            etqs.pop(i, None)
        if etqs:
            por_mat[mat] = etqs
            proibidas[mat] = fora
    return por_mat, proibidas


def coletar_edital(pagina, progresso, so_disciplina=None, horas=None,
                   ritmo_questao=None, por_pagina=POR_PAGINA_PADRAO,
                   adiar_leves=False, uma_etiqueta=True, pacote=None):
    niveis = [NIVEL_PADRAO]
    ano_atual = datetime.now().year
    prazo = (time.monotonic() + horas * 3600) if horas else None

    por_mat, soldado, ja = {}, {}, set()

    # A FONTE DO FILTRO E O EDITAL.
    #
    # Fica antes de tudo: se o arquivo existir, nem leio os cadernos. A lista
    # ja vem com as proibidas (regra 4) retiradas e a evidencia_soldado
    # (regra 2) incluida, entao nao ha o que podar aqui - toda a logica de
    # estado/municipio/raiz generica que eu vinha empilhando deixou de ser
    # necessaria: o que nao esta na lista simplesmente nao entra.
    # O PACOTE NAO SUBSTITUI O EDITAL - so diz o que ja esta no disco.
    #
    # Aqui era `and not pacote`, e na VPS (que sempre roda com --pacote) o
    # filtro do edital seria ignorado e ela voltaria a varrer os cadernos -
    # justo o que o dono mandou parar. O pacote continua util pelo que ele de
    # fato carrega: a lista de ids ja coletados, que evita rebaixar 107 mil
    # questoes.
    if ARQ_FILTROS_EDITAL.exists():
        por_edital, _proibidas = filtro_do_edital(so_disciplina)
        if por_edital:
            por_mat = {m: set(v) for m, v in por_edital.items()}
            _NOMES_DE_ASSUNTO.update(
                {i: n for v in por_edital.values() for i, n in v.items() if n})
            if pacote:
                _pm, _sol, ja = _ler_pacote(pacote, so_disciplina)
                print(f"  ids ja coletados, do pacote: {len(ja)}")
            else:
                ja = _ids_ja_coletados()
            soldado = {}
            print(f"  FILTRO: {ARQ_FILTROS_EDITAL.name} "
                  f"({sum(len(v) for v in por_mat.values())} etiquetas do "
                  f"edital, em {len(por_mat)} materias)")
            # A lista ja e final: gravo como o filtro da materia e marco a
            # nomeacao como feita, pra o laco nao tentar descobrir raiz nem
            # podar estado - nada disso se aplica a uma lista escolhida item
            # por item do edital.
            guardados = progresso.setdefault("filtros_edital", {})
            for mat, etqs in por_mat.items():
                g0 = guardados.setdefault(mat, {})
                antes = set(map(str, g0.get("assuntos") or []))
                g0["assuntos"] = sorted(etqs, key=int)
                g0["fonte_do_filtro"] = ARQ_FILTROS_EDITAL.name
                g0["nomeacao_feita"] = len(g0["assuntos"])
                if antes and antes != set(g0["assuntos"]):
                    # filtro trocou: o que ficou pra tras vira fora_edital na
                    # carga, mas o progresso por bloco nao vale mais
                    g0["status"] = "em andamento"
                    g0["filtro_anterior"] = sorted(antes - set(g0["assuntos"]))
            gravar_progresso(progresso)

    if por_mat:
        # ja veio do filtros_edital.json - nao leio caderno nenhum. Sem este
        # `if`, a linha de baixo sobrescrevia a lista do edital pelas 118
        # etiquetas do caderno de Portugues, e todo o recorte se perdia
        # (o resumo continuava dizendo "118 etiquetas dos cadernos").
        pass
    elif pacote:
        # Rodando fora do PC que coletou as aulas (tipicamente a VPS): tudo
        # que eu varreria em saida/aulas (55 MB) e saida/questoes (32 MB) vem
        # mastigado num arquivo de algumas centenas de KB.
        por_mat, soldado, ja = _ler_pacote(pacote, so_disciplina)
    else:
        por_mat = _filtros_das_aulas(so_disciplina)
        soldado = _etiquetas_de_soldado()
        ja = _ids_ja_coletados()
    if not por_mat:
        raise Parada("nenhuma materia com caderno de questoes"
                     + (f" (filtro --disciplina '{so_disciplina}')" if so_disciplina else ""))
    ordem = [m for m in ORDEM_MATERIAS if m in por_mat] + \
            sorted(m for m in por_mat if m not in ORDEM_MATERIAS)
    guardados = progresso.setdefault("filtros_edital", {})

    print(f"  Materias: {len(ordem)}   (fora: {', '.join(sorted(MATERIAS_FORA_DA_COLETA))})")
    for m in ordem:
        st = (guardados.get(m) or {}).get("status", "")
        fonte_txt = ("do edital" if (guardados.get(m) or {}).get("fonte_do_filtro")
                     else "dos cadernos")
        print(f"    - {m}: {len(por_mat[m])} etiquetas {fonte_txt}"
              + (f"   [{st}]" if st else ""))
    print(f"  Questoes que ja estao no PC (nao baixo de novo): {len(ja)}")
    print(f"  Etiquetas de prova de Soldado lidas: {sum(len(v) for v in soldado.values())}")
    print(f"  Cortes: bloco com mais de {CORTE_GIGANTE} questoes -> so de "
          f"{ano_atual - ANOS_GIGANTE} em diante; Atualidades -> so de {ano_atual - 1} em diante")
    print(f"  Etiquetas em blocos de ate {ETIQUETAS_POR_BLOCO} (consulta curta); "
          f"uma listagem a cada {LISTAGEM_MIN_S:.0f}s no minimo; sem comentario do professor.")
    if horas:
        print(f"  Limite desta rodada: {horas:g}h (para no fim da pagina e retoma depois).")
    print(f"  Bancas: SO estas {len(BANCAS_PERMITIDAS)} "
          f"({', '.join(sorted(set(BANCAS_PERMITIDAS.values())))})")
    print("\n  Lembre: nao use o Gran em outra janela/aparelho enquanto isto roda.")

    # 1) APRENDER O PARAMETRO DE BANCA, PROVANDO QUE FILTRA.
    global PARAM_BANCA
    PARAM_BANCA = progresso.get("param_banca")
    if PARAM_BANCA:
        print(f"\n  filtro de banca: '{PARAM_BANCA}=' (aprendido antes)")
    else:
        print()
        etq = next(iter(sorted(por_mat[ordem[0]], key=int)))
        PARAM_BANCA = descobrir_param_banca(pagina, etq, niveis)
        if not PARAM_BANCA:
            raise Parada(
                "nao descobri como a tela nomeia o filtro de banca.\n"
                "     Nao comeco: sem ele eu coletaria o acervo inteiro\n"
                "     achando que estava filtrando.")
        progresso["param_banca"] = PARAM_BANCA
        gravar_progresso(progresso)

    # 2) OS BLOCOS QUE TINHAM SIDO PULADOS: antes x depois do filtro.
    #    Medido, nao estimado - e a tabela que o dono pediu ver ANTES.
    _relatorio_dos_pulados(pagina, ordem, guardados, niveis, por_pagina)

    if not confirmar("\n  Comecar a coleta?"):
        print("  Ok, nao comecei.")
        return

    ritmo_q = ritmo_questao or Ritmo(piso=0.4, acelera_a_cada=20, unidade="questoes")
    comecou = False
    falhadas = []
    for mat in ordem:
        g = guardados.get(mat) or {}
        if g.get("status") == "completa":
            print(f"\n  ===== {mat}: ja completa ({g.get('coletadas', 0)} questoes) =====")
            continue
        if prazo and comecou and time.monotonic() > prazo:
            print(f"\n  Limite de {horas:g}h atingido. Rode o mesmo comando pra continuar.")
            break
        if comecou:
            # Materia nova, aba nova: a que vem da materia anterior passou por
            # centenas de filtros e e ela que carrega o peso.
            pagina = renovar_aba(pagina)
        comecou = True
        print(f"\n  ===== {mat} =====")

        # 1) filtro final: cadernos - raiz + etiquetas de Soldado da mesma raiz
        if not g.get("assuntos"):
            base = set(por_mat[mat])
            amostra = _blocos(base, niveis, por_pagina)[0]
            # Tambem pela TELA. Este passo ficou pra tras na 1a conversao e
            # derrubou a passada inteira na primeira materia: adiantou pouco
            # tirar o meu cliente das 300 paginas e deixa-lo na descoberta da
            # raiz, que acontece uma vez por materia e vem antes de tudo.
            j = _amostra_pela_tela(pagina, amostra, niveis, por_pagina,
                                   "descobrindo a raiz da materia")
            raiz = _raiz_dominante((j.get("data") or {}).get("rows") or [], set(amostra))
            filtro = set(base)
            if raiz and raiz in filtro:
                filtro.discard(raiz)
                print(f"    [i] tirei a etiqueta generica (raiz {raiz}) - com ela vinha a materia inteira")
            extra = (soldado.get(raiz) or set()) - filtro if raiz else set()
            if extra:
                filtro |= extra
                print(f"    [i] +{len(extra)} etiquetas que cairam em prova de Soldado")
            if not filtro and raiz:
                filtro = {raiz}
                print("    [!] os cadernos so tinham a raiz - vou pela materia inteira, com os cortes")
            g = {"raiz": raiz, "assuntos": sorted(filtro, key=int),
                 "status": "em andamento"}
            guardados[mat] = g
            gravar_progresso(progresso)

        # ESTADOS: so a Bahia, e eles nao podem NEM ENTRAR na lista.
        #
        # FORA do `if not g.get("assuntos")` DE PROPOSITO. Quando estava
        # dentro, so valia pra materia cujo filtro fosse montado agora - e na
        # VPS o filtro de Historia JA existia, com as 149 etiquetas. Toda a
        # poda viraria enfeite: passaria no teste e nao faria nada na maquina
        # que importa. O teste conferia a funcao; o defeito estava em ONDE ela
        # era chamada.
        _carregar_nomes_de_assunto()
        _carregar_disciplinas()
        # A RAIZ DA DISCIPLINA SAI SEMPRE, em qualquer materia.
        #
        # A 77 ("Historia") passou por todos os filtros: nao e estado, nao e
        # pai de estado, e o `_raiz_dominante` nao a pegou. Ela sozinha traz a
        # materia inteira - inclusive os estados que acabei de excluir. Entao
        # a poda por nome de disciplina roda pra todas, nao so pras regionais.
        fica0, sai0 = podar_estados(mat, g.get("assuntos") or [],
                                    _NOMES_DE_ASSUNTO)
        if sai0 and len(fica0) != len(g.get("assuntos") or []):
            g["assuntos"] = sorted(fica0, key=int)
            g.setdefault("estados_removidos", {}).update({e: n for e, n in sai0})
            print(f"    [i] -{len(sai0)} etiqueta(s) fora do recorte:")
            for e, n in sai0[:10]:
                print(f"        {e}  {n}")
            gravar_progresso(progresso)

        # SO NOMEIA SE HOUVER ALGO NOVO.
        #
        # A nomeacao custa 2-3 consultas e nem sempre acha tudo (13 de 51 na
        # 1a passada de Historia): o que nao aparece na amostra e etiqueta sem
        # questao nesta faixa, e tentar de novo so repete o gasto. Guardo a
        # marca por materia e o tamanho do filtro - se o filtro mudar, refaz.
        marca = len(g.get("assuntos") or [])
        ja_nomeada = g.get("nomeacao_feita") == marca
        if (any(t in (mat or "").lower() for t in MATERIAS_COM_ESTADO)
                and not ja_nomeada):
            nomear_filtro(pagina, mat, g.get("assuntos") or [],
                          niveis, por_pagina)
            fica, sai = podar_estados(mat, g.get("assuntos") or [],
                                      _NOMES_DE_ASSUNTO)
            if sai:
                print(f"    [i] estados fora do edital: -{len(sai)} "
                      f"etiqueta(s), nem vou olhar pra elas")
                for e, n in sai[:10]:
                    print(f"        {e}  {n}")
                g["assuntos"] = sorted(fica, key=int)
                g.setdefault("estados_removidos", {}).update(
                    {e: n for e, n in sai})
            progresso.setdefault("nomes_de_assunto", {}).update(
                {e: n for e, n in _NOMES_DE_ASSUNTO.items() if n})
            g["nomeacao_feita"] = len(g.get("assuntos") or [])
            guardados[mat] = g
            gravar_progresso(progresso)

        if uma_etiqueta:
            # UMA ETIQUETA POR CONSULTA: poe uma, colhe, tira, poe a proxima.
            #
            # Pedido do dono depois de ver o filtro da tela com 7 tags de uma
            # vez. Nao e so estetica - com 50 etiquetas juntas, uma etiqueta
            # que o Gran pendurou no caderno por engano passa despercebida.
            # Em Direitos Humanos a 407288 arrastou a disciplina inteira
            # "Direito Sanitario e Saude" pra dentro do filtro, e so daria pra
            # ver uma por uma.
            # Custo: mais consultas (889 etiquetas no lugar de 24 blocos), mas
            # cada uma e curta e o vinculo questao<->etiqueta fica exato.
            blocos = [[str(e)] for e in sorted(g["assuntos"], key=int)]
            print(f"    {len(blocos)} etiquetas, UMA POR CONSULTA")
        else:
            blocos = _blocos(g["assuntos"], niveis, por_pagina)
            print(f"    {len(g['assuntos'])} etiquetas em {len(blocos)} bloco(s)")
        rotulo = f"edital {mat}"
        pasta = DIR_SAIDA / "questoes" / nome_de_arquivo(rotulo, 60)
        arq = pasta / f"{nome_de_arquivo(rotulo, 60)}.json"
        coletadas = []
        if arq.exists():
            try:
                coletadas = json.loads(arq.read_text(encoding="utf-8")).get("questoes") or []
            except Exception:
                coletadas = []
        vistos = set(ja) | {q.get("id") for q in coletadas}

        terminou = True
        for k, bloco in enumerate(blocos, 1):
            # ABA NOVA DE TEMPOS EM TEMPOS.
            #
            # So vale a pena trocar se ja houve trabalho nesta aba: renovar
            # antes da 1a etiqueta descartaria a aba recem-aberta.
            if k > 1 and (k - 1) % RENOVAR_ABA_A_CADA == 0:
                pagina = renovar_aba(pagina)
            try:
                if not _coletar_bloco(pagina, mat, k, len(blocos), bloco, niveis,
                                      por_pagina, progresso, g, arq, coletadas,
                                      vistos, ritmo_q, ano_atual, prazo, horas,
                                      adiar_leves):
                    terminou = False
                    break
            except Exception as e:
                # UMA ETIQUETA RUIM NAO PODE MATAR A NOITE.
                #
                # Aqui era `except Parada`. Na 1a noite de verdade o que
                # matou a rodada foi um erro do Playwright ("Element is not
                # attached to the DOM"), que nao e Parada e passou direto: a
                # rede de seguranca tinha um buraco do tamanho da causa mais
                # provavel. Pego tudo; `_e_bloqueio` decide o que ainda
                # merece parar de verdade.
                #
                # Rodando sem ninguem olhando, morrer na etiqueta 3 de 902 as
                # 3h da manha custa a noite inteira. Entao: falha de pagina
                # (lentidao, pagina que nao chegou) vira "pula esta etiqueta e
                # segue". Mas BLOQUEIO DE VERDADE continua parando tudo -
                # insistir contra 429/401/captcha foi o que derrubou duas
                # sessoes em 24/09.
                if _e_bloqueio(e):
                    raise
                falhadas.append((mat, ",".join(bloco), str(e).splitlines()[0]))
                print(f"    [!] bloco {k}/{len(blocos)} falhou, pulando: "
                      f"{str(e).splitlines()[0][:90]}")
                g.setdefault("falhadas", {})[",".join(bloco)] =                     str(e).splitlines()[0][:160]
                gravar_progresso(progresso)
                continue
        g["coletadas"] = len(coletadas)
        if terminou:
            g["status"] = "completa"
            print(f"  {mat}: completa - {len(coletadas)} questoes em {arq.name}")
            # Com blocos eu perco a vigilancia de ver etiqueta por etiqueta.
            # Isto devolve o mesmo aviso, pelo dado e sem gastar requisicao.
            _avisar_suspeitas(mat, coletadas, g.get("assuntos") or [])
        gravar_progresso(progresso)
        salvar_json(ARQ_CONTAGEM, {
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "obs": "total_blocos = soma dos totais dos blocos (uma questao pode estar em mais de um)",
            "materias": {k2: {"etiquetas": len(v.get("assuntos") or []), "raiz": v.get("raiz"),
                              "total_blocos": sum((b or {}).get("total_no_gran") or 0
                                                  for b in (v.get("blocos") or {}).values()),
                              "blocos": v.get("blocos"), "coletadas": v.get("coletadas"),
                              "status": v.get("status")}
                         for k2, v in guardados.items()}})
        if not terminou:
            break

    print(f"\n  {ritmo_q.resumo('pausa por chamada')}")
    print(f"  Resumo por materia: {ARQ_CONTAGEM}")


def main():
    ap = argparse.ArgumentParser(
        description="Extrai aulas e questoes do Gran usando a sua sessao logada no Chrome.")
    g = ap.add_argument_group("o que fazer")
    g.add_argument("--aula", action="store_true",
                   help="TESTE: extrai so a aula aberta na aba")
    g.add_argument("--questoes", action="store_true",
                   help="TESTE: extrai o filtro de questoes aberto na aba")
    g.add_argument("--curso", action="store_true",
                   help="AUTOMATICO: varre todas as aulas do curso")
    g.add_argument("--todas-questoes", action="store_true", dest="todas_questoes",
                   help="AUTOMATICO: varre as questoes de cada disciplina do curso")
    g.add_argument("--coletar-pela-tela", type=str, default=None, metavar="ROTULO",
                   dest="coletar_pela_tela",
                   help="coleta a disciplina INTEIRA sem gastar requisicao minha "
                        "(a listagem ja traz enunciado, alternativas e gabarito). "
                        "Precisa de --assunto. Deixa as leves pendentes")
    g.add_argument("--completar-leves", type=str, default=None, metavar="ROTULO",
                   dest="completar_leves",
                   help="2o passo: estatistica e comentario das questoes que "
                        "ficaram pendentes. Este gasta a minha cota")
    g.add_argument("--colher-da-tela", type=str, default=None, metavar="ROTULO",
                   help="o script CLICA na paginacao e so escuta a resposta - "
                        'nenhuma requisicao minha. Ex: --colher-da-tela "Direito Penal"')
    g.add_argument("--testar", action="store_true",
                   help="faz UMA chamada e diz se o limite do Gran ja liberou")
    g.add_argument("--orgao", type=str, default=None, metavar="IDS",
                   help="AUTOMATICO: passada por orgao (spec 3.7). Ex: --orgao 544 "
                        "(PM BA). Varios: --orgao 544,999. Sem filtro de assunto.")
    g.add_argument("--contar", type=str, default=None, metavar="ARQUIVO",
                   help="so CONTA quantas questoes cada filtro do edital da (arquivo JSON de filtros)")
    g.add_argument("--coletar-edital", action="store_true", dest="coletar_edital",
                   help="coleta as questoes de concurso de cada materia pelo filtro das aulas "
                        "(cadernos) + etiquetas de prova de Soldado. Retomavel. Use --horas pra limitar")
    g.add_argument("--completar-assuntos", action="store_true", dest="completar_assuntos",
                   help="rele SO a listagem e preenche o id do assunto nas questoes "
                        "ja coletadas (barato: ~70 requisicoes por disciplina)")

    o = ap.add_argument_group("opcoes")
    o.add_argument("--limite-etiqueta", type=int, default=None,
                   dest="limite_etiqueta",
                   help=f"pula etiqueta que sozinha traga mais de N questoes "
                        f"(padrao {LIMITE_ETIQUETA}). Etiqueta larga demais nao "
                        f"liga questao a aula - e ruido, igual a raiz")
    o.add_argument("--pacote", type=str, default=None, metavar="ARQUIVO",
                   help="le as etiquetas e os ids ja coletados de um "
                        "pacote_coleta.json em vez de varrer saida/. Serve pra "
                        "rodar a coleta noutra maquina (VPS) sem levar 87 MB")
    o.add_argument("--pausa-pagina", type=float, default=None,
                   dest="pausa_pagina",
                   help=f"segundos entre paginas na coleta pela tela (padrao "
                        f"{PAUSA_PAGINA_TELA:g}s = ~6 req/min; entre etiquetas usa "
                        f"2,5x). Sem freio deu ~55 req/min e a conta "
                        f"levou bloqueio em 24/09")
    o.add_argument("--navegador", choices=("chrome", "firefox"), default="chrome",
                   help="'firefox' LANCA o Firefox com perfil proprio (o "
                        "Cloudflare barrou o endpoint das questoes no Chrome "
                        "em 24/09). 'chrome' conecta no que o .bat abriu")
    o.add_argument("--em-blocos", action="store_true", dest="em_blocos",
                   help="no --coletar-edital: junta ate 50 etiquetas por "
                        "consulta. Economiza 2,2x em requisicao, MAS o corte "
                        "de ano e decidido pelo total do BLOCO: com 50 juntas "
                        "ele sempre passa de 5.000 e o corte 2018+ cai em "
                        "todas, custando ~22%% do acervo. Padrao: uma por vez")
    o.add_argument("--adiar-leves", action="store_true", dest="adiar_leves",
                   help="no --coletar-edital: nao busca o indice de acerto na "
                        "hora. A passada fica SEM requisicao minha e roda ate "
                        "durante castigo de 429; depois use --completar-leves")
    o.add_argument("--horas", type=float, default=None,
                   help="so no --coletar-edital: para depois de N horas (no fim da pagina)")
    o.add_argument("--disciplina", type=str, default=None,
                   help='limita a uma disciplina (nome ou id). Ex: --disciplina "Direito Penal"')
    o.add_argument("--nivel", type=str, default=NIVEL_PADRAO,
                   help=f"escolaridade das questoes (padrao: {NIVEL_PADRAO}; use '' pra todas)")
    o.add_argument("--sem-comentarios", action="store_true", dest="sem_comentarios",
                   help="nao busca o comentario do professor (1 requisicao a menos por questao)")
    o.add_argument("--por-pagina", type=int, default=POR_PAGINA_PADRAO,
                   dest="por_pagina",
                   help=f"questoes por pagina (padrao {POR_PAGINA_PADRAO}; "
                        "--colher-da-tela usa 100 se voce nao disser outro)")
    o.add_argument("--assunto", type=str, default=None,
                   help="id do assunto RAIZ pra montar o filtro da tela "
                        "(ex: 406223 = Direito Penal). So com --colher-da-tela")
    o.add_argument("--url-filtro", type=str, default=None, dest="url_filtro",
                   help="url pronta do filtro, se voce preferir montar na mao")
    o.add_argument("--pagina-inicial", type=int, default=1, dest="pagina_inicial",
                   help="de que pagina da tela comecar (padrao: 1)")
    o.add_argument("--sem-tela", action="store_true", dest="sem_tela",
                   help="nao use a tela pra buscar a listagem - volta ao caminho "
                        "antigo, em que o meu cliente pede tudo (sujeito a 429)")
    o.add_argument("--ordem", type=str, default=None,
                   choices=sorted(ORDENS.keys()),
                   help="ordenacao da tela. 'antigas' inverte a lista e alcanca "
                        "as questoes que vem embutidas na carga do servidor")
    o.add_argument("--max-paginas", type=int, default=None, dest="max_paginas",
                   help="para depois de N paginas (bom pra testar)")
    o.add_argument("--pausa", type=float, default=None,
                   help="forca o ponto de partida das listagens. Por padrao comeca "
                        "no piso e so desacelera se o Gran reclamar.")
    # Medido em 23/09, nao chutado:
    #   listagem a cada ~29s -> 89 paginas seguidas, zero 429
    #   listagem a cada  10s -> 429 na 13a pagina
    # O piso fica em 20s: dentro da faixa que funcionou, e o adaptativo
    # recua sozinho se ainda for rapido demais.
    o.add_argument("--pausa-min", type=float, default=20.0, dest="pausa_min",
                   help="o mais rapido que as LISTAGENS podem ir, e onde elas "
                        "comecam (padrao 20s). 10s ja deu 429; ~29s passou liso.")
    o.add_argument("--max-429", type=int, default=3, dest="max_429",
                   help="quantos 429 tolerar antes de desistir (padrao 3)")
    o.add_argument("--sim", action="store_true",
                   help="responde SIM automaticamente (pra rodar sem ninguem na frente)")
    o.add_argument("--forcar", action="store_true",
                   help="refaz aulas que ja estao no progresso")
    o.add_argument("--recomecar", action="store_true",
                   help="questoes: ignora o progresso e recomeca da pagina 1")
    o.add_argument("--pausa-aulas", type=float, default=None, dest="pausa_aulas",
                   help="so --curso/--aula: pausa minima entre requisicoes (ex. 0.4); "
                        "comeca em 1,0s e desce aos poucos; padrao: 2 a 3s")
    o.add_argument("--porta", type=int, default=PORTA_CDP, help="porta CDP (padrao 9380)")
    o.add_argument("--url", type=str, default=None, metavar="TEXTO",
                   help="escolhe a aba cujo endereco contem TEXTO")
    args = ap.parse_args()

    modos = [args.aula, args.questoes, args.curso, args.todas_questoes,
             args.completar_assuntos, args.orgao, args.testar, args.colher_da_tela,
             args.coletar_pela_tela, args.completar_leves, args.contar, args.coletar_edital]
    if sum(bool(m) for m in modos) != 1:
        ap.error("escolha UM modo: --aula, --questoes, --curso, --todas-questoes, "
                 "--orgao, --contar, --coletar-edital ou --completar-assuntos")
    if args.horas is not None and (not args.coletar_edital or args.horas <= 0):
        ap.error("--horas vale so com --coletar-edital, e tem que ser maior que zero")

    niveis = [n.strip() for n in (args.nivel or "").split(",") if n.strip()]

    global SEMPRE_SIM, PAUSA_LISTAGEM, PAUSA_AULAS_PISO, PAUSA_AULAS_ATUAL
    SEMPRE_SIM = args.sim
    if args.pausa_aulas is not None:
        if not (args.curso or args.aula):
            ap.error("--pausa-aulas so vale com --curso ou --aula")
        if args.pausa_aulas < 0.3:
            ap.error("--pausa-aulas abaixo de 0,3s nao - e bater no servidor sem respiro")
        PAUSA_AULAS_PISO = float(args.pausa_aulas)
        PAUSA_AULAS_ATUAL = max(PAUSA_AULAS_PISO, 1.0)
        print(f"  Pausa nas aulas: comeca em {PAUSA_AULAS_ATUAL:.1f}s e desce ate "
              f"{PAUSA_AULAS_PISO:.1f}s (25% a cada {PAUSA_AULAS_DEGRAU} requisicoes limpas)")
    if args.pausa:
        PAUSA_LISTAGEM = args.pausa
    if args.limite_etiqueta is not None:
        globals()["LIMITE_ETIQUETA"] = int(args.limite_etiqueta)
    if args.pausa_pagina is not None:
        if args.pausa_pagina < 1.0:
            ap.error("--pausa-pagina abaixo de 1s nao - foi assim que a conta "
                     "levou bloqueio em 24/09")
        globals()["PAUSA_PAGINA_TELA"] = float(args.pausa_pagina)
        globals()["PAUSA_BLOCO_TELA"] = float(args.pausa_pagina) * 2.5

    print("=" * 70)
    print("  EXTRATOR GRAN CURSOS")
    print("=" * 70)

    with sync_playwright() as p:
        if args.navegador == "firefox":
            # FIREFOX: eu LANCO o navegador, nao me conecto a um ja aberto.
            #
            # O Firefox nao tem CDP, entao nao da pra anexar num que voce
            # abriu - o Playwright so controla o que ele mesmo lanca. O perfil
            # mora em PERFIL_FIREFOX, entao o login sobrevive entre execucoes:
            # voce loga UMA vez, na janela que abrir.
            #
            # Existe porque em 24/09 o Cloudflare passou a barrar
            # /v1/elastic/questao em TODO Chrome desta maquina (com e sem
            # automacao, perfil novo) e a liberar no Firefox.
            print(f"  Abrindo o Firefox (perfil {PERFIL_FIREFOX})...")
            try:
                ctx_ff = p.firefox.launch_persistent_context(
                    str(PERFIL_FIREFOX), headless=False, viewport=None)
            except Exception as e:
                print(f"\n[ERRO] nao consegui abrir o Firefox: {str(e)[:120]}")
                print("       Falta baixar? rode:")
                print("       python -m playwright install firefox")
                sys.exit(1)
            paginas = [pg for pg in ctx_ff.pages if pg.url.startswith("http")]
            if not paginas:
                paginas = [ctx_ff.new_page()]
            paginas = preparar_firefox(ctx_ff, paginas)
        else:
            try:
                nav = p.chromium.connect_over_cdp(f"http://localhost:{args.porta}")
            except Exception as e:
                print(f"\n[ERRO] Nao consegui conectar no Chrome na porta {args.porta}.")
                print("       Rode o abrir_chrome_gran.bat e deixe a janela aberta.")
                print(f"       Detalhe: {e}")
                sys.exit(1)
            paginas = [pg for ctx in nav.contexts for pg in ctx.pages
                       if pg.url.startswith("http")]
        alvos = [pg for pg in paginas if "grancursosonline" in pg.url]
        if args.url:
            alvos = [pg for pg in alvos if args.url.lower() in pg.url.lower()]
        if not alvos:
            print("\n[ERRO] Nenhuma aba do Gran aberta" +
                  (f" com '{args.url}' no endereco." if args.url else "."))
            for pg in paginas:
                print(f"         - {pg.url[:100]}")
            sys.exit(1)
        # Separar as abas pelo HOST, nao por "/aluno/": a aba de questoes e
        # questoes.grancursosonline.com.br/aluno/filtro/... - ela TAMBEM tem
        # "/aluno/" no endereco, e por isso o modo --aula pegava a aba errada.
        aba_aula = next((pg for pg in alvos
                         if "questoes.grancursosonline" not in pg.url
                         and re.search(r"/codigo/[^/?#]+", pg.url)), None)
        aba_questoes = next((pg for pg in alvos
                             if "questoes.grancursosonline" in pg.url), None)

        precisa_aula = (args.aula or args.curso or args.todas_questoes
                        or args.completar_assuntos)
        if (args.colher_da_tela or args.coletar_pela_tela
                or args.completar_leves):
            precisa_aula = False
        precisa_q = (args.questoes or args.todas_questoes or args.completar_assuntos
                     or bool(args.orgao) or args.testar or bool(args.colher_da_tela)
                     or bool(args.coletar_pela_tela) or bool(args.completar_leves)
                     or bool(args.contar) or args.coletar_edital)
        # --todas-questoes usa as DUAS: a da aula pra saber as disciplinas e
        # os assuntos de cada uma; a de questoes pelo token, que o navegador
        # guarda separado por dominio.
        if precisa_aula and not aba_aula:
            print("\n[ERRO] Preciso de uma aba com uma AULA do curso aberta")
            print("       (o endereco tem /aluno/curso/video/codigo/...).")
            print("       Abas do Gran que achei:")
            for pg in alvos:
                print(f"         - {pg.url[:95]}")
            sys.exit(1)
        if precisa_q and not aba_questoes:
            print("\n[ERRO] Preciso de uma aba do GRAN QUESTOES aberta")
            print("       (questoes.grancursosonline.com.br), com o filtro aplicado.")
            print("       Abas do Gran que achei:")
            for pg in alvos:
                print(f"         - {pg.url[:95]}")
            sys.exit(1)

        pagina = aba_aula if precisa_aula else aba_questoes
        pagina.bring_to_front()
        print(f"\n  Aba da aula    : {(aba_aula.url[:88] + '...') if aba_aula else '(nao preciso)'}")
        print(f"  Aba de questoes: {(aba_questoes.url[:88] + '...') if aba_questoes else '(nao preciso)'}\n")

        progresso = ler_progresso()
        # Antes de qualquer requisicao: ainda estamos de castigo?
        #
        # So --colher-da-tela escapa, porque ele nao faz requisicao NENHUMA -
        # a listagem sai do cache do proprio app.
        #
        # Cheguei a liberar tambem a varredura "pela tela", no argumento de que
        # o 429 de hoje veio todo da listagem e as chamadas leves nunca o
        # causaram. O teste derrubou isso na primeira estatistica: as leves nao
        # CAUSAM 429, mas sao barradas junto durante a penalidade - o castigo e
        # da conta, nao do endpoint. Tirar a listagem do meu cliente ajuda
        # muito (69 chamadas pesadas viram 0), mas nao torna a coleta imune.
        sem_cota = (bool(args.colher_da_tela) or bool(args.coletar_pela_tela)
                    or (bool(args.coletar_edital) and args.adiar_leves)
                    )
        if not sem_cota:
            checar_castigo(progresso, forcar=args.forcar)
        codigo = 0
        try:
            pausa(pagina, "estabilizando")
            checar_bloqueio(pagina)

            if args.aula or args.curso:
                m = re.search(r"/codigo/([^/?#]+)", pagina.url)
                if not m:
                    raise Parada(
                        "nao consegui achar o codigo do curso nesta aba.\n"
                        "     Abra uma aula do curso no Chrome e rode de novo."
                    )
                curso = Curso(pagina, unquote(m.group(1)))

                if args.aula:
                    disc, topico, detalhe = achar_aula_da_aba(curso, pagina)
                    print(f"  Curso: {curso.curso().get('nome')}")
                    print(f"  Disciplina: {disc.get('nome')}")
                    print(f"  Modulo: {(topico.get('desc') or '')[:70]}\n")
                    d = extrair_aula(curso, disc, topico, detalhe, progresso, forcar=True)
                    if d:
                        arq = RAIZ / progresso["aulas"][d["codigo_aula"]]["arquivo"]
                        print(f"\n  SALVO: {arq}")
                        print("\n  Confira esse JSON. Se o formato estiver bom, solte a varredura:")
                        print('    python gran_extrator.py --curso --disciplina "Direito Penal"')
                else:
                    n = varrer_curso(pagina, curso.id, progresso, args.disciplina, args.forcar)
                    print(f"\n  {n} aula(s) novas coletadas.")

            elif args.orgao:
                ids = [x.strip() for x in args.orgao.split(",") if x.strip()]
                # A spec 3.7 pede a passada por orgao SEM filtro de nivel:
                # e o conjunto "caiu na prova", nao "caiu na prova de nivel X".
                niv = niveis if args.nivel != NIVEL_PADRAO else []
                print(f"  Passada por orgao: {ids}")
                print(f"  Escolaridade: {niv or '(todas - e o que a spec pede)'}")
                print("\n  Lembre: nao use o Gran em outra janela enquanto isto roda.")
                if confirmar("\n  Comecar?"):
                    for oid in ids:
                        print(f"\n  ===== orgao {oid} =====")
                        arq, n = varrer_orgao(
                            aba_questoes, oid, progresso,
                            com_comentarios=not args.sem_comentarios,
                            por_pagina=args.por_pagina, niveis=niv,
                            max_paginas=args.max_paginas)
                        print(f"  SALVO: {arq}  ({n} questoes)")

            elif args.coletar_edital:
                print("\n  COLETA DAS QUESTOES DO EDITAL, materia por materia.\n")
                ritmo_q = Ritmo(piso=0.4, acelera_a_cada=20,
                                max_429=args.max_429, unidade="questoes")
                # 100 POR PAGINA.
                #
                # Cheguei a fixar 20 aqui: a carga SSR grava a pagina 1 como
                # `perPage=20`, e forcando 100 a pagina 1 daquela fatia nao
                # existia no cache. Isso deixou de valer quando a escuta saiu
                # do CDP para `page.on("response")`: a troca de 20 para 100
                # FAZ o app pedir, e agora eu pego esse pedido pela rede - nao
                # dependo mais do cache pra primeira pagina.
                # Com 100, uma etiqueta de 390 questoes vai em 4 paginas em
                # vez de 20.
                pp = (100 if args.por_pagina == POR_PAGINA_PADRAO
                      else args.por_pagina)
                if args.adiar_leves:
                    print("  --adiar-leves: o indice de acerto fica pra depois.")
                    print("  Esta passada nao faz requisicao nenhuma pelo meu "
                          "cliente.\n")
                coletar_edital(aba_questoes, progresso, so_disciplina=args.disciplina,
                               horas=args.horas, ritmo_questao=ritmo_q,
                               por_pagina=pp,
                               adiar_leves=args.adiar_leves,
                               uma_etiqueta=not args.em_blocos,
                               pacote=args.pacote)

            elif args.contar:
                print("\n  CONTAGEM DO FILTRO DO EDITAL - so conta, nao coleta nada.\n")
                contar_filtros(aba_questoes, args.contar, progresso,
                               pausa_min=args.pausa_min)

            elif args.coletar_pela_tela:
                if not args.assunto:
                    raise Parada(
                        "--coletar-pela-tela precisa de --assunto <id da raiz>.\n"
                        "     Ex: Direito Penal = 406223, Penal Militar = 404290")
                print("\n  COLETANDO PELA TELA - nenhuma requisicao minha.")
                print("  A listagem ja traz enunciado, alternativas e gabarito.")
                print("  Estatistica e comentario ficam pendentes pro 2o passo.\n")
                pp = 100 if args.por_pagina == POR_PAGINA_PADRAO else args.por_pagina
                arq, n = coletar_pela_tela(
                    aba_questoes, args.coletar_pela_tela, [args.assunto], niveis,
                    args.assunto, por_pagina=pp, max_paginas=args.max_paginas)
                print(f"  SALVO: {arq}  ({n} questoes)")

            elif args.completar_leves:
                print("\n  COMPLETANDO AS LEVES (estatistica + comentario).")
                print("  Este passo GASTA a minha cota - e o que leva 429.\n")
                ritmo = Ritmo(inicial=args.pausa, piso=float(args.pausa_min),
                              max_429=args.max_429, unidade="questoes",
                              acelera_a_cada=40)
                arq, n = completar_leves_do_arquivo(
                    aba_questoes, args.completar_leves,
                    com_comentarios=not args.sem_comentarios, ritmo=ritmo,
                    limite=args.max_paginas)
                print(f"\n  {n} questao(oes) completadas · {ritmo.resumo()}")
                print(f"  SALVO: {arq}")

            elif args.colher_da_tela:
                print("\n  COLHENDO DA TELA - eu nao faco requisicao nenhuma.")
                print("  O script clica na paginacao, o app do Gran pede, eu escuto.\n")
                url_f = args.url_filtro
                if not url_f and args.assunto:
                    url_f = montar_url_tela(args.assunto, niveis)
                    print(f"  filtro: assunto {args.assunto} · nivel {niveis or 'todos'}")
                elif not url_f:
                    print("  usando o filtro que ja esta na aba (sem --assunto)")
                # 100 por pagina corta 341 paginas para 69. So respeito o
                # --por-pagina se voce tiver pedido outro de proposito.
                pp = 100 if args.por_pagina == POR_PAGINA_PADRAO else args.por_pagina
                arq, n = colher_da_tela(
                    aba_questoes, args.colher_da_tela,
                    max_paginas=args.max_paginas, pagina_inicial=args.pagina_inicial,
                    url_filtro=url_f, por_pagina=pp, ordem=args.ordem)
                print(f"  SALVO: {arq}")

            elif args.testar:
                # UMA chamada de listagem, a mais barata possivel (perPage=1),
                # so pra saber se o balde voltou a ter credito. Serve pra nao
                # descobrir isso comecando uma varredura de 1h25.
                print("\n  Uma unica chamada de listagem, so pra testar o limite...")
                try:
                    # perPage=20 como o app: a ideia e testar a MESMA
                    # chamada que funciona, nao uma variante minha
                    j = buscar_json_q(aba_questoes, montar_url_questoes(
                        ["406223"], ["Médio"], 1, 20))
                    total = ((j.get("data") or {}).get("total"))
                    print(f"\n  LIBERADO. O Gran respondeu normalmente ({total} questoes "
                          f"no filtro de teste).")
                    print("  Pode rodar a coleta.\n")
                except Parada as e:
                    if e.status == 429:
                        registrar_429(progresso)
                        print("\n  AINDA DE CASTIGO. O Gran respondeu 429.")
                        print(f"  Foram {progresso.get('n_429')} hoje. Espere mais e teste de novo -")
                        print("  cada tentativa parece estender a janela, entao nao insista seguido.\n")
                    else:
                        raise

            elif args.completar_assuntos:
                m = re.search(r"/codigo/([^/?#]+)", aba_aula.url)
                curso = Curso(aba_aula, unquote(m.group(1)))
                discs = escolher_disciplinas(curso, args.disciplina)
                print(f"  Curso: {curso.curso().get('nome')}")
                print("  Vou reler SO a listagem pra preencher o id do assunto")
                print("  nas questoes ja coletadas (nao refaz estatistica).\n")
                # Ritmo adaptativo: mede o teto seguro ENQUANTO trabalha.
                # As paginas da calibracao enriquecem questoes de verdade,
                # entao o unico custo e o risco do 429 - e esse ele absorve.
                ritmo = Ritmo(inicial=args.pausa,          # None = comeca no piso
                              piso=float(args.pausa_min),
                              max_429=args.max_429)
                for d in discs:
                    ass = assuntos_do_caderno(d.get("caderno_questoes"))
                    if not ass:
                        continue
                    print(f"  ===== {d.get('nome')} =====")
                    reenriquecer_filtro(aba_questoes, d.get("nome"), ass, niveis,
                                        progresso, por_pagina=args.por_pagina,
                                        ritmo=ritmo)
                    print(f"  {ritmo.resumo()}")

            elif args.questoes:
                # TESTE: usa o filtro que esta na aba, uma pagina por vez
                if "questoes.grancursosonline" not in pagina.url:
                    raise Parada("abra o Gran Questoes COM UM FILTRO e rode de novo")
                q = parse_qs(urlparse(pagina.url).query)
                assuntos = []
                for v in q.get("assunto", []):
                    assuntos.extend([x for x in str(v).split(",") if x.strip().isdigit()])
                if not assuntos:
                    raise Parada("nao achei assunto no filtro desta aba")
                mapa = {"1": "Fundamental", "2": "Médio", "3": "Superior"}
                niv = [mapa.get(x, x) for v in q.get("nivel", []) for x in str(v).split(",")]
                # nome do assunto em vez de "assunto 406223": pergunto UMA
                # questao so pra descobrir como o Gran chama esse assunto
                rotulo = f"assunto {assuntos[0]}"
                try:
                    amostra = buscar_json_q(
                        pagina, montar_url_questoes(assuntos, niv or niveis, 1, 1))
                    prim = ((amostra.get("data") or {}).get("rows") or [{}])[0]
                    nome = ((prim.get("assuntos") or [{}])[0].get("descricao")
                            or (prim.get("assuntos") or [{}])[0].get("nome"))
                    if nome:
                        rotulo = f"{assuntos[0]}_{nome}"
                except Parada:
                    pass
                raiz = None
                if not args.sem_tela:
                    raiz = args.assunto or descobrir_raiz(
                        pagina, assuntos, niv or niveis)
                    if not raiz:
                        raise Parada(
                            "nao descobri o assunto raiz deste filtro.\n"
                            "     Passe --assunto <id>, ou --sem-tela pra "
                            "aceitar o risco de 429.")
                arq, n = varrer_filtro(
                    pagina, rotulo, assuntos, niv or niveis, progresso,
                    com_comentarios=not args.sem_comentarios, por_pagina=args.por_pagina,
                    perguntar_por_pagina=True, max_paginas=args.max_paginas,
                    recomecar=args.recomecar, assunto_raiz=raiz)
                print(f"\n  SALVO: {arq}  ({n} questoes)")
                print("\n  Confira esse JSON. Se estiver bom, solte a varredura:")
                print('    python gran_extrator.py --todas-questoes --disciplina "Direito Penal"')

            else:  # --todas-questoes
                m = re.search(r"/codigo/([^/?#]+)", pagina.url)
                if not m:
                    raise Parada(
                        "abra uma AULA do curso na aba (e de onde eu tiro a lista de\n"
                        "     disciplinas e os assuntos de cada uma)."
                    )
                curso = Curso(pagina, unquote(m.group(1)))
                discs = escolher_disciplinas(curso, args.disciplina)
                print(f"  Curso: {curso.curso().get('nome')}")
                planos = []
                for d in discs:
                    ass = assuntos_do_caderno(d.get("caderno_questoes"))
                    if ass:
                        planos.append((d.get("nome"), ass))
                    else:
                        print(f"  [!] {d.get('nome')}: sem caderno de questoes")
                print(f"\n  Vou varrer {len(planos)} disciplina(s):")
                for nome, ass in planos:
                    print(f"    - {nome}: {len(ass)} assuntos")
                print(f"  Escolaridade: {niveis or '(todas)'}")
                print("\n  Lembre: nao use o Gran em outra janela/aparelho enquanto isto roda.")
                if not confirmar("\n  Comecar?"):
                    print("  Ok, nao comecei.")
                else:
                    for nome, ass in planos:
                        print(f"\n  ===== {nome} =====")
                        raiz = None
                        if not args.sem_tela:
                            raiz = args.assunto or descobrir_raiz(
                                aba_questoes, ass, niveis)
                            # Cair calado no caminho antigo seria trocar "nao
                            # sei a raiz" por "69 listagens pelo meu cliente" -
                            # exatamente o que derruba. Melhor parar e dizer.
                            if not raiz:
                                raise Parada(
                                    f"nao descobri o assunto raiz de {nome}.\n"
                                    "     Passe --assunto <id>, ou --sem-tela "
                                    "pra aceitar o risco de 429.")
                        # as questoes vao pela aba DE QUESTOES: o token fica
                        # guardado por dominio, entao a aba da aula nao o tem
                        varrer_filtro(
                            aba_questoes, nome, ass, niveis, progresso,
                            com_comentarios=not args.sem_comentarios,
                            por_pagina=args.por_pagina, perguntar_por_pagina=False,
                            max_paginas=args.max_paginas, recomecar=args.recomecar,
                            assunto_raiz=raiz)

        except Parada as e:
            if e.status == 429:
                registrar_429(progresso)
            print("\n" + "!" * 70)
            print(f"  PAREI: {e}")
            print("  Nao vou contornar nem repetir. O progresso ficou gravado -")
            print("  e so rodar de novo depois que voce resolver.")
            print("!" * 70)
            codigo = 2
        except KeyboardInterrupt:
            print("\n\n  Interrompido por voce. O progresso ficou gravado.")
            codigo = 0

        # Sair daqui de DENTRO do bloco do Playwright deixava uma operacao
        # pendente quando a conexao caia, e o Python cuspia
        # "Future exception was never retrieved / TargetClosedError" DEPOIS
        # da mensagem de progresso gravado. Nao era falha - era barulho, e
        # barulho assim esconde erro de verdade. Entao solto o navegador
        # antes e so saio quando o bloco fecha.
        for linha in TEMPOS.relatorio():
            print(linha)
        try:
            nav.close()
        except Exception:
            pass

    if codigo:
        sys.exit(codigo)
    print("\n  Pronto.\n")


if __name__ == "__main__":
    main()
