# Gralha Indicadores — diretório vivo e histórico comercial

## Objetivo

Manter lojas, equipes, gerentes e corretores atualizados automaticamente a
partir do Vista e do Pipeimob, sem recalcular o passado com a estrutura atual e
sem depender de cadastro manual no portal.

## Regra de autoridade

| Informação | Autoridade |
|---|---|
| Situação e vínculo atuais | Última sincronização completa e válida da fonte |
| Quantidade, data e VGV da venda | Versão vigente do fato vindo do Pipeimob |
| Loja, equipe e corretor da venda | Fotografia registrada quando o fato foi observado |
| Correção retroativa | Nova versão do fato, mantendo a versão anterior auditável |
| Registro ausente na fonte | Estado `source_missing`; nunca exclusão automática |

O portal deve apresentar a origem, o horário da última observação e o motivo de
qualquer diferença entre a fotografia atual da fonte e o histórico consolidado.

Quando Vista e Pipeimob discordarem sobre o vínculo atual, as duas evidências
serão preservadas e o vínculo ficará em `conflict`. O sistema não escolhe por
nome, ordem de chegada ou maioria. A venda permanece no total geral da Gralha,
mas não entra silenciosamente em uma equipe ou loja específica até existir uma
atribuição única validada. A definição de eventual precedência entre os sistemas
depende da validação real dos campos e de sua cobertura.

## Identidade

Pessoas, equipes e lojas são relacionadas por identificadores estáveis do
sistema de origem. Nome normalizado serve apenas para busca e exibição; nunca
para decidir silenciosamente que dois registros são a mesma entidade. Uma
mudança de nome no mesmo identificador gera histórico de nome, não uma nova
entidade.

## Vigência

Vínculos pessoa→equipe e equipe→loja usam intervalos semiabertos
`[valid_from, valid_to)`. Em uma transferência, o vínculo anterior termina no
mesmo instante em que o novo começa. Um negócio é atribuído ao vínculo vigente
na data do negócio, ou à fotografia explícita contida no próprio negócio quando
ela estiver disponível e validada.

## Soft delete

- Inativação explícita encerra o vínculo atual e mantém a entidade.
- Ausência em resposta parcial não produz nenhuma alteração.
- Ausência em sincronização completa marca `source_missing` e preserva o último
  estado conhecido.
- Extinção de equipe impede novos vínculos, mas não remove fatos históricos.
- Um negócio que desaparece da API continua computado e fica sinalizado para
  conciliação.

## Correções e divergências

Cada negócio possui uma identidade estável e versões imutáveis. Se data, VGV ou
atribuição forem corrigidos na origem, o portal encerra a versão vigente e cria
outra com `change_reason=source_correction`. Relatórios usam a versão vigente;
auditoria e conciliação conseguem explicar o antes e o depois.

Não há soma dupla: a tabela de identidade do fato define uma venda, enquanto as
versões registram suas mudanças.

## Contrato normalizado de entrada

### Semântica obrigatória das fontes

No Pipeimob, os conceitos não são intercambiáveis:

| Objeto da fonte | Uso no portal |
|---|---|
| Grupo de unidade/filial | Loja/unidade operacional |
| Time com Team Leader e membros | Equipe comercial e seus vínculos vivos |
| Grupo de acesso | Permissão; nunca equipe comercial |

O cadastro estático `PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON` permanece apenas como
compatibilidade do diagnóstico atual. Ele não é autoridade para o novo
diretório vivo e não pode classificar sozinho uma filial ou um grupo de acesso
como equipe. O adaptador somente será habilitado quando a fonte expuser IDs
estáveis de pessoas, times e lojas, os vínculos time→loja e pessoa→time, o estado
ativo/inativo e uma indicação confiável de snapshot completo.

O Vista passa a ser a autoridade do diretório organizacional. A listagem de
equipes fornece equipe→gerente/diretor e o cadastro de usuários fornece
pessoa→agência. Quando os dois lados expuserem identificadores estáveis, o portal
resolve equipe→loja pela cadeia equipe→gerente→agência. A resolução nunca ocorre
por semelhança de nomes. Um vínculo direto equipe→agência, se existir na API, tem
preferência por eliminar uma etapa da cadeia.

O status declarado da equipe no Vista é evidência fraca: equipes antigas podem
continuar marcadas como ativas. Por isso, `ATIVO` sozinho resulta em
`unverified`. O estado `operational_active` exige ao menos uma evidência viva,
como membro ativo, negócio aberto ou venda recente. `INATIVO` é preservado como
estado declarado, e não apaga equipe, vínculos ou resultados históricos.

A interface web do Vista não é tratada como contrato de integração. Uma ação da
tela pode responder apenas `{"status":"ok"}` sem transportar os dados exibidos.
O adaptador deve usar a API REST autorizada e sua negociação controlada de
campos. Antes de sincronizar, um diagnóstico agregado conta a cobertura de IDs
estáveis para corretor→equipe, equipe→loja e equipe→gerente. Nomes podem ser
armazenados como rótulos históricos, mas nunca completam um vínculo ausente nem
servem como identidade automática. O diagnóstico não retorna nomes, payloads
brutos, chaves ou dados pessoais.

O Pipeimob permanece como fonte oficial de quantidade de vendas, data de CCV e
VGV. Ele deixa de ser necessário para formar o diretório de equipes e lojas;
quando uma transação trouxer evidência organizacional, ela será guardada apenas
como fotografia histórica e elemento de conciliação.

O motor implementa `validateLiveDirectorySourceContract` como trava de
habilitação. Se a fonte só fornecer grupos genéricos, não distinguir times
comerciais ou não permitir a resolução das lojas diretamente ou pela cadeia
gerente→agência, a sincronização deve permanecer bloqueada e registrar o motivo;
ela não deve completar os dados por inferência.

Os adaptadores de Vista e Pipeimob devem entregar ao motor:

- sistema de origem;
- tipo e identificador estável da entidade;
- nome atual e estado ativo/inativo;
- identificadores de pessoa, equipe e loja;
- função da pessoa (`broker` ou `manager`);
- instante da observação e indicação de snapshot completo/parcial;
- para vendas: identificador da transação, data, VGV, atribuição fotografada e
  hash do payload sanitizado.

Campos específicos dos fornecedores só serão ligados a esse contrato após
validação real de presença, estabilidade e cobertura. Segredos e payloads brutos
não fazem parte do contrato persistido.

## Esquema Supabase candidato

O desenho será convertido em migração somente com o Supabase CLI disponível e
depois de validado em projeto isolado:

| Tabela candidata | Finalidade |
|---|---|
| `organizational_sync_runs` | execução, cobertura e completude da sincronização |
| `organizational_entities` | identidade canônica e estado atual |
| `organizational_source_keys` | IDs estáveis de Vista/Pipeimob |
| `organizational_name_periods` | histórico de nomes |
| `organizational_relationship_periods` | pessoa→equipe e equipe→loja com vigência |
| `sales_facts` | identidade única da venda e presença na origem |
| `sales_fact_versions` | versões imutáveis de data, VGV e atribuição |
| `organizational_reconciliation_events` | divergências e transições auditáveis |

Todas as tabelas devem ter RLS habilitada, nenhum acesso para `anon`, nenhuma
leitura direta de pessoas por `authenticated` e escrita restrita ao processo de
sincronização. O MCP continua responsável por aplicar RBAC e retornar somente o
escopo autorizado.

## Casos obrigatórios de teste

1. Transferência preserva a atribuição das vendas anteriores.
2. Inativação não remove pessoa, equipe nem resultado.
3. Renome mantém a identidade e registra a mudança.
4. Extinção mantém o resultado na meta geral da Gralha.
5. Snapshot parcial nunca inativa registros por ausência.
6. Snapshot completo marca ausência sem apagar.
7. Correção retroativa cria nova versão.
8. Reprocessamento do mesmo payload é idempotente.
9. Grupo de acesso nunca é convertido em equipe comercial.
10. A fonte só é habilitada quando possui IDs estáveis, diretórios vivos,
    vínculos pessoa→time e time→loja, estado ativo e snapshot completo.
11. Equipe→loja pode ser resolvida por equipe→gerente→agência somente com IDs.
12. `ATIVO` no Vista, sem membros ou negócios atuais, permanece `unverified`.
