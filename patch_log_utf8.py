"""
patch_log_utf8.py - Aplica 1 patch no bot_executor.py: força UTF-8 no logging

Por que: Python 3.14 no Windows usa cp1252 nos handlers de log por padrão.
Isso quebra os emojis (✅ 📊) que o bot_executor usa pra info/stats.
Sem o patch, as linhas com emoji vão pro stderr como Logging Error,
mesmo que o programa continue rodando.

Uso:
    cd C:\\Users\\Administrator\\PyCharmMiscProject\\tipmike_api
    C:\\Users\\Administrator\\PyCharmMiscProject\\.venv\\Scripts\\python.exe patch_log_utf8.py
    "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor
"""
import re
from pathlib import Path

ARQUIVO = Path("workers/bot_executor.py")

OLD = """# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('bot_executor')"""

NEW = """# Logging - forca UTF-8 no stdout pra emojis nao quebrarem (Python 3.14 + Windows = cp1252)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('bot_executor')"""


def main():
    if not ARQUIVO.exists():
        print(f"Arquivo {ARQUIVO} nao encontrado. Rode esse patch da pasta tipmike_api/")
        return 1

    conteudo = ARQUIVO.read_text(encoding='utf-8')

    if "sys.stdout.reconfigure" in conteudo:
        print("Patch ja aplicado, nada a fazer.")
        return 0

    if OLD not in conteudo:
        print("AVISO: bloco de logging nao encontrado no formato esperado.")
        print("Aplique manualmente: adicione antes do logging.basicConfig:")
        print("    try:")
        print("        sys.stdout.reconfigure(encoding='utf-8', errors='replace')")
        print("        sys.stderr.reconfigure(encoding='utf-8', errors='replace')")
        print("    except Exception:")
        print("        pass")
        return 1

    novo = conteudo.replace(OLD, NEW)
    ARQUIVO.write_text(novo, encoding='utf-8')
    print("OK! Patch aplicado em workers/bot_executor.py")
    print("Agora reinicia o servico:")
    print('    "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
