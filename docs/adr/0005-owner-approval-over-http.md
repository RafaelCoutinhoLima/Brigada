# 0005. Aprovação das ações propostas pela API e pelo console

- Status: proposta
- Data: 2026-10-30

## Contexto

As ferramentas de escrita (`pause_menu_item`, `create_promotion`, `propose_purchase`) só
criam uma linha em `pending_actions` (ADR 0002). Até aqui, quem decidia era uma linha de
comando (`python -m brigada.mcp_server.actions`), que só funciona na máquina do servidor e não
tem escopo: enxerga as ações de todos os restaurantes. O dono, que é quem precisa decidir,
usa o console no navegador.

Restrições:
- o modelo nunca pode aprovar o que ele mesmo propôs: o caminho de aprovação não pode ser uma
  ferramenta MCP;
- o escopo por restaurante vem da chave de API, do lado confiável, como em `/ask`;
- uma ação de outro restaurante não pode nem ser confirmada como existente;
- toda decisão é auditada, com quem decidiu.

## Opções consideradas

1. Rotas HTTP `GET /actions` e `POST /actions/{id}/approve|reject`, que delegam às mesmas
   funções da linha de comando, agora com `restaurant_ids`. Prós: uma única implementação de
   aprovar (trava a linha, valida de novo o payload, aplica e audita na mesma transação), e o
   console só consome a API. Contras: a API ganha uma superfície de escrita.
2. Uma ferramenta MCP `approve_action` liberada só para um "cliente humano". Contras: a
   separação entre leitura e escrita passa a depender de quem chama, e um erro de
   configuração entrega a aprovação ao modelo.
3. Um painel separado, com banco próprio de usuários. Contras: duplica a autenticação antes
   de existir conta de usuário.

## Decisão

_Pendente._

## Consequências

_Pendente._

Pontos para avaliar:
- `decided_by` é o nome que a pessoa digita. Como a chave de API não tem dono, a auditoria
  grava também `principal`, um hash curto da chave usada, para não depender só do que o
  cliente declarou. Quando existirem usuários, `decided_by` deve vir da sessão.
- Uma ação fora do escopo responde 404, igual a uma inexistente, para impedir a enumeração de
  ids.
