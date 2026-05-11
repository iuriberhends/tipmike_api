"""
patch_resolver_esports.py - Ajusta timers do resolver pra E-sports

ANTES:
- Aposta tem que ter 30min antes de tentar resolver
- Ultimo tick com 30min sem novidade pra considerar jogo terminado
- Total: ~45min depois da aposta

DEPOIS (otimizado pra FIFA 2x6, 12min max de jogo):
- Aposta tem que ter 15min antes de tentar resolver
- Ultimo tick com 3min sem novidade pra considerar jogo terminado
- Total: ~15-20min depois da aposta

Uso:
    cd C:\\Users\\Administrator\\PyCharmMiscProject\\tipmike_api
    C:\\Users\\Administrator\\PyCharmMiscProject\\.venv\\Scripts\\python.exe patch_resolver_esports.py
    "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor
"""
from pathlib import Path

ARQUIVO = Path("workers/bot_executor.py")

# ====== TROCA 1: tempo minimo da aposta ======
OLD_1 = "                  AND a.apostado_em < NOW() - INTERVAL '30 minutes'"
NEW_1 = "                  AND a.apostado_em < NOW() - INTERVAL '15 minutes'"

# ====== TROCA 2: tempo sem ticks do jogo ======
OLD_2 = "                if (datetime.now(ultimo_tick.tzinfo) - ultimo_tick).total_seconds() < 1800:"
NEW_2 = "                if (datetime.now(ultimo_tick.tzinfo) - ultimo_tick).total_seconds() < 180:"

# ====== TROCA 3: comentario do docstring (opcional, so pra ficar coerente) ======
OLD_3 = "            # Busca apostas pendentes mais antigas que 30min (provavelmente jogo terminou)"
NEW_3 = "            # Busca apostas pendentes mais antigas que 15min (e-sport FIFA 2x6 = 12min jogo)"


def main():
    if not ARQUIVO.exists():
        print(f"Arquivo {ARQUIVO} nao encontrado. Rode esse patch da pasta tipmike_api/")
        return 1

    conteudo = ARQUIVO.read_text(encoding='utf-8')

    # Idempotencia: se ja tem o INTERVAL '15 minutes', nao faz nada
    if "INTERVAL '15 minutes'" in conteudo and "total_seconds() < 180:" in conteudo:
        print("Patch ja aplicado, nada a fazer.")
        return 0

    trocou_1 = trocou_2 = trocou_3 = False

    if OLD_1 in conteudo:
        conteudo = conteudo.replace(OLD_1, NEW_1)
        trocou_1 = True
    if OLD_2 in conteudo:
        conteudo = conteudo.replace(OLD_2, NEW_2)
        trocou_2 = True
    if OLD_3 in conteudo:
        conteudo = conteudo.replace(OLD_3, NEW_3)
        trocou_3 = True

    if not (trocou_1 and trocou_2):
        print(f"AVISO: nao consegui encontrar todos os blocos pra trocar.")
        print(f"  Aposta INTERVAL 30min: {'OK' if trocou_1 else 'FALHOU'}")
        print(f"  Tick threshold 1800s : {'OK' if trocou_2 else 'FALHOU'}")
        print(f"  Comment docstring    : {'OK' if trocou_3 else 'opcional, ignorado'}")
        if not trocou_1 or not trocou_2:
            print("Aplique manualmente as 2 trocas obrigatorias.")
            return 1

    ARQUIVO.write_text(conteudo, encoding='utf-8')
    print("OK! Patch aplicado em workers/bot_executor.py")
    print()
    print("Mudancas:")
    print(f"  [{'X' if trocou_1 else ' '}] Tempo minimo aposta: 30min -> 15min")
    print(f"  [{'X' if trocou_2 else ' '}] Threshold ultimo tick: 30min -> 3min")
    print(f"  [{'X' if trocou_3 else ' '}] Comment do docstring")
    print()
    print("Tempo MAXIMO de resolucao agora: ~15-20 minutos depois da aposta.")
    print()
    print("Agora reinicia o servico:")
    print('   "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
