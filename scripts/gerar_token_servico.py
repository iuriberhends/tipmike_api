"""
scripts/gerar_token_servico.py — Gera um JWT de serviço para bots/workers internos.

Exige TIPMIKE_JWT_SECRET definida (o MESMO segredo usado pela API); caso
contrário o token deixaria de valer no próximo restart da API.

Uso:
    python scripts/gerar_token_servico.py --nome bot_executor --dias 365
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    if not (os.getenv("TIPMIKE_JWT_SECRET") or "").strip():
        print("ERRO: defina TIPMIKE_JWT_SECRET (o mesmo da API) antes de gerar tokens de serviço.")
        print('Gere um segredo com: python -c "import secrets; print(secrets.token_urlsafe(64))"')
        return 1

    parser = argparse.ArgumentParser(description="Gera token de serviço do TipMike.")
    parser.add_argument("--nome", required=True, help="Identificador do serviço (ex.: bot_executor)")
    parser.add_argument("--dias", type=int, default=365, help="Validade em dias (default: 365)")
    args = parser.parse_args()

    nome = (args.nome or "").strip()
    if not (1 <= len(nome) <= 64):
        print("ERRO: --nome deve ter entre 1 e 64 caracteres.")
        return 1
    if not (1 <= args.dias <= 3650):
        print("ERRO: --dias deve estar entre 1 e 3650.")
        return 1

    # Import tardio: só depois de validar que o secret existe.
    from security import criar_token_servico

    try:
        token = criar_token_servico(nome, dias=args.dias)
    except Exception as e:
        print(f"ERRO ao gerar token: {e}")
        return 1

    print("\nToken de serviço gerado (guarde com segurança — ele NÃO fica salvo em lugar nenhum):\n")
    print(token)
    print("\nUse no header:   Authorization: Bearer <token>")
    print("Revogação: apenas trocando TIPMIKE_JWT_SECRET (invalida TODOS os tokens emitidos).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
