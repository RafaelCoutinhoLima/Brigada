# Lakehouse no Databricks Free Edition

Passo a passo para colocar o lakehouse da Brigada de pé numa conta gratuita do Databricks e
ligar as ferramentas `sales_vs_baseline` e `item_trends` no especialista de Vendas. A decisão
e os trade-offs estão no [ADR 0009](adr/0009-lakehouse-databricks.md).

```
Postgres ──export──► Volume (CSV) ──► bronze ──► silver ──► gold ──SQL warehouse──► MCP ──► Vendas
          scripts/export_lakehouse.py   └──── pipeline Lakeflow (serverless) ────┘
```

| Peça | Arquivo |
|------|---------|
| Bundle (schema, Volume, pipeline, job) | `lakehouse/databricks.yml` |
| Pipeline (só a ligação das tabelas) | `lakehouse/src/pipeline.py` |
| Transformações PySpark (a lógica) | `lakehouse/src/brigada_lakehouse/transforms.py` |
| Export para o Volume | `scripts/export_lakehouse.py` |
| Ferramentas MCP (SQL fixo no gold) | `src/brigada/lakehouse/` |
| Testes com Spark local | `tests/lakehouse/` |

## 0. Rodar os testes antes (sem conta nenhuma)

Precisa de Java 17 ou mais novo.

```bash
uv sync --extra lakehouse --group spark
uv run pytest tests/lakehouse
```

Esses testes geram o seed, exportam em CSV, rodam bronze, silver e gold num Spark local e
executam o SQL exato das ferramentas por cima do gold. Se passam aqui, o que sobra para o
Databricks é configuração.

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

## 4. Exportar os dados para o Volume

Na raiz do repositório:

```bash
uv sync --extra lakehouse

# do seu Postgres (o mesmo dado que o servidor MCP lê)
uv run python scripts/export_lakehouse.py

# ou direto do gerador, sem banco
uv run python scripts/export_lakehouse.py --source seed --end-date 2026-10-27
```

Cada tabela vira `/Volumes/workspace/brigada/raw/<tabela>/<tabela>.csv`. Rodar de novo
substitui os arquivos.

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
restaurante 1.

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

Com os três valores preenchidos, o servidor MCP registra `sales_vs_baseline` e `item_trends`
e o especialista de Vendas passa a recebê-las. Com qualquer um vazio, tudo fica como antes.

4. Teste:

```bash
uv run python -m brigada.runtime "Por que minhas vendas caíram na última terça?" \
  --restaurant 1 --today 2026-10-28
uv run python -m brigada.runtime "Qual prato vendeu menos essa semana?" \
  --restaurant 2 --today 2026-10-28
```

No console (`/console`), o especialista de Vendas mostra as duas ferramentas novas.

## Quando algo dá errado

| Sintoma | Causa provável |
|---------|----------------|
| `bundle deploy` reclama do catálogo | O catálogo não se chama `workspace`: veja em **Catalog** e passe `--var catalog=...` |
| Pipeline falha em `bronze_*` com "path does not exist" | O export (passo 4) ainda não rodou, ou foi para outro catálogo/schema |
| A ferramenta responde "lakehouse query failed" | Warehouse parado ou token vencido; o detalhe está no log do servidor MCP |
| `note` diz que o lakehouse só tem dados até um dia anterior | Faltou exportar e rodar o job depois do último seed |
| Timeout na primeira pergunta | Warehouse ligando: aumente `TOOL_TIMEOUT_SECONDS` |

O Free Edition não pode ser usado para fins comerciais. Aqui o dado é todo sintético, gerado
por `scripts/seed_data.py`.
