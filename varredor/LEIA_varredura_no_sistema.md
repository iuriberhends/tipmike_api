# Varredor como job do sistema — instalação

Tudo **aditivo**: nenhuma linha do que já roda é alterada. Se der errado, é só
parar o serviço novo e apagar a tabela — o resto do sistema nem percebe.

## Onde cada arquivo vai

| arquivo entregue | vai para | o que é |
|---|---|---|
| `migration_varredura.sql` | rodar no psql | tabela `varredura_jobs` |
| `apostas_export.py` | `workers/apostas_export.py` | formato do export em um lugar só |
| `varredura_job.py` | `workers/varredura_job.py` | executa 1 job de ponta a ponta |
| `run_varredura.py` | `workers/run_varredura.py` | entrada do subprocesso |
| `varredura_daemon.py` | `workers/varredura_daemon.py` | a fila (2 slots) |
| `varredura_router.py` | `routers/varredura.py` | os endpoints |
| `varredura.py` | `mineracao/varredura.py` | o varredor (com a API `varrer()`) |
| `repontua.py` | `mineracao/repontua.py` | mede no holdout |
| `validar_varredor.py` | `mineracao/validar_varredor.py` | o gate T1/T2 |

Crie a pasta `mineracao/` na raiz do projeto. Se preferir outro lugar, defina
`VARREDURA_DIR` no ambiente.

## Passos

**1. Tabela**
```
set PGPASSWORD=mikedb0702&& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb -f migration_varredura.sql
```

**2. Arquivos** — copie conforme a tabela acima.

**3. Registrar o router** no `main.py`:
```python
from routers import varredura
app.include_router(varredura.router)
```
E reinicie: `nssm restart TipMikeAPI`

**4. Testar o miolo SEM fila e SEM front** (é o teste que importa):
```
cd C:\Users\Administrator\PyCharmMiscProject\tipmike_api
python -m workers.run_varredura 1
```
Antes disso, crie um job na mão para ter o id 1:
```
set PGPASSWORD=mikedb0702&& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb -c "INSERT INTO varredura_jobs (user_id, job_backtest_id, nome, params) SELECT user_id, 874, 'teste', '{\"modo\":\"completo\",\"min_apostas\":250,\"guardar\":8000,\"nlmax\":14}'::jsonb FROM backtest_jobs WHERE id=874"
```
Acompanhe:
```
set PGPASSWORD=mikedb0702&& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d mikedb -c "SELECT id,status,progresso,progresso_msg,erro FROM varredura_jobs ORDER BY id DESC LIMIT 5"
```

**5. Serviço da fila** (só depois do passo 4 passar)
```
nssm install TipMikeVarredura "C:\Users\Administrator\PyCharmMiscProject\tipmike_api\.venv\Scripts\python.exe" "-m" "workers.varredura_daemon"
nssm set TipMikeVarredura AppDirectory "C:\Users\Administrator\PyCharmMiscProject\tipmike_api"
nssm start TipMikeVarredura
```

## O que ficou embutido

**Fila com 2 slots, prioridade baixa.** O garimpo cede CPU na hora que o
executor de bots ou os coletores precisarem — sinal perdido é dinheiro,
garimpo atrasado é só espera. Configurável por `VARREDURA_SLOTS`.

**Parada de segurança na estimativa.** Se o plano passar de 400 milhões de
configurações, o job para em `planejado` e mostra o contrato. Você confirma
sabendo que vai levar horas, em vez de descobrir depois. (`--modo total` com 6
janelas no job 201 estima 1,2 **bilhão** — é exatamente esse caso.)

**Holdout obrigatório.** O job separa 30% do fim do período, a busca só enxerga
o treino (`--ate`), e no fim o `repontua` mede as configs achadas nos dias que
ela nunca viu. Sem isso, o painel mostraria ROI de treino como se fosse
resultado — que é o erro que mais custa nesse projeto.

**Gate T1/T2.** No encerramento roda o `validar_varredor`. Se a liquidação ou a
leitura falharem, o job vai para **erro**: garimpo que não passa no carimbo não
deveria nem aparecer na tela.

**Contrato salvo.** Grades de linha, janelas em uso, eixos detectados, cego,
baseline e total estimado ficam no job. Daqui a um mês dá para saber o que
aquela rodada cobriu de verdade.

**Aviso de origem filtrada.** Se o backtest de origem já tinha filtro, fica
registrado — a busca só procura *dentro* da estratégia dele e nunca fora. O
endpoint `/varredura/origens` marca quais jobs são escancarados.

**Faxina.** Job órfão (serviço reiniciou no meio) vira erro com mensagem clara
em vez de barra travada; job que passa de 12h é morto; cancelamento pela tela
mata o processo no ciclo seguinte.

## Endpoints

```
GET  /varredura/origens              backtests elegíveis (marca escancarado)
POST /varredura/jobs                 cria e enfileira
GET  /varredura/jobs                 lista com status e progresso
GET  /varredura/jobs/{id}            detalhe + contrato + resumo
POST /varredura/jobs/{id}/confirmar  libera job parado em 'planejado'
POST /varredura/jobs/{id}/cancelar   mata o processo
GET  /varredura/jobs/{id}/download?tipo=xlsx|tudo|holdout
```

## Duas coisas que eu não fiz

**O front.** É a próxima peça: uma tela com o dropdown de origem, os eixos, a
prévia do contrato e a lista com barra de progresso. Os endpoints já entregam
tudo que ela precisa.

**Paralelismo por par de janela.** Com os 2 slots dá para fazer o "modo turbo"
(um job se divide em 2 processos e funde com o `juntar.py`). Deixei de fora da
v1 de propósito — primeiro o caminho simples tem que estar de pé.
