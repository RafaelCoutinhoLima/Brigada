# 0007. Busca de avaliações: embeddings via OpenRouter, vetorial por padrão

- Status: proposta
- Data: 2026-10-30

## Contexto

O especialista de cardápio precisa achar "o que os clientes dizem sobre X" quando X é um tema
("comida fria", "embalagem"), não um prato. As avaliações chegam escritas de muitos jeitos
("chegou frio", "comida gelada"), estão em português e são texto não confiável (há injeções
plantadas no restaurante 3).

Restrições:
- sem framework de RAG;
- sem baixar um modelo local, porque o disco está cheio e torch ocupa gigabytes;
- o CI não pode chamar uma API;
- o escopo por restaurante continua do lado confiável;
- ler texto de cliente desliga as escritas (taint);
- falhas nunca são silenciosas.

## Opções consideradas

1. **Embeddings via OpenRouter `/embeddings`** (`nvidia/nemotron-3.5-embed-1b:free`, 2048
   dimensões). Prós: o provedor já é do projeto, é grátis e entende sinônimos em português.
   Contras: uma chamada HTTP por busca e a dependência de um modelo grátis com limite de uso.
2. **LSA local** (TF-IDF + SVD com scikit-learn). Prós: offline e sem custo. Contras: não liga
   "pequena" a "pouca", porque uma palavra fora do vocabulário é ignorada.
3. **Só full-text do Postgres.** Prós: simples. Contras: R-precision de 0,31 em paráfrases.

Para o ranking:

1. **Vetorial** com R-precision de 0,81.
2. **Híbrido RRF** com 0,74.
3. **Palavra-chave** com 0,55.

As três foram medidas em 30 consultas rotuladas (`evals/retrieval.py`).

## Decisão

_Pendente._

## Consequências

_Pendente._

Pontos para avaliar:
- O híbrido perdeu porque a perna de palavra-chave, com OR e stemmer, traz ruído. Testar RRF
  ponderado, ou um limiar de `ts_rank`, antes de descartá-lo de vez. Ganhos em 30 consultas
  sintéticas pedem desconfiança.
- **Índice incompleto.** O índice pode ficar atrás do corpus, por avaliações novas ou por um
  reseed. A ferramenta mede a cobertura e responde `hybrid` (cobertura parcial) ou `keyword`
  (cobertura vazia), com `unindexed` no resultado. Indexar no momento da escrita resolveria na
  origem.
- **Escala da busca exata.** A busca não usa índice ANN. No pgvector, o HNSW sobre `vector`
  aceita até 2000 dimensões, e este modelo tem 2048. Com milhares de avaliações por
  restaurante, avaliar `halfvec` + HNSW ou o parâmetro de dimensão do modelo.
- **Chave do processo filho.** O servidor MCP recebe só o `EMBEDDING_API_KEY`, nunca a chave
  de chat. Sem ela, a busca é só por palavra.
