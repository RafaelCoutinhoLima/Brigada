# 0004. Provedor de modelo trocável por um adaptador no formato da API da Anthropic

- Status: proposta
- Data: 2026-10-29

## Contexto

O runtime, a persistência (`runs.messages`), os evals e a orquestração trabalham com os tipos
da API de Mensagens da Anthropic: blocos `tool_use`/`tool_result`, `stop_reason`, `usage`.
Rodar evals e desenvolvimento só com a API paga é uma barreira; o OpenRouter oferece modelos
`:free` com suporte a ferramentas por um endpoint compatível com OpenAI (chat completions).

Restrições:

- nenhum framework de agentes nem SDK extra: só o SDK `anthropic`, o SDK `mcp` e bibliotecas
  de base;
- o resto do sistema não pode saber qual provedor respondeu: retry, tratamento de erros da API,
  checagem de números e auditoria continuam iguais;
- JSON de argumentos escrito pelo modelo nunca é confiado sem validação;
- custo nunca pode virar zero por acidente.

## Opções consideradas

1. **Só Anthropic.** Nada muda; desenvolvimento e evals ficam pagos.
2. **Camada de abstração própria** (tipos internos neutros, um adaptador por provedor).
   Mais limpa em tese, mas reescreve runtime, persistência e testes para tipos novos.
3. **Adaptador que fala o formato da Anthropic** (escolhida): `OpenRouterClient` implementa o
   mesmo `messages.create(...)` que o loop usa, traduz a requisição para chat completions e a
   resposta de volta para `anthropic.types.Message`. Erros HTTP viram os erros do próprio SDK
   (429 → `RateLimitError` com `retry-after`), então `with_retry` e a API não mudam.
   Contras: recursos sem equivalente (blocos de thinking, prompt caching) se perdem nesse
   provedor; o formato da Anthropic vira o "idioma interno".
4. **SDK `openai` ou LiteLLM.** Menos código, mas adiciona dependência e, no caso do LiteLLM,
   uma camada que esconde justamente o que o projeto quer tornar explícito.

Detalhes que fazem parte da opção 3:

- argumentos de ferramenta com JSON inválido viram um único argumento desconhecido
  (`_invalid_arguments`), que o servidor MCP recusa e audita; o modelo vê o erro e corrige;
- `finish_reason` "stop" com chamadas de ferramenta conta como `tool_use`;
- um erro dentro de uma resposta 200 do OpenRouter é tratado como falha do provedor;
- modelos `:free` custam zero de forma explícita; um modelo pago sem preço cadastrado continua
  sendo erro.

## Decisão

_Pendente._

## Consequências

_Pendente._
