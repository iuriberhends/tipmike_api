# ENTENDIMENTO — Mike Mentor

Resposta ao §7 da spec. Todos os números vêm da coleta real (111 aulas,
8.890 questões de Direito Penal + Direito Penal Militar), calculados por
`verticalizar.py` e `entendimento_dados.py` — nenhum foi estimado.

> **Nada de produção foi escrito e nada foi carregado depois de eu ler esta
> versão da spec.** O que já está no banco entrou antes dela — ver
> "Dívida com a spec anterior", no fim.

---

## 1. O que o sistema faz (com minhas palavras)

O Gran é a fonte: aulas, transcrições, resumos, flashcards e questões. O Mike
Mentor copia esse material da sua conta e o reorganiza pelo edital, em quatro
níveis: matéria → módulo → assunto → aula. A partir daí ele deixa de ser um
arquivo e vira mentor.

Você estuda um bloco (o assunto inteiro, até 60 min). Ele te dá 3 questões
antes, para você errar de propósito, e as questões de verdade depois — só as
que você já tem conteúdo para resolver. Se acertar 70%, a aula fecha e ele
agenda R1, R7, R30 e manutenção mensal até a prova. Se não, manda reestudar.

Cada erro vira explicação, card e prioridade na próxima revisão. Ele sabe o
que cai porque conta nas provas reais da PM-BA, não porque acha. E mostra num
painel onde você está forte, fraco e atrasado.

O princípio por trás de tudo: **testar vale mais que reler**, e o sistema
otimiza aprovação, não domínio.

---

## 2. A árvore completa

Gerada em `edital_verticalizado.yaml` (42 KB, no formato do §3.0.1).
Está como **RASCUNHO — `aprovado_em: null`**, com as pendências dentro do
próprio arquivo, porque o §3.0.1 exige sua revisão antes de valer.

| Matéria | Módulos | Assuntos | Aulas | Assuntos com +1 aula |
|---|---|---|---|---|
| Direito Penal | 8 | 53 | 97 | 23 |
| Direito Penal Militar | 8 | 13 | 14 | 1 |

Bate com a spec (§3.0.1: "Penal: 1 matéria, 8 módulos, 97 aulas").

Trecho, no formato do arquivo:

```yaml
- id: 10
  nome: Direito Penal
  fonte: gran
  gran_materia_id: null            # ver pendência 3
  gran_disciplina_curso_id: '3392'
  modulos:
    - ordem: 2
      nome: '3. Contravenção.'
      gran_topico_id: 2
      item_edital: null            # preenche quando o edital sair
      assuntos:
        - ordem: 1
          nome: Decreto - Lei 3.688/41 (Contravenções Penais)
          aulas:
            - {ordem: 1, parte: 1, tipo: conteudo, duracao_min: 30,
               titulo: 'Decreto - Lei 3.688/41 (Contravenções Penais)'}
            - {ordem: 2, parte: 2, tipo: conteudo, duracao_min: 30,
               titulo: 'Decreto - Lei 3.688/41 (Contravenções Penais) II'}
            # ... até a parte 7
```

**O que mudou na minha cabeça:** eu vinha tratando "Contravenções Penais",
"Contravenções Penais II" … "VII" como **sete coisas**. São **um assunto de
sete partes**. Isso explica, sozinho, por que o meu relatório de ontem dizia
"8 matérias em alerta" quando eram 8 assuntos — e por que "Furto" aparecia
quatro vezes como se fossem quatro prioridades.

---

## 3. Três assuntos com mais de uma aula

**Crimes Contra o Patrimônio - Furto** (módulo 6) — 8 partes, 245 min
```
p1 Furto        36min   p5 Furto V     32min
p2 Furto II     30min   p6 Furto VI    36min
p3 Furto III    32min   p7 Furto VII   32min
p4 Furto IV     31min   p8 Furto VIII  16min
```

**Decreto - Lei 3.688/41 (Contravenções Penais)** (módulo 3) — 7 partes, 214 min

**Crimes Contra a Pessoa - Homicídio** (módulo 4) — 5 partes, 168 min

Nos três, o assunto passa de 60 min → o §3.4 manda dividir o bloco pelas
partes. Nenhum deles vira um bloco só.

---

## 4. Lista de revisão — 2 itens

O script **não decide** nenhum deles (§3.0.1, regra 4). São decisões sobre o
**dado**, diferentes das dúvidas sobre a spec (item 11).

**4.1 — "Da Deserção" aparece duas vezes, com título idêntico**
`CRIMES CONTRA O SERVIÇO MILITAR E O DEVER MILITAR – Da Deserção`
(ordem 1 e ordem 2, módulo 6 de Penal Militar). O Gran não numerou as partes.
→ **Proposta:** parte 1 e parte 2, pela ordem do curso. Você confirma.

**4.2 — tipo das aulas, para confirmação**
Detectei pelo título e preciso da sua confirmação (§3.0):
- `exercicios`: "— Exercícios", "Questões de Consolidação do Aprendizado"
- `revisao`: "— Revisão"
- `apresentacao`: "Apresentação"

Um caso mereceu cuidado: existe a aula **"…e Do Exercício de Comércio"**, que
é **conteúdo** (o crime de exercer comércio), não lista de exercícios. Um
filtro pela palavra solta a classificaria errado; por isso só conto como
rótulo o que vem no começo do título ou depois de um travessão.

Outro: o Gran escreveu **"Exercícos II"** (sem o "i") numa aula. O typo tinha
separado esse assunto em dois lugares até eu tratar.

**4.3 — `gran_materia_id` de Penal Militar**
O coletor não guardou o id da MATÉRIA do Gran, só o da disciplina no curso
(3392 / 363). Para **Direito Penal a própria spec informa (§3.2: `574 / 3392`)**
— já está preenchido no arquivo, não é pergunta. Falta só o de Penal Militar,
que a spec não cita.
→ **Proposta:** pegar da árvore do Gran na próxima coleta. Não bloqueia nada.

*Cuidado com o nome:* o campo `materia` do JSON **não** é a matéria — é o
sub-agrupamento do Gran ("Direito Penal - Parte Geral", "Parte Especial",
"Legislação Especial", "Lei 9.455/97"), que não é nível da nossa hierarquia.

**Espelhos:** nenhum detectado entre Penal e Penal Militar (nenhum título
igual nas duas). O teste real só vale quando as outras 11 matérias entrarem —
a spec cita "Princípios Fundamentais" em Igualdade e em Constitucional.

---

## 5. Dez questões, com requisito (§3.9)

Peguei de assuntos com várias aulas, que é o caso difícil.

| # | Questão | Etiquetas | Candidatas | Requisito | Via | Confiança |
|---|---|---|---|---|---|---|
| 1 | Q3168900 CESPE 2023 | 404290, 404319 | 4 | 4 aulas | conservador | baixa |
| 2 | Q2856129 FCC 2023 | 404290, 404318 | 8 | 9 aulas | conservador | baixa |
| 3 | Q2856132 FCC 2023 | 404290, 404318 | 8 | 9 aulas | conservador | baixa |
| 4 | Q2195368 IADES 2022 | 404290, 404315, 404318 | 10 | 11 aulas | conservador | baixa |
| 5-10 | idem | — | 4 a 13 | 4 a 13 | conservador | baixa |

**Nenhuma das dez caiu em `etiqueta_unica`.** Em toda a base, só **36 de
8.890** (0,4%) têm candidata única. O resto cai na regra conservadora do §3.9
passo 5 — e é aí que o modelo trava. Ver item 11.1.

---

## 6. Contagens

**Questões por via de requisito (§3.9):**

| via | questões | |
|---|---|---|
| conservador (passo 5) | 6.858 | 77,1% |
| cerebro (sem aula candidata) | 1.892 | 21,3% |
| sem_etiqueta | 104 | 1,2% |
| etiqueta_unica (passo 3) | 36 | 0,4% |
| fixacao (passo 1) | **0** | ver nota |

*Nota:* as 1.058 questões de fixação têm **id local** (`q-1`…`q-10`, repetido
em cada aula) e vivem noutra tabela. Nunca são as 8.890 do banco. O passo 1 do
§3.9 se aplica a elas e já nasce resolvido — mas não resolve nenhuma questão
do banco.

**Questões sem aula nenhuma:** 1.996 (22,5%) → fila do Cérebro.

**Por número de alternativas:**

| alternativas | questões | |
|---|---|---|
| 5 | 5.742 | 64,6% |
| 4 | 1.595 | 17,9% |
| 2 (C/E) | 1.553 | **17,5%** |

As 1.553 de C/E ficam fora do gate, fora do placar oficial e fora do simulado
(§3.10), e no máximo 20% de qualquer sessão. Como são 17,5% do acervo, o teto
de 20% por sessão é folgado — não vão sobrar.

---

## 7. Elegibilidade de Crimes Culposos — **o número que trava tudo**

O assunto tem 3 aulas:

| parte | título | etiquetas |
|---|---|---|
| 1 | Crimes Culposos | 406223, 413717, 413750, 413751, 418517 |
| 2 | Crimes Culposos II | 406223, 413755, 418518, 418519 |
| 3 | Crimes Culposos III | 406223, 406277, 413755 |

**Com a regra como está escrita:**

| estudou | questões elegíveis |
|---|---|
| p1 | **0** |
| p1 + p2 | **0** |
| p1 + p2 + p3 | **0** |

**Zero, mesmo depois de estudar o assunto inteiro.** O sistema não teria uma
única questão para mandar.

Mesma conta, tirando só a etiqueta 406223:

| estudou | questões elegíveis |
|---|---|
| p1 | 8 |
| p1 + p2 | 8 |
| p1 + p2 + p3 | 9 |

Funciona — mas 9 questões para 3 aulas, contra o alvo de **45 por aula** e o
mínimo de 20 do §3.8.

---

## 8. Calendário (§3.10) — aula aprovada na quinta, 01/10/2026

**Tudo em dia:**

| | prevista | regra |
|---|---|---|
| R1 | 02/10/2026 (sexta) | D+1 |
| R7 | 08/10/2026 (quinta) | R1 feita + 6 |
| R30 | 31/10/2026 (sábado) | R7 feita + 23 |
| M1 | 30/11/2026 (segunda) | última + 30 |
| M2 | 30/12/2026 (quarta) | última + 30 |
| M3 | 29/01/2027 (sexta) | última + 30 |

Até 28/02/2027 são **3 manutenções**. A M4 cairia em 28/02 (domingo) → vai
para 01/03, fora da janela.

**Com a R1 feita 3 dias atrasada** (feita em 05/10, segunda — a prevista era
sexta e sábado/domingo passaram):

| | prevista | |
|---|---|---|
| R1 | 02/10 → feita 05/10 | atraso de 3 dias |
| R7 | 12/10/2026 (segunda) | 05/10 + 6 = 11/10 (domingo) → segunda |
| R30 | 04/11/2026 (quarta) | |
| M1 | 04/12/2026 (sexta) | |
| M2 | 04/01/2027 (segunda) | |
| M3 | 03/02/2027 (quarta) | |

O calendário inteiro anda junto: o atraso de 3 dias na R1 empurra a R30 de
31/10 para 04/11. O intervalo entre revisões nunca encolhe.

---

## 9. Repetição de uma questão (§3.10)

Questão errada na pós-aula → acertada com **chute** na R1 → acertada com
**certeza** na R7:

| momento | resposta | quando pode voltar | conta no gate? |
|---|---|---|---|
| pós-aula | errou | na próxima revisão (R1), **a própria questão** | **sim** — inédita |
| R1 | acertou com chute | chute = erro → volta na R7, a própria | **não** — repetida |
| R7 | acertou com certeza | não volta por 30 dias | **não** — repetida |

Só a **primeira** conta no gate e no placar oficial. As outras duas são treino:
o §3.10 diz que questão repetida mede memória da resposta, não do conteúdo.

Consequência prática: a R1 tem 3 questões por aula, e se todas forem a volta
planejada dos erros, **nenhuma conta**. Ver item 11.4.

---

## 10. Planejador — 5 dias a partir do zero (§3.11)

Estado: nada estudado. Só Penal e Penal Militar coletadas — e **as duas são
pesadas** (jurídicas). O §3.11 regra 3 proíbe duas vezes a mesma matéria no
dia, e a config pede "2 pesados + 1 leve" até novembro. Como não há matéria
leve coletada, o dia sai com **2 blocos pesados e nenhum leve**.

Fila de vídeo: Penal 83 blocos (49,4 h) · Penal Militar 13 blocos (7,4 h).

| dia | blocos | por quê |
|---|---|---|
| qui 24/09 | Penal: Conceitos Introdutórios **p1** (39min)<br>Militar: Crimes Contra Autoridade (30min) | assunto de 71 min > 60 → divide pelas partes |
| sex 25/09 | Penal: Conceitos Introdutórios **p2** (32min)<br>Militar: Crimes Contra Autoridade (35min) | continua o assunto |
| sáb 26/09 | Penal: Teoria do Crime - Conceito de Crime (29min)<br>Militar: (29min) | assunto < 30 min, sem vizinho no módulo para juntar |
| **dom 27/09** | **livre** | §3.10 |
| seg 28/09 | Penal: Fato Típico - Conduta (43min)<br>Militar: (30min) | assunto inteiro cabe em 60 min |

A ordem dentro da matéria é a do arquivo — nunca pula à frente.

---

## 11. O que não ficou claro ou parece contraditório

### 11.1 A ligação por etiqueta e a elegibilidade se anulam — **RESOLVIDO**

> **Decidido pelo dono em 23/09/2026: tirar a etiqueta raiz.** A regra passa a
> ser: etiqueta presente em **mais de 1/3 dos cadernos da matéria** sai do
> vínculo — medida por matéria, não listada à mão. O custo aceito é a fila do
> Cérebro maior (11.2). O diagnóstico que levou à decisão fica abaixo.


O §3.0 manda ligar a questão a **toda** aula com quem ela divide etiqueta,
ponderando por `peso`. O §3.9 passo 5 manda, quando a confiança é baixa,
exigir **todas as aulas dos assuntos envolvidos**.

Juntas, as duas regras produzem isto:

> **A etiqueta 406223 ("Direito Penal" inteiro) está em 39 dos 95 cadernos de
> aula e em quase toda questão de Penal.**

Medido:

| regra | aulas que cada questão alcança |
|---|---|
| §3.0 literal | **média 38,9** · máximo 53 |
| tirando a 406223 | média 2,6 · máximo 15 |

Com 38,9 aulas por questão, o requisito conservador vira "a matéria inteira", e
**nada nunca fica elegível** — é o zero do item 7.

**Não é problema de coleta: é a taxonomia do Gran.** Ele pendura a raiz da
matéria em todo caderno.

**O que eu proponho, para você decidir:** ignorar no vínculo a etiqueta que
apareça em mais de 1/3 dos cadernos da matéria (medido, não listado à mão).
Hoje isso seria só a 406223 em Penal e a 404318 em Penal Militar.

**O custo dessa escolha:** 5.873 questões (66%) passam a não alcançar aula
nenhuma e caem na fila do Cérebro — ver 11.2.

### 11.2 O volume da fila do Cérebro

Pela regra literal: 1.996 questões sem aula. Tirando a etiqueta genérica:
5.873. A §3.3 fala em lote de 20 por chamada — são 100 a 294 chamadas só de
Penal, e ainda faltam 11 matérias. O §3.12 tem teto de custo, mas não diz o
que fazer quando a fila do requisito não cabe no teto. **Pergunta:** o
requisito pode ficar pendente e a questão simplesmente não ser usada até
alguém resolver?

### 11.3 Suficiência x realidade do acervo

O §3.8 quer **≥45 questões por aula** (mínimo 20), contando só as com
`aula_principal` nela. Medido em Crimes Culposos: **9 questões para as 3
aulas**. Mesmo com a coleta inteira de Penal, o alvo de 45 por aula parece
fora de alcance para a maior parte das 97 aulas. **Pergunta:** o alvo é
aspiracional (e o "insuficiente" vira o estado normal), ou é gatilho para as
medidas do §3.8 (ampliar filtro → provas públicas → questão gerada)?

### 11.4 A R1 pode não ter questão que conte

O §3.10 diz: R1 = 3 questões por aula; o erro volta como **a própria questão**;
e **só inédita conta** no gate e no placar. Numa aula em que o usuário errou 3
na pós-aula, a R1 inteira é repetição → nenhuma questão conta. Some-se o piso
de **60% de inéditas por sessão** e fica ambíguo se a R1 deve ter 3 questões
(2 inéditas + 1 volta) ou 3 voltas + 2 inéditas extras. **Pergunta:** as 3 da
R1 são além das voltas planejadas, ou as incluem?

### 11.5 Tier S pode se apoiar numa questão só

O §3.7 define tier S como "apareceu em prova real da PM-BA". Pela letra, **uma
questão basta** — e é o que o código faz hoje. Conferindo a evidência ontem,
dois assuntos eram tier S por causa de **uma única questão que, lida, é de
outro assunto**: "Crimes Culposos II e III" apoiados numa questão de *erro de
tipo*, e "Dignidade Sexual II (216-A/216-B)" apoiado numa de *importunação
sexual* (215-A). **Pergunta:** tier S com 1 questão vira S mesmo, ou merece um
selo mais fraco até ter 2?

### 11.6 Duas palavras que eu usei erradas e a spec proíbe

- **"tópico"** é proibida (§3.0) exceto em `gran_topico_id`. A tabela central
  do banco que já está no ar se chama `mentor.topico`.
- **"assunto"** para os rótulos do Gran: a spec chama **etiqueta**. Hoje o
  banco tem `assunto_ids`, `mentor.assunto_generico`.

Ambas exigem renomeação. Não mexi — é código de produção, e o §7 manda esperar.

### 11.7 Menor, mas vai incomodar

- **`ordem` das aulas se repete entre módulos** (ordem 1 existe no módulo 1, 2,
  3 e 8). A ordem é por módulo, não global. O §3.11 fala em "ordem do curso" —
  entendi como *módulo, depois ordem dentro dele*. Confirma?
- **`item_edital` está nulo** em todos os módulos. Só dá para preencher quando
  o edital sair (fim de novembro).
- **Matéria 13 (Bahia)** não existe no Gran e não tem fonte definida. Fica
  fora do arquivo por enquanto.

---

## Dívida com a spec anterior

Construí hoje, na versão anterior da spec, e agora está desalinhado:

| o que existe | status |
|---|---|
| `migrations/020` + `021` — schema com `mentor.topico` | **no ar, com vocabulário proibido** |
| `mentor_carga.py` — carga idempotente | funciona, mas carrega a hierarquia de 3 níveis |
| `mentor_tiers.py` — tier e suficiência | lógica aproveitável |
| `routers/mentor.py` — 5 rotas de leitura | aproveitável; consulta nomes que vão mudar |
| 8.890 questões + 111 aulas carregadas | dados bons; estrutura por cima deles é que muda |

**Nada disso foi apagado.** A migração para os quatro níveis precisa da sua
aprovação deste documento primeiro — é o que o §7 manda.

---

## O que precisa de resposta, e de quem

Separei porque não são a mesma coisa — e eu tinha misturado numa lista só.

### Para o dono da spec (11.x)

Perguntas sobre **as regras**, não sobre os dados. Quem escreveu a spec decide.

| | assunto | bloqueia? |
|---|---|---|
| ~~11.1~~ | resolvido pelo dono em 23/09: tirar a etiqueta raiz | **não** |
| 11.2 | volume da fila do Cérebro (1.996 ou 5.873) vs teto de custo | não |
| 11.3 | alvo de 45 questões por aula vs 9 medidas em Culposos | não |
| 11.4 | as 3 questões da R1 incluem as voltas planejadas? | não |
| 11.5 | tier S apoiado em 1 questão vale S? | não |
| 11.6 | renomear `topico` → e `assunto` → `etiqueta` no que já está no ar | não |
| 11.7 | `ordem` é por módulo; `item_edital` nulo; matéria 13 sem fonte | não |

### Para você (§4)

Decisões sobre **o dado coletado**, que o script não pode tomar sozinho:

1. **"Da Deserção"** — duas aulas, título idêntico, ordem 1 e 2. Proponho
   parte 1 e parte 2. Confirma?
2. **Tipo das aulas** — confirmar o que detectei como exercícios/revisão/
   apresentação.
3. **Aprovar a árvore** no `edital_verticalizado.yaml` (está `aprovado_em: null`).

### O que eu resolvi sozinho e não devia ter perguntado

`gran_materia_id` de Penal = **574**, escrito na §3.2 da spec. Eu li a linha,
usei o 3392 dela e perguntei pelo 574 que estava ao lado. Já está no arquivo.
