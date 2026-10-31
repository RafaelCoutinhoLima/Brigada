# 0005. Aprovação das ações propostas pela API e pelo console

- Status: aceita
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

Escolhi a opção 1: rotas na API (`GET /actions`, `POST /actions/{id}/approve|reject`), que
o console usa para aprovar e rejeitar. A aprovação fica fora do MCP: a IA propõe, mas nunca
consegue aprovar o que ela mesma propôs.

## Consequências

- **Melhora:**
  - o dono decide pelo navegador, no mesmo console em que fez a pergunta;
  - cada chave só enxerga e decide as ações do próprio restaurante;
  - toda decisão fica auditada.
- **Ainda é fraco:**
  - **Quem aprovou.** Hoje o nome é digitado à mão (`decided_by`), e a auditoria guarda
    junto só um hash da chave. O nome deve vir do usuário logado.
  - **O que o dono vê.** O quadro mostra só a ação e o motivo em texto. Para decidir bem, ele
    precisa ver a resposta e a sugestão da IA embasadas nos dados: os números que as
    ferramentas devolveram, por exemplo cobertura de 2 dias contra prazo de 3, ou 10
    reclamações de comida fria.
- **Como sei que deu certo:**
  - uma ação de outro restaurante responde 404, igual a uma ação que não existe;
  - aprovar duas vezes ao mesmo tempo aplica a mudança uma vez só;
  - as duas coisas estão provadas por testes, e cada uma pega a mutação da sua defesa.

Pontos para avaliar:
- `decided_by` é o nome que a pessoa digita. Como a chave de API não tem dono, a auditoria
  grava também `principal`, um hash curto da chave usada, para não depender só do que o
  cliente declarou. Quando existirem usuários, `decided_by` deve vir da sessão.
- O `principal` é SHA-256 sem segredo, cortado em 32 bits: com chaves fracas, quem lê a
  auditoria pode testar candidatas offline. Um id declarado na configuração das chaves, ou um
  HMAC com segredo do servidor, resolve; fica para quando as chaves tiverem rótulo.
- Uma tentativa sobre ação de outro restaurante fica auditada sob o restaurante dono da ação,
  para que ele veja a sondagem.
- Uma ação fora do escopo responde 404, igual a uma inexistente, para impedir a enumeração de
  ids.
