# Inventário de Indicadores Power BI × Gralha Indicadores
## Diagnóstico Baseado em Evidência Documental Direta, Análise de PDFs e Validação REST

> **Projeto**: `MarcelManduca/pipeimob-report` — Gralha Indicadores  
> **Status do Documento**: **Inventário Estruturado com Conteúdo Efetivamente Observado**  
> **Data de Atualização**: 09/09/2026  
> **Branch**: `docs/bi-replacement-indicator-inventory`  
> **Commit Base (`origin/main`)**: `6ef35df950e06fcf401286d3cd45cb23035f97c8`  

---

## 1. Mapeamento dos Materiais Originais Examinados

Examinamos integralmente os arquivos PDF e documentos de suporte disponíveis no ambiente:

### 1.1 Documentos PDF do Power BI
1. **`Lançamentos - Indicadores Por Corretor e Equipe - Power BI.pdf` (1 página)**:
   - **Tela Coberta**: `INDICADORES POR CORRETOR E EQUIPE - LANÇAMENTOS`.
2. **`Indicadores Por Corretor e Equipe.pdf` (3 páginas)**:
   - **Página 1**: `INDICADORES POR CORRETOR E EQUIPE - TERCEIROS / CAPTAÇÃO`.
   - **Página 2**: `INDICADORES POR CORRETOR E EQUIPE - LANÇAMENTOS`.
   - **Página 3**: `INDICADORES POR CORRETOR E EQUIPE - GERAL (CONSOLIDADO)`.

### 1.2 Documentos Técnicos e Resposta do Suporte Vista/Loft
1. **`09-09-E-mail de Gralha Imóveis - Loft _ Suporte.pdf` (2 páginas)**:
   - Resposta formal da equipe de Relacionamento Plataforma Loft em 09/09/2026 às 15:52:
     - **Vínculos Organizacionais Atuais**: Disponíveis via `GET /usuarios/listar` com campos `Nome`, `CodigoAgencia`, `GerenteDoCorretor: ["Nome"]`, `Equipe: ["Nome"]` e filtro `Corretor: "Sim"`.
     - **Cadastro de Agências**: Disponível em `GET /agencias/listar` (`Codigo`, `Nome`, `Empresa`, etc.).
     - **Histórico Organizacional**: A API REST **não disponibiliza endpoint de histórico de alterações de equipe/gerente**. O histórico só existe nos logs internos do CRM (`Menu > Logs do sistema`). Portanto, a API retorna apenas o **vínculo organizacional presente**.
2. **`MATRIZ_COBERTURA_POWERBI.md` e `EVIDENCIAS_SUPORTE_VISTA_2662.md`**:
   - Mapeamento das 10 telas conceituais e documentação de limitações técnicas comprovadas na API da empresa 2662.

---

## 2. Interpretação e Transcrição das Regras do Guideline

Conforme registrado na arquitetura do Power BI original, a página **GuideLine** estabelece 4 princípios de negócio fundamentais:

1. **Funil Baseado em Histórico de Movimentação dos Cards**:
   - O funil original media a transição temporal dos cards por cada fase. Na API REST do Vista, **não existe endpoint de log de transições**; as consultas retornam apenas a etapa atual (`NomeEtapa`). Portanto, no portal o bloco reflete estritamente: *"Vista: negócios criados no período, por etapa atual. Pipeimob: vendas por data de assinatura do CCV."*
2. **Distinção entre Clientes Únicos e Movimentações**:
   - Clientes distintos (`COUNT_DISTINCT CodigoCliente`) vs. total de cards/ações (`COUNT CodigoNegocio`).
3. **Data de Ganho Contábil**:
   - A venda é registrada pela data oficial de fechamento. No Gralha Indicadores, a fonte soberana é a data de assinatura do contrato CCV no Pipeimob (`data_oficial_ccv`).
4. **Funil de Atividades**:
   - Visitas e propostas provêm de eventos operacionais registrados no card, e não unicamente do campo cadastral de etapa.

---

## 3. Inventário Detalhado por Tela Observada nos PDFs

### 3.1 Tela A: Indicadores Por Corretor e Equipe — Lançamentos (Pág. 2 do PDF / PDF 1)

| Elemento / Visual | Indicador Observado | Unidade | Filtros e Dimensões Visíveis | Fórmulas Comprovadas | Status de Fonte |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **KPI Card 1** | `Leads Únicos` (ex: 9.905) | Cliente | Período (01/05/2023 a 31/05/2025), Equipe, Corretor, Fonte | `COUNT_DISTINCT(CodigoCliente)` | Vista CRM `/negocios/listar` |
| **KPI Card 2** | `Fechamentos em Progresso` (ex: 4) | Negócio | Status em negociação final | `COUNT(Negocios)` em etapa de fechamento | Vista CRM `/negocios/listar` |
| **KPI Card 3** | `Corretores Ativos Lançamentos` (ex: 41) | Usuário | Vertical = Lançamentos | `COUNT(Usuarios)` onde `Ativo = Sim` e vertical Lançamentos | Vista CRM `/usuarios/listar` |
| **KPI Card 4** | `Leads Totais Lançamentos` (ex: 17.351) | Negócio / Lead | Total de cards na vertical | `COUNT(CodigoNegocio)` | Vista CRM `/negocios/listar` |
| **Gráfico de Barras** | `Corretores Por Equipe` (CML1=21, ESL1=12, INL1=7, CEL1=4, BLB1=3) | Usuário | Agrupado por código de equipe | `COUNT(Corretores)` por equipe | Vista CRM `/usuarios/listar` |
| **Gráfico Donut** | `Quantidade de Corretores Ativos e Inativos` (Ativos: 41, Inativos: 55) | Usuário | Status do usuário | `COUNT(Usuarios)` por `Status` | Vista CRM `/usuarios/listar` |
| **Tabela 1** | `Total de Leads, Imóveis Vendidos e % Conversão Por Corretor` | Misto | Corretor, Leads Totais, Imóveis Vendidos, % Conversão | Leads = contagem de cards; Imóveis = vendas; % Conversão = fórmula pendente de confirmação | Pipeimob (Vendas) + Vista (Leads) |
| **Gráfico de Barras** | `Leads Acionados Por Equipe` | Negócio | Agrupado por equipe | `SUM(Leads)` acionados | Vista CRM |
| **Tabela 2** | `Leads Gerais (Proprietários e Compradores) Por Corretor` | Negócio | Corretor, Leads Ativados, Leads Receptivos, Leads Totais | Distinção entre Ativado (Outbound) e Receptivo (Inbound) | Vista CRM `/negocios/listar` |
| **Gráfico de Barras** | `Visitas Realizadas Por Corretor / Equipe` | Visita | CMT1=38, INT1=38, COT2=27, COT1=19, CET1=16, CET2=7, ESL1=4, AVT1=3, CML1=1 | `COUNT(Visitas)` realizadas | Vista CRM `/agenda/listar` |
| **Gráfico de Barras** | `Fechamentos em Progresso Por Corretor / Equipe` | Negócio | CEL1=2, CML1=1, CMT1=1 | `COUNT(Negocios)` em fechamento | Vista CRM `/negocios/listar` |
| **Gráfico / Tabela** | `Imóveis Vendidos Por Corretor / Equipe (com Desistências)` | Contrato | CML1=3 (0 des.), ESL1=2, CET2=1, CMT1=1 | `COUNT(Vendas)` e `COUNT(Desistencias)` | Pipeimob CCV |

---

### 3.2 Tela B: Indicadores Por Corretor e Equipe — Terceiros / Captação (Pág. 1 do PDF)

| Elemento / Visual | Indicador Observado | Unidade | Filtros e Dimensões Visíveis | Fórmulas Comprovadas | Status de Fonte |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **KPI Card / Barra** | `Imóveis em Pauta Brognoli x Loft` (LOFT: 4.232, BROGNOLI: 1.799) | Imóvel | Carteira ativa comparada | Contagem de estoque ativo por portal | Exportação / Integração de Catálogo |
| **Tabela 1** | `Imóveis Captados Por Corretor (Bitrix)` (Total: 1.468) | Captação | Corretor, Imóveis Captados | `COUNT(ImoveisCaptados)` por corretor | Fonte original Bitrix / API de Imóveis |
| **Gráfico de Barras** | `Imóveis Captados Por Equipe` | Captação | Agrupado por equipe | `COUNT(ImoveisCaptados)` por equipe | Fonte original Bitrix / Imóveis |
| **Gráfico de Barras** | `Corretores Por Equipe` (CET1=15, CET2=12, CMT1=12, COT1=12, COT2=12, INT1=12) | Usuário | Agrupado por equipe | `COUNT(Corretores)` por equipe | Vista CRM `/usuarios/listar` |
| **Gráfico Donut** | `Quantidade de Corretores Ativos e Inativos` (Ativos: 64, Inativos: 35) | Usuário | Vertical Terceiros | `COUNT(Usuarios)` por `Status` | Vista CRM `/usuarios/listar` |

---

### 3.3 Tela C: Indicadores Por Corretor e Equipe — Geral / Consolidado (Pág. 3 do PDF)

| Elemento / Visual | Indicador Observado | Unidade | Filtros e Dimensões Visíveis | Fórmulas Comprovadas | Status de Fonte |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **KPI Card 1** | `Leads Únicos` (26.355) | Cliente | Período (27/01/2023 a 29/07/2025), Global | `COUNT_DISTINCT(CodigoCliente)` | Vista CRM `/negocios/listar` |
| **KPI Card 2** | `Fechamentos em Progresso` (19) | Negócio | Total geral em fechamento | `COUNT(Negocios)` em fechamento | Vista CRM `/negocios/listar` |
| **KPI Card 3** | `Corretores Ativos` (104) | Usuário | Total de corretores ativos na empresa | `COUNT(Usuarios)` ativos | Vista CRM `/usuarios/listar` |
| **KPI Card 4** | `Leads Totais` (45.650) | Negócio / Lead | Volume acumulado de cards | `COUNT(CodigoNegocio)` | Vista CRM `/negocios/listar` |
| **Gráfico Donut** | `Corretores Ativos Por Vertical` (Terceiros: 64, Lançamentos: 41) | Usuário | Vertical | `COUNT(Usuarios)` por vertical de atuação | Vista CRM `/usuarios/listar` |
| **Tabela 1** | `Leads Acionados Por Corretor` | Negócio | Corretor, Leads Ativados (5.217), Leads Receptivos (24.964), Leads Totais (30.181) | Soma de ativados e receptivos por corretor | Vista CRM `/negocios/listar` |
| **Gráfico de Barras** | `Leads Por Fonte / Mídia` | Lead | Facebook (10.100), Grupo ZAP (7.867), Reativação (5.796), Captação ativa (4.342), Sites Brognoli (3.193), Indicasse (2.491), Chaves na Mão (2.383), etc. | `COUNT(Negocios)` agrupado por `Midia` | Vista CRM `/negocios/listar` (campo `Veiculo`/`Midia`) |
| **Tabela 2** | `Visitas Realizadas Por Corretor / Equipe` (Total: 162 visitas) | Visita | AVT1=3, CET1=18, CET2=8, CML1=2, etc. | `COUNT(Visitas)` | Vista CRM `/agenda/listar` |
| **Tabela 3** | `Fechamentos em Progresso Por Corretor / Equipe` (Total: 19) | Negócio | CEL1=2, CET1=9, CET2=4, etc. | `COUNT(Negocios)` em fechamento | Vista CRM `/negocios/listar` |
| **Tabela 4** | `Imóveis Vendidos Por Corretor / Equipe` (159 vendas, 23 desistências) | Contrato | CET1=30, CET2=24, CML1=21, INT1=19, COT1=17, ESL1=11, CMT1=10, BLB1=7, INL1=7, CEL1=4 | Vendas oficiais CCV | Pipeimob Transações |

---

## 4. Separação Estrita: Fórmulas Comprovadas × Fórmulas Desconhecidas

### 4.1 Fórmulas Comprovadas no Código e nos Dados
- **Vendas Oficializadas, VGV e VGC**: Originadas do Pipeimob CCV (`data_oficial_ccv`). $\text{VGV} = \sum \text{ValorCCV}$; $\text{VGC} = \sum \text{ComissaoCCV}$.
- **Reconciliação Comercial (Partição Nativa de 6 Categorias)**: Total vinculado = $\text{fully\_audited} + \text{value\_matched\_date\_unresolved} + \text{value\_mismatch\_date\_unresolved} + \text{value\_only} + \text{date\_only} + \text{value\_and\_date}$.
- **Distribuição de Negócios Criados por Etapa Atual**: `COUNT(CodigoNegocio)` por `NomeEtapa` onde `DataInicial` $\in [I, F]$.
- **Visitas Realizadas por Equipe/Corretor**: `COUNT(Visitas)` onde `Status = Realizada` e `DataInicio` $\in [I, F]$.
- **Contagem de Usuários e Equipes Ativas**: `COUNT(Usuarios)` agrupados por `Equipe` e `CodigoAgencia` via `/usuarios/listar`.

### 4.2 Fórmulas e Regras Desconhecidas / Pendentes
- **`% Conversão Por Corretor` (Tela de Lançamentos)**: Não foi possível determinar se o denominador é `Leads Totais` ou `Leads Únicos` na medida DAX original.
- **Diferenciação Inbound / Outbound (`Leads Ativados` vs `Leads Receptivos`)**: A regra exata que classifica um card como Ativado ou Receptivo no CRM (se por tipo de mídia ou por flag de ação ativa do corretor) requer mapeamento do campo de origem.
- **Controle Histórico de Pauta de Imóveis (Brognoli x Loft)**: Regra de consolidação externa entre portais não documentada na API REST do Vista.

---

## 5. Lacunas Específicas de Conteúdo (Material Restante)

Com a leitura integral dos PDFs disponíveis, delimitamos exatamente quais partes do Power BI ainda não possuem registro visual direto:

1. **Telas 5 a 10 do Power BI (Funis por Loja Física)**:
   - Os PDFs cobrem as visões por vertical (`Lançamentos`, `Terceiros`, `Geral`). Faltam capturas específicas dos dashboards individuais das agências físicas: Centro (Tela 6), Campeche (Tela 7), Trindade (Tela 8), Pedra Branca (Tela 9) e Jurerê (Tela 10).
2. **Tela 3 (Comparativo Entre Dois Períodos Independentes)**:
   - A visualização exata de layout da tela de comparação lado a lado entre dois períodos arbitrários não está nos 3 PDFs examinados.
3. **Controle de Placas em Campo (Metadados de Vistoria)**:
   - Indicadores de tempo médio de placa e status de instalação não aparecem nos relatórios PDF examinados.
