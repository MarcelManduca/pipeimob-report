# Baseline versionada — candidata para revisão

## Estado

O arquivo
`20260904150012_establish_sanitized_schema_baseline.sql` é uma migração única
para revisão e replay descartável. O nome foi criado pelo Supabase CLI `2.20.3`
no [GitHub Actions 33886952900](https://github.com/MarcelManduca/pipeimob-report/actions/runs/33886952900).

Ele permanece deliberadamente fora de `supabase/migrations`. Portanto, não é
descoberto por `db push`, `db reset` ou pelo fluxo de publicação atual.

## Garantias desta etapa

- recompõe os 20 objetos de tabela próprios observados, sem copiar dados;
- reúne identidade/RBAC, estrutura comercial, portal, funções, RLS e grants;
- não inclui usuário inicial, e-mail privilegiado, credencial ou seed comercial;
- só executa quando o banco se chama exatamente `gralha_baseline_ci` ou quando
  uma sessão transacional autoriza explicitamente um projeto Supabase isolado
  por meio de `gralha.baseline_ci_target=isolated-supabase-project`;
- não inicia backfill nem ampliação da relação corretor→equipe;
- preserva as seis migrações atuais e as cópias históricas como evidência.

O gerador `scripts/build_gralha_baseline_candidate.mjs` monta o arquivo em ordem
determinística a partir dos componentes revisados. O teste integral aplica apenas
o arquivo unificado, em PostgreSQL descartável e sem conexão com o Supabase.

Para a validação em projeto Supabase vazio, a autorização deve ser definida com
`SET LOCAL` dentro da mesma transação do candidato. Sem essa marca de sessão, a
execução continua bloqueada. O candidato permanece fora da cadeia automática.

## Por que não está na pasta ativa

A migração é posterior aos arquivos já existentes, mas contém dependências que
eles exigem. Apenas copiá-la para `supabase/migrations` faria um replay novo
falhar antes de alcançá-la. No projeto existente, a baseline também apareceria
como pendente apesar de os objetos já existirem.

Promovê-la agora combinaria dois riscos: ordem incorreta para ambientes vazios e
colisão de objetos no projeto remoto. A reconciliação do histórico precisa ser
decidida antes da promoção.

## Portões para uma futura promoção

1. Aprovar o SQL, as diferenças de segurança e o catálogo esperado.
2. Definir uma cadeia limpa para novos ambientes e um tratamento separado do
   histórico do projeto existente.
3. Validar essa cadeia numa branch ou projeto Supabase descartável.
4. Executar uma prévia explícita do que ficaria pendente, sem aplicar em produção.
5. Aprovar plano de rollout, observabilidade e migração corretiva de rollback.

`migration repair`, `db push`, merge, SQL remoto e deploy continuam bloqueados.

O replay em Supabase gerenciado e seus achados estão registrados em
[`SUPABASE_VALIDATION_20260906.md`](SUPABASE_VALIDATION_20260906.md). A validação
estrutural foi aprovada; a promoção continua bloqueada pelos portões acima.

## Recuperação atual

Como este candidato é apenas um arquivo de revisão, a recuperação consiste em
reverter o commit que o adicionou. Nenhum rollback de banco é necessário porque
nenhum banco remoto foi alterado.
