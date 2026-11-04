# 0009. Lakehouse no Databricks: pipeline medalhão com dados reais, lido por ferramentas MCP fixas

- Status: proposta
- Data: 2026-11-03

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
  quando entra uma fonte nova. Fontes agora: `brigada` (sintético) e `delivery_center`. A
  próxima é o Zomato, quando o arquivo estiver disponível.
- **Ids sem colisão:** restaurante do Delivery Center vira `1_000_000 + store_id`.
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
- **Erros:** falha do driver vira uma mensagem genérica para o modelo, que manda usar as
  ferramentas do Postgres; o detalhe (host, trecho da query) só vai para o log.

## Consequências

- **Melhora:**
  - diagnosticar uma queda vira uma chamada, com a comparação já feita: no dado plantado do
    restaurante 1, o `app` aparece com -100% na última terça e os outros canais normais;
  - prato que parou de vender aparece com zero, não some;
  - o projeto ganha a forma de um pipeline de dados de produção: medalhão, expectativas de
    qualidade, deploy declarativo, serverless, e dado real sujo tratado de forma auditável;
  - os mesmos agentes respondem sobre restaurantes reais, e dizem quando a base não tem o dado
    (itens, promessa de entrega) em vez de inventar.
- **Piora:**
  - mais uma peça: conta no Databricks, token, export e job para rodar;
  - o gold pode estar atrasado: o export não é contínuo (sem CDC);
  - o warehouse do Free Edition desliga quando ocioso, e a primeira consulta pode passar do
    `TOOL_TIMEOUT_SECONDS` padrão (15 s);
  - restaurantes reais não têm gabarito: os evals continuam no sintético, e perguntas sobre os
    reais só têm os testes do pipeline como garantia;
  - o Delivery Center é de 2021: perguntas sobre ele precisam de `--today` em maio de 2021.
- **Como sei que deu certo:**
  - testes locais com Spark (`tests/lakehouse`, job `lakehouse` da CI): o seed e um recorte do
    Delivery Center com os defeitos reais passam pelo upload, pelo pipeline e pelas consultas
    exatas das ferramentas; o gold reproduz os padrões plantados (app -100% na terça; sábado à
    noite com entrega 40% acima do prometido) e o tratamento de cada defeito;
  - com `DELIVERY_CENTER_DIR` apontando para o download, o mesmo pipeline roda sobre a base
    real inteira (fora da CI);
  - no eval ao vivo com o lakehouse ligado: os casos de queda de vendas acertam com menos
    passos que com só o Postgres. Ainda não medido.
- **Próximos passos possíveis:** a fonte Zomato (itens, notas e comentários; os comentários
  entram como texto não confiável, como as avaliações de hoje); casos de eval sobre os
  restaurantes reais com gabarito tirado do gold; ingestão incremental com Auto Loader; CDC do
  Postgres.
