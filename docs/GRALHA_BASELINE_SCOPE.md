# Gralha Indicadores — escopo da baseline de banco

## Resultado da auditoria

Em 03/09/2026, o catálogo do projeto Supabase `kmysinxpdkeszrtdyhid` foi
inventariado em transação `READ ONLY`. Nenhuma linha de usuário, conversa ou venda
foi consultada. O snapshot sanitizado está em
`docs/migrations/commercial-schema-inventory-20260903.json`.

O banco possui 20 tabelas próprias: 11 em `public` e 9 em `validation`. Todas têm
chave primária e RLS habilitado. Também foram observadas 12 sequências, 189 colunas,
104 constraints, 67 índices válidos/prontos, 2 enums, 8 funções próprias e 20
políticas RLS. Não há views, materialized views nem associação dessas tabelas a uma
publication.

A cadeia executável do repositório cria somente 7 dessas 20 tabelas. Portanto, a
reconciliação de timestamps do PR #36 não é uma baseline completa e não permite
recriar o projeto do zero.

### Objetos ausentes da cadeia executável

| Camada | Relações observadas, mas não criadas pelas migrações executáveis |
|---|---|
| Identidade e RBAC | `public.profiles`, `public.user_roles` |
| Fontes e referência comercial | `public.internal_sales_spreadsheet_rows`, `public.sales_team_reference` |
| Validação comercial | As 9 tabelas de `validation`: `brokers`, `broker_system_identifiers`, `teams`, `broker_team_history`, `source_ingestion_runs`, `pipeimob_sales`, `vista_gains`, `consolidated_sales` e `reconciliation_events` |

São 13 tabelas ausentes, além das sequências, tipos, funções, grants, políticas,
índices e gatilhos dos quais elas dependem.

## Limite da baseline

A baseline futura deve conter somente objetos próprios do projeto, sem dados de
produção e sem reconstruir componentes gerenciados pelo Supabase.

| Ordem | Camada própria | Conteúdo e limite |
|---:|---|---|
| 1 | Pré-requisitos da plataforma | Referenciar `auth.users`, `auth.uid()`, os papéis `anon`, `authenticated` e `service_role`; não recriá-los. Habilitar extensões necessárias por nome, sem fixar versão. |
| 2 | Identidade/RBAC | Enums, `profiles`, `user_roles` e helpers. A identidade fixa do bootstrap histórico deve ser removida de uma variante sanitizada; nenhuma identidade substituta será presumida. |
| 3 | Fontes comerciais | `internal_sales_spreadsheet_rows` e `sales_team_reference`, sem registros ou seeds. |
| 4 | Validação | Schema, 9 tabelas, sequências, constraints e índices de `validation`, sem dados. Isso documenta o schema e **não inicia** a ampliação corretor→equipe. |
| 5 | Objetos derivados | `manager_team_reference` e `integration_failure_diagnostics`, depois das fontes das quais dependem. |
| 6 | Portal | `teams`, `user_team_access`, `conversations`, `conversation_messages` e `user_management_audit`, depois de identidade/RBAC e referências. |
| 7 | Funções e acesso | RPC de reconciliação, trigger, políticas RLS e grants explícitos, depois de todas as relações dependentes. |

Objetos dos schemas gerenciados `auth`, `storage`, `realtime`, `vault` e demais
internos ficam fora. O vínculo corretor→equipe permanece congelado até o código
`INICIAR-VALIDACAO-EQUIPES-GRALHA`; nem o inventário nem a baseline podem incluir
backfill, seed ou mudança dessa atribuição.

## Decisões de segurança exigidas antes do DDL

O snapshot registra o estado observado, mas privilégios excessivos não devem ser
copiados para a baseline como padrão desejado:

- `profiles` e `user_roles` concedem privilégios efetivos amplos a `anon` e
  `authenticated`, inclusive operações não controladas por RLS como `TRUNCATE`;
- as cinco sequências em `public` concedem `USAGE`, `SELECT` e `UPDATE` aos mesmos
  papéis;
- privilégios padrão em `public` ajudam a propagar grants amplos para novas tabelas,
  sequências e funções;
- as tabelas de `validation` têm RLS habilitado, zero políticas e nenhum `USAGE` de
  schema para `anon`, `authenticated` ou `service_role`; o acesso observado ocorre
  por funções `SECURITY DEFINER` e deve continuar explicitamente delimitado;
- o trigger de `auth.users` está habilitado e sua função ainda contém a exceção de
  identidade fixa descrita em `GRALHA_BOOTSTRAP_REVIEW.md`;
- todas as 20 políticas observadas são permissivas. O endurecimento de conversas e
  mensagens continua separado no PR #35.

Esses itens exigem uma proposta de least privilege revisada, testes negativos e
aprovação própria. Este documento não altera grants, RLS, funções ou defaults.

## Forma segura de produzir a baseline executável

1. Gerar a definição do schema a partir de um ambiente controlado usando a CLI do
   Supabase, restringindo os schemas próprios e excluindo dados.
2. Remover do dump os objetos gerenciados pela plataforma e qualquer owner/grant
   incidental; revisar cada dependência e qualquer literal sensível.
3. Substituir o bootstrap histórico por DDL sanitizado, sem e-mail privilegiado,
   sem usuário seed e sem alteração automática de cargos existentes.
4. Declarar grants, políticas e `search_path` de funções de forma explícita e com
   privilégio mínimo; não herdar os defaults amplos observados.
5. Reproduzir do zero em banco descartável, com identidades e dados fictícios.
   Validar constraints, RLS, grants, trigger, funções, idempotência e o conjunto
   completo de migrações.
6. Comparar o catálogo descartável ao inventário sanitizado. Diferenças de segurança
   intencionais devem ser documentadas, não ocultadas como drift.
7. Somente depois criar uma migração executável e apresentar plano de rollout,
   rollback e impacto para nova autorização.

Não usar `migration repair` para preencher DDL ausente: ele ajusta o registro do
histórico, não cria objetos. Mudanças feitas diretamente no dashboard também devem
voltar ao repositório como arquivos de migração revisados, para evitar novo drift.

## Estado do portão

- Limite e inventário da baseline: **definidos**.
- DDL candidato sanitizado: **preparado somente em `tests/fixtures`**, fora de
  `supabase/migrations`.
- Replay integral em banco descartável: **aprovado na primeira execução**.
- `db push`, `migration repair`, merge e deploy: **não autorizados**.
- Ampliação corretor→equipe: **pausada**.

Os candidatos são divididos em identidade/RBAC, estrutura comercial e least
privilege. Cada arquivo interrompe a execução se o banco não se chamar exatamente
`gralha_baseline_ci`. O teste integral monta as dependências mínimas do Supabase
com identidades fictícias, aplica os candidatos e os arquivos já reconciliados e
confere o catálogo esperado. Nenhum desses arquivos é uma migração.

Resultado do CI em 04/09/2026:

- 28 testes offline/Worker;
- 25 testes do bootstrap parcial;
- 6 testes da baseline integral;
- **59 testes aprovados, zero falhas**.

O replay confirmou 20 tabelas com chave primária e RLS, 12 sequências, 189 colunas,
104 constraints e 67 índices válidos/prontos. Também confirmou: nenhum acesso de
`anon` às tabelas próprias, nenhum `TRUNCATE` de `authenticated` nas tabelas
testadas, ausência de acesso direto dos papéis de API a `validation`, RPC comercial
restrita a `service_role` e trigger sem privilégio derivado de e-mail ou metadata.

[Execução isolada 33867351310](https://github.com/MarcelManduca/pipeimob-report/actions/runs/33867351310).

## Referências

- [Migrações de banco no Supabase](https://supabase.com/docs/guides/deployment/database-migrations)
- [Dump de banco pela CLI](https://supabase.com/docs/reference/cli/supabase-db-dump)
- [Histórico e cópias revisadas](./GRALHA_MIGRATION_RECONCILIATION.md)
- [Revisão do bootstrap](./GRALHA_BOOTSTRAP_REVIEW.md)
