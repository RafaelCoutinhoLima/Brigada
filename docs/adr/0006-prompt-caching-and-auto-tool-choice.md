# 0006. Prompt caching e `tool_choice: auto` com ferramenta `strict`

- Status: aceita
- Data: 2026-10-30

## Contexto

Duas mudanças na API dos modelos atuais afetam o loop de agente:

1. Os modelos Claude 5.5 recusam `tool_choice` forçado (`{"type": "tool"}` ou `"any"`) com
   400. O planner e o crítico dependiam disso para responder pela ferramenta terminal (ADR
   0003).
2. Um loop de agente reenvia a conversa inteira a cada passo. Sem cache, o passo N paga de
   novo os tokens dos passos 1 a N-1, além das ferramentas e do prompt.

## Opções consideradas

Para a resposta estruturada:

1. **`tool_choice: auto`, ferramenta `strict: true` e lembrete no loop** quando o modelo
   responde em texto. Prós: o protocolo não muda (continua sendo `tool_use`), a validação
   Pydantic e a correção pelo modelo continuam valendo, e o OpenRouter segue funcionando.
   Contras: uma resposta em texto custa um passo, e é preciso tratar a retomada de um run que
   parou no texto.
2. **Structured outputs (`output_config.format`).** Prós: a forma do JSON é garantida sem
   ferramenta. Contras: a resposta vira texto JSON, perde o caminho de "erro de ferramenta,
   corrija" e precisaria de outra tradução no adaptador do OpenRouter.

Para o cache:

1. **Dois pontos de cache.** Um no bloco de `system`, que cobre ferramentas e prompt; outro
   automático, que acompanha a conversa.
2. **Só o cache automático.** É mais simples, mas o prefixo comum entre execuções diferentes
   (ferramentas e prompt) só é aproveitado quando a conversa é idêntica.
3. **Separar o contexto do dia num segundo bloco de `system`.** O cache seria compartilhado
   entre dias e restaurantes, mas os prompts são curtos, perto do mínimo cacheável.

## Decisão

- **Cache:** dois pontos de cache, um no bloco de `system` e um automático que acompanha a
  conversa. Sem se preocupar com o orçamento, um sistema de agentes pode gastar uma fortuna,
  porque cada passo reenvia tudo. O objetivo é gastar o mínimo possível sem perder qualidade.
- **Resposta estruturada:** `tool_choice: auto`, ferramenta `strict` e um lembrete no loop
  quando o modelo responde em texto. O plano e o veredito precisam vir num formato fixo,
  porque é código que os lê:
  - o executor roda os passos do plano em ordem de dependência;
  - o orquestrador decide pelo veredito se aprova a resposta ou pede uma rodada extra.

  Texto livre teria que ser interpretado, e aí um erro passaria sem ninguém ver.

## Consequências

- **Melhora:**
  - **Custo.** Num loop de vários passos, o que se repete passa a ser lido do cache, que custa
    uma fração do preço normal.
  - **Modelos novos.** O sistema funciona com os modelos 5.5, que recusam a escolha forçada de
    ferramenta.
  - **Retomada.** Um papel que insiste em responder em texto termina num erro que pode ser
    retomado, sem texto solto passando por plano.
- **Piora:**
  - Uma resposta em texto custa um passo a mais.
  - O cache só funciona acima de um tamanho mínimo de prompt, e quando não acontece nada avisa.
  - No OpenRouter os dois recursos são descartados.
- **Como sei que deu certo:** o orquestrador rodou ao vivo com o planner e a crítica chamando
  as ferramentas `strict` no primeiro passo. A economia do cache se confere em
  `cache_read_input_tokens` no trace, numa execução com a Anthropic.

Pontos para avaliar:
- Meça `cache_read_input_tokens` numa execução real. Abaixo do mínimo cacheável do modelo, o
  cache não acontece e nada avisa.
- Um papel que gasta todos os passos em texto termina com `NoStructuredAnswer`, que é um
  `MaxStepsExceeded` e pode ser retomado com orçamento novo. O texto fica no erro.
- No OpenRouter, `cache_control` e `strict` são descartados. A garantia de forma passa a
  depender só da validação Pydantic.
