# TipMike — Autenticação e Usuários (Fase 1)

Sistema de registro/login com JWT (access) + refresh tokens rotacionados, roles
(`admin`/`user`) e suporte a tokens de serviço para os bots/workers internos.

## 1. Dependências novas

```
pip install argon2-cffi PyJWT
```

## 2. Variáveis de ambiente

| Variável | Obrigatória | Default | Descrição |
|---|---|---|---|
| `TIPMIKE_JWT_SECRET` | em produção | efêmero | Segredo dos JWTs (mín. 32 chars). Sem ela, um segredo aleatório é gerado a cada start — funciona, mas todo access token morre no restart. |
| `TIPMIKE_REGISTRO_ABERTO` | não | `0` (fechado) | `1` abre o `POST /auth/registro` ao público. Fechado, só admins autenticados criam usuários. |
| `TIPMIKE_ACCESS_MIN` | não | `30` | Validade do access token em minutos. |
| `TIPMIKE_REFRESH_DIAS` | não | `30` | Validade do refresh token em dias. |
| `TIPMIKE_DSN` | não | DSN do `database.py` | Usada só pelos scripts em `scripts/`. |

Gerar um segredo forte:

```
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

No Windows (VPS), defina de forma persistente e reinicie o terminal/serviço:

```
setx TIPMIKE_JWT_SECRET "<segredo-gerado>"
```

## 3. Passo a passo de instalação

1. `pip install argon2-cffi PyJWT`
2. Rodar a migration: `psql -U postgres -d mikedb -f migrations/013_usuarios_auth.sql`
3. Definir `TIPMIKE_JWT_SECRET` (acima)
4. Criar o primeiro admin: `python scripts/criar_admin.py`
5. Subir a API e testar (abaixo)

## 4. Endpoints

| Método | Rota | Auth | Descrição |
|---|---|---|---|
| POST | `/auth/registro` | opcional | Cria usuário. Fechado por default (403) — abre com env var, ou admin autenticado cria (podendo definir `role`). |
| POST | `/auth/login` | — | Retorna `access_token` + `refresh_token` + dados do usuário. |
| POST | `/auth/refresh` | — | Rotaciona o refresh e emite novo access. Reuso de refresh antigo revoga todas as sessões (proteção contra roubo). |
| POST | `/auth/logout` | — | Revoga o refresh informado (idempotente). |
| POST | `/auth/logout-todas` | Bearer | Revoga todas as sessões do usuário. |
| GET | `/auth/me` | Bearer | Dados do usuário do token. |

## 5. Teste rápido (curl)

```bash
# Login
curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@tipmike.com","senha":"SuaSenha123"}'

# Me (troque <ACCESS> pelo access_token do login)
curl -s http://localhost:8000/auth/me -H "Authorization: Bearer <ACCESS>"

# Refresh
curl -s -X POST http://localhost:8000/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<REFRESH>"}'
```

## 6. Token de serviço (bots/workers)

Quando as rotas forem trancadas (Fase 3), os scripts internos passam a enviar
`Authorization: Bearer <token-de-serviço>`:

```
python scripts/gerar_token_servico.py --nome bot_executor --dias 365
```

Revogação de token de serviço = trocar `TIPMIKE_JWT_SECRET` (invalida todos).

## 7. Decisões de segurança (resumo)

- **Argon2id** (argon2-cffi) para senha, com rehash automático no login quando
  os parâmetros evoluírem.
- **Refresh rotation + detecção de reuso**: cada refresh usado é revogado; se um
  token já revogado reaparecer, todas as sessões do usuário caem.
- Refresh token guardado **hasheado (SHA-256)** — vazamento do banco não vaza sessões.
- **Respostas genéricas** no login (mesma mensagem p/ e-mail inexistente, senha
  errada e conta desativada) + verificação contra hash isca p/ equalizar timing.
- **Rate limit** em memória: login 8/min (por IP e por e-mail), registro 5/5min,
  refresh 30/min.
- `algorithms=[HS256]` fechado no decode (sem downgrade), claims obrigatórias
  (`exp`, `sub`, `type`), `sub` como string (PyJWT ≥ 2.10).
- `get_current_user` **consulta o banco**: desativar usuário corta o acesso na
  hora, mesmo com JWT ainda válido.
- Registro **fechado por default**; `role` do registro só é honrada se quem cria
  é admin.
- Erros de banco viram 500/503 genéricos; detalhes só no log do servidor.

## 8. Limitações conhecidas / próximos passos

- **HTTPS pendente (Fase 0!)** — a API hoje responde em HTTP puro no IP público.
  NÃO usar login em produção sem TLS na frente (Caddy/Cloudflare Tunnel). O front
  no Vercel (HTTPS) inclusive será bloqueado por mixed content ao chamar HTTP.
- Rate limit é por processo (suficiente p/ 1 instância uvicorn).
- Rotas existentes seguem **abertas** — proteção gradual entra na Fase 3:
  `app.include_router(bots.router, dependencies=[Depends(get_current_user)])`.
- Fase 4: coluna `user_id` em `bots` (ownership) — fundação do marketplace.
- Mover o DSN do `database.py` para env var e trocar a senha do Postgres.
