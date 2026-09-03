# Gralha — permissões de perfis e acesso ao histórico

## Estado e escopo

Proposta baseada na `main` em `e53627b265e6193b5eca69589c859ca37119bbdb`, preparada em branch separada. Não inclui merge, publicação do Worker, publicação de Edge Functions, alteração de dados ou aplicação de SQL no projeto existente.

Projeto de referência: `kmysinxpdkeszrtdyhid` (Pipeimob MCP Homologação). O nome “Homologação” não autoriza tratá-lo como banco descartável. Os PRs #33 (agregados por equipe) e #34 (gráficos, perfil e mobile) são independentes e permanecem pendentes. Nenhuma ampliação corretor→equipe está incluída.

## Motivação da auditoria somente de leitura

- As quatro tabelas auditadas (`profiles`, `user_team_access`, `conversations`, `conversation_messages`) tinham RLS habilitado.
- `profiles` possuía privilégios de tabela excessivos para `anon` e `authenticated`, incluindo operações fora do controle de RLS. Isso não demonstra que dados pessoais tenham sido expostos: as políticas de linhas também precisam ser consideradas.
- As políticas de histórico verificavam o proprietário, mas não o status atual do perfil. Desativar um perfil não implica invalidar imediatamente o JWT de Auth.
- As funções publicadas correspondiam à base: `gralha-portal-admin` versão de plataforma 2 e `gralha-indicadores-mcp` versão de plataforma 23 (interna 1.15.0). Esta proposta não as modifica.

## Proteções propostas

| Camada | Comportamento |
|---|---|
| Worker — histórico | Valida Auth e consulta somente `id,status,access_role` do próprio perfil, usando o JWT do usuário; orçamento de 5 segundos, sem retentativa automática nem cache de perfil |
| Worker — resposta persistida | Verifica o perfil antes de consultar o cache idempotente, evitando devolver respostas antigas a um perfil desativado |
| SQL — `profiles` | Revoga privilégios de tabela e de colunas de `PUBLIC`, `anon` e `authenticated`; concede apenas `SELECT` a `authenticated`, sujeito às políticas atuais |
| SQL — histórico | Acrescenta políticas `AS RESTRICTIVE FOR ALL`, com propriedade e perfil ativo/cargo válido em `USING` e `WITH CHECK`; mantém as políticas existentes de proprietário e vínculo mensagem/conversa |
| Administração | Preserva os privilégios existentes de `service_role` e o helper executivo; mudanças de perfil continuam pelo serviço administrativo autorizado |

CEO, CSO, CMO, diretor de loja e gerente de equipe ativos continuam acessando somente o próprio histórico. O cargo executivo não concede acesso a conversas de terceiros. Indicadores por cargo/equipe continuam sendo autorizados no MCP.

O Worker retorna `401` para sessão inválida, `403` para perfil ausente/desativado/convidado ou cargo inválido, e `503` quando não consegue concluir a verificação. Não devolve corpos de erro do Supabase, identificadores ou credenciais. A exclusão bem-sucedida do histórico agora traduz o `204` sem corpo do PostgREST para `200` com `{ "deleted": true }`, evitando a construção inválida de uma resposta JSON com status `204`.

## Limites importantes

- O SQL em `docs/security/portal_history_access_proposal.sql` é uma **proposta fora da pasta de migrações automáticas**. O CLI não estava disponível e a tentativa de uso local foi bloqueada pelo ambiente. Não foi inventado um nome de migração nem aplicado SQL por outro caminho.
- O Worker sozinho não protege chamadas diretas à Data API. As políticas propostas precisam ser testadas e aplicadas em etapa posterior autorizada.
- O histórico de migrações remoto não registrava as três migrações do portal de 2026-09-01, embora seus objetos e índices estivessem presentes. Há também diferenças de versões anteriores entre o repositório e o histórico remoto. **Não executar `db push` indiscriminadamente nem reaplicar as migrações antigas.**
- Clientes legados de `/api/chat` sem IDs válidos de conversa/requisição não usam persistência; seu fluxo atual de geração e autorização MCP não foi modificado.
- O bloqueio vale para novas verificações/requisições e, após aplicação do SQL, para comandos sujeitos a RLS. Não apaga conteúdo já entregue ao navegador nem promete cancelar operações em andamento. Não revoga sessões de Auth nem impede o login no provedor.
- Convidados precisam ser ativados pela administração para usar o portal, como já exigido no MCP. Não se deve promover ou ativar perfis automaticamente para contornar um bloqueio.

## Verificações locais

```sh
node --test tests/worker_history_access.test.mjs tests/worker_circuit_breaker.test.mjs
node --check cloudflare/gralha-indicadores-chat-worker-v11.js
git diff --check
```

Os testes usam UUIDs e respostas fictícias, com rede externa bloqueada pelo mock. Cobrem os cinco cargos, perfis desativados/convidados, cargo inválido, perfil ausente ou de terceiro, falhas de rede/JSON, timeout, JWT inválido e metadados editáveis que não podem autorizar acesso. Incluem todas as rotas de histórico e a resposta persistida de `/api/chat`.

Resultado desta branch: **47 testes aprovados** (32 novos e 15 existentes), além de sintaxe JavaScript e `git diff --check` sem erros.

As verificações do SQL são **estáticas**, não uma execução de PostgreSQL/RLS. Não havia PostgreSQL local disponível. A sintaxe e os efeitos efetivos das concessões/políticas precisam de validação em banco descartável, com dados sintéticos, antes de aplicação no projeto existente.

### Compatibilidade com os PRs pendentes

A composição local desta proposta com os commits dos PRs #33 (`f7edae0`) e #34 (`b41c269`) não apresentou conflito textual. A suíte de gráficos do #34 ainda não simula a nova consulta de perfil: seu teste de persistência falha de forma fechada com `503`, como esperado para uma fonte de autorização indisponível. Ao integrar as propostas, acrescentar ao mock de `runChat` em `tests/worker_portal_charts.test.mjs`, logo depois do tratamento de `/auth/v1/user`:

```js
if (url.includes("/rest/v1/profiles?")) return json([{ id: "test-user", status: "active", access_role: "cmo" }]);
```

Essa adaptação foi usada somente na cópia local de teste; os PRs #33 e #34 não foram alterados. Ela não deve ser implementada no Worker, nem substituída por liberação de acesso quando faltar perfil. O identificador acima é exclusivamente fictício.

Resultado da composição local, com a adaptação explícita do mock: **81 testes aprovados**, incluindo gráficos, histórico, proteção contra falhas e escopo por equipe.

## Próxima etapa — exige autorização separada

1. **GitHub:** revisar este PR e manter como rascunho até completar a validação SQL e resolver a divergência de migrações. Não mesclar nem publicar automaticamente.
2. **Ambiente de testes isolado:** disponibilizar PostgreSQL/Supabase local e o CLI; gerar o arquivo com `supabase migration new harden_portal_history_access` após consultar `--help`, usando o conteúdo revisado da proposta. Não usar credenciais ou cópias de dados de produção.
3. **Testes RLS:** executar como `anon` e `authenticated` (não apenas como dono/superusuário). Verificar leitura, inserção, atualização e exclusão do histórico próprio; tentativas contra outro usuário; mensagens vinculadas a conversa alheia; desativação com o mesmo JWT; perfis convidados/ausentes/inválidos; e a manutenção do acesso administrativo pelo serviço. Para perfis, confirmar `SELECT` autorizado e ausência de privilégios de escrita, `TRUNCATE`, `REFERENCES`, `TRIGGER`, `MAINTAIN` quando suportado, incluindo concessões por coluna. Não testar operações destrutivas no projeto existente.
4. **Supabase:** comparar catálogo e histórico, aprovar um plano específico de reconciliação antes de registrar/aplicar qualquer migração. Não marcar migrações como aplicadas apenas com base no nome.
5. **Publicação futura:** somente depois de nova autorização, aplicar a migração validada e publicar o Worker; verificar o acesso com contas de teste autorizadas. Este PR não exige redeploy de Edge Functions.

Em caso de regressão, interromper a publicação e preparar uma correção específica. Não restaurar permissões amplas ou remover políticas de isolamento como solução rápida.

## Referências

- [Supabase — Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [PostgreSQL — CREATE POLICY e combinação de políticas restritivas](https://www.postgresql.org/docs/current/sql-createpolicy.html)
- [PostgreSQL — REVOKE](https://www.postgresql.org/docs/current/sql-revoke.html)
