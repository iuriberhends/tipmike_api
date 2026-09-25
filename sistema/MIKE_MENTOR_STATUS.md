# MIKE MENTOR — estado do projeto (23/09/2026)

Informativo de repasse. Cobre o que existe, o que foi descoberto sobre o Gran,
e as armadilhas que já custaram tempo — para não serem redescobertas.

---

## 1. Onde estamos

| Fase (da spec) | Estado |
|---|---|
| **0** — coletor validado em 1 aula + 1 filtro | ✅ concluída |
| **1** — varredura completa de Direito Penal | ✅ concluída |
| **2** — banco + carga + painel | 🟡 **banco e carga prontos; PAINEL NÃO COMEÇOU** |
| 3 — motor de agenda + bot Telegram | ⬜ |
| 4 — Cérebro (explicação de erro, destilado, SRS) | ⬜ |
| 5 — simulados + diagnóstico + redação | ⬜ |
| 6 — as outras 11 disciplinas | 🟡 coletor blindado (puxado pra frente); coleta não começou |

**Saímos da ordem da spec em 23/09 e vale registrar por quê.** Depois da carga,
a tarde foi para o coletor — metade por necessidade, metade fora de ordem:

- **Era caminho crítico:** recoletar os `assunto_ids`. Sem o vínculo
  questão↔tópico (§3.3.1), tier (§3.7) e suficiência (§3.8) davam número
  falso — quase tudo caía em "insuficiente" por falta de vínculo, não por
  falta de questão. Passou de 634 para 6.894 questões ligadas.
- **NÃO era:** blindar o coletor contra o 429 (`--coletar-pela-tela`,
  `--completar-leves`). Isso é Fase 6. Foi puxado pra frente com o contexto
  fresco do 429, e trabalho que teria que ser feito de todo jeito — mas
  atrasou a Fase 2.

**O que está parado: o painel.** O Direito Penal inteiro está no JSON e no
schema `mentor`, e não existe nenhuma tela lendo isso. É o próximo passo.

---

## 2. O que já foi coletado

**Aulas: 111** (Direito Penal 97 + Direito Penal Militar 14)

- Todos os campos preenchidos em 111/111: `titulo`, `disciplina`, `modulo`,
  `ordem`, `professor`, `duracao`, `transcricao`, `resumo`, `resumo_bolso`,
  `mapa_mental`, `pdf_degravacao`
- 111 PDFs de degravação (48,6 MB), 0 corrompidos
- 2.270 flashcards · 1.058 questões de fixação (com gabarito **e** explicação
  por alternativa — servem direto ao fluxo "explicar erro" da spec §3.3.3)
- 2.543.959 caracteres de texto, 0 problemas de acentuação
- Ausências reais do Gran (não da coleta): 4 aulas sem `questoes_fixacao`,
  1 sem `flashcards` — gravadas como `null`

**Questões: 7.247**

- Direito Penal: 6.821 · Direito Penal Militar: 426 (100% do filtro)
- `enunciado`, `alternativas`, `gabarito`, `banca`, `ano`, `orgao`, `assuntos`,
  `indice_acerto`, `dificuldade`: **100% preenchidos**
- 0 duplicadas · 0 divergências de gabarito · 205 bancas · anos 2001–2026
- Índice de acerto: 7% a 100%, média 71,6%
- **Sem comentário do professor** (escolha de tempo: dobrava a coleta de 5h
  para 9,5h). Dá para buscar depois sem recoletar nada.

> ⚠️ **PENDÊNCIA ABERTA — ler antes de escrever a carga.**
> A primeira coleta gravou o assunto de cada questão **só pelo nome**, sem o id.
> O `caderno_questoes` de cada aula amarra **por id**, e é dele que sai o
> vínculo questão↔aula da spec (§3.3.1). Ou seja: **as 7.247 questões já
> coletadas não têm a chave da junção.**
>
> Já corrigido no extrator — `assuntos[]` agora traz `id`, `nome`, `raiz`, `pai`,
> mais um `assunto_ids[]` achatado. **As próximas coletas nascem certas.**
>
> **Consertar as já coletadas está BLOQUEADO por rate limit.** O modo
> `--completar-assuntos` relê só a listagem (69 páginas), mas levou **cinco
> 429 seguidos**, inclusive na primeira chamada depois de 30 min de espera.
> Testado e descartado: pausa de 2,5s, 8s, e mandar os mesmos cabeçalhos do
> app (Origin/Referer/User-Agent). Nada resolveu.
>
> O único ritmo **comprovadamente seguro** é o da varredura original: uma
> chamada de listagem a cada ~4 minutos (lá as estatísticas as espaçavam
> naturalmente) — 69 páginas passaram sem um 429 sequer.
>
> **Opções, em ordem de risco:**
> 1. Deixar pra Fase 6: ao coletar as outras 11 disciplinas no ritmo normal,
>    tudo já sai com id. Penal se resolve numa releitura futura.
> 2. Uma noite com `python gran_extrator.py --completar-assuntos --disciplina "Penal" --pausa 240`
>    (~4,5 h, no ritmo que já se provou seguro).
> 3. **Não** insistir com pausas curtas. É o que faz o Gran endurecer.
>
> **Nada disso bloqueia a Fase 2**: modelar o banco, escrever a carga e subir o
> painel funcionam com o que já existe. O vínculo questão↔aula entra depois,
> num `UPDATE` — as 111 aulas já guardam o `caderno_questoes`, então o outro
> lado da ponte está preservado.

> ⚠️ **SEGUNDA PENDÊNCIA — questões que não são só texto.**
> 6 das 6.821 de Penal (0,09%) estão **sem enunciado**: têm alternativas,
> gabarito e estatística, mas nenhum texto de pergunta. São questões cujo
> enunciado é **imagem** ou que pertencem a um **grupo** (aquele "leia o texto
> e responda as questões 10 a 13", em que o texto comum fica fora de cada uma).
> Ids: `58650, 58652, 1061970, 1061971, 1061972, 1061973`.
>
> A API sinaliza isso em `hasImage`, `hasImageItens` e `grupoQuestao` — e a
> primeira coleta **descartou os três**. Já corrigido: agora grava
> `tem_imagem`, `tem_imagem_nas_alternativas`, `grupo_questao`, `discursiva` e
> um `so_texto` derivado. Nas questões já coletadas esses campos não existem.
>
> **Por que importa:** o bot do Telegram (§3.5) manda a questão como texto.
> Questão com enunciado em imagem chega quebrada, e hoje não há como saber
> quais são sem reler. Na carga, tratar `enunciado` vazio como questão
> inutilizável (excluir da pós-aula e do simulado) resolve o caso conhecido.

**Onde está:** `E:\MikeBacktest\MikeCode\saida\`
```
saida/aulas/<disciplina>/<ordem>_<titulo>.json   (+ .pdf ao lado)
saida/questoes/<disciplina>/<disciplina>.json
saida/_progresso.json
```

---

## 3. Arquivos do projeto

| Arquivo | Para que serve |
|---|---|
| `abrir_chrome_gran.bat` | Abre o Chrome dedicado (CDP **9380**, perfil `C:\chrome_gran`). O usuário loga **na mão** aqui; nenhum script recebe senha. |
| `gran_inspecionar.py` | Só inspeciona páginas (não extrai). Foi com ele que a API foi mapeada. Modos: `--espiar N`, `--rede`, `--sondar-api`, `--sondar-questao`, `--sondar-listagem`, `--sondar-endpoints`. |
| `gran_extrator.py` | O coletor. |
| `rodar_gran_penal.bat` | Roda a coleta de Penal inteira sem supervisão, com log. |
| `gran_extracao.log` | Log da última execução. |

### Comandos

```bat
:: teste de um item (valida formato)
python gran_extrator.py --aula
python gran_extrator.py --questoes --max-paginas 1

:: varredura
python gran_extrator.py --curso --disciplina "Direito Penal"
python gran_extrator.py --todas-questoes --disciplina "Direito Penal" --por-pagina 100 --sem-comentarios

:: tudo (Fase 6)
python gran_extrator.py --curso
python gran_extrator.py --todas-questoes --por-pagina 100
```

Opções: `--sim` (não pergunta, para rodar dormindo) · `--forcar` (refaz aulas já
coletadas) · `--recomecar` (questões: ignora o progresso) · `--max-paginas N` ·
`--nivel` (padrão `Médio`) · `--url TEXTO` (escolhe a aba).

**Pré-requisito:** Chrome aberto pelo `.bat`, logado, com **uma aba de aula** e
**uma aba do Gran Questões** abertas. O modo `--todas-questoes` usa as duas (a
da aula para a árvore do curso, a de questões para o token).

---

## 3b. Onde cada arquivo mora

**A coleta roda no PC, a carga roda na VPS.** O extrator depende do Chrome
logado (que está no PC); o Postgres está na VPS. Os JSONs viajam entre os dois.

### Fica no PC — `E:\MikeBacktest\MikeCode\`
```
abrir_chrome_gran.bat      abre o Chrome dedicado (CDP 9380)
gran_inspecionar.py        inspeção das páginas
gran_extrator.py           o coletor
rodar_gran_penal.bat       coleta sem supervisão
mentor_carga.py            (cópia; roda de verdade na VPS)
MIKE_MENTOR_STATUS.md      este arquivo
saida/                     gerado pela coleta
```

### Vai para a VPS

| Arquivo | Destino |
|---|---|
| `020_mentor_schema.sql` | `<tipmike_api>/migrations/` |
| `mentor_carga.py` | `<tipmike_api>/` (ou `scripts/`) |
| `saida/aulas/` + `saida/questoes/` + `_progresso.json` | pasta de dados, ex.: `<tipmike_api>/dados_m/` |

**71,4 MB** no total (8,1 JSON de aulas + 48,6 PDFs + 14,6 JSON de questões).
**Não copiar `saida/_inspecao/`** — são 10,6 MB de diagnóstico, sem uso em produção.

O `mentor_carga.py` aceita `--saida <pasta>`, então os dados podem ficar em
qualquer lugar. Sem esse parâmetro, procura `saida/` ao lado do script.

### Sequência na VPS
```bash
psql "$MIKEDB_DSN" -f migrations/020_mentor_schema.sql
python mentor_carga.py --simular --saida dados_m    # confere
python mentor_carga.py --tudo    --saida dados_m    # carrega
```
O `--tudo` roda em transação única: falhou, dá rollback e não grava pela metade.

### Os PDFs
`aula.pdf_path` guarda caminho relativo à pasta de dados, com barra normal
(`aulas/Direito Penal/07_....pdf`) — funciona no Windows e no Linux. A API
prefixa a própria base na hora de servir. Os 111 PDFs precisam estar num
diretório que o `tipmike_api` consiga ler.

## 4. A API do Gran (mapa completo)

São **três hosts**:

### Área do aluno — `www.grancursosonline.com.br` (cookies de sessão)
```
GET /aluno/sala-de-aula/curso/co/<cursoId>            → nome do curso
GET /aluno/sala-de-aula/video/co/<cursoId>/a/<aulaId> → título, código, legenda
GET /aluno/curso/listar-conteudo-aula/codigo/<cursoId>/tipo/video
                                                      → as 12 disciplinas
      .../disciplina/<discId>                         → os tópicos (módulos do edital)
      .../disciplina/<discId>/conteudo/<topicoId>     → as aulas
POST /aluno/sala-de-aula/artefatos                    → as 6 URLs assinadas
GET /aluno/espaco/download-resumo/codigo/<cursoId>/c/<fk_material_resumo> → PDF
```

O POST de artefatos leva:
```json
{"type":"VIDEO","disciplinaNome":"...","aulaNome":"...","cursoId":"...",
 "aulaId":"...","disciplinaId":"3392","arquivos":{"legenda":"<player.caption.src>"},
 "artifactTypes":["summary","transcript","review","quiz","mindmap","flashcards"]}
```

### Conteúdo — `ltp.infra.grancursosonline.com.br` (URLs assinadas do CloudFront)
```
/conteudos/video/<codigoAula>/resumo.md       → resumo
/conteudos/video/<codigoAula>/review.md       → resumo de bolso
/conteudos/video/<codigoAula>/mindmap.md      → mapa mental
/conteudos/video/<codigoAula>/transcript.json → transcrição (tones.<defaultTone>.content)
/conteudos/video/<codigoAula>/flashcards.json → decks[].cards[]
/conteudos/video/<codigoAula>/quiz.json       → questions[] com gabarito e explicação
```
As URLs **expiram e não podem ser montadas à mão** — vêm do POST de artefatos.

### Questões — `rota-api.grancursosonline.com.br`
Exige `Authorization: Bearer <localStorage["auth/token"]>` **e**
`X-Client-Id: 2u8s7nrp2bthg4pdv8lvbh54b8` (identificador público do app, não é
credencial do usuário).
```
GET /v1/elastic/questao?perPage=100&page=N&assunto[]=<id>&nivel[]=Médio
                        &resolucao=TODAS&anulada=0&desatualizada=0
GET /v1/questao/<id>/estatisticas              → {acertos, erros, brancos, total}
GET /v1/comentario/questao/<id>?professor=1    → comentário do professor
```

**O gabarito é o campo `resposta`** — o id do item correto, que bate com
`itens[].id`. Confirmado em 20/20 questões e cruzado com o texto do comentário
do professor ("Letra X" / "GABARITO: X") em 12/12, sem divergência.
**Não é preciso responder questão nenhuma** — o que preservaria a estatística
real do usuário no Gran.

---

## 5. Armadilhas (não repetir)

1. **O Gran derruba sessão por acesso simultâneo** (`401` com "Usuário
   desconectado por acessos simultâneos"). Disparou sozinho em 60s de teste.
   Não usar o Gran em outra janela/aparelho durante a coleta. No 401, **parar** —
   nunca reautenticar em laço.
2. **reCAPTCHA v3 invisível em toda página** do Gran Questões. Procurar a palavra
   "recaptcha" no HTML dá alarme falso em 100% das páginas. Checar captcha
   *visível* (iframe > 80px) e frases de bloqueio no texto *visível*.
3. **Playwright síncrono não entrega eventos de CDP durante `time.sleep()`.**
   Usar `page.wait_for_timeout()` em qualquer espera que dependa de rede.
4. **Ids base64 vêm codificados na URL e crus no JSON.** `jMysMmHHepM%3D` na URL,
   `jMysMmHHepM=` na API. Misturar faz a aula "não existir" sem erro nenhum.
   Cuidado especial com ids que contêm `+` e `/`.
5. **`video["disciplina_id"]` = 574 é o id da MATÉRIA**; o que as URLs usam é
   `video["conteudo"]["disciplina_id"]` = 3392.
6. **Arquivos `.md` chegam sem charset** e viram cp1252 (`VocÃª`). Pegar os bytes
   e decodificar como UTF-8 explicitamente.
7. **Comentários vêm com entidades nomeadas** (`&ccedil;`) e HTML malformado
   (`<p>` aberto duas vezes). Usar `html.unescape` e quebrar linha na abertura
   *e* no fechamento das tags de bloco.
8. **`POST /v1/comentario/<id>/like` curte de verdade.** Não disparar sem querer.
9. **O POST de artefatos pode GERAR conteúdo por IA**, não só ler. A resposta traz
   `action`: se vier diferente de `RETURNED_EXISTING`, o extrator para de
   propósito — gerar em massa custa dinheiro ao Gran e é padrão suspeito.
10. **Filtro de disciplina por trecho pega demais**: "Direito Penal" também casa
    "Direito Penal Militar". Já corrigido (nome exato ganha do parcial).
11. **`%` em texto de ajuda do argparse quebra o programa** — e `py_compile` não
    pega, só executar a CLI.

---

## 6. Decisões tomadas

- **Não responder questão nenhuma.** O gabarito vem da API; responder sujaria a
  estatística real do usuário no Gran.
- **Estatística (`indice_acerto`) é obrigatória** — custa 1 requisição por
  questão (o grosso do tempo de coleta), mas é a dificuldade real que ordena a
  pós-aula, a R7 e o simulado.
- **Comentário do professor ficou de fora nesta rodada** (dobrava o tempo).
  Pode ser buscado depois, por questão, sem recoletar o resto.
- **Filtro de escolaridade `nivel[]=Médio`** (`nivel=2` na tela). Cortou o
  acervo de Direito Penal de ~32.500 para 6.818 questões, sem perder nada
  pertinente ao concurso de Soldado.
- **Todas as bancas entram** — variedade testa conhecimento; e o edital ainda
  não saiu (previsto para o fim de novembro), então não dá para calibrar por
  banca real ainda.
- **Ritmo:** 2 a 3 segundos entre ações, sem paralelismo, retomável, para em
  captcha/bloqueio/401.
- `anulada=0&desatualizada=0` na origem: questões anuladas e desatualizadas
  **não são coletadas**. Consequência: a coluna `desatualizada` da spec ficará
  sempre `false`.

---

## 7. Vocabulário — duas armadilhas

**"tópico" quer dizer coisas diferentes:**

- Na **spec**: `topico` é 1:1 com uma aula (§3.2).
- No **Gran**: "tópico" é o item do edital que agrupa dezenas de aulas.

No JSON coletado, o do Gran foi chamado de **`modulo`** justamente para não
colidir. Direito Penal tem 8 módulos (tópicos do edital) e 97 aulas.

**"matéria" ≠ "assunto"** — o dono corrigiu isso em 23/09, e ele usa o
vocabulário de quem estuda:

| palavra | o que é | exemplos |
|---|---|---|
| **matéria** (= disciplina) | a cadeira do edital | Direito Penal, Direito Penal Militar |
| **assunto** | o recorte dentro dela | Crimes Culposos, Erro de Proibição, Contravenções Penais, Dolo |
| **aula** | a videoaula (= `topico` da spec) | "Crimes Culposos II" |

Eu escrevi "8 matérias no alerta" quando eram 8 **assuntos** de uma matéria só.
Não é preciosismo: falar errado com quem estuda faz o relatório parecer
dizer outra coisa — "8 matérias em alerta" soa como metade do edital em risco,
quando é um pedaço de Direito Penal.

---

## 8. O que falta

**Imediato (Fase 2):** o **painel lendo do banco** — é o único pedaço da
Fase 2 que falta. Modelar o Postgres (§3.2) e a carga idempotente já estão
feitos: `migrations/020_mentor_schema.sql` (21 tabelas + `v_questao_usavel`),
`mentor_carga.py` e `mentor_tiers.py`, todos rodados e conferidos na VPS.

O painel vive em `tipmike_api` (mentor.tipmike.br) e lê do schema `mentor`.
Nada o bloqueia hoje.

Pontos de atenção para a carga:
- `modulo` (Gran) ≠ `topico` (spec)
- `flashcards` estão agrupados em decks; a tabela `card` da spec é plana
- `questoes_fixacao` já trazem gabarito e explicação — origem `gran`
- o campo `caderno_questoes` de cada aula é a URL do Gran Questões **já filtrada
  pelos assuntos daquela aula**: é o vínculo aula↔questões pronto, que a spec
  esperava resolver com o Cérebro (§3.3.1)

**Fase 6:** as outras 11 disciplinas — 696 aulas restantes (~4h) mais as
questões. É o mesmo comando sem `--disciplina`.
