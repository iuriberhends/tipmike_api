"""
patch_lados_array.py - Suporta filtro 'lados' como ARRAY (multi-select)

ANTES (patch v1):
    filtros.lado = 'over' | 'under' | 'ambos'
    Single string, restritivo.

DEPOIS (patch v2):
    filtros.lados = ['over'] | ['under'] | ['over','under'] | [] | null
    Array, multi-select.

Regras:
- lados null/undefined/[] -> aceita qualquer lado (default ambos)
- lados = ['over']        -> so aceita Over
- lados = ['under']       -> so aceita Under
- lados = ['over','under']-> aceita ambos (igual a [])

Suporta tambem outras direcoes (sim/nao, casa/empate/fora, par/impar)
no mesmo array, ja deixando preparado pra mais mercados.

Uso:
    cd C:\\Users\\Administrator\\PyCharmMiscProject\\tipmike_api
    C:\\Users\\Administrator\\PyCharmMiscProject\\.venv\\Scripts\\python.exe patch_lados_array.py
    "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor
"""
from pathlib import Path

ARQUIVO = Path("workers/bot_executor.py")

# Helper antigo (mantemos compatibilidade)
OLD_HELPER = '''def _selecao_eh_over_under(selecao: str) -> Optional[str]:
    """
    Retorna 'over', 'under' ou None com base no texto da selecao do tick.
    Reconhece: Mais/Menos, Over/Under, +/-, Sim/Nao.
    Retorna None pra selecoes que nao sao binarias (Casa, Empate, Fora, etc).
    """
    if not selecao:
        return None
    s = selecao.lower().strip()
    if any(w in s for w in ['mais', 'over', 'acima', '+', 'sim']):
        return 'over'
    if any(w in s for w in ['menos', 'under', 'abaixo', '-', 'nao', 'não']):
        return 'under'
    return None'''

# Helper novo (multi-direcao)
NEW_HELPER = '''def _selecao_normalizada(selecao: str) -> Optional[str]:
    """
    Normaliza o texto da selecao do tick pra um lado canonico.
    Reconhece varios mercados:
    - Mais/Over/Acima/+/Sim -> 'over' ou 'sim'
    - Menos/Under/Abaixo/-/Nao -> 'under' ou 'nao'
    - Casa -> 'casa'
    - Empate -> 'empate'
    - Fora/Visitante -> 'fora'
    - Par -> 'par'
    - Impar -> 'impar'
    Retorna None se nao reconhecer (deixa passar).
    """
    if not selecao:
        return None
    s = selecao.lower().strip()
    # BTTS (Ambos Marcam): Sim/Nao
    if s in ('sim', 'yes'):
        return 'sim'
    if s in ('nao', 'não', 'no'):
        return 'nao'
    # Over/Under
    if any(w in s for w in ['mais', 'over', 'acima']) or s.startswith('+'):
        return 'over'
    if any(w in s for w in ['menos', 'under', 'abaixo']) or s.startswith('-'):
        return 'under'
    # ML: Casa/Empate/Fora
    if 'casa' in s or 'home' in s:
        return 'casa'
    if 'empate' in s or 'draw' in s:
        return 'empate'
    if 'fora' in s or 'visitante' in s or 'away' in s:
        return 'fora'
    # Par/Impar
    if s in ('par', 'even'):
        return 'par'
    if s in ('impar', 'ímpar', 'odd'):
        return 'impar'
    return None


# Mantido por compatibilidade (chamado por patch v1 antigo, se ainda existir)
def _selecao_eh_over_under(selecao: str) -> Optional[str]:
    lado = _selecao_normalizada(selecao)
    if lado in ('over', 'sim'):
        return 'over'
    if lado in ('under', 'nao'):
        return 'under'
    return None'''

# Bloco antigo no _avaliar_e_apostar
OLD_BLOCK = """    # 0.5. Filtro de LADO (over/under) — bot pode operar so um lado
    filtros = bot.get('filtros') or {}
    lado_bot = (filtros.get('lado') or 'ambos').lower()
    if lado_bot in ('over', 'under'):
        selecao_lado = _selecao_eh_over_under(tick.get('selecao'))
        if selecao_lado is not None and selecao_lado != lado_bot:
            state.contador_rejeicoes['lado'] = state.contador_rejeicoes.get('lado', 0) + 1
            return"""

# Bloco novo
NEW_BLOCK = """    # 0.5. Filtro de LADOS (array) - bot pode operar 1+ lados, ou nenhum (= ambos)
    filtros = bot.get('filtros') or {}
    lados_bot = filtros.get('lados')
    # Compatibilidade com patch v1: se nao tem 'lados' mas tem 'lado' string, converte
    if lados_bot is None and filtros.get('lado'):
        lado_str = filtros.get('lado').lower()
        if lado_str == 'ambos':
            lados_bot = []
        else:
            lados_bot = [lado_str]
    # Normaliza pra lista de strings lowercase
    if lados_bot and isinstance(lados_bot, list) and len(lados_bot) > 0:
        lados_bot_norm = [str(l).lower().strip() for l in lados_bot if l]
        selecao_lado = _selecao_normalizada(tick.get('selecao'))
        # Se reconhecemos o lado do tick E ele NAO esta nos lados aceitos, rejeita
        if selecao_lado is not None and selecao_lado not in lados_bot_norm:
            state.contador_rejeicoes['lado'] = state.contador_rejeicoes.get('lado', 0) + 1
            return
    # Se lados_bot eh None, [] ou nao-lista, aceita qualquer (default ambos)"""


def main():
    if not ARQUIVO.exists():
        print(f"Arquivo {ARQUIVO} nao encontrado. Rode esse patch da pasta tipmike_api/")
        return 1

    conteudo = ARQUIVO.read_text(encoding='utf-8')

    if "_selecao_normalizada" in conteudo:
        print("Patch v2 ja aplicado, nada a fazer.")
        return 0

    # 1. Troca o helper antigo pelo novo (e mantem compatibilidade)
    if OLD_HELPER in conteudo:
        conteudo = conteudo.replace(OLD_HELPER, NEW_HELPER)
        print("[OK] Helper _selecao_normalizada substituido")
    else:
        print("[AVISO] Helper antigo nao encontrado. Talvez patch v1 nao foi aplicado.")
        return 1

    # 2. Troca o bloco de filtro
    if OLD_BLOCK in conteudo:
        conteudo = conteudo.replace(OLD_BLOCK, NEW_BLOCK)
        print("[OK] Filtro de LADOS atualizado")
    else:
        print("[AVISO] Bloco antigo de filtro nao encontrado.")
        return 1

    ARQUIVO.write_text(conteudo, encoding='utf-8')
    print()
    print("Patch v2 aplicado em workers/bot_executor.py")
    print()
    print("MUDANCAS:")
    print(" - filtros.lado (string) -> filtros.lados (array)")
    print(" - Compatibilidade: bots com filtros.lado='over' continuam funcionando")
    print(" - Bots novos vao usar filtros.lados = ['over'] ou ['over','under'] ou []")
    print(" - Helper _selecao_normalizada agora reconhece: over, under, sim, nao,")
    print("   casa, empate, fora, par, impar")
    print()
    print("Bot 11 ja tem filtros.lado='over' - vai continuar funcionando.")
    print("Pra migrar pra novo formato:")
    print('   psql -c "UPDATE bots SET filtros = filtros - \'lado\' || \'{\\"lados\\": [\\"over\\"]}\'::jsonb WHERE id=11;"')
    print()
    print("Restart:")
    print('   "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
