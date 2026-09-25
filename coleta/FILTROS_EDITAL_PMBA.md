# Filtros de coleta — edital PM-BA Soldado × etiquetas do Gran

Arquivo de referência pra coleta de questões. O arquivo `filtros_edital.json` tem o mesmo conteúdo em formato de máquina.

## Regras gerais

1. Base: edital PM-BA Soldado 2022 (FCC), parte da PM. Quando sair o edital novo (fim de novembro), este arquivo é refeito.
2. Coletar SÓ as etiquetas listadas em 'itens' e em 'evidencia_soldado' (caiu em prova de Soldado da PM/CBM-BA, mesmo fora do edital de 2022). Mais as que forem achadas em 'buscar_por_nome', depois de aprovadas.
3. Nunca usar a raiz da matéria (etiqueta geral). Exceção: raiz que é uma lei só (Lei Maria da Penha [406410]).
4. Etiqueta pai traz todas as filhas. Por isso nunca usar os pais listados em 'nao_coletar' (ex.: História dos Estados Brasileiros [397244], Constituições dos Estados [421825]); usar só as filhas indicadas.
5. 'buscar_por_nome': procurar o nome na árvore de etiquetas do Gran, no lugar indicado, e mostrar id + nome + total antes de coletar.
6. Filtros de toda consulta: nível Médio; só as 12 bancas (FCC, IBFC, FGV, VUNESP, IDECAN, AOCP, SELECON, IBADE, Consulplan, UNEB, CONSULTEC, CEBRASPE); sem anuladas e sem desatualizadas; certo/errado entra.
7. Cortes de ano, contados DEPOIS do filtro de banca: etiqueta com mais de 5.000 questões → só de 2018 em diante; Atualidades → só de 2025 em diante.
8. Não baixar de novo questão que já está no PC. Não apagar nada: questão já coletada fora desta lista ganha a marca fora_edital (campo no JSON) e fica guardada.
9. Exceção: provas de Soldado de PM/Bombeiro de qualquer estado continuam com qualquer banca.
10. Antes de coletar: uma tabela por matéria com etiqueta, nome, total antes e depois do filtro de banca, quantas já estão no PC e quantas faltam. Esperar aprovação.
11. Ritmo: no primeiro 429 real, parar e esperar 3 horas; 'Failed to fetch' com token vencido = recarregar a aba, não é bloqueio.
12. Estatística (percentual de acerto) e comentários (professor e alunos) ficam pendentes: vêm depois, em lote, só das questões que forem usadas.

## Língua Portuguesa — raiz [403587] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Compreensão e interpretação de textos | [403701] Interpretação de Texto · caiu 36× em Soldado<br>_inclui as filhas (pressupostos, sentidos do texto, reescrita, coesão e coerência, inferência, estilística, funções da linguagem)_ |
| 2. Tipologia e gêneros textuais | [403703] Tipologias e Gêneros Textuais · caiu 1× em Soldado<br>_já está dentro de 403701_ |
| 3. Ortografia oficial / 4. Acentuação gráfica | [403613] Ortografia oficial e acentuação gráfica · caiu 4× em Soldado<br>_inclui Ortografia [419321] e Acentuação [403612]_ |
| 5. Classes de palavras | [419336] Classes de palavras (classes gramaticais)<br>[403617] Morfologia · caiu 3× em Soldado<br>_Morfologia [403617] inclui análise morfológica e formação de palavras (3 questões de Soldado)_ |
| 6. Crase / 7. Sintaxe da oração e do período / 8. Pontuação / 9. Concordância / 10. Regência | [403651] Morfossintaxe do período · caiu 12× em Soldado<br>_Morfossintaxe do período: todas as filhas são do edital (análise sintática, crase, pontuação, concordância, regência, colocação, período composto, SE, QUE, termos da oração)_ |
| 11. Significação das palavras | [409314] Semântica (significação das palavras) · caiu 2× em Soldado<br>_Semântica: já está dentro de 403701_ |

**Não coletar**

- [403589] Literatura — Literatura: fora do edital
- [406881] Redação Oficial — Redação Oficial: outra raiz, fora do edital

## Matemática — raiz [403825] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Conjuntos numéricos (inclui complexos), operações e aplicações | [403909] Conjuntos<br>[403826] Aritmética e Fundamentos básicos<br>[408692] Conjunto dos Números Complexos (ou imaginários)<br>_Aritmética e Fundamentos [403826] = operações e aplicações (porcentagem, frações, MMC/MDC)_ |
| 1. Sequências, PA e PG | [403853] Sequência, progressão aritmética e geométrica |
| 2. Álgebra, polinômios, equações e inequações | [403837] Álgebra e equações polinominais · caiu 1× em Soldado |
| 3. Funções | [403844] Funções |
| 4. Sistemas lineares, matrizes e determinantes | [403891] Álgebra Linear |
| 5. Análise combinatória, binômio de Newton e probabilidade | [403868] Análise Combinatória · caiu 2× em Soldado<br>[403869] Probabilidade<br>🔎 procurar: **Binômio de Newton** (dentro de Matemática [403825]) |
| 6. Geometria plana, espacial e analítica | [403871] Geometria · caiu 1× em Soldado |
| 7. Trigonometria | [403857] Trigonometria e Funções Trigonométricas |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [404259] Lógica Proposicional - (Lógica de Primeira Ordem) — Raciocínio Lógico não está no edital de 2022, mas 53 questões de Soldado (provas IBFC) são dele: Lógica Proposicional
- [411115] Raciocínio lógico envolvendo problemas aritméticos, geométricos e matriciais — Raciocínio lógico com problemas aritméticos (prova IBFC)

**Não coletar**

- [404257] Raciocínio Lógico — NÃO usar a raiz Raciocínio Lógico inteira: só as 2 etiquetas da evidência
- [403794] Matemática Financeira — Matemática Financeira: fora do edital

## História do Brasil — raiz [77] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Descobrimento / 2. Brasil Colônia | [416810] Mercantilismo, Colonialismo e a ocupação portuguesa no Brasil · caiu 3× em Soldado<br>[416813] Período Colonial: produção de riqueza e escravismo · caiu 7× em Soldado<br>🔎 procurar: **outras etiquetas de Brasil Colônia (administração, capitanias, expansão territorial)** (dentro de História do Brasil [397241]; nomes com 'Colonial' ou 'Colônia')<br>_Economia colonial: ocupação portuguesa e produção de riqueza/escravismo_ |
| 3. Independência (e 14. Conjuração Baiana, 11. Independência da Bahia) | [416814] Processo de Independência: dos movimentos nativistas à libertação de Portugal · caiu 12× em Soldado<br>_'dos movimentos nativistas à libertação de Portugal'_ |
| 4. Primeiro Reinado | [416815] Brasil Monárquico - Primeiro Reinado 1822- 1831 |
| 5. 'Segundo Reinado (1831-1840)': Período Regencial e Segundo Reinado; 13. Malês; 15. Sabinada | [433737] Brasil Monárquico - Período Regencial - 1831 a 1840<br>[416816] Brasil Monárquico - Segundo Reinado 1840- 1889 |
| 6. Primeira República; 12. Canudos | [416817] República Oligárquica - 1889 a 1930 · caiu 7× em Soldado<br>[397262] Conflitos/Revoluções/Guerras · caiu 3× em Soldado<br>_Conflitos/Revoluções/Guerras [397262] do Brasil entra junto (Canudos, Revolta da Armada, tenentismo)_ |
| 7. Revolução de 1930 / 8. Era Vargas | [416818] Era Vargas - 1930-1945 · caiu 8× em Soldado |
| 9. Presidentes de 1964 até hoje | [416820] República Autoritária: 1964- 1984 · caiu 3× em Soldado<br>[416821] Reconstrução Democrática: Governo Sarney · caiu 1× em Soldado<br>[416822] Reconstrução Democrática: Governo Collor e o Impeachment<br>[416833] Reconstrução da democracia e suas crises<br>🔎 procurar: **todas as 'Reconstrução Democrática: Governo ...' (Itamar, FHC, Lula, Dilma, Temer, Bolsonaro...)** (dentro de História do Brasil [397241]) |
| 10. História da Bahia | [419732] Bahia - BA · caiu 5× em Soldado<br>[420275] Salvador - BA<br>_Bahia - BA e Salvador - BA (e filhas)_ |

**Não coletar**

- [397244] História dos Estados Brasileiros — História dos Estados Brasileiros (pai): puxa todos os estados — só as etiquetas da Bahia entram
- [397242] História Geral — História Geral: fora do edital
- [397245] Teoria em História — Teoria em História: fora do edital
- [397252] Educação em História — Educação em História: fora do edital
- [416819] República de 1954 a 1964 — República de 1954 a 1964: fora do edital
- [397241] História do Brasil — NÃO usar o pai História do Brasil: puxaria os outros estados

## Geografia do Brasil — raiz [49] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Relevo | [397320] Solos e Relevos · caiu 5× em Soldado |
| 2. Urbanização e contingente populacional | [397316] Urbanização · caiu 2× em Soldado<br>[397313] População Brasileira · caiu 3× em Soldado |
| 3. Matriz energética e fontes de energia | [397317] Energia e Industria · caiu 3× em Soldado<br>[416564] Fontes de energia e recursos naturais · caiu 1× em Soldado |
| 4. Problemas ambientais | [397322] Problemas e Catástrofes Ambientais · caiu 4× em Soldado<br>[397291] Problemas e Catastrofes ambientais |
| 5. Clima | [397319] Clima e Domínios Morfoclimáticos · caiu 4× em Soldado<br>[397290] Clima · caiu 3× em Soldado |
| 6. Geografia da Bahia | [400487] Bahia - BA · caiu 9× em Soldado<br>_Bahia - BA dentro de Geografia (9 questões de Soldado)_ |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [397323] Meio Rural — caiu em prova de Soldado PM/CBM-BA (7 questões), fora do edital 2022
- [397329] Regionalização — caiu em prova de Soldado PM/CBM-BA (2 questões), fora do edital 2022
- [397321] Recursos Hídricos — caiu em prova de Soldado PM/CBM-BA (3 questões), fora do edital 2022
- [416553] Vegetação — caiu em prova de Soldado PM/CBM-BA (3 questões), fora do edital 2022
- [398336] Geomorfologia — caiu em prova de Soldado PM/CBM-BA (2 questões), fora do edital 2022
- [397307] Globalização — caiu em prova de Soldado PM/CBM-BA (4 questões), fora do edital 2022
- [419373] Industrialização — caiu em prova de Soldado PM/CBM-BA (1 questões), fora do edital 2022
- [397324] Território — caiu em prova de Soldado PM/CBM-BA (1 questões), fora do edital 2022

**Não coletar**

- [397331] (pai das etiquetas de estado em Geografia) — pai dos estados em Geografia: só Bahia - BA [400487] entra
- [397310] Geografia do Brasil — NÃO usar o pai Geografia do Brasil: puxaria economia/geopolítica fora do edital
- [430453] Astronomia — Astronomia
- [397592] Meteorologia — Meteorologia
- [398915] Topografia — Topografia
- [397312] Geopolítica — Geopolítica (Brasil)
- [397278] Geopolítica — Geopolítica (Geral)
- [397311] Economia — Economia (Brasil)
- [397277] Economia — Economia (Geral)

## Atualidades — raiz [407341] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Globalização | [407352] Globalização · caiu 16× em Soldado<br>[397307] Globalização · caiu 4× em Soldado |
| 2. Multiculturalidade, pluralidade e diversidade cultural | [407362] Arte e Cultura · caiu 5× em Soldado<br>[407421] Arte e Cultura<br>[407424] Direitos Humanos/Direitos Sociais e outras Questões Sociais · caiu 1× em Soldado |
| 3. Tecnologias de informação e comunicação | [407369] Tecnologia · caiu 2× em Soldado |
| Atualidades em geral (Mundo e Brasil), só de 2025 em diante | [407347] Mundo · caiu 17× em Soldado<br>[407400] Brasil · caiu 6× em Soldado<br>_o edital é amplo e a prova cobra fato recente: Mundo e Brasil inteiros, com o corte de ano_ |
| Bahia | [407435] Bahia · caiu 2× em Soldado |

**Não coletar**

- [407431] Conhecimentos Específicos dos Estados — Conhecimentos Específicos dos Estados (pai): só Bahia [407435] entra

## Informática — raiz [405030] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Word/Excel/PowerPoint e Writer/Calc/Impress | [405107] Suítes ou Pacotes de Escritório · caiu 5× em Soldado |
| 2. Windows 7, 10 e Linux / 3. Arquivos e pastas | [405080] Sistemas Operacionais · caiu 3× em Soldado |
| 4. Atalhos, ícones, área de trabalho e lixeira | 🔎 procurar: **Atalhos (teclas de atalho)** (dentro de Informática [405030]) |
| 5. Internet e intranet / 7. Nuvem | [405050] Internet<br>[405161] Intranet · caiu 1× em Soldado |
| 6. Correio eletrônico | [405059] Correio Eletrônico (E-mail) · caiu 1× em Soldado |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [405134] Redes de Computadores — Redes de Computadores: 7 questões de Soldado

**Não coletar**

- [405067] Segurança da Informação — Segurança da Informação: fora do edital, nenhuma questão de Soldado
- [419743] Conceitos básicos em Informática — Conceitos básicos / Hardware: fora do edital

## Direito Constitucional — raiz [405199] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| CF 1.1 Princípios fundamentais (arts. 1º a 4º) | [405212] Princípios fundamentais - Título I - Artigos 1º a 4º da CF · caiu 2× em Soldado |
| CF 1.2 Direitos e garantias fundamentais (Título II) | [405217] Direitos e garantias fundamentais - Título II - Artigos 5º a 17 da CF · caiu 3× em Soldado |
| CF 1.3 Organização do Estado / 1.4 Administração Pública / 1.5 Militares dos Estados | [405231] Organização do Estado - Título III - Artigos 18 a 43 · caiu 1× em Soldado<br>_inclui Administração Pública [405240] e Militares dos Estados [405244]_ |
| CF 1.6 Segurança Pública (art. 144) | [405289] Segurança pública ou segurança pública: organização da segurança pública (artigo 144 da CF) · caiu 7× em Soldado |
| Constituição da Bahia: princípios, direitos, servidores militares, segurança pública (e cap. 'Do Negro', de Igualdade) | [421830] Constituição do Estado do Bahia · caiu 9× em Soldado |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [405260] Poder Executivo (artigos 76 a 91 da CF) — caiu em prova de Soldado PM/CBM-BA (2 questões), fora do edital 2022
- [405288] Forças armadas (artigos 142 e 143 da CF) — caiu em prova de Soldado PM/CBM-BA (1 questões), fora do edital 2022
- [405207] Poder Constituinte — caiu em prova de Soldado PM/CBM-BA (2 questões), fora do edital 2022

**Não coletar**

- [421825] Constituições dos Estados — Constituições dos Estados (pai): só a da Bahia [421830] entra
- [405246] Organização dos Poderes - Título IV da CF - Artigos 44 a 135 — Organização dos Poderes: fora do edital (só o Poder Executivo entra, pela evidência)

## Direitos Humanos — raiz [407069] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Declaração Universal (1948) | [407086] Declaração Universal dos Direitos Humanos · caiu 10× em Soldado |
| 2. Pacto de San José (arts. 1º a 32) | [407112] Convenção Americana sobre Direitos Humanos (Pacto de San José) · caiu 7× em Soldado |
| 3. PIDESC (arts. 1º a 15) | [407088] Pacto Internacional de Direitos Econômicos e Sociais e Culturais · caiu 8× em Soldado |
| 4. Declaração de Pequim | [430371] Declaração de Pequim adotada pela quarta conferência Mundial sobre as mulheres: Ação para igualdade, Desenvolvimento e paz · caiu 1× em Soldado |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [407087] Pacto Internacional de Direitos Civis e Políticos — caiu em prova de Soldado PM/CBM-BA (4 questões), fora do edital 2022
- [407070] Direito Internacional dos Direitos Humanos — caiu em prova de Soldado PM/CBM-BA (9 questões), fora do edital 2022
- [407108] Direitos Humanos no Ordenamento Nacional — caiu em prova de Soldado PM/CBM-BA (3 questões), fora do edital 2022
- [407107] Organização Internacional do Trabalho — caiu em prova de Soldado PM/CBM-BA (1 questões), fora do edital 2022

**Não coletar**

- [407085] Carta da ONU — Carta da ONU: fora do edital

## Direito Administrativo — raiz [404335] (não usar)

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Administração Pública | [404342] Organização Administrativa · caiu 2× em Soldado |
| 2. Princípios | [404358] Regime Jurídico Administrativo · caiu 4× em Soldado |
| 3. Poderes e deveres, uso e abuso do poder, poder de polícia | [404363] Poderes da Administração · caiu 4× em Soldado<br>🔎 procurar: **Deveres dos administradores públicos** (dentro de Direito Administrativo [404335]) |
| 4. Servidores: cargo, emprego e função | [404354] Agentes Públicos · caiu 1× em Soldado |
| 5. Estatuto dos PMs da Bahia (Lei 7.990/2001, arts. 1º a 59) | [399854] Polícia Militar da Bahia - PM BA · caiu 4× em Soldado<br>_Polícia Militar da Bahia - PM BA: inclui a Lei 7.990 [405100] e Legislação Aplicada - PM BA [399855] (8 questões de Soldado). Fica em outra raiz: Legislação dos Órgãos [2924]_ |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [404372] Atos Administrativos — Atos Administrativos: 8 questões de Soldado (está no edital do Bombeiro)
- [404402] Responsabilidade Civil do Estado — caiu em prova de Soldado PM/CBM-BA (1 questões), fora do edital 2022
- [404390] Serviços Públicos — caiu em prova de Soldado PM/CBM-BA (1 questões), fora do edital 2022

**Não coletar**

- [404431] Improbidade Administrativa - Lei 8.429/92 e suas alterações — Improbidade: fora do edital
- [404533] Legislação Administrativa — Legislação Administrativa (licitações etc.): fora do edital
- [404409] Controle da Administração — Controle da Administração: fora do edital

## Igualdade Racial e de Gênero

> Não tem raiz própria no Gran: as leis ficam espalhadas em outras matérias. CF (arts. 1º, 3º, 4º e 5º) e Constituição da Bahia ('Do Negro') já entram por Direito Constitucional.

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 3. Estatuto da Igualdade Racial (Lei 12.288/2010) | [398667] Estatuto da Igualdade Racial - Lei nº 12.288/2010 · caiu 8× em Soldado |
| 4. Crimes de preconceito (Leis 7.716 e 9.459; injúria racial da Lei 14.532/2023) | [406523] Crimes Resultantes de Preconceito de Raça ou Cor - Lei nº 7.716/1989 · caiu 7× em Soldado<br>[401056] Lei nº 9.459/1997 - Altera os arts. 1º e 20 da Lei nº 7.716, de 5 de janeiro de 1989, que define os crimes resultantes de preconceito de raça ou de cor, e acrescenta parágrafo ao art. 140 do Decreto-lei nº 2.848, de 7 de dezembro de 1940. · caiu 2× em Soldado<br>[439136] Lei n° 14.532/2023 - Altera a Lei nº 7.716, de 5 de janeiro de 1989 (Lei do Crime Racial), e o Decreto-Lei nº 2.848, de 7 de dezembro de 1940 (Código Penal), para tipificar como crime de racismo a injúria racial.<br>_7.716 fica em Direito Penal (já coletado)_ |
| 5. Convenção sobre discriminação racial (Decreto 65.810/1969) | [407089] Convenção Internacional sobre Eliminação de Todas as Formas de Discriminação Racial · caiu 3× em Soldado<br>[398579] Decreto nº 65.810/1969 - Promulga a Convenção Internacional sobre a Eliminação de todas as Formas de Discriminação Racial. · caiu 1× em Soldado |
| 6. Convenção sobre discriminação contra a mulher (Decreto 4.377/2002) | [407090] Convenção sobre a Eliminação de Todas as Formas de Discriminação contra a Mulher · caiu 3× em Soldado<br>[418626] Decreto nº 4.377/2002 - Promulga a Convenção sobre a Eliminação de Todas as Formas de Discriminação contra a Mulher, de 1979, e revoga o Decreto no 89.460, de 20 de março de 1984 · caiu 1× em Soldado |
| 7. Lei Maria da Penha | [406410] Lei nº 11.340/2006 - Lei Maria da Penha · caiu 5× em Soldado<br>_é raiz própria de lei: pode usar inteira_ |
| 8. CP art. 140 (injúria) | [406354] Injúria · caiu 4× em Soldado<br>_em Direito Penal (já coletado)_ |
| 9. Tortura (Lei 9.455) | [406526] Lei dos Crimes de Tortura - Lei nº 9.455/1997 · caiu 6× em Soldado<br>_em Direito Penal (já coletado)_ |
| 10. Genocídio (Lei 2.889) | [421171] Lei 2.889/1956 - Genocídio · caiu 5× em Soldado<br>[407101] Convenção para a Prevenção e Repressão do Crime de Genocídio · caiu 1× em Soldado<br>_421171 em Direito Penal (já coletado)_ |
| 11. Lei Caó (7.437/1985) | [399647] Lei nº 7.437/1985 - Inclui, entre as contravenções penais a prática de atos resultantes de preconceito de raça, de cor, de sexo ou de estado civil, dando nova redação à Lei nº 1.390, de 3 de julho de 1951 - Lei Afonso Arinos · caiu 1× em Soldado |
| 12. Lei estadual 10.549/2006 (SEPROMI) | [398689] Lei nº 10.549/2006 - Modifica a estrutura organizacional da administração pública do Poder Executivo do estado da Bahia |
| 13. Lei 10.678/2003 (SEPPIR) | [407139] Sistema Políticas de Promoção da Igualdade Racial (SEPPIR) · caiu 1× em Soldado<br>🔎 procurar: **Lei nº 10.678/2003** (dentro de Legislação Federal [2932]) |

## Direito Penal

> JÁ COLETADO (matéria inteira no nível médio) e com árvore aprovada. Nada a coletar: esta lista só serve pra marcar fora_edital na carga.

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1. Do crime (elementos, tentativa, desistência, arrependimentos, crime impossível, ilicitude, culpabilidade) | [406256] Teoria Geral do Crime · caiu 10× em Soldado |
| 3. Contravenção | [406519] Lei de Contravenções Penais - Decreto-Lei nº 3.688/1941 · caiu 2× em Soldado<br>[413699] Contravenção penal |
| 4. Contra a vida (homicídio, lesão corporal, rixa) | [406315] Crimes Contra a Vida · caiu 1× em Soldado<br>[406320] Lesões Corporais<br>[406333] Rixa |
| 5. Contra a liberdade pessoal | [406335] Crimes Contra a Liberdade Pessoal |
| 6. Contra o patrimônio (furto, roubo, extorsão, apropriação indébita, receptação) | [406361] Furto · caiu 3× em Soldado<br>[406362] Roubo · caiu 2× em Soldado<br>[406364] Extorsão · caiu 1× em Soldado<br>[406369] Apropriação indébita<br>[406371] Da Receptação |
| 7. Contra a dignidade sexual (estupro, importunação, assédio) | [406344] Crimes Contra a Dignidade Sexual |
| 8. Corrupção ativa / 9. Corrupção passiva | [406480] Corrupção ativa · caiu 1× em Soldado<br>[406453] Corrupção passiva · caiu 2× em Soldado |
| 10. Tortura (Lei 9.455) | [406526] Lei dos Crimes de Tortura - Lei nº 9.455/1997 · caiu 6× em Soldado |

**Coletar — caiu em prova de Soldado, fora do edital 2022**

- [406245] Aplicação da Lei Penal — Aplicação da Lei Penal (aula própria aprovada)
- [406334] Crimes Contra a Honra — Crimes contra a Honra (aula própria aprovada)
- [421171] Lei 2.889/1956 - Genocídio — Genocídio (aula própria aprovada)
- [406523] Crimes Resultantes de Preconceito de Raça ou Cor - Lei nº 7.716/1989 — Lei 7.716
- [406408] Associação Criminosa — Associação Criminosa (aula própria aprovada)
- [406432] Peculato — Peculato (assunto aprovado)
- [406446] Concussão — Concussão (assunto aprovado)
- [418522] Erro de tipo — Erro de tipo (aula própria aprovada)

## Direito Penal Militar

> JÁ COLETADO e com árvore aprovada. Nada a coletar: esta lista só serve pra marcar fora_edital na carga.

**Coletar — itens do edital**

| Item do edital | Etiquetas do Gran |
|---|---|
| 1 a 7. Crimes contra a autoridade ou disciplina militar (motim, revolta, conspiração, aliciação, violência contra superior, desrespeito, recusa de obediência, reunião ilícita, publicação ou crítica indevida, resistência) | [404318] Crimes Contra a Autoridade ou Disciplina Militar · caiu 20× em Soldado |
| 8. Crimes contra o serviço militar e o dever militar (deserção, abandono de posto, descumprimento de missão, embriaguez, dormir em serviço) | [404319] Crimes Contra o Serviço Militar e o Dever Militar · caiu 4× em Soldado |
| 9. Crimes contra a Administração Militar (desacato, desobediência, peculato, peculato-furto, concussão) / 10. Prevaricação | [404323] Crimes Contra a Administração Militar · caiu 13× em Soldado |
