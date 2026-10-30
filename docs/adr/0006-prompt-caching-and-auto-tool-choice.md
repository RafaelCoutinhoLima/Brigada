# 0006. Prompt caching e `tool_choice: auto` com ferramenta `strict`

- Status: proposta
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

_Pendente._

## Consequências

_Pendente._

Pontos para avaliar:
- Meça `cache_read_input_tokens` numa execução real. Abaixo do mínimo cacheável do modelo, o
  cache não acontece e nada avisa.
- Um papel que gasta todos os passos em texto termina com `NoStructuredAnswer`, que é um
  `MaxStepsExceeded` e pode ser retomado com orçamento novo. O texto fica no erro.
- No OpenRouter, `cache_control` e `strict` são descartados. A garantia de forma passa a
  depender só da validação Pydantic.
