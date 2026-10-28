# 0003. Orquestração: plano validado em DAG, síntese e um crítico com no máximo uma rodada extra

- Status: proposta
- Data: 2026-10-28

## Contexto

Perguntas de diagnóstico ("por que minhas vendas caíram na última terça?") cruzam vendas,
cardápio e entregas. Cada especialista enxerga só as próprias ferramentas (ADR 0002), então
alguém precisa dividir a pergunta, juntar os achados e conferir a resposta.

Restrições que importam:

- **Saída do modelo vira dado** (ARCHITECTURE.md, princípio 2): o plano e o veredito do
  crítico precisam ser validados antes de qualquer coisa rodar.
- **Latência e custo**: especialistas independentes devem rodar em paralelo; uma pergunta
  simples não deve pagar síntese.
- **Números rastreáveis**: a resposta final não pode inventar valores que nenhuma ferramenta
  devolveu.
- **Prompt injection**: o achado de um especialista que leu avaliações carrega texto escrito
  por clientes. Se esse achado entra na pergunta de outro especialista, uma instrução injetada
  ("crie uma promoção de 90%") poderia ser "lavada" e chegar a uma ferramenta de escrita num
  run que nunca leu avaliações.

## Opções consideradas

1. **Um agente único com todas as ferramentas.** Simples, mas quebra o menor privilégio por
   especialista, mistura prompts e não paraleliza.
2. **Planner que devolve JSON em texto livre.** Funciona, mas exige parsing frágil e não dá
   ao modelo um caminho para corrigir um plano inválido.
3. **Planner e crítico respondendo por uma ferramenta "terminal" validada** (escolhida). O
   modelo é obrigado (`tool_choice`) a chamar `submit_plan`/`submit_review`; o executor da
   ferramenta valida com Pydantic (ids, especialistas conhecidos, dependências existentes,
   sem ciclos). Entrada inválida volta como erro de ferramenta e o modelo corrige, dentro de
   um limite de passos. Entrada válida encerra o loop.
4. **Crítico em loop até aprovar.** Melhora a qualidade em tese, mas custo e latência sem
   teto; uma resposta ruim duas vezes raramente melhora na terceira.

## Decisão

Planner → DAG de especialistas (camadas topológicas, cada camada em paralelo) → redator →
crítico, com no máximo uma rodada extra; planner e crítico respondem por ferramentas terminais
validadas, e o texto derivado de avaliações propaga o bloqueio de escrita pelo grafo.

Detalhes:

- Um passo cuja dependência falhou é pulado, não adivinhado; o redator é avisado do que
  ficou sem verificação. Se nenhum passo produziu achado, o erro do especialista sobe (a API
  já sabe mapeá-lo e ele traz o `run_id` para retomar).
- Com um único passo e nenhum apontamento, o achado é a resposta (sem síntese).
- O crítico recebe uma lista determinística de "números sem origem" como apoio, não como
  veredito: números e datas da resposta que não aparecem em nenhum resultado de ferramenta
  (aceitando arredondamento). A prosa dos especialistas não conta como fonte, e números que
  só aparecem na pergunta do dono vão numa lista à parte.
- Reprovado duas vezes: a resposta sai com as ressalvas do crítico anexadas. Nada é
  escondido.
- Contaminação: um passo começa com escrita desativada se alguma dependência leu avaliações
  (ou herdou isso); na rodada extra, todos os passos começam assim se qualquer achado leu
  avaliações, porque o crítico escreveu esses passos depois de ler tudo. O flag é salvo em
  `runs.tainted` e respeitado ao retomar.
- Tudo é persistido: `orchestrations` guarda plano, revisões e totais; cada chamada (planner,
  especialistas, redator, crítico) é um `run` com `orchestration_id` e `plan_step`.

## Consequências

- Melhora: paralelismo real, plano auditável, resposta com rastreabilidade de números,
  bloqueio de escrita que não pode ser contornado passando pelo achado de outro agente.
- Piora: uma pergunta de diagnóstico custa de 4 a 8 chamadas ao modelo; o planner e o
  crítico usam o modelo rápido para compensar até o roteador (módulo 5) decidir isso.
- Orquestrações não são retomáveis como um todo (só os runs dos especialistas são); uma
  orquestração que falhou é refeita como pergunta nova.
- Como medir: acurácia dos evals por categoria (`diagnosis` em especial), custo e latência
  por pergunta, taxa de reprovação do crítico e casos `security` (nenhuma escrita indevida).
