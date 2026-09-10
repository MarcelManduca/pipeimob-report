# Inventário Preliminar de Indicadores Power BI × Gralha Indicadores
## Diagnóstico Metodológico, Viabilidade Histórica e Matriz de Substituição

> **Projeto**: `MarcelManduca/pipeimob-report` — Gralha Indicadores  
> **Status do Documento**: **Inventário Preliminar** (aguardando conferência individual com prints/telas das 11 páginas completas e leitura integral do Guideline original)  
> **Data de Atualização**: 09/09/2026  
> **Branch**: `docs/bi-replacement-indicator-inventory`  
> **Commit Base (`origin/main`)**: `6ef35df950e06fcf401286d3cd45cb23035f97c8`  
> **Estado de Branches Relacionadas**: `origin/feature/vista-organization-resolution` confirmada no commit `6ef35df950e06fcf401286d3cd45cb23035f97c8` (idêntica a `origin/main`), sem código funcional pendente nesta etapa.

---

## 1. Estado Real do Projeto

### 1.1 Mapeamento de Branches, PRs e Ambientes Reais

| Componente | Ambiente Real / Hostname | Referência Git | Status | Evidência Rastreável |
| :--- | :--- | :--- | :--- | :--- |
| **Repositório Base (`main`)** | GitHub `origin/main` | `6ef35df950e06fcf401286d3cd45cb23035f97c8` | Publicado | Incorpora PRs #59, #60, #61, #62 e #63. |
| **PR #63 (Funil & Reconciliação)** | `origin/fix/executive-funnel-post-prod-corrections` | `eed1478d1c8a0ccade94c9fd7e1f0448b9431c25` | **MERGED** | Partição nativa de 6 categorias auditadas; compatibilidade v1.1/legado; DataFinal nulo tratado como data não auditável no CRM (`linked_with_unresolved_gain_date`). |
| **PR #58 (Governança & RBAC)** | `origin/docs/data-governance-rbac-policy` | `f003e6704bbd` | **OPEN** | Política de governança de dados, RBAC e matriz de conferência. |
| **Branch Organizacional** | `origin/feature/vista-organization-resolution` | `6ef35df950e06fcf401286d3cd45cb23035f97c8` | Criada | Branch isolada para integração de `/usuarios/listar`, mantendo o mesmo commit da `main`. |
| **Backend FastAPI** | Render (`pipeimob-report.onrender.com`) | Commit de produção `6ef35df` | Publicado | Endpoints autenticados `/api/reconciliation/sales` e `/api/vista/funnel/summary` com cache single-flight. |
| **Edge API / Frontend** | Cloudflare Workers (`gralha-indicadores-chat.marcelmanduca-b05.workers.dev`) | Commit de produção `6ef35df` | Publicado | Proxy de borda, validação JWT Supabase, cache e painel executivo. |
| **Autenticação & RBAC** | Supabase Auth (`roles` na tabela de perfis) | Produção | Operacional | Perfis auditados: `CEO`, `CSO`, `CMO` (escopo global executivo); `store_director` (diretor de loja); `team_manager` (gerente de equipe). Aplicação fail-closed. |

### 1.2 Diferenciação Semântica: Implementado × Publicado × Homologado

- **Implementado**: Código funcional presente no repositório e validado por suites automatizadas locais (`pytest` e `node --test`).
- **Publicado**: Código em execução ativa nos hosts de produção (Render e Cloudflare Workers).
- **Homologado**: Indicador com equivalência contábil ou conceitual formalmente conferida contra a fonte oficial (Pipeimob CCV soberano para vendas/VGV/VGC).

---

## 2. Inventário Preliminar Estruturado

Para evitar distorções de contagem, separamos o levantamento em:
- **Seção 2.1**: Indicadores de Negócio das 11 Páginas
- **Seção 2.2**: Dimensões de Agrupamento e Filtros
- **Seção 2.3**: Regras Metodológicas e Lacunas do Guideline
- **Seção 2.4**: Endpoints e Fontes de Dados

---

### 2.1 Indicadores de Negócio

| # | Página e Nome no BI | Finalidade | Unidade | Fórmula e Filtros Conhecidos | Data do Período | Fonte Disponível / Estado | Implementação Atual no Portal | Evidência Rastreável | Diferença Encontrada | Pendência / Lacuna | Critério de Aceite |
| :-: | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **IND-01** | **Pág 1: Negócios por Etapa** | Medir volume de negociações em andamento por etapa | Negócio | `COUNT(CodigoNegocio)` por `NomeEtapa` | `DataInicial` (criação) | Vista REST `/negocios/listar` | Implementado no Card de Funil | `/api/vista/funnel/summary` | BI filtrava por movimentação histórica; portal filtra por criação | Rótulo deve explicitar: "Negócios criados no período, distribuídos pela etapa atual" | Exibição correta das contagens por etapa sem conectores de conversão |
| **IND-02** | **Pág 1: Volume Financeiro do Funil** | Medir valor monetário acumulado por etapa | Negócio (R$) | `SUM(ValorNegocio)` por `NomeEtapa` | `DataInicial` (criação) | Vista REST `/negocios/listar` | Implementado no Funil Executivo | `/api/vista/funnel/summary` | Valores nulos convertidos para R$ 0,00 | Nenhuma | Exibição de R$ total por etapa formatado |
| **IND-03** | **Pág 2: Volume de Agenciamentos** | Monitorar entrada de imóveis captados | Imóvel / Captação | `COUNT(CodigoAgenciamento)` | `DataAgenciamento` | **Não comprovado na REST v1** | Não implementado | `CONSULTAS_E_PERGUNTAS_SUPORTE_VISTA.md` P2 | Endpoint `/agenciamentos/listar` não localizado na API REST | Aguardando resposta de chamado ao suporte ou fonte alternativa | Grid de captações por corretor e período |
| **IND-04** | **Pág 2: Placas Ativas em Campo** | Medir presença física de sinalização | Imóvel | `COUNT(Imoveis)` onde `Placa = 'Ativa'` | Data da vistoria / status atual | **Não comprovado** | Não implementado | `CONSULTAS_E_PERGUNTAS_SUPORTE_VISTA.md` P3 | Campo `Placa` ausente no retorno padrão de `/imoveis/detalhes` | Verificar campos customizados ou fonte externa | Totalizador de placas ativas por região/loja |
| **IND-05** | **Pág 2: Tempo Médio de Placa Exposta** | Avaliar giro de imóveis com placa | Dias | `AVG(DataRetirada - DataInstalacao)` | Data de instalação | **Não comprovado** | Não implementado | `MATRIZ_COBERTURA_POWERBI.md` Tela 2 | Ausência de campos de ciclo de vida da placa na REST API | Fonte de dados não examinada | Média de dias de placa exposta |
| **IND-06** | **Pág 3: Comparativo de Vendas Período A × B** | Analisar crescimento de VGV e VGC | Contrato (R$) | $\Delta\% = \frac{\text{VGV}_B - \text{VGV}_A}{\text{VGV}_A}$ | `data_oficial_ccv` | Pipeimob Transações (Soberano) | Suportado pela API; UI possui seletor único | Testes dinâmicos multi-período | UI não permite comparar 2 intervalos simultaneamente | Criar componente de seleção dupla na UI | Tabela comparativa lado a lado com $\Delta$ absoluto e % |
| **IND-07** | **Pág 3: Comparativo de Visitas Período A × B** | Comparar esforço operacional | Visita | $\Delta\% = \frac{\text{Visitas}_B - \text{Visitas}_A}{\text{Visitas}_A}$ | `DataInicio` da visita | Vista REST `/agenda/listar` | Suportado pela API; não exposto na UI | `/agenda/listar` | Falta componente de comparação dupla | Query paralela para o período B | Comparação de visitas realizadas por loja |
| **IND-08** | **Pág 4: Ranking de Vendas por Equipe** | Medir entrega comercial por equipe | Equipe (R$) | `SUM(ValorCCV)` agrupado por equipe | `data_oficial_ccv` | Pipeimob + Vista `/usuarios/listar` | Reconciliação implementada; agregação por agência em PR #63 | PR #63 / `feature/vista-organization-resolution` | Nomes reais de equipes ausentes no V1; obtidos via `/usuarios/listar` | Integrar `/usuarios/listar` com `resolved_current_not_historical` | Tabela ordenada por VGV com nomes reais de equipes |
| **IND-09** | **Pág 4: Visitas Realizadas por Equipe** | Medir tração operacional por equipe | Visita | `COUNT(Visitas)` realizadas | `DataInicio` da visita | Vista REST `/agenda/listar` | Dados brutos disponíveis; agregação UI pendente | `visitas_detalhes.json` | Requer vínculo do corretor à agência | Agrupar por `CodigoAgencia` / `Equipe_Codigo` | Coluna de visitas no ranking de equipes |
| **IND-10** | **Págs 5 a 10: Funis por Loja / Agência** | Visão do funil de vendas por loja (Centro, Trindade, Campeche, Pedra Branca, Jurerê) | Negócio | Funil de etapas filtrado por `CodigoAgencia` | `DataInicial` (criação) | Vista `/negocios/listar` + `/usuarios/listar` | Não implementado no portal | `/usuarios/listar` retorna `CodigoAgencia`, `Gerente_Codigo`, `Equipe_Codigo` | No V1 agências não eram identificáveis; agora mapeáveis via `/usuarios/listar` | Integrar branch `feature/vista-organization-resolution` | Painéis de funil individualizados por agência |
| **IND-11** | **Módulo Adicional: Motivos de Perda** | Analisar motivos de descarte de negócios | Negócio Perdido | `COUNT(NegociosPerdidos)` por motivo | `DataFinal` (descarte) | Vista REST `/negocios/listar` (`status='Perdido'`) | Catálogo de motivos catalogado; agregação métrica pendente | `perda_motivos.json` (21 motivos mapeados) | Catálogo de motivos não comprova métricas de perdas sem agregação dos negócios | Agregar negócios perdidos no período por motivo | Gráfico de perdas com volume e valor financeiro |
| **IND-12** | **Módulo Adicional: Veículos / Origem de Leads** | Identificar mídias geradoras de leads | Negócio / Lead | `COUNT(CodigoNegocio)` por `Veiculo` / `Midia` | `DataInicial` (criação) | Vista REST `/negocios/listar` (campos `Veiculo`/`Midia`) | Disponível na API; não exposto em gráfico | `dashboard_veiculo_captacao_cruzado_pipeimob.xlsx` | Variação de nomenclatura de mídias | Criar card de canais de aquisição de leads | Distribuição de leads por canal |

---

### 2.2 Dimensões de Agrupamento e Filtros

| Dimensão | Finalidade | Unidade | Fonte Disponível | Estado no Portal | Evidência Rastreável | Observações / Regras |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Agência / Loja** | Segmentar funil e vendas por agência física | Loja (`CodigoAgencia`) | Vista REST `/usuarios/listar` | Não integrada | Endpoint `/usuarios/listar` comprovado | Vínculo atual não comprova alocação histórica (`resolved_current_not_historical`). |
| **Equipe Interna** | Segmentar por subequipe comercial | Equipe (`Equipe_Codigo`) | Vista REST `/usuarios/listar` | Parcial (códigos numéricos no V1) | PR #63 | Nomes reais dependem de `/usuarios/listar`. |
| **Gerente** | Agrupar equipes sob uma liderança | Usuário (`Gerente_Codigo`) | Vista REST `/usuarios/listar` | Não integrada | Endpoint `/usuarios/listar` comprovado | Suporte confirmou presença do campo na resposta de 09/09/2026. |
| **Corretor** | Atribuição individual de negócio/venda | Corretor (`CodigoCorretor`) | Vista REST + Pipeimob | Integrado | `/api/reconciliation/sales` | Rateio de co-corretagem depende de tabela de comissões Pipeimob. |
| **Tipologia (Pronto vs. Lançamento)** | Classificar por estágio do imóvel | Imóvel (`Lancamento = Sim/Nao`) | Vista REST `/imoveis/listar` | Bloqueado (`NAO_CLASSIFICADO`) | `EVIDENCIAS_SUPORTE_VISTA_2662.md` E1 | 447 imóveis inativos/arquivados com leitura negada na chave REST atual. |

---

### 2.3 Regras Metodológicas e Lacunas Documentadas

| Tópico / Regra | Definição no Power BI / Guideline | Realidade Técnica Comprovada | Lacuna / Diferença |
| :--- | :--- | :--- | :--- |
| **Funil de Coorte (Transição)** | Medir passagem histórica de cards por cada etapa no período | API REST do Vista **não possui log de transições** de etapas (`EtapaOrigem`, `EtapaDestino`, `DataTransicao`). | Impossível inferir conversão de coorte. Percentuais entre etapas no portal são razões de estoque estáticas. |
| **Deduplicação de Clientes** | GuideLine prevê contagem de clientes distintos (`CodigoCliente`). | Portal deduplica por `CodigoNegocio` (cards de negociação). | Manter rótulo explícito "Negócios" e não implementar "clientes únicos" sem validar a chave e a completude do campo `CodigoCliente`. |
| **Data de Ganho** | Venda reconhecida no fechamento. | Vista `DataFinal` ausente em cards em aberto representa data de ganho não auditável no CRM (`linked_with_unresolved_gain_date`), e **não divergência comprovada**. | Vendas oficiais, VGV e VGC pertencem soberanamente ao Pipeimob (`data_oficial_ccv`). O campo `UltimaAtualizacao` nunca deve ser usado como data de ganho. |
| **Funil de Atividades (Visitas e Propostas)** | Visita e Proposta provêm de atividades registradas no card (não apenas da etapa). | O endpoint `/negocios/atividades` possui arrays desalinhados (`Assunto`, `Data`, `ValorProposta`), impedindo reconciliação temporal precisa de propostas via atividades. | Propostas estruturadas não comprovadas via atividades; visitas comprovadas via `/agenda/listar`. |
| **Métricas de Perdas** | Relatório de motivos e valores perdidos. | O arquivo `perda_motivos.json` comprova apenas o catálogo de motivos cadastrados, **não comprovando** métricas de perdas no período sem agregação dos negócios descartados. | Requer agregação dinâmica sobre negócios com `status = 'Perdido'` no período. |
| **Lifecycle / Tempo em Estágio** | Tempo médio de permanência dos cards em cada etapa. | Sem log de transição, o tempo em cada etapa intermediária **não é comprovado**. | Snapshots futuros não comprovam eventos passados, e mudanças entre snapshots não registram saltos intermediários. |
| **Evolução Temporal** | Gráficos de evolução histórica semanal/mensal. | Material original completo ainda **não examinado** individualmente por tela. | Consultas multi-período dinâmicas viáveis via API, mas gráficos originais do BI pendentes de evidência de tela. |

---

### 2.4 Endpoints e Fontes de Dados Mapeadas

| Endpoint / Fonte | Tenant / Host | Método | Finalidade | Estado de Acesso |
| :--- | :--- | :---: | :--- | :--- |
| `/negocios/listar` | `gralhaim-rest.vistahost.com.br` | GET | Listagem de negócios por `DataInicial` e campos do card | Operacional |
| `/usuarios/listar` | `gralhaim-rest.vistahost.com.br` | GET | Resolução de corretores, `CodigoAgencia`, `Gerente_Codigo`, `Equipe_Codigo` | Operacional (confirmado pelo suporte) |
| `/agenda/listar` | `gralhaim-rest.vistahost.com.br` | GET | Listagem de visitas e agendamentos por data | Operacional |
| `/imoveis/listar` | `gralhaim-rest.vistahost.com.br` | GET | Consulta de imóveis (ativos) | Parcial (imóveis inativos/arquivados retornam vazio) |
| `/corretores/listar` | `gralhaim-rest.vistahost.com.br` | GET | Listagem de corretores | HTTP 401 (Permissão negada na chave REST; fallback para `/usuarios/listar`) |
| `/propostas/listarcampos` | `gralhaim-rest.vistahost.com.br` | GET | Metadados de campos de proposta | Rota comprovada; `/propostas/listar` **inexistente** na REST v1 |
| `Pipeimob Transações` | Banco de Dados / API Pipeimob | SQL/REST | Vendas, VGV, VGC oficiais e comissões por CCV assinado | Soberano e Operacional |

---

## 3. Diagnóstico do Funil Comercial

### 3.1 Rastreamento Ponta a Ponta: Da Consulta à Interface

1. **Backend (`services/vista_funnel_service.py`)**:
   - Consulta `GET /negocios/listar` filtrando por `DataInicial` dentro do período selecionado.
   - Deduplica os registros por `Codigo` (`CodigoNegocio`).
   - Agrupa os registros pelo valor atual do campo `NomeEtapa`.
2. **Worker Edge (`cloudflare/worker.js`)**:
   - Valida autorização do usuário (apenas papéis autorizados têm acesso).
   - Aplica cache de borda e repassa o payload para o frontend executivo.
3. **Interface Web (`frontend/src/components/ExecutiveFunnel.jsx`)**:
   - Exibe a contagem de negócios e a soma de valor em cada etapa.

### 3.2 Correção Metodológica sobre Período e Movimentação

> [!IMPORTANT]
> **Exemplo Crítico de Comportamento Temporal**:  
> Um negócio criado em julho/2026 com `DataInicial = 2026-07-15` que avança para a etapa "Proposta" em agosto/2026 será **totalmente excluído** de uma consulta restrita ao período de agosto/2026 (`2026-08-01` a `2026-08-31`), pois o filtro da API `/negocios/listar` seleciona negócios pela data de criação (`DataInicial`), e não pela data de movimentação de estágio.

Portanto:
- O bloco de etapas do funil reflete rigorosamente: **"Negócios criados no período, distribuídos pela etapa atual na data da consulta"**.
- Não deve ser denominado "ativos" sem aplicação de filtro de status comprovado (`Status = 'Em Aberto'`).
- Os percentuais entre etapas **não comprovam conversão de coorte** e misturam populações do CRM com as vendas oficializadas (CCVs) do Pipeimob. Devem ser removidos da visualização desse bloco.

### 3.3 Regra de Enriquecimento Organizacional e Permissões

- Qualquer transação comercial (inclusive do dia de hoje) enriquecida com a estrutura organizacional consultada no presente deve receber a classificação auditável:
  $$\text{classificação} = \texttt{resolved\_current\_not\_historical}$$
- A API do Vista nunca amplia permissões de acesso. O portal mantém controle de autorização fail-closed por RBAC no Edge/Backend.

---

## 4. Classificação de Viabilidade Histórica

```
[A] Disponível e Comprovada          -> Pronta para consumo e homologada
[B] Disponível, mas Não Integrada    -> API possui suporte; pendente integração
[C] Depende de Exportação / Histórico -> Requer extração histórica contínua ou liberação de acesso
[D] Não Comprovada / Indisponível    -> Não localizada nas fontes examinadas até o momento
```

| Categoria | Descrição | Itens Classificados |
| :---: | :--- | :--- |
| **A** | **Disponível e Comprovada** | - Vendas, VGV e VGC Oficiais Pipeimob (`data_oficial_ccv`)<br>- Reconciliação Vista × Pipeimob com partição nativa de 6 categorias<br>- Distribuição de negócios criados por etapa atual no Pipe 1 (`/negocios/listar`)<br>- Visitas agendadas e realizadas (`/agenda/listar`) |
| **B** | **Disponível, mas Não Integrada** | - Resolução organizacional de Corretores $\to$ Equipe $\to$ Gerente $\to$ Agência (`/usuarios/listar`)<br>- Agrupamento de funil por Loja física (Centro, Trindade, Campeche, etc.)<br>- Comparativo de 2 períodos via chamadas paralelas na API |
| **C** | **Depende de Exportação / Histórico Externo** | - Coorte de transição histórica de cards (inexistente na REST v1)<br>- Classificação Pronto vs. Lançamento (depende de permissão para 447 imóveis arquivados) |
| **D** | **Não Comprovada / Indisponível** | - Controle de Placas em campo (instalação, retirada, tempo de exposição)<br>- Metas comerciais de corretores/equipes via API REST<br>- Endpoint `/propostas/listar` (inexistente na REST v1)<br>- Alinhamento 1:1 de atividades em `/negocios/atividades` |

---

## 5. Materiais Complementares e Validação de Fontes

Para consolidação e refinamento do inventário de substituição:
1. **Evidência visual individual das telas operacionais (1 a 10) e do GuideLine** para conferência de componentes de interface.
2. **Consulta a medidas DAX / arquivo `.pbix`** como fonte desejável para conferência de fórmulas de apoio (não pré-requisito absoluto).
3. **Mapeamento das mídias/canais de captação** para validação dos nomes dos veículos de captação.
4. **Esclarecimento do suporte Vista CRM (empresa 2662)** quanto à existência de rotas para agenciamentos, placas e acesso a imóveis inativos/arquivados.
