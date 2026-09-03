# Gralha — revisão do bootstrap e ensaio descartável

## Escopo

Etapa de revisão e teste isolado do PR #36. Não é migração, autorização de deploy,
correção aplicada ou comprovação da reconstrução integral do banco.
O projeto remoto `kmysinxpdkeszrtdyhid` continua tratado como produção.

Em 03/09/2026, uma consulta `READ ONLY` ao catálogo confirmou:

- `on_validation_auth_user_access` está habilitado em `auth.users` para INSERT e
  UPDATE de `email`/`email_confirmed_at`.
- `public.sync_validation_user_access()` ainda contém a exceção de e-mail fixo
  para ativação/cargo legado e sobrescreve `user_roles.role` em conflito.
- O e-mail foi ocultado no próprio resultado SQL, antes de retornar à ferramenta.
  Nenhuma linha de `auth.users`, `profiles` ou vendas foi consultada no projeto.

O backfill histórico também sobrescreve status e cargos ao ser reaplicado. Isso é
diferente do gatilho: o gatilho preserva status `disabled`; o backfill não o preserva.
Não foi avaliada a explorabilidade externa da exceção de e-mail (configuração de
signup, posse da identidade, confirmação e outros controles não foram auditados).

## Decisão técnica proposta, ainda não decisão de produção

Preservar o histórico como evidência e desenvolver uma baseline sanitizada para
recriação, em vez de tentar executar a cópia ocultada como se fosse original.
O ensaio testa uma alternativa limitada, com as seguintes regras:

1. Nenhuma identidade recebe cargo administrativo por corresponder a um e-mail.
2. Novos perfis recebem apenas `viewer` no modelo legado. A confirmação do e-mail
   não concede cargo executivo; os cinco cargos do portal continuam em `access_role`.
3. Atualizações do Auth preservam cargos explicitamente atribuídos e perfis desativados.
4. O preenchimento de perfis ausentes não sobrescreve decisões existentes.
5. O primeiro administrador exige provisionamento explícito, identificado e auditado.
   Esse mecanismo não foi criado nem executado nesta etapa.

O SQL candidato fica em `tests/fixtures/bootstrap_candidate.sql`, com bloqueio por
nome do banco descartável. Não o mover para `supabase/migrations` nem executar em
produção. A guarda SQL não substitui autorização ou isolamento da infraestrutura.

## O que o ensaio executa

O ambiente local não dispõe de PostgreSQL, Docker ou Supabase CLI. O workflow
`.github/workflows/gralha-bootstrap-review.yml` cria PostgreSQL 17.11 descartável
no GitHub Actions, exclusivamente na branch do PR #36. Não usa segredos do projeto,
não publica portas e não possui etapas de merge ou deploy. O token GitHub é somente
de leitura e não é persistido pelo checkout. O runner verifica imagem, container,
nome do banco e ausência de portas publicadas antes de executar SQL via socket local.

O teste gera **em memória** uma variante histórica que substitui o marcador ocultado
por uma identidade fictícia em `example.test`. Essa variante serve exclusivamente
como controle negativo; não é a migração original, não usa a identidade real e não
é gravada como migração. A cópia com `[REDACTED_EMAIL]` não é executada diretamente.

Depois, o ensaio monta uma variante candidata usando as definições de tipos/tabelas
e helpers/políticas históricas, troca somente o mecanismo de sincronização/backfill
pelo candidato e aplica o hardening histórico de privilégios de funções. Aplica
também os três arquivos inalterados de migrações do portal para verificar compatibilidade.

As dependências `auth.users`, `auth.uid()`, `sales_team_reference` e
`manager_team_reference` são modelos mínimos fictícios. Não houve teste nem alteração
da identificação corretor→equipe. Inserções de teste ocorrem em transações revertidas.
O serviço é removido automaticamente ao terminar o job.

## Limites e pendências

- É uma **reconstrução parcial de bootstrap + portal**, não do backend comercial.
  A primeira migração executável ainda depende de objetos não criados pela cadeia.
- Faltam definições versionadas completas de `validation.consolidated_sales`,
  `validation.pipeimob_sales`, `validation.vista_gains`,
  `public.internal_sales_spreadsheet_rows` e `public.sales_team_reference`, além
  dos componentes gerenciados pelo Supabase. Não foram inventados schemas comerciais.
- PostgreSQL 17.11 compartilha a major 17 do projeto auditado (17.6.1.155), mas não
  reproduz toda a plataforma Supabase, seus defaults ou a mesma versão de patch.
- Não testa Auth HTTP, assinatura/expiração de JWT, entrega de convite, SMTP ou login real.
- O candidato não decide suporte a identidades sem e-mail; o teste rejeita essas entradas
  de forma atômica. Não aplicar a projetos com fluxos de telefone/anonimato sem revisão.
- Preservar o cargo legado não resolve sua relação com `profiles.access_role`.
  A administração atual usa `viewer` no convite e atualiza `access_role` separadamente;
  a convergência dos modelos e a revogação administrativa precisam de desenho explícito.
- A fraqueza de histórico de perfis desativados permanece como controle negativo no
  ensaio; sua correção continua separada no PR #35. Não alterar seus resultados para
  mascarar a pendência. Grants de sequências também continuam fora deste candidato.

## Resultados

Preparação local: sintaxe JavaScript válida e 22 testes existentes aprovados.
Resultado PostgreSQL: pendente da execução do workflow; não presumir aprovação.

## Referência técnica

[Supabase — gerenciamento de dados de usuários e gatilhos](https://supabase.com/docs/guides/auth/managing-user-data)
destaca o risco de falhas em gatilhos bloquearem cadastros; por isso a validação usa
isolamento e não executa o candidato no projeto existente.
