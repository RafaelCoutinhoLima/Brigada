# 0001. Construir o runtime de agentes sem framework

- Status: aceita
- Data: 2026-10-28

## Contexto

Frameworks de agentes aceleram protótipos, mas escondem o loop de tool use, a política de retry, o estado da execução e o controle de custo. Neste projeto essas peças são o produto: precisam ser explícitas, testadas e medidas.

## Opções consideradas

1. LangChain / LangGraph: rápido de começar, abstrações pesadas, difícil de depurar.
2. SDK oficial da Anthropic + código próprio: mais trabalho, controle total e cada decisão explícita.

## Decisão

Escolhi a opção 2: o SDK oficial e o loop de agente escrito por mim. Assim cada agente funciona
do jeito que eu quero, e consigo ajustar qualquer parte dele. Uma biblioteca pronta nem sempre é
o melhor caminho: ela decide por mim coisas que neste projeto são o produto.

## Consequências

- **Ficou mais fácil:** ajustar os agentes quando algo mudou. Alguns exemplos:
  - os modelos novos passaram a recusar `tool_choice` forçado, e a correção (ferramenta
    `strict` + lembrete no loop) coube em poucas linhas;
  - trocar a Anthropic pelo OpenRouter foi só um adaptador (ADR 0004);
  - uma resposta cortada vira o erro `ResponseTruncated`, em vez de uma resposta pela metade.
- **Ficou mais difícil:** testar tudo para ficar o melhor possível. Retry, timeout, estado,
  retomada de run e custo, que um framework daria prontos, eu tive que escrever e cobrir com
  testes. A revisão achou bugs nessas partes, e cada correção ganhou um teste.
- **Como sei que deu certo:** consigo explicar e mudar cada passo do loop. Os testes provam
  o comportamento: cerca de 375 sem banco e 215 com banco, e uma mutação desligando cada
  defesa faz algum teste falhar.
