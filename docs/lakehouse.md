# Lakehouse no Databricks Free Edition

Passo a passo para colocar o lakehouse da Brigada de pé numa conta gratuita do Databricks,
tratar duas bases reais (Delivery Center, do Brasil, e Zomato, da Índia) junto com os dados
sintéticos e ligar as ferramentas
`sales_vs_baseline`, `item_trends` (Vendas) e `delivery_performance` (Operações). A decisão e
os trade-offs estão no [ADR 0009](adr/0009-lakehouse-databricks.md).

```
Postgres (sintético) ─export─┐
Kaggle (Delivery Center) ────┼─► Volume raw/<fonte>/ ─► bronze ─► silver ─► gold ─SQL warehouse─► MCP ─► agentes
Kaggle (Zomato) ─────────────┘                        └── pipeline Lakeflow (serverless) ──┘
```

| Peça | Arquivo |
|------|---------|
| Bundle (schema, Volume, pipeline, job) | `lakehouse/databricks.yml` |
| Pipeline (só a ligação das tabelas) | `lakehouse/src/pipeline.py` |
| Tratamento por fonte | `lakehouse/src/brigada_lakehouse/sources/` |
| Gold (agregados) | `lakehouse/src/brigada_lakehouse/gold.py` |
| Export do sintético | `scripts/export_lakehouse.py` |
| Upload de base real | `scripts/upload_source.py` |
| Cadastro dos restaurantes reais no Postgres | `scripts/register_lakehouse_restaurants.py` |
| Ferramentas MCP (SQL fixo no gold) | `src/brigada/lakehouse/` |
| Testes com Spark local | `tests/lakehouse/` |

## 0. Rodar os testes antes (sem conta nenhuma)

Precisa de Java 17 ou mais novo.

```bash
uv sync --extra lakehouse --group spark
uv run pytest tests/lakehouse
```

Esses testes geram o seed e recortes do Delivery Center e do Zomato escritos à mão com os
defeitos de cada base real, rodam bronze, silver e gold num Spark local e executam o SQL exato
das ferramentas por cima do gold. Com as bases reais baixadas (passo 4), dá para rodar o
pipeline inteiro sobre elas:

```bash
DELIVERY_CENTER_DIR=~/Downloads/delivery-center \
ZOMATO_FILE=~/Downloads/zomato/order_history_kaggle_data.csv \
  uv run pytest tests/lakehouse -k real -s
```

Saída esperada: `368999 raw orders, 321733 kept, 384 FOOD restaurants` no Delivery Center e
`21321 raw orders, 21315 kept` com 21 filiais no Zomato.

## 1. Criar a conta

1. Entre em <https://www.databricks.com/learn/free-edition> e faça o cadastro (dá para usar a
   conta Google). O workspace é criado na hora.
2. Anote a URL do workspace, algo como `https://dbc-1234abcd-5678.cloud.databricks.com`.

O Free Edition já vem com um catálogo do Unity Catalog chamado `workspace` e um SQL warehouse
serverless pequeno. É isso que o bundle usa.

## 2. Instalar e autenticar a CLI

```bash
# macOS
brew tap databricks/tap && brew install databricks
# Linux / WSL
curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh

databricks auth login --host https://SEU-WORKSPACE.cloud.databricks.com --profile DEFAULT
```

O login abre o navegador. Com `--profile DEFAULT`, a CLI e o SDK Python (usado no export)
encontram a mesma credencial sem configuração extra.

## 3. Fazer o deploy do bundle

```bash
cd lakehouse
databricks bundle validate
databricks bundle deploy
```

Isso cria, no catálogo `workspace`:

- o schema `brigada`;
- o Volume `brigada.raw`, para onde vão os CSVs;
- o pipeline `brigada_medallion` (serverless);
- o job `brigada_refresh`, que roda o pipeline do zero.

Outro catálogo ou schema: `databricks bundle deploy --var catalog=X --var schema=Y` (e as
mesmas variáveis `LAKEHOUSE_CATALOG` / `LAKEHOUSE_SCHEMA` no `.env`).

## 4. Subir os dados para o Volume

Na raiz do repositório, `uv sync --extra lakehouse` e depois as duas fontes.

**Sintético** (o mesmo dado que o servidor MCP lê do Postgres):

```bash
uv run python scripts/export_lakehouse.py                     # do Postgres
uv run python scripts/export_lakehouse.py --source seed --end-date 2026-10-27   # sem banco
```

**Delivery Center** (base real, 369 mil pedidos de jan a abr de 2021):

1. Baixe em <https://www.kaggle.com/datasets/nosbielcs/brazilian-delivery-center> e descompacte.
2. Suba os arquivos como vieram (o tratamento é do pipeline):

```bash
uv run python scripts/upload_source.py delivery_center ~/Downloads/delivery-center
```

**Zomato** (base real, 21 mil pedidos de 21 filiais em Delhi NCR, de set/2024 a jan/2025, em
rúpias):

1. Baixe o histórico de pedidos do Zomato no Kaggle (arquivo `order_history_kaggle_data.csv`)
   e descompacte numa pasta.
2. Suba a pasta como veio; o arquivo vira a tabela `orders`:

```bash
uv run python scripts/upload_source.py zomato ~/Downloads/zomato
```

Tudo vai para `/Volumes/workspace/brigada/raw/<fonte>/<tabela>/`. Rodar de novo substitui os
arquivos. Os dados não entram no repositório.

## 5. Rodar o pipeline

```bash
cd lakehouse
databricks bundle run brigada_refresh
```

Na interface, em **Jobs & Pipelines > brigada_medallion**, aparece o grafo bronze, silver e
gold. Clique numa tabela silver para ver as expectativas de qualidade: quantas linhas
passaram e quantas foram descartadas.

Para conferir, no **SQL Editor**:

```sql
SELECT day, channel, orders, revenue, baseline_revenue, revenue_change_pct
FROM workspace.brigada.gold_daily_channel_sales
WHERE restaurant_id = 1 AND day = '2026-10-27'
ORDER BY channel;
```

O canal `app` deve vir com 0 pedidos e `revenue_change_pct = -100`: é a queda plantada no
restaurante 1. Para ver os restaurantes reais:

```sql
SELECT restaurant_id, name, city, completed_orders, data_from, data_until
FROM workspace.brigada.gold_restaurants
WHERE source = 'delivery_center'
ORDER BY completed_orders DESC LIMIT 10;
```

Para olhar o tratamento, abra `silver_delivery_center_orders` no grafo do pipeline: a aba de
qualidade mostra quantos pedidos cada expectativa descartou (valor zerado ou absurdo, data
impossível, canal desconhecido). Em `silver_zomato_orders` devem aparecer 6 descartados por
`known_status` (3 "Picked up" e 3 "Return cancelled", pedidos que nunca fecharam).

Cada restaurante traz a própria moeda e o próprio fuso:

```sql
SELECT source, currency, timezone, count(*) AS restaurants, min(data_from), max(data_until)
FROM workspace.brigada.gold_restaurants
GROUP BY ALL;
```

O Zomato não tem preço por prato nem tempo de entrega: em `gold_item_daily_sales` a coluna
`revenue` dos restaurantes dele é NULL (a quantidade está certa), e em `gold_delivery_hourly`
`timed_orders` é 0 (a distância está lá). Os comentários de cliente ficam só no bronze.

## 6. Ligar as ferramentas na Brigada

1. **Token:** foto do perfil > **Settings > Developer > Access tokens > Generate new token**.
2. **Warehouse:** **SQL Warehouses >** o warehouse serverless **> Connection details**. Copie
   *Server hostname* e *HTTP path*.
3. No `.env`:

```bash
DATABRICKS_SERVER_HOSTNAME=dbc-1234abcd-5678.cloud.databricks.com
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123def456
DATABRICKS_TOKEN=dapi...
# o warehouse desliga quando fica ocioso; a primeira consulta pode levar uns 20 s
TOOL_TIMEOUT_SECONDS=60
```

Com os três valores preenchidos, o servidor MCP registra `sales_vs_baseline`, `item_trends` e
`delivery_performance`, e os especialistas de Vendas e Operações passam a recebê-las. Com
qualquer um vazio, tudo fica como antes.

4. Cadastre os restaurantes reais no Postgres (runs e chaves de API precisam do id). Antes,
   aplique a migration `db/migrations/0010_restaurant_data_source.sql`:

```bash
uv run python scripts/register_lakehouse_restaurants.py --min-orders 500 --dry-run
uv run python scripts/register_lakehouse_restaurants.py --min-orders 500
```

   O id de uma loja do Delivery Center é `1000000 + store_id` (a loja 53 vira `1000053`) e o
   de uma filial do Zomato é `100000000 + Restaurant ID` (a 20320607 vira `120320607`). Para
   o console, inclua os ids na chave: `API_KEYS=dev-key=1,2,3,1000053,120320607`.

5. Teste:

```bash
# sintético
uv run python -m brigada.runtime "Por que minhas vendas caíram na última terça?" \
  --restaurant 1 --today 2026-10-28
# real: a base vai até 30/04/2021, então "hoje" precisa estar logo depois
uv run python -m brigada.orchestration "Como foram minhas vendas em abril e onde a entrega demora mais?" \
  --restaurant 1000053 --today 2021-05-01
# Zomato: vai até 31/01/2025 e os valores voltam em rúpias (currency = INR)
uv run python -m brigada.orchestration "Que prato caiu mais na última semana de janeiro?" \
  --restaurant 120320607 --today 2025-02-01
```

Nos restaurantes reais, as ferramentas do Postgres respondem que o restaurante só existe no
lakehouse, e os agentes usam as do lakehouse. Perguntas que a base não responde recebem um
"essa informação não existe para este restaurante", não um número inventado: no Delivery
Center, que prato vendeu mais, avaliações e tempo prometido; no Zomato, faturamento por prato
(só a quantidade existe) e tempo de entrega.

No console (`/console`), o especialista de Vendas mostra as duas ferramentas novas.

## 7. Eval com dados reais

Os casos de avaliação dos restaurantes reais saem dos próprios downloads, calculados em pandas
sobre o silver, sem passar pelo gold nem pelas consultas das ferramentas
(`evals/build_real_cases.py`, detalhes em `evals/README.md`). O agente responde lendo o
lakehouse no Databricks: se os dois discordarem, é defeito em algum dos lados.

1. Confira no **SQL Editor** que o Databricks construiu o mesmo que os testes locais. Se um
   número não bater, o problema está no deploy, não no agente:

```sql
-- pedidos no silver: 321733 (delivery_center) e 21315 (zomato)
SELECT source, count(*) FROM workspace.brigada.silver_orders GROUP BY source;
-- moeda, fuso e país por fonte
SELECT source, country, currency, timezone, count(*)
FROM workspace.brigada.gold_restaurants GROUP BY ALL;
-- linhas de feriado: 3798 (delivery_center) e 145 (zomato)
SELECT source, count(*) FROM workspace.brigada.gold_daily_channel_sales
WHERE is_holiday GROUP BY source;
-- PAPA SUCIS: 02/04 é Sexta-feira da Paixão; a média de 30/04 soma ~8339,65
SELECT day, holiday_name, sum(revenue), sum(baseline_revenue)
FROM workspace.brigada.gold_daily_channel_sales
WHERE restaurant_id = 1000053 AND day IN ('2021-04-02', '2021-04-30') GROUP BY ALL;
-- IUMPICA: cozinha 24,84 min em média
SELECT round(sum(prep_minutes_sum) / sum(prep_count), 2)
FROM workspace.brigada.gold_delivery_hourly WHERE restaurant_id = 1000676;
```

2. Com o pipeline rodado (passo 5) e o `.env` preenchido (passo 6), cadastre os restaurantes
   reais no banco do eval (`brigada_eval`, com a migration 0010 aplicada):

```bash
DATABASE_URL=postgresql://brigada:brigada@localhost:55434/brigada_eval \
  uv run python scripts/register_lakehouse_restaurants.py --min-orders 100
```

3. Gere os casos e confira o harness com o oracle (deve dar 100%). O build compara o gabarito
   com o gold feito dos mesmos arquivos e para se divergirem:

```bash
uv run python -m evals.build_real_cases --delivery-center ~/Downloads/delivery-center \
  --zomato ~/Downloads/zomato/order_history_kaggle_data.csv
uv run python -m evals.run --cases evals/cases_real.jsonl --answerer evals.oracle:answer
```

4. Rode ao vivo:

```bash
DATABASE_URL=postgresql://brigada:brigada@localhost:55434/brigada_eval TOOL_TIMEOUT_SECONDS=60 \
  uv run python -m evals.run --cases evals/cases_real.jsonl --concurrency 2
```

5. Leia à mão as respostas que falharam nos casos "não existe" e no `real-dc-015` antes de
   confiar na nota (limitações em `evals/README.md`), e veja à parte o subconjunto `derived`,
   que mede também a conta do modelo.

O arquivo `evals/cases_real.jsonl` não entra no git (dado de terceiros, com nomes reais).
Depois de mudar o pipeline, rode o job com **full refresh**: o gold muda de formato.

### Pelo navegador, sem a CLI

Os passos 3 e 5 também podem ser feitos na interface:

- **Volume:** Catalog > `workspace` > criar schema `brigada` e Volume `raw` > Upload. O que
  importa é a pasta: `raw/delivery_center/<tabela>/` (orders, stores, hubs, channels,
  deliveries) e `raw/zomato/orders/` com o `order_history_kaggle_data.csv`.
- **Código:** Workspace > Create > Git folder com a URL do repositório.
- **Pipeline:** Jobs & Pipelines > Create > ETL pipeline, serverless, catálogo `workspace`,
  schema `brigada`, código-fonte o notebook `lakehouse/src/pipeline.py` da Git folder, e em
  Configuration: `bundle.sourcePath` = a pasta `lakehouse/src` da Git folder,
  `brigada.raw_path` = `/Volumes/workspace/brigada/raw`, `brigada.sources` =
  `brigada,delivery_center,zomato`. Start com full refresh.

## Quando algo dá errado

| Sintoma | Causa provável |
|---------|----------------|
| `bundle deploy` reclama do catálogo | O catálogo não se chama `workspace`: veja em **Catalog** e passe `--var catalog=...` |
| Pipeline falha em `bronze_*` com "path does not exist" | O export ou o upload (passo 4) daquela fonte não rodou. Para construir só uma fonte: `--var sources=brigada` |
| A ferramenta responde "lakehouse query failed" | Warehouse parado ou token vencido; o detalhe está no log do servidor MCP |
| `note` diz que o lakehouse só tem dados até um dia anterior | Faltou exportar e rodar o job depois do último seed |
| Timeout na primeira pergunta | Warehouse ligando: aumente `TOOL_TIMEOUT_SECONDS` |

O Free Edition não pode ser usado para fins comerciais. O dado sintético é gerado por
`scripts/seed_data.py`; o Delivery Center e o Zomato são públicos no Kaggle (o Delivery Center
anonimizado pelos autores, o Zomato com o cliente num hash) e ficam fora do repositório.
