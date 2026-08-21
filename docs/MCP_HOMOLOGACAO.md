# Homologação do MCP gerencial — Pipeimob × CRM Vista

## Objetivo

Permitir que diretores consultem indicadores consolidados em seus próprios chats,
sem alterar o Pipeimob BI Analytics publicado e sem expor dados de clientes.

## Isolamento obrigatório

- Serviço MCP próprio, em endereço HTTPS de homologação.
- Backend de validação separado do serviço atualmente publicado.
- Banco, segredos, logs e variáveis separados por ambiente.
- Nenhum deploy automático para produção a partir desta branch.
- O dashboard atual continua sendo apenas Pipeimob até aprovação formal.

## Fontes e precedência

1. Pipeimob confirma a existência da venda, a data do contrato e o VGV.
2. Vista com `Status = Ganho` fornece o corretor comercial.
3. `Etapa = Fechamento` isoladamente não representa venda.
4. O corretor fiscal do Pipeimob é preservado em campo separado e não substitui o
   corretor comercial.
5. A equipe deve ser atribuída por histórico na data da venda, nunca pela equipe
   atual do corretor.

## Ferramentas disponíveis na homologação

- Resumo de vendas e VGV.
- Conciliação Pipeimob × Vista.
- Lista de divergências operacionais.
- Corretores comerciais com vendas.
- Funil, mantendo Fechamento separado de Ganho.
- Qualidade e cobertura dos dados.

Todas são somente leitura, com intervalo máximo de 366 dias e respostas sem nome,
e-mail, telefone ou documento de cliente.

## Autenticação e segurança

- Cada diretor usa uma identidade individual; não há credencial compartilhada.
- A autorização é validada no servidor por e-mail ou domínio permitido.
- Os escopos OAuth anunciados são `openid` e `email`, ambos suportados pelo
  provedor; a permissão de Diretoria continua sendo aplicada no servidor.
- O token do diretor é encaminhado ao backend protegido e nunca é retornado ao chat.
- O modo `MCP_AUTH_REQUIRED=false` é restrito à inspeção local isolada.
- Produção exige OAuth 2.1 compatível com o cliente ChatGPT, HTTPS estável, rate
  limit, auditoria de acesso e logs sem tokens ou PII.

## Critérios de aceite

- [ ] Consulta ao vivo às APIs Pipeimob e Vista no ambiente de validação.
- [ ] Totais reproduzem a conciliação controlada do mesmo período.
- [ ] Nenhum registro em Fechamento sem Ganho é contado como venda.
- [ ] Data e VGV oficiais são os do Pipeimob.
- [ ] Corretor comercial é o do Vista e corretor fiscal permanece separado.
- [ ] Transferências e desligamentos preservam equipe histórica na data da venda.
- [ ] Diretor não autorizado recebe acesso negado.
- [ ] Respostas e logs não contêm dados pessoais de clientes nem tokens.
- [ ] Ferramentas validadas no MCP Inspector e em um ChatGPT de teste.
- [ ] Aprovação da Diretoria e da Secretaria de Vendas antes de qualquer consolidação.

## Liberação gradual

1. Validar APIs e reconciliação apenas com a equipe técnica.
2. Liberar o MCP de homologação a um diretor-piloto.
3. Registrar divergências e ajustar regras sem alterar o dashboard publicado.
4. Aprovar os indicadores consolidados e a política de histórico de equipes.
5. Somente então planejar a incorporação ao BI oficial.

## Pendências conhecidas

- Confirmar no Vista ao vivo o campo definitivo da data em que o negócio foi dado
  como Ganho. No CSV analisado, o campo útil foi `Encerramento do negócio`.
- Validar a cobertura do vínculo por código do imóvel para contratos fora da amostra.
- Implantar dimensão histórica de corretor e equipe com vigência inicial e final.
- Homologar o provedor OAuth usado pelo ChatGPT; a validação JWT do serviço não
  substitui o fluxo completo de autorização e descoberta OAuth.
