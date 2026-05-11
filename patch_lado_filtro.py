"""
patch_lado_filtro.py - Adiciona filtro de LADO (Mais/Menos) no bot_executor.py

Por que: o bot atual aceita Mais E Menos no mesmo jogo, gerando 2 apostas
opostas que se cancelam. Pra mercados over_under, btts, ah, eh, ml etc,
o usuário deve poder definir qual lado o bot opera.

Como funciona:
- Lê filtros do bot (campo JSONB) o atributo `lado` (over/under/ambos)
- No _avaliar_filtros_basicos, se tick.selecao não bater com lado escolhido, rejeita
- Default: 'ambos' (mantém comportamento atual)

Heurística pra reconhecer Mais/Menos:
- selecao contém 'Mais', 'Over', '+' → over
- selecao contém 'Menos', 'Under', '-' → under
- Outros tipos (Casa, Empate, Fora, Sim, Não) → não filtra (passa)

Uso:
    cd C:\\Users\\Administrator\\PyCharmMiscProject\\tipmike_api
    C:\\Users\\Administrator\\PyCharmMiscProject\\.venv\\Scripts\\python.exe patch_lado_filtro.py
    "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor
"""
import re
from pathlib import Path

ARQUIVO = Path("workers/bot_executor.py")

# Função helper a ser inserida ANTES de _avaliar_e_apostar (cole no início, antes da linha 230)
HELPER_FUNC = '''
def _selecao_eh_over_under(selecao: str) -> Optional[str]:
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
    return None


'''

# Bloco que vai INSERIR como filtro novo no _avaliar_e_apostar
# Vai antes do "# 1. Filtros basicos (linha, odds, mercado, blacklist/whitelist pares)"
INSERIR_ANTES_DE = """    # 1. Filtros basicos (linha, odds, mercado, blacklist/whitelist pares)
    passou, motivo = _avaliar_filtros_basicos(tick, bot)"""

NOVO_BLOCO = """    # 0.5. Filtro de LADO (over/under) — bot pode operar so um lado
    filtros = bot.get('filtros') or {}
    lado_bot = (filtros.get('lado') or 'ambos').lower()
    if lado_bot in ('over', 'under'):
        selecao_lado = _selecao_eh_over_under(tick.get('selecao'))
        if selecao_lado is not None and selecao_lado != lado_bot:
            state.contador_rejeicoes['lado'] = state.contador_rejeicoes.get('lado', 0) + 1
            return

    # 1. Filtros basicos (linha, odds, mercado, blacklist/whitelist pares)
    passou, motivo = _avaliar_filtros_basicos(tick, bot)"""


def main():
    if not ARQUIVO.exists():
        print(f"Arquivo {ARQUIVO} nao encontrado. Rode esse patch da pasta tipmike_api/")
        return 1

    conteudo = ARQUIVO.read_text(encoding='utf-8')

    if "_selecao_eh_over_under" in conteudo:
        print("Patch ja aplicado, nada a fazer.")
        return 0

    # 1. Inserir helper antes de "async def _avaliar_e_apostar"
    marker = "async def _avaliar_e_apostar"
    if marker not in conteudo:
        print(f"AVISO: nao encontrei '{marker}' no arquivo.")
        return 1

    conteudo = conteudo.replace(marker, HELPER_FUNC + marker)

    # 2. Inserir bloco do filtro de lado no _avaliar_e_apostar
    if INSERIR_ANTES_DE not in conteudo:
        print("AVISO: nao encontrei o bloco '# 1. Filtros basicos'.")
        return 1

    conteudo = conteudo.replace(INSERIR_ANTES_DE, NOVO_BLOCO)

    ARQUIVO.write_text(conteudo, encoding='utf-8')
    print("OK! Patch aplicado em workers/bot_executor.py")
    print()
    print("Proximos passos:")
    print("1. Setar BOTB pra so operar Over:")
    print()
    print('   "C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe" -U postgres -d mikedb -c "UPDATE bots SET filtros = COALESCE(filtros, \'{}\'::jsonb) || \'{\\"lado\\": \\"over\\"}\'::jsonb WHERE id = 11;"')
    print()
    print("2. Restart o servico:")
    print('   "C:\\nssm-2.24\\win64\\nssm.exe" restart BotExecutor')
    print()
    print("3. Apagar apostas erradas pra ficar so com Over novo:")
    print('   "C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe" -U postgres -d mikedb -c "DELETE FROM apostas WHERE bot_id = 11;"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
