"""
scripts/criar_admin.py — Cria ou promove um usuário administrador.

Uso (na raiz do projeto, com o venv ativo):
    python scripts/criar_admin.py

Conexão: usa a env var TIPMIKE_DSN se definida; caso contrário, o DSN de database.py.
Pré-requisito: migration 013_usuarios_auth.sql já executada.
"""

import asyncio
import getpass
import os
import re
import sys

# Permite importar módulos da raiz do projeto quando rodado de qualquer pasta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from security import hash_senha  # noqa: E402

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def _dsn() -> str:
    dsn = (os.getenv("TIPMIKE_DSN") or "").strip()
    if dsn:
        return dsn
    try:
        from database import DSN  # fallback: DSN atual do projeto
        return DSN
    except Exception:
        print("ERRO: defina TIPMIKE_DSN ou rode a partir da raiz do projeto (database.py).")
        sys.exit(1)


async def main() -> int:
    print("── Criar/promover administrador do TipMike ──")

    email = input("E-mail: ").strip().lower()
    if not _EMAIL_RE.match(email or ""):
        print("E-mail inválido.")
        return 1

    nome = " ".join(input("Nome: ").split())
    if not (2 <= len(nome) <= 80):
        print("Nome deve ter entre 2 e 80 caracteres.")
        return 1

    senha = getpass.getpass("Senha (mín. 8, com letra e número): ")
    if (
        not (8 <= len(senha) <= 128)
        or not any(c.isalpha() for c in senha)
        or not any(c.isdigit() for c in senha)
    ):
        print("Senha fraca: mínimo 8 caracteres, com pelo menos uma letra e um número.")
        return 1
    if getpass.getpass("Confirme a senha: ") != senha:
        print("Senhas não conferem.")
        return 1

    try:
        conn = await asyncpg.connect(_dsn())
    except Exception as e:
        print(f"ERRO ao conectar no banco: {e}")
        return 1

    try:
        existente = await conn.fetchrow(
            "SELECT id, role FROM usuarios WHERE lower(email) = $1", email
        )
        if existente:
            resp = input(
                f"Usuário já existe (id={existente['id']}, role={existente['role']}). "
                "Promover a admin e redefinir a senha? [s/N] "
            ).strip().lower()
            if resp != "s":
                print("Cancelado.")
                return 0
            await conn.execute(
                "UPDATE usuarios SET nome = $1, senha_hash = $2, role = 'admin', ativo = true WHERE id = $3",
                nome, hash_senha(senha), existente["id"],
            )
            print(f"OK: usuário {existente['id']} agora é admin (senha redefinida).")
        else:
            row = await conn.fetchrow(
                "INSERT INTO usuarios (email, nome, senha_hash, role) VALUES ($1, $2, $3, 'admin') RETURNING id",
                email, nome, hash_senha(senha),
            )
            print(f"OK: admin criado com id={row['id']}.")
        return 0
    except asyncpg.UndefinedTableError:
        print("ERRO: tabela 'usuarios' não existe. Rode a migration 013 antes.")
        return 1
    except Exception as e:
        print(f"ERRO: {e}")
        return 1
    finally:
        try:
            await conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
