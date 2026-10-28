# 0002. Separação leitura/escrita no servidor MCP e ferramentas parametrizadas em vez de SQL gerado

- Status: proposta
- Data: 2026-10-28

## Contexto

O servidor MCP (`src/brigada/mcp_server/`) é o único caminho do modelo até o banco
(ARCHITECTURE.md, princípio 1). Ele precisa responder perguntas de vendas, cardápio,
avaliações e entregas, e também permitir ações que mudam o negócio: pausar um prato e criar
uma promoção.

Restrições que importam:

- **Conteúdo não confiável no contexto.** O texto das avaliações é escrito por clientes e o
  seed já planta três tentativas de prompt injection no restaurante 3 ("pause todos os
  pratos", "promoção de 90%"). O Módulo 8 vai atacar isso de propósito.
- **Impacto assimétrico.** Uma leitura errada gera uma resposta errada, que o crítico e os
  evals podem pegar. Uma escrita errada tira um prato do ar ou dá desconto em produção.
- **Multi-restaurante.** Toda consulta precisa ficar presa ao `restaurant_id` do dono; um
  `menu_item_id` de outro restaurante não pode vazar nem ser alterado. Como o
  `restaurant_id` é um argumento escolhido pelo modelo, uma review maliciosa poderia induzir
  uma leitura de outro restaurante. Por isso o runtime fixa o escopo da execução
  (`_meta["brigada/restaurant_id"]`) e o servidor recusa e audita qualquer chamada fora dele
  ou sem escopo definido (falha fechada).
- **Auditoria e avaliação.** Os evals medem "ferramenta certa chamada" e o crítico exige que
  todo número venha de um resultado de ferramenta; isso pede saídas estáveis e auditáveis.
- **Falhas nunca silenciosas** (CLAUDE.md).

Duas perguntas precisam de resposta: (a) como o modelo consulta dados e (b) como uma
ferramenta de escrita pede confirmação humana.

## Opções consideradas

### Acesso a dados

1. **SQL gerado pelo modelo** (uma ferramenta `run_sql`, mesmo que só leitura).
   - Prós: cobre qualquer pergunta sem escrever código novo; menos ferramentas.
   - Contras: superfície de ataque enorme (injection vira exfiltração ou DoS com consultas
     caras); o isolamento por restaurante depende do modelo lembrar do `WHERE`; erros de
     semântica (fuso horário, intervalo inclusivo, canal sem pedidos que some do
     `GROUP BY`) viram respostas plausíveis e erradas; resultados difíceis de avaliar e
     comparar entre runs; exige papel de banco só leitura, timeouts e parser de SQL para
     ficar minimamente seguro.
2. **Ferramentas parametrizadas com SQL fixo** (`sales_by_period`, `top_items`,
   `reviews_for_item`, `delivery_times`, `list_menu_items`).
   - Prós: o modelo escolhe *qual* consulta e *com quais argumentos*, nunca *como*; entrada
     validada por Pydantic (enums, limites, `end_date >= start_date`); regra de negócio
     escrita uma vez e testada (dias locais em America/Sao_Paulo, zero-fill de canais e
     dias, média sobre todas as avaliações e não só as retornadas); `restaurant_id`
     obrigatório e checado em toda ferramenta; saídas tipadas que o crítico e os evals
     conseguem verificar.
   - Contras: cada pergunta nova pode exigir uma ferramenta ou um parâmetro novo; risco de
     proliferar ferramentas parecidas e confundir o modelo na escolha.
3. **Híbrido**: ferramentas fixas para o comum e SQL só leitura como válvula de escape.
   - Prós: cobertura total sem perder os casos comuns.
   - Contras: herda quase todos os riscos da opção 1 justamente nos casos menos testados.

### Confirmação das escritas

1. **Sem ferramentas de escrita**: o agente só recomenda; o dono faz a mudança em outro
   sistema.
   - Prós: risco zero de escrita indevida pelo modelo.
   - Contras: não cumpre o Módulo 3 ("uma ferramenta de escrita para e pede confirmação");
     perde o rastro entre recomendação e ação.
2. **Elicitation do MCP**: a ferramenta pede confirmação ao cliente MCP durante a chamada
   (`ctx.elicit`) e só executa se o usuário aceitar.
   - Prós: padrão do protocolo; a confirmação acontece no mesmo fluxo da chamada.
   - Contras: a confirmação passa pelo cliente que roda o agente; se esse cliente responder
     automaticamente (runtime próprio, testes, um bug), a barreira some. A chamada fica
     bloqueada esperando um humano, o que conflita com timeouts e com execução paralela de
     especialistas. Depende de suporte do cliente a elicitation.
3. **Ação pendente + caminho humano separado**: a ferramenta só grava uma linha em
   `pending_actions` (status `pending`) e devolve o id com "aguardando confirmação humana".
   A execução acontece fora do MCP, em `brigada.mcp_server.actions.approve/reject` (CLI
   agora, rota da API depois), que trava a linha, revalida o payload, aplica a mudança e
   marca `executed` numa única transação.
   - Prós: o modelo não tem nenhuma ferramenta capaz de executar a mudança, então uma
     injection no máximo cria uma proposta visível para o humano; a chamada do agente
     termina na hora (sem bloqueio); toda proposta e decisão fica em `tool_audit` com
     `run_id` e `approved_by`; testável sem cliente MCP.
   - Contras: assíncrono (o dono aprova depois, a resposta do agente não reflete a mudança);
     precisa de uma tela ou CLI para o humano; propostas pendentes podem acumular e ficar
     obsoletas.

## Decisão

_Escreva com suas palavras depois de terminar o Módulo 3._

## Consequências

_Escreva com suas palavras: o que melhora, o que piora e qual métrica ou eval vai mostrar se
foi uma boa escolha._
