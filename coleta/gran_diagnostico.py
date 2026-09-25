# -*- coding: utf-8 -*-
"""
gran_diagnostico.py
===================
Por que a tela de questoes nao carrega neste navegador?

SO OBSERVA. Nao coleta, nao pagina, nao clica em nada. Abre um filtro pequeno
e relata o que o servidor respondeu: status, tempo e os cabecalhos de limite
de taxa - que sao a diferenca entre "o Gran esta recusando esta sessao" e "o
meu codigo esta errado".

Existe porque em 24/09 o Firefox carregava e o Chrome dedicado nao, com perfil
novo e cache limpo. Chutar navegador sem esse dado seria trocar no escuro.

USO:
    python gran_diagnostico.py                 (porta 9380, padrao)
    python gran_diagnostico.py --porta 9222
"""

import sys
import io
import json
import time
import argparse

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

FILTRO = ("https://questoes.grancursosonline.com.br/aluno/filtro/concursos"
          "?assunto=407086&nivel=2&desatualizada=0&anulada=0")

# cabecalhos que denunciam estrangulamento
CAB_LIMITE = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
              "x-ratelimit-reset", "ratelimit-remaining", "ratelimit-reset",
              "x-rate-limit-remaining", "cf-ray", "x-cache", "via", "server")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=9380)
    ap.add_argument("--navegador", choices=("chrome", "firefox"),
                    default="chrome",
                    help="'firefox' lanca o Firefox do perfil C:/firefox_gran")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = None
        if args.navegador == "firefox":
            try:
                ctx = p.firefox.launch_persistent_context(
                    "C:/firefox_gran", headless=False, viewport=None)
            except Exception as e:
                print(f"  [ERRO] nao abri o Firefox: {str(e)[:100]}")
                return 1
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            print("  navegador: firefox (perfil C:/firefox_gran)")
        else:
            try:
                b = p.chromium.connect_over_cdp(f"http://localhost:{args.porta}")
            except Exception as e:
                print(f"  [ERRO] nao conectei na porta {args.porta}: {str(e)[:90]}")
                print("         abra o navegador pelo abrir_chrome_gran.bat")
                return 1
            pgs = [x for c in b.contexts for x in c.pages]
            pg = next((x for x in pgs if "grancursos" in x.url), None) or (
                pgs[0] if pgs else b.contexts[0].new_page())
            print(f"  navegador: {b.version}")
        print(f"  aba: {pg.url[:100]}\n")

        eventos, consoles = [], []
        pg.on("console", lambda m: consoles.append((m.type, m.text[:160]))
              if m.type in ("error", "warning") else None)

        def ao_responder(r):
            if "rota-api" not in r.url and "grancursos" not in r.url:
                return
            if "elastic/questao" not in r.url and "/v1/" not in r.url:
                return
            try:
                cab = {k.lower(): v for k, v in r.headers.items()}
            except Exception:
                cab = {}
            eventos.append({
                "url": r.url.split("?")[0].split("/")[-1],
                "status": r.status,
                "limite": {k: v for k, v in cab.items() if k in CAB_LIMITE},
            })

        pg.on("response", ao_responder)

        print("  abrindo um filtro de UMA etiqueta e esperando 25s...")
        t0 = time.time()
        try:
            pg.goto(FILTRO, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  [!] nem o documento carregou: {str(e)[:90]}")
        print(f"     documento em {time.time()-t0:.1f}s")

        for s in range(5):
            pg.wait_for_timeout(5000)
            estado = pg.evaluate("""() => ({
                cards: document.querySelectorAll('.ds-question').length,
                barra: document.querySelectorAll('.pagination-bar__item').length,
                contagem: (() => { const c =
                    document.querySelector('.page__options__count');
                    return c ? c.innerText.split(String.fromCharCode(10)).join(' ').trim()
                             : null; })(),
            })""")
            print(f"     {(s+1)*5:>2}s: {estado['cards']} cards · "
                  f"barra {estado['barra']} · contagem {estado['contagem']!r}")
            if estado["cards"] > 5 and estado["barra"]:
                print("     -> carregou.")
                break

        print(f"\n  {len(eventos)} resposta(s) da API:")
        for e in eventos[-12:]:
            print(f"     {e['status']}  {e['url'][:44]}")
            if e["limite"]:
                print(f"        {json.dumps(e['limite'], ensure_ascii=False)}")
        if not eventos:
            print("     NENHUMA. O app nem tentou pedir - nao e o servidor.")

        ruins = [e for e in eventos if e["status"] >= 400]
        print(f"\n  respostas de erro: {len(ruins)}")
        for e in ruins[:5]:
            print(f"     {e['status']} em {e['url']}  {e['limite']}")

        if consoles:
            print(f"\n  {len(consoles)} erro(s)/aviso(s) no console:")
            for t, m in consoles[-8:]:
                print(f"     [{t}] {m}")

        print("\n  --- leitura ---")
        if not eventos:
            print("  O app nao pediu nada. Ou nao hidratou (JS travado), ou a")
            print("  sessao nao esta valendo. Nao e limite de taxa.")
        elif ruins:
            print("  O servidor RECUSOU. Se vier 429 ou Retry-After, e limite")
            print("  de taxa desta sessao - trocar de navegador nao resolve,")
            print("  so esperar.")
        else:
            print("  O servidor respondeu OK. Se a tela mesmo assim nao mostra,")
            print("  o problema e de renderizacao do app neste navegador.")
        if ctx:
            ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
