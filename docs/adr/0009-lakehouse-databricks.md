# 0009. Lakehouse no Databricks: pipeline medalhão como camada analítica, lido por ferramentas MCP fixas

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

## Decisão

Opção 3. O Databricks vira a camada analítica, alimentada por uma cópia das tabelas de vendas,
e o modelo só o enxerga por duas ferramentas de leitura com SQL fixo.

- **Ingestão:** `scripts/export_lakehouse.py` exporta `restaurants`, `menu_items`, `orders` e
  `order_items` em CSV para um Volume do Unity Catalog. `reviews` fica de fora de propósito: é
  texto de cliente (onde mora a prompt injection) e nada no lakehouse precisa dele.
  `created_at` vai como `created_at_utc`, relógio UTC sem offset, para o Spark ler igual em
  qualquer fuso de sessão.
- **Pipeline** (Lakeflow Declarative Pipelines, serverless, `lakehouse/src/pipeline.py`):
  - bronze: o CSV como chegou, tudo texto, com arquivo de origem e hora da leitura;
  - silver: tipado com `try_cast` (um valor quebrado vira NULL em vez de derrubar o pipeline
    sob ANSI), deduplicado e com expectativas `expect_all_or_drop` (canal conhecido, total não
    negativo, quantidade positiva); dia e hora locais calculados aqui, uma vez;
  - gold: `gold_daily_channel_sales` (dia x canal, zero-fill, com a média do mesmo dia da semana
    nas 4 semanas anteriores) e `gold_item_daily_sales` (dia x prato, zero-fill sobre todos os
    pratos do restaurante).
- **Lógica fora do pipeline:** as transformações são funções PySpark puras em
  `lakehouse/src/brigada_lakehouse/transforms.py`; o arquivo do pipeline só as liga às
  tabelas. Assim os testes rodam o mesmo código num Spark local.
- **Deploy:** Databricks Asset Bundle (`lakehouse/databricks.yml`) cria schema, Volume,
  pipeline e o job de atualização.
- **Leitura:** `sales_vs_baseline` e `item_trends` no servidor MCP consultam o gold num SQL
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
    qualidade, deploy declarativo, serverless.
- **Piora:**
  - mais uma peça: conta no Databricks, token, export e job para rodar;
  - o gold pode estar atrasado: o export não é contínuo (sem CDC);
  - o warehouse do Free Edition desliga quando ocioso, e a primeira consulta pode passar do
    `TOOL_TIMEOUT_SECONDS` padrão (15 s).
- **Como sei que deu certo:**
  - testes locais com Spark (`tests/lakehouse`, job `lakehouse` da CI): o seed passa pelo
    export, pelo pipeline e pelas consultas exatas das ferramentas, e o gold reproduz o padrão
    plantado (app -100% na terça, total do dia igual ao gabarito do gerador);
  - no eval ao vivo com o lakehouse ligado: os casos de queda de vendas acertam com menos
    passos que com só o Postgres. Ainda não medido.
- **Próximos passos possíveis:** ingestão incremental com Auto Loader no lugar do export
  completo; CDC do Postgres; mais tabelas gold (entregas por hora).
