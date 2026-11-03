# Lakehouse no Databricks Free Edition

Passo a passo para colocar o lakehouse da Brigada de pé numa conta gratuita do Databricks,
tratar uma base real (Delivery Center) junto com os dados sintéticos e ligar as ferramentas
`sales_vs_baseline`, `item_trends` (Vendas) e `delivery_performance` (Operações). A decisão e
os trade-offs estão no [ADR 0009](adr/0009-lakehouse-databricks.md).

```
Postgres (sintético) ─export─┐
                             ├─► Volume raw/<fonte>/ ─► bronze ─► silver ─► gold ─SQL warehouse─► MCP ─► agentes
Kaggle (Delivery Center) ────┘                        └── pipeline Lakeflow (serverless) ──┘
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

Esses testes geram o seed e um recorte do Delivery Center com os defeitos da base real, rodam
bronze, silver e gold num Spark local e executam o SQL exato das ferramentas por cima do gold.
Com a base real baixada (passo 4), dá para rodar o pipeline inteiro sobre ela:

```bash
DELIVERY_CENTER_DIR=~/Downloads/delivery-center uv run pytest tests/lakehouse -k real -s
```

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
impossível, canal desconhecido).

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

   O id de uma loja do Delivery Center é `1000000 + store_id` (a loja 53 vira `1000053`).
   Para o console, inclua os ids na chave: `API_KEYS=dev-key=1,2,3,1000053`.

5. Teste:

```bash
# sintético
uv run python -m brigada.runtime "Por que minhas vendas caíram na última terça?" \
  --restaurant 1 --today 2026-10-28
# real: a base vai até 30/04/2021, então "hoje" precisa estar logo depois
uv run python -m brigada.orchestration "Como foram minhas vendas em abril e onde a entrega demora mais?" \
  --restaurant 1000053 --today 2021-05-01
```

Nos restaurantes reais, as ferramentas do Postgres respondem que o restaurante só existe no
lakehouse, e os agentes usam as do lakehouse. Perguntas que a base não responde (que prato
vendeu mais, avaliações, tempo prometido) recebem um "essa informação não existe para este
restaurante", não um número inventado.

No console (`/console`), o especialista de Vendas mostra as duas ferramentas novas.

## Quando algo dá errado

| Sintoma | Causa provável |
|---------|----------------|
| `bundle deploy` reclama do catálogo | O catálogo não se chama `workspace`: veja em **Catalog** e passe `--var catalog=...` |
| Pipeline falha em `bronze_*` com "path does not exist" | O export ou o upload (passo 4) daquela fonte não rodou. Para construir só uma fonte: `--var sources=brigada` |
| A ferramenta responde "lakehouse query failed" | Warehouse parado ou token vencido; o detalhe está no log do servidor MCP |
| `note` diz que o lakehouse só tem dados até um dia anterior | Faltou exportar e rodar o job depois do último seed |
| Timeout na primeira pergunta | Warehouse ligando: aumente `TOOL_TIMEOUT_SECONDS` |

O Free Edition não pode ser usado para fins comerciais. O dado sintético é gerado por
`scripts/seed_data.py`; o Delivery Center é público no Kaggle, anonimizado pelos autores, e fica
fora do repositório.
