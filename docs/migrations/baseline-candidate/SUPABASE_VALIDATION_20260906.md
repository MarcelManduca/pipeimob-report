# Validação Supabase isolada — 2026-09-06

## Escopo e isolamento

- ambiente: projeto vazio `Gralha Baseline CI`, em `sa-east-1`;
- custo confirmado antes da criação: US$ 0 por mês;
- projeto `Pipeimob MCP Homologação`: não alterado;
- dados pessoais, conversas e fatos comerciais: não lidos nem copiados;
- relação corretor→equipe: expansão e backfill não iniciados;
- método: `execute_sql` em transação única, sem registrar migration history.

## Trava de execução

A primeira prova negativa revelou que comparar uma configuração ausente com
`<>` produz `NULL` no PostgreSQL e não bloqueia o bloco `IF`. A condição foi
corrigida para `IS DISTINCT FROM`.

Depois da correção:

- a execução sem autorização de sessão foi recusada;
- a execução com `SET LOCAL gralha.baseline_ci_target` foi aceita somente na
  mesma transação do replay;
- o candidato continuou fora de `supabase/migrations`.

## Resultado estrutural

| Verificação | Resultado |
|---|---:|
| Tabelas próprias | 20 |
| Tabelas com chave primária | 20 |
| Sequências próprias | 12 |
| Colunas próprias | 189 |
| Restrições próprias | 104 |
| Índices válidos e prontos | 67 |
| Tabelas com RLS | 20 |
| Registros persistidos pelo replay/teste | 0 |
| Migrações registradas no projeto isolado | 0 |

## RLS e privilégios

Um teste transacional com duas identidades sintéticas confirmou:

- `profiles` expõe somente o próprio perfil para um usuário não executivo;
- `user_team_access` expõe somente o próprio escopo;
- `conversations` e `conversation_messages` isolam os proprietários;
- atualização própria de conversa funciona;
- leitura, atualização e inserção cruzadas são bloqueadas;
- o papel `anon` não lê `profiles`;
- `authenticated` não possui `TRUNCATE` em `profiles`;
- `service_role` não possui `USAGE` no schema `validation`.

Todas as identidades e linhas sintéticas foram removidas por `ROLLBACK`.

## Achados dos advisors

1. Nove avisos informativos indicam RLS sem políticas nas tabelas do schema
   `validation`. Isso é intencional no candidato: o schema e seus objetos não
   são concedidos a `anon`, `authenticated` ou `service_role`.
2. Cinco alertas registram funções `SECURITY DEFINER` no schema `public`
   executáveis por `authenticated`: `get_my_role`, `has_role`,
   `is_active_super_admin`, `is_active_user` e `is_super_admin`. Todas são
   `STABLE`, têm `search_path` explícito e não são executáveis por `anon`, mas a
   superfície RPC deve ser aprovada, restringida ou movida antes da promoção.
3. Os avisos de índices sem uso são esperados num banco recém-criado, vazio e
   sem carga de trabalho.

## Diferença deliberada em relação ao catálogo observado

O catálogo de produção registra 20 políticas. O candidato isolado registra 17,
pois não replica três políticas de escrita direta em `public.user_roles`
(`INSERT`, `UPDATE` e `DELETE` por superadministradores). A administração via
`service_role` permanece possível. Essa redução de privilégio não deve ser
promovida sem validar o contrato do portal administrativo.

## Veredito

O replay estrutural em Supabase gerenciado foi aprovado. A baseline ainda não é
uma migração de produção: permanecem bloqueadas a promoção para a cadeia ativa,
a reconciliação final do histórico, a decisão sobre as cinco funções expostas e
a validação de compatibilidade do fluxo administrativo de papéis.

