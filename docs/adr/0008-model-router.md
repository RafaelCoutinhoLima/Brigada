# 0008. Roteador de modelos: classificador de texto, avaliado fora da amostra, desligado por padrão

- Status: proposta
- Data: 2026-10-31

## Contexto

Os especialistas rodam no modelo forte. Se uma parte das perguntas pudesse ir para o modelo
rápido sem perder acerto, o custo cairia muito: com os preços da Anthropic, Haiku 5.5 custa
1/20 do Sonnet 5.5 por token.

Dados disponíveis: as mesmas 62 perguntas do eval rodadas ao vivo duas vezes, uma com o modelo
forte (`nemotron-3.5-super-vl:free`, 56/62) e outra com o rápido
(`nemotron-3.5-lightning:free`, 54/62 nesses 62). Ficam em `evals/router_labels.jsonl`
(`scripts/build_router_labels.py`).

Restrições:
- só 8 perguntas em que o rápido falha (os negativos);
- uma execução por modelo, e os modelos grátis variam entre execuções;
- as duas execuções foram feitas em commits diferentes (`3d12ae8` e `96746cf`: entre elas
  entraram a busca de avaliações e duas correções de prompt);
- os modelos usados são grátis, então o custo só pode ser estimado com preços hipotéticos;
- o roteador não pode depender da categoria do eval, que não existe em produção;
- falhas nunca são silenciosas, e o CI não chama API.

## Opções consideradas

1. **TF-IDF de caracteres e palavras + regressão logística balanceada** sobre o texto da
   pergunta. Prós: treina em milissegundos, é determinístico, offline e explicável (pesos
   por n-grama). Contras: com 62 exemplos, aprende o vocabulário das perguntas do eval, não
   a dificuldade.
2. **Embeddings da pergunta + classificador.** Prós: generaliza paráfrases. Contras: uma
   chamada de API por pergunta e no treino; com 8 negativos o gargalo são os dados, não as
   features.
3. **Pedir ao modelo rápido que classifique a própria pergunta (LLM como roteador).** Prós:
   sem treino. Contras: custa uma chamada por pergunta, não é mensurável sem os mesmos
   rótulos e a autoavaliação de modelos é mal calibrada.
4. **Regras fixas** (por exemplo, "por que" vai para o forte). Prós: simples. Contras: as
   falhas do rápido nestes dados não seguem um padrão visível.
5. **Cascata** (tentar o rápido e escalar se o crítico reprovar). Prós: usa um sinal real de
   falha. Contras: paga duas vezes nas perguntas difíceis e depende de um crítico calibrado,
   que ainda não existe.

Para a avaliação: validação cruzada estratificada com 4 dobras, repetida com 20 seeds e
limiar fixo em 0,5, comparando sempre-forte, sempre-rápido, roteado e oráculo. Leave-one-out
foi descartado porque dá uma única partição sem medida de variância, e ajustar o limiar com 8
negativos seria ajustar ruído.

Resultado medido fora da amostra: AUC média de 0,43 (de 0,31 a 0,51). O roteado acerta 54/62
em todas as repetições, igual ao sempre-rápido, e manda 97% das perguntas ao rápido.

## Decisão

_Pendente._

## Consequências

_Pendente._
