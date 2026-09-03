# Gralha Indicadores — reconciliação do histórico de migrações

## Escopo e decisão

Preparação em rascunho, autorizada em 03/09/2026. Base: `main` em
`e53627b265e6193b5eca69589c859ca37119bbdb`. Projeto auditado:
`kmysinxpdkeszrtdyhid` (Pipeimob MCP Homologação), tratado como produção.

**Não está liberado executar `db push`, `migration up`, `migration repair`,
`db reset`, aplicar SQL, fazer merge ou publicar serviços neste projeto.**
Este PR não faz essas operações, não altera o banco e não corrige RLS.
GitHub, Cloudflare, Supabase, Render, Vista, PipeImob e OpenAI são preservados.
A ampliação corretor→equipe continua pausada até o código de autorização combinado.

## Evidência de leitura

O histórico remoto foi consultado em transação explicitamente `READ ONLY`, com
timeout de 15 segundos. Foram lidos somente catálogo e SQL armazenado no histórico,
sem consultar registros de usuários, conversas ou vendas e sem invocar funções.
Cada registro remoto contém uma string SQL. MD5 e tamanho identificam seu conteúdo;
esses hashes servem para comparação de integridade, não para segurança criptográfica.
O inventário legível por máquina está em
`docs/migrations/reconciliation-20260903.json`.

### Três arquivos com SQL idêntico e timestamps diferentes

| Migração | Versão anterior no GitHub | Versão registrada no Supabase |
|---|---|---|
| `add_manager_team_reference` | `20260830120000` | `20260830162242` |
| `add_integration_failure_diagnostics` | `20260831001000` | `20260831003640` |
| `allow_authorized_diagnostic_reads` | `20260831010000` | `20260831013118` |

Somente os nomes desses três arquivos foram alinhados. Seus bytes SQL permanecem
idênticos aos arquivos anteriores e aos registros remotos. Não se reaplica o SQL,
não se altera a atribuição de equipes e não se cria uma nova migração.

### Quatro registros remotos sem arquivo correspondente na main

| Versão | Nome | Tratamento neste PR |
|---|---|---|
| `20260821133318` | `add_consolidated_sales_fiscal_broker_index` | Cópia para revisão |
| `20260821133813` | `create_validation_sales_reconciliation_rpc` | Cópia para revisão |
| `20260821164016` | `validation_rbac_bootstrap` | Cópia com e-mail ocultado, NÃO equivalente ao original |
| `20260821164200` | `validation_rbac_security_hardening` | Cópia para revisão |

As cópias ficam em `docs/migrations/history-review/*.sql.txt`, fora de
`supabase/migrations`. Não são entradas executáveis de migração. Nas cópias sem
ocultação, a única normalização permitida é um LF terminal. Os hashes do SQL remoto
original são preservados no inventário, inclusive para a cópia ocultada.

O bootstrap contém uma identidade fixa em regras de ativação e concessão de cargo
administrativo. O valor foi substituído por `[REDACTED_EMAIL]` em todas as ocorrências.
Essa cópia não deve ser executada, renomeada para migração ou marcada como original.
Não se verificou nesta etapa se a função histórica mantém hoje a mesma definição.
Não publicar o valor original nem substituir silenciosamente essa regra por outra.

Há também dependências pré-existentes, como `validation.consolidated_sales`,
`validation.pipeimob_sales`, `validation.vista_gains` e `auth.users`. O histórico
recuperado não é uma prova de que o banco inteiro possa ser recriado do zero.

### Três arquivos do portal ausentes no histórico remoto

| Versão | Evidência observada | Limite |
|---|---|---|
| `20260901150000` | Colunas, tabelas, constraints, índices, helper, políticas e grants do portal examinados | O backfill de cargos e o upsert de equipes não foram comprovados |
| `20260901182000` | `request_id` e índice único parcial presentes | Não há registro de execução da migração |
| `20260901191000` | Índices presentes e política antiga removida | Não há registro de execução da migração |

Esses três arquivos permanecem intactos. A auditoria do catálogo examinou oito
tabelas, 42 constraints validadas e 21 índices válidos/prontos (excluindo índices
de `profiles` dessa contagem). Isso não constitui auditoria integral do banco.
Estrutura presente não comprova a execução histórica de DML nem autoriza reaplicá-lo.

## Segurança: pendências independentes

- A política de leitura de `profiles` limita linhas, mas os privilégios de tabela
  de `anon` e `authenticated` ainda são amplos, incluindo `TRUNCATE`.
- As políticas atuais de conversas e mensagens verificam propriedade, mas não
  exigem perfil ativo. A proposta de correção segue separada no PR #35.
- As sequências `conversation_messages_id_seq` e `user_management_audit_id_seq`
  concedem `USAGE`, `SELECT` e `UPDATE` a `anon` e `authenticated`. O SQL do portal
  concede privilégios de sequência sem revogar privilégios herdados/preexistentes.
  Revisar os grants efetivos e defaults em proposta separada; este PR não os altera.
- Não se demonstrou vazamento ou exploração. Não foram testadas operações de
  escrita, `nextval`, `setval` ou `TRUNCATE` no projeto remoto.

Os testes isolados do PR #35 não validam esta cadeia histórica completa nem a
recriação do projeto. Não apresentar a reconciliação de nomes como correção de acesso.

## Portões antes de qualquer execução futura

1. Revisar este inventário e as cópias históricas. Resolver o tratamento da identidade
   fixa sem publicar dados pessoais. Completar as dependências históricas ausentes.
2. Escolher explicitamente entre recuperar a cadeia original com controles adequados
   e criar uma baseline sanitizada. Não misturar as duas como se fossem equivalentes.
3. Reproduzir a opção em banco descartável, sem credenciais nem dados de produção.
   Validar schema, grants, RLS, triggers, funções e idempotência com identidades fictícias.
4. Revalidar o catálogo e o histórico remoto em leitura. Resolver a evidência do DML
   do portal sem alterar cargos ou equipes por suposição.
5. Apresentar separadamente o plano exato de ajuste do histórico remoto, os efeitos,
   a recuperação e a revisão de segurança. Exigir autorização antes da execução.

`migration repair` altera apenas o registro do histórico; não aplica ou desfaz SQL.
Não usar `--status applied` apenas porque uma tabela existe, nem `--status reverted`
para contornar divergências. Um eventual `db pull` também deve ser revisado quanto
a alterações no histórico, não tratado automaticamente como operação somente de leitura.

## Verificação local deste PR

`node --test tests/migration_reconciliation.test.mjs` confere nomes, hashes, arquivos
preservados, separação dos arquivos de revisão e ocultação de identidade. Não acessa
rede nem executa SQL. O teste de regressão existente do Worker pode ser executado
separadamente. Esses testes não substituem a reprodução isolada do banco.

Resultado local em 03/09/2026: **22 testes aprovados** (7 de reconciliação e 15
de regressão do Worker), sem falhas ou testes ignorados. O SQL histórico não foi
executado; nenhuma validação em produção é inferida desses resultados.

## Referências

- [Histórico e reconciliação de migrações](https://supabase.com/docs/guides/deployment/database-migrations)
- [Recuperação de arquivos do histórico](https://supabase.com/docs/reference/cli/supabase-migration-fetch)
- [PR #35 — restrições de acesso ao histórico](https://github.com/MarcelManduca/pipeimob-report/pull/35)
