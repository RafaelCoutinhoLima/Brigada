# 0009. Lakehouse no Databricks: pipeline medalhão com dados reais, lido por ferramentas MCP fixas

- Status: proposta
- Data: 2026-11-03 (fonte Zomato, fuso e moeda por fonte: 2026-11-04)

## Contexto

Duas perguntas aparecem o tempo todo e hoje custam várias chamadas ao especialista de Vendas:

- **"Por que minhas vendas caíram na terça?"** O prompt manda separar o dia por canal e
  comparar com o mesmo dia da semana anterior. O modelo faz isso em duas ou três chamadas de
  `sales_by_period` e a conta de comparação fica com ele, que é justamente onde erra.
- **"Qual prato vendeu menos essa semana?"** Exige dois `top_items` (semana atual e anterior)
  e o modelo cruzando as listas. Um prato que parou de vender some da lista, em vez de aparecer
  com zero.

Essas contas (média do mesmo dia da semana nas últimas semanas, zero-fill de dias, canais e
pratos) são agregados históricos: o lugar natural delas é uma camada analítica, calculada uma
vez, e não um `GROUP BY` refeito a cada pergunta no banco transacional.

Restrições que importam:

- **O Postgres continua sendo a fonte da verdade** (ADR 0002): é nele que as escritas
  pendentes, a auditoria e o escopo por restaurante vivem.
- **Custo zero.** O projeto roda com modelos gratuitos; a camada analítica também precisa ser
  gratuita. O Databricks Free Edition é serverless, com cota, um SQL warehouse pequeno e sem
  uso comercial.
- **Nada de SQL gerado pelo modelo**, pelo mesmo motivo do ADR 0002.
- **Falhas nunca silenciosas**: um dado atrasado no lakehouse não pode virar "vendeu zero".
- **CI sem Databricks**: os testes precisam rodar sem conta nem rede.

## Opções consideradas

1. **Calcular tudo no Postgres** (views ou novas ferramentas com SQL mais pesado).
   - Prós: nenhuma peça nova; dado sempre fresco.
   - Contras: mistura carga analítica com a transacional; não exercita o padrão que times de
     dados usam em produção (medalhão, expectativas de qualidade, deploy declarativo).
2. **Lakehouse no Databricks, com o modelo escrevendo SQL no warehouse** (Genie, ou uma
   ferramenta `run_sql`).
   - Prós: cobre qualquer pergunta analítica.
   - Contras: os mesmos problemas do SQL gerado no ADR 0002 (escopo por restaurante dependendo
     do `WHERE` do modelo, fuso, zero-fill, saída difícil de avaliar), agora fora do servidor
     que faz a auditoria.
3. **Lakehouse no Databricks com pipeline medalhão e ferramentas MCP de SQL fixo sobre o
   gold.**
   - Prós: o modelo continua escolhendo só a ferramenta e os argumentos; a regra de negócio
     (fuso, baseline, zero-fill) fica no pipeline, escrita uma vez e testada; as ferramentas
     passam pelo mesmo servidor, com escopo, auditoria e erro mapeado; tudo cabe no Free
     Edition.
   - Contras: mais uma peça para operar; o gold pode estar atrasado em relação ao Postgres;
     ferramentas novas para o modelo escolher.

### Dados reais

O dado sintético tem gabarito (os evals dependem dele), mas é limpo por construção: não exercita
tratamento nenhum. A busca por uma base pública, real e de restaurante brasileiro com pedidos
por prato não achou nenhuma. As opções avaliadas foram Olist (real, brasileira, mas e-commerce),
datasets de "food delivery" do Kaggle (quase todos sintéticos), raspagem do iFood (descartada:
termos de uso, e pega mal numa candidatura ao próprio iFood), Delivery Center (brasileira, 369
mil pedidos de jan a abr de 2021, anonimizada pelos autores, sem itens nem avaliações) e um
histórico de pedidos de restaurantes no Zomato (itens, notas e comentários, mas da Índia).

## Decisão

Opção 3. O Databricks vira a camada analítica, alimentada por uma cópia das tabelas de vendas,
e o modelo só o enxerga por duas ferramentas de leitura com SQL fixo.

- **Várias fontes, um contrato.** Cada base tem um módulo em
  `lakehouse/src/brigada_lakehouse/sources/` que leva o bronze dela ao mesmo silver conformado
  (restaurantes, pedidos com hora local, status, canal, tempo de entrega, distância; itens
  quando a base tem). O gold só lê esse contrato, então as ferramentas e os agentes não mudam
  quando entra uma fonte nova. Fontes agora: `brigada` (sintético), `delivery_center` (Brasil)
  e `zomato` (Índia).
- **Fuso por fonte:** cada módulo declara `TIMEZONE` (`America/Sao_Paulo` no sintético e no
  Delivery Center, `Asia/Kolkata` no Zomato), e as funções de conversão de `common.py` recebem
  o fuso como argumento, em vez de um `LOCAL_TZ` fixo. O dia e a hora locais continuam
  calculados uma vez no silver; `gold_restaurants.timezone` diz qual fuso vale para cada
  restaurante. Alternativa descartada: guardar tudo em UTC e converter na consulta, o que
  espalharia a regra de fuso pelas ferramentas e quebraria o zero-fill por dia local.
- **Moeda por fonte:** cada módulo declara `CURRENCY` (ISO 4217: `BRL`, `INR`). Os valores
  ficam na moeda de origem, nunca convertidos: uma taxa de câmbio de 2024 aplicada a pedidos
  de 2021 seria um número inventado. `currency` entra no contrato de restaurantes, em
  `gold_restaurants` e no resultado das três ferramentas, e os prompts de Vendas e Operações
  mandam usar a moeda devolvida. As ferramentas do Postgres continuam em reais.
- **Ids sem colisão, cabendo em `integer`:** restaurante do Delivery Center vira
  `1_000_000 + store_id` (até 1.004.679) e do Zomato vira `100_000_000 + id` (até
  ~121,5 milhões, abaixo do limite de 2.147.483.647 de `restaurants.id`). Um id do Zomato que
  passaria do intervalo vira NULL e a expectativa `restaurant_present` descarta e conta o
  pedido, em vez de colidir em silêncio.
- **Ingestão:** `scripts/export_lakehouse.py` exporta `restaurants`, `menu_items`, `orders` e
  `order_items` em CSV para um Volume do Unity Catalog. `reviews` fica de fora de propósito: é
  texto de cliente (onde mora a prompt injection) e nada no lakehouse precisa dele.
  `created_at` vai como `created_at_utc`, relógio UTC sem offset, para o Spark ler igual em
  qualquer fuso de sessão.
- **Bases reais sobem como vieram** (`scripts/upload_source.py`): o bronze guarda o original e
  todo tratamento é reexecutável. Os dados não entram no repositório: cada um baixa da fonte.
- **O que o profiling do Delivery Center achou, e o tratamento** (detalhes em
  `sources/delivery_center.py`):
  - horários em **UTC**: o volume por hora tem pico às 15h e às 22h, que só é almoço e jantar
    em UTC-3. Convertidos para America/Sao_Paulo; depois da conversão os picos caem em 12h e
    19h (testado sobre a base inteira);
  - lojas e hubs em Latin-1, nomes com espaços sobrando;
  - valores de pedido zerados ou absurdos (máximo de R$ 1,79 milhão; p99,9 = R$ 1.500):
    expectativa descarta fora de (0, 5000];
  - tempo de ciclo de pedidos que ficaram abertos por dias (p99 ~45 h): acima de 240 min vira
    nulo, mas o pedido continua contando no faturamento;
  - ~20 mil pedidos com mais de uma entrega e distâncias de até 7.251 km: vale a primeira
    entrega concluída, e só até 30 km;
  - 40 canais viram os dois que importam ao dono: marketplace e canal próprio;
  - só lojas FOOD viram restaurantes (384 de 951).
  Resultado na base real: 368.999 pedidos brutos, 321.733 no silver (os outros são de lojas de
  varejo ou falharam numa expectativa).
- **O que o profiling do Zomato achou, e o tratamento** (detalhes em `sources/zomato.py`). É
  um arquivo só, `order_history_kaggle_data.csv`: 21.321 pedidos de set/2024 a jan/2025, 21
  filiais de 6 marcas em Delhi NCR, em rúpias.
  - horários **já em hora local da Índia**, não em UTC: almoço com pico às 13h, jantar às
    20h e nenhum pedido entre 5h e 10h (em UTC o pico do jantar cairia às 1h30 na Índia).
    Lidos como o relógio que são, sem conversão. Os 2.672 pedidos da madrugada (0h a 4h) ficam
    no dia do calendário, como nas outras fontes, e não no "dia de operação" anterior;
  - os cabeçalhos têm espaços e parênteses ("Restaurant discount (Flat offs, Freebies &
    others)"), que o Delta recusa: o bronze passa a gravar nomes de coluna em snake_case (só o
    cabeçalho muda; os valores continuam como vieram, e os nomes das outras fontes já eram
    assim);
  - itens em texto, `"1 x Animal Fries, 2 x Salted Fries"`: todas as 38.232 partes seguem o
    padrão `N x nome`. O mesmo prato aparece duas vezes no mesmo pedido (80 pedidos) e com
    caixa diferente ("OG"/"Og Cheese Burger"): as linhas são somadas pelo nome normalizado;
  - `menu_item_id` = hash de 63 bits (`xxhash64`) de restaurante + nome normalizado: estável
    entre execuções e por filial, porque 190 dos 244 pratos são vendidos por mais de uma.
    Alternativa descartada: um id sequencial por ordem alfabética, que muda quando entra um
    prato novo;
  - **não há preço por item**, só o subtotal do pedido: `unit_price` fica NULL (a expectativa
    passou a ser `unit_price IS NULL OR unit_price >= 0`), e a receita por prato fica NULL em
    todas as linhas do restaurante. Antes, o `coalesce(revenue, 0)` do gold teria escrito zero.
    Estimar o preço a partir de pedidos de um prato só foi descartado: mistura descontos e
    mudanças de cardápio, e seria um número calculado apresentado como fato;
  - **não há tempo de entrega**: `KPT duration` é o preparo na cozinha e `Rider wait time` a
    espera do entregador, nenhum dos dois é do pedido até a porta. `delivery_minutes` fica NULL
    e o `gold_delivery_hourly` passou a contar os pedidos entregues mesmo sem tempo
    (`delivered_orders`, com `timed_orders` à parte), para a distância continuar aparecendo;
  - distância em faixas inteiras ("3km") e `<1km`, que vira 0,5 km: é uma faixa, não falta de
    dado, e nunca zero;
  - status: Delivered vira completed; Rejected, Returned e Timed out viram canceled. Picked up
    e Return cancelled (3 cada, pedidos que nunca fecharam) ficam sem mapa e a expectativa
    `known_status` descarta e conta;
  - um canal só: `Delivery` é "Zomato Delivery" em 100% dos pedidos. Um canal que nunca aparece
    (entrega própria) não entra em `SOURCE_CHANNELS`, porque o zero-fill criaria zeros
    inventados; um valor desconhecido é descartado pela expectativa `channel_present`;
  - `Total` = subtotal + embalagem - descontos em todos os pedidos, sem zero nem absurdo (o
    maior, ₹12.663, é um pedido de 20 pizzas). Mesmo assim `total > 0` é expectativa;
  - a mesma marca tem várias filiais (Aura Pizzas, 5): o nome mostrado é "Marca (subzone)", e
    três ids que aparecem em dois subzones ficam com o mais frequente. Cidade "Delhi NCR", sem
    estado (a região cobre três);
  - **texto de cliente não entra**: `Review` (296 pedidos), `Instructions` (720) e as tags de
    reclamação são texto livre não confiável (ADR 0002), e `Customer ID` identifica uma
    pessoa. Ficam no bronze, nunca chegam ao silver nem a uma ferramenta; há teste com uma
    instrução plantada no comentário. A nota de 1 a 5 (12% dos pedidos, todos entregues) vai
    até o silver em `rating`, mas nenhuma ferramenta a expõe por enquanto: quem avalia escolheu
    avaliar, então a média é enviesada e precisa de cuidado antes de virar resposta.
  - nomes de prato, marca, subzone e cidade chegam ao modelo (`item_trends`,
    `gold_restaurants`) e vêm de um arquivo externo: só passam se parecerem nomes (letras de
    qualquer alfabeto, dígitos, espaço e pontuação básica, até 80 caracteres,
    `common.SAFE_NAME`). O resto vira NULL e é descartado e contado. Todos os nomes do arquivo
    real passam. Isso tira marcação e textos longos do caminho, mas não torna o nome confiável:
    uma frase curta em letras ainda passaria. Alternativa descartada: marcar `item_trends`
    como saída não confiável, o que bloquearia as ferramentas de escrita em qualquer run que a
    usasse, inclusive no sintético.
  Resultado na base real: 21.321 pedidos brutos, 21.315 no silver (21.131 entregues, 184
  cancelados), 21 restaurantes, 38.137 linhas de item e 915 pratos somando todas as filiais.
- **Restaurante real no Postgres é só cadastro.** Runs, auditoria e chaves de API referenciam
  `restaurants.id`, então `scripts/register_lakehouse_restaurants.py` registra os restaurantes
  do gold com `data_source = 'lakehouse'` (migration 0010). As ferramentas do Postgres recusam
  esses restaurantes com uma mensagem que aponta as ferramentas do lakehouse, em vez de
  responder "zero vendas".
- **Pipeline** (Lakeflow Declarative Pipelines, serverless, `lakehouse/src/pipeline.py`):
  - bronze: o CSV como chegou, tudo texto, com arquivo de origem e hora da leitura;
  - silver: tipado com `try_cast` (um valor quebrado vira NULL em vez de derrubar o pipeline
    sob ANSI), deduplicado e com expectativas `expect_all_or_drop` (canal conhecido, total não
    negativo, quantidade positiva); dia e hora locais calculados aqui, uma vez;
  - gold: `gold_restaurants` (fonte e cobertura), `gold_daily_channel_sales` (dia x canal da
    fonte, zero-fill, cancelados à parte, com a média do mesmo dia da semana nas 4 semanas
    anteriores), `gold_item_daily_sales` (dia x prato, só das fontes que têm itens) e
    `gold_delivery_hourly` (somas e contagens de tempo, promessa, atraso e distância por dia e
    hora, para qualquer agrupamento tirar médias certas).
- **Lógica fora do pipeline:** as transformações são funções PySpark puras em
  `lakehouse/src/brigada_lakehouse/`; o arquivo do pipeline só as liga às tabelas, e
  `build.py` liga as mesmas funções numa chamada só para rodar local. Um teste garante que os
  dois definem as mesmas tabelas.
- **Deploy:** Databricks Asset Bundle (`lakehouse/databricks.yml`) cria schema, Volume,
  pipeline e o job de atualização.
- **Leitura:** `sales_vs_baseline`, `item_trends` (Vendas) e `delivery_performance`
  (Operações) no servidor MCP consultam o gold num SQL
  warehouse com parâmetros nomeados. O único texto interpolado é o nome da tabela, montado de
  identificadores da configuração validados por regex. Só são registradas quando a conexão
  está completa (`DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`); sem
  ela o servidor e o especialista de Vendas ficam exatamente como antes.
- **Frescor explícito:** toda resposta traz `data_until`, e `note` avisa quando o dia pedido
  não está no lakehouse ("faltando, não zero"), apontando para `sales_by_period`.
- **Feriados fora da média de referência** (`brigada_lakehouse/holidays.py`). A média "mesmo
  dia da semana nas 4 semanas anteriores" engolia feriados: no PAPA SUCIS (Rio), a Sexta-feira
  da Paixão de 02/04/2021 entrava na média de 30/04 e escondia uma queda de 14,5% (aparecia
  como -2,3%). Agora a média usa os 4 últimos mesmos dias da semana que **não** foram feriado
  para aquele restaurante, buscando até 8 semanas para trás (com menos dias normais,
  `baseline_weeks` diz quantos; com nenhum, a média é NULL). O próprio feriado ganha média de
  dias normais e vem marcado (`is_holiday`, `holiday_name`); `sales_vs_baseline` devolve
  `holiday` no dia e explica no `note`, e o prompt de Vendas manda dizer "foi feriado" antes de
  falar em queda.
  - O calendário cobre só os períodos dos dados, só feriados legais, cada um com a fonte:
    nacionais (Lei 662/1949, Lei 9.093/1995), Rio em 2021 (Lei estadual 9.224/2021, que criou
    feriados de 26/03 a 01/04 e antecipou Tiradentes para 29/03 e São Jorge para 30/03), São
    Paulo capital em 2021 (Decreto municipal 60.131/2021, cinco feriados de 26/03 a 01/04),
    aniversário de SP, São Sebastião no Rio, Navegantes em Porto Alegre, e a lista oficial do
    governo de Delhi (notificações F.53/538, F.53/711 e F.53/689). Ponto facultativo
    (Carnaval) fica de fora: o comércio abre, e em 2021 muitas cidades o cancelaram.
  - Escopo por país, estado e cidade, com `country` novo no contrato de restaurantes. Feriado
    municipal exige estado e cidade iguais; o sintético, sem estado, só recebe os nacionais.
    Delhi NCR cobre três estados e o Zomato não diz qual: a lista de Delhi vale para a região
    toda, uma aproximação.
  - Queda nos dados nunca é fonte de feriado. Em 2021 São Jorge (23/04) foi antecipado no RJ,
    então 23/04 entra na média como dia normal, apesar de as vendas terem caído 39% nesse dia.
    Alternativa descartada: marcar dias atípicos pelo próprio dado (detecção de anomalia), que
    esconderia justamente as quedas que o dono quer ver.
- **Onde o tempo de entrega vai: cozinha ou entrega.** O tempo total sozinho fazia o dono culpar
  o entregador: no IUMPICA (São Paulo), a sexta às 20h leva 61,7 min, e é a cozinha que sobe
  (de 24,8 para 34,9 min), não o trânsito (16,4 para 17,5). O contrato ganhou quatro fases,
  todas NULL quando a fonte não registra:
  - `prep_minutes`: cozinha, do pedido até ficar pronto. No Delivery Center vem de
    `order_metric_production_time`; no Zomato, do KPT, exceto quando a marcação de pronto foi
    "Missed" (aí o KPT é só a hora do clique, média de 0,6 min) ou fora de (0, 120];
  - `pickup_minutes`: de pronto até o entregador sair, a comida esperando o entregador (DC,
    `order_metric_expediton_speed_time`);
  - `transit_minutes`: da saída até o pedido fechar (DC);
  - `rider_wait_minutes`: o entregador esperando a comida no balcão (Zomato). Fica separado de
    `pickup_minutes` de propósito: os dois são "espera no balcão", mas apontam culpados opostos.
  No DC as três fases somam o tempo total (conferido contra os horários brutos); só são
  guardadas juntas, quando o total é válido, nenhuma é negativa e a soma bate com o total em
  até 0,05 min (arredondamento). Isso mantém 96% dos pedidos válidos; nos outros as três ficam
  NULL e o total continua, então a divisão nunca contradiz o total. O
  `gold_delivery_hourly` soma e conta cada fase e o `delivery_performance` devolve as médias
  (`mean_prep_minutes`, `mean_pickup_minutes`, `mean_transit_minutes`,
  `mean_rider_wait_minutes`); o prompt de Operações manda apontar a fase que cresce no horário
  lento e ignorar horários com menos de 20 pedidos (o agente achou "9h com 1 pedido de 0,6 min"
  como hora mais rápida).
- **Dado que a fonte não tem é NULL e vem explicado:** sem itens, `item_trends` diz que a base
  não tem linhas de pedido; sem preço, devolve receita nula, ordena por quantidade e diz por
  quê; sem tempo de entrega, `delivery_performance` devolve `mean_delivery_minutes` nulo e
  avisa no `note`.
- **Erros:** falha do driver vira uma mensagem genérica para o modelo, que manda usar as
  ferramentas do Postgres; o detalhe (host, trecho da query) só vai para o log.

## Consequências

- **Melhora:**
  - diagnosticar uma queda vira uma chamada, com a comparação já feita: no dado plantado do
    restaurante 1, o `app` aparece com -100% na última terça e os outros canais normais;
  - prato que parou de vender aparece com zero, não some;
  - o projeto ganha a forma de um pipeline de dados de produção: medalhão, expectativas de
    qualidade, deploy declarativo, serverless, e dado real sujo tratado de forma auditável;
  - os mesmos agentes respondem sobre restaurantes reais de dois países, em reais e em
    rúpias, e dizem quando a base não tem o dado (itens, preço por prato, promessa ou tempo de
    entrega) em vez de inventar.
- **Piora:**
  - mais uma peça: conta no Databricks, token, export e job para rodar;
  - o gold pode estar atrasado: o export não é contínuo (sem CDC);
  - o warehouse do Free Edition desliga quando ocioso, e a primeira consulta pode passar do
    `TOOL_TIMEOUT_SECONDS` padrão (15 s);
  - o gabarito dos restaurantes reais é o próprio dado: 35 casos calculados em pandas sobre o
    silver dos downloads (`evals/build_real_cases.py`), escritos à parte do gold e do SQL das
    ferramentas (só a lista de feriados é compartilhada), com um teste que exige que os dois
    lados concordem nos recortes. Ele mede se o agente responde certo sobre o que o pipeline
    tratou, não se o tratamento está certo (isso são os testes do pipeline). Pergunta do tipo
    "qual" só entra com 10% de folga entre o 1º e o 2º; "pior dia de abril" saiu porque, sem a
    Sexta-feira Santa na média, três dias ficam a menos de 3 pontos um do outro. O arquivo não
    entra no git (dado de terceiros, nomes reais);
  - o Delivery Center é de 2021 e o Zomato vai até jan/2025: perguntas sobre eles precisam de
    `--today` logo depois do fim de cada base;
  - no Zomato, "qual canal mais cancelou" é trivial (um canal só), "em que hora a entrega
    demora mais" não tem resposta (sem tempo de entrega) e "qual prato faturou mais" só tem
    resposta em quantidade;
  - a nota do Zomato existe no silver, mas ainda não chega às ferramentas.
- **Como sei que deu certo:**
  - testes locais com Spark (`tests/lakehouse`, job `lakehouse` da CI): o seed e recortes do
    Delivery Center e do Zomato, escritos à mão com os defeitos reais, passam pelo upload, pelo pipeline e pelas consultas
    exatas das ferramentas; o gold reproduz os padrões plantados (app -100% na terça; sábado à
    noite com entrega 40% acima do prometido) e o tratamento de cada defeito;
  - com `DELIVERY_CENTER_DIR` e `ZOMATO_FILE` apontando para os downloads, o mesmo pipeline
    roda sobre cada base
    real inteira (fora da CI);
  - no eval ao vivo com o lakehouse ligado: os casos de queda de vendas acertam com menos
    passos que com só o Postgres. Ainda não medido.
- **Próximos passos possíveis:** as ferramentas que os casos `gap` cobram (nota com o número
  de avaliações, desconto, cancelamento por hora e receita perdida, motivo da rejeição;
  recorrência só como contagem agregada, nunca o id do cliente); tempo de preparo e nota do Zomato numa ferramenta (a nota com o viés de seleção
  explicado); comentários do Zomato como texto não confiável, pelo mesmo caminho das avaliações
  de hoje; ingestão incremental com Auto Loader; CDC do
  Postgres.
