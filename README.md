# Pipeimob Report API

Backend API para catalogação de dados e geração de indicadores de Business Intelligence (BI) integrados com o CRM Pipeimob.

---

## 🛠️ Como rodar o backend localmente

### 1. Requisitos
- Python 3.8 ou superior.

### 2. Instalar dependências
No diretório do projeto, execute:
```bash
pip install -r requirements.txt
```

### 3. Configurar variáveis de ambiente
Crie um arquivo `.env` na raiz do projeto com base no modelo `.env.example`:
```bash
cp .env.example .env
```

Edite o arquivo `.env` preenchendo as configurações:
```env
APP_ENV=development
PIPEIMOB_API_VERSION=v2
PIPEIMOB_BASE_URL=https://api.pipeimob.com.br
PIPEIMOB_API_KEY=
PIPEIMOB_SECRET_KEY=
PIPEIMOB_TRANSACTIONS_PATH=
ALLOWED_ORIGINS=http://localhost:5173
```

### 4. Executar o servidor local
Inicie o servidor Uvicorn:
```bash
python -m uvicorn main:app --reload --port 8000
```
O servidor estará disponível em: `http://localhost:8000`.

A documentação interativa do Swagger OpenAPI estará disponível em: `http://localhost:8000/docs`.

---

## 📋 Endpoints de Diagnóstico e Catálogo

### 1. Health Check
- **Rota:** `GET /api/health`
- **Descrição:** Retorna HTTP 200 sempre que a aplicação estiver rodando e funcional. Não executa autenticação ou chamadas externas ao CRM Pipeimob para garantir isolamento em verificações de infraestrutura.
- **Resposta esperada:**
  ```json
  {
    "status": "ok",
    "service": "pipeimob-report",
    "version": "0.1.0",
    "api_version": "v2",
    "pipeimob_connection": "pending",
    "timestamp": "2026-07-14T22:00:00Z"
  }
  ```

### 2. Catálogo de Recursos
- **Rota:** `GET /api/catalog`
- **Descrição:** Informa o status de desenvolvimento dos recursos planejados, campos disponíveis para extração, filtros suportados e itens pendentes de validação.
- **Resposta esperada:**
  ```json
  {
    "api_version": "v2",
    "resources": [
      {
        "id": "transactions",
        "name": "Transações",
        "backend_endpoint": "/api/transactions",
        "pipeimob_endpoint": null,
        "status": "pending_auth_confirmation",
        "implemented": false,
        "validated": false,
        "description": "Transações comerciais do Pipeimob",
        "primary_key": "transacao_unique_id_pipeimob",
        "available_fields": [ ... ],
        "supported_filters": [ ... ],
        "pending_items": [ ... ]
      }
    ]
  }
  ```

---

## 📊 Endpoints de Transações e BI (Business Intelligence)

### 🛡️ Autenticação da API (Lovable para Backend)
Os endpoints de dados e dashboard da API (`/api/transactions`, `/api/transactions/{id}` e `/api/dashboard/*`) exigem autenticação segura por meio de validação de token JWT assinado pelo Supabase Auth.
* **Cabeçalho Obrigatório:** `Authorization: Bearer <supabase_access_token>`
* **Configurações Server-side Obrigatórias:**
  - `SUPABASE_JWKS_URL`: URL oficial do JWKS para verificar assinaturas assimétricas. Ex: `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`
  - `SUPABASE_ISSUER`: O issuer oficial do JWT. Ex: `https://<project-ref>.supabase.co/auth/v1`
  - `SUPABASE_SECONDARY_JWKS_URL`: JWKS de um segundo projeto Supabase autorizado (opcional; deve ser usado junto com `SUPABASE_SECONDARY_ISSUER`).
  - `SUPABASE_SECONDARY_ISSUER`: Issuer exato do segundo projeto Supabase autorizado (opcional; deve ser usado junto com `SUPABASE_SECONDARY_JWKS_URL`).
  - `SUPABASE_JWT_AUDIENCE`: Audiência esperada (geralmente `authenticated`).
  - `ALLOWED_USER_EMAILS`: E-mails específicos autorizados (separados por vírgula).
  - `ALLOWED_EMAIL_DOMAINS`: Domínios autorizados. Padrão: `gralhaimoveis.com.br`

---

### 🔒 Camada de Sanitização de Dados Pessoais (LGPD)
Por motivos de segurança e privacidade, por padrão, o backend sanitiza os dados das transações de forma que nenhuma informação pessoal ou sensível dos compradores, vendedores ou corretores seja exposta publicamente.

* **Filtro de Exposição:** `EXPOSE_RAW_TRANSACTIONS` (booleano, padrão: `false`)
  * `false`: Remove campos sensíveis como `cpf_cnpj`, `cpf_cnpj_conjuge`, `data_nascimento`, `celular`, emails, `link_acesso`, `documentos`, `cobrancas_bancarias`, e dados detalhados de pagadores. Reduz compradores/vendedores a quantidades numéricas e comissionados a nome, participação e valor.
  * `true`: Expõe os dados completos da transação (não recomendado em produção).

O backend possui suporte a **modo dual**:
1. **Live Mode:** As transações são buscadas em tempo real da API V2 do Pipeimob de forma paralela. As chaves de acesso (`PIPEIMOB_API_KEY` e `PIPEIMOB_SECRET_KEY`) são carregadas exclusivamente das variáveis de ambiente configuradas de forma segura no servidor (Render) ou no arquivo `.env` local. **Nenhum cabeçalho HTTP (como X-API-Key ou X-Secret-Key) ou parâmetro de requisição é aceito para envio de credenciais do Pipeimob por segurança.**
2. **Mock Mode (Fallback):** Caso não haja credenciais, o servidor retorna um conjunto de **60 negócios simulados** contendo dados demográficos e corretores fictícios estruturados de forma anônima, ideal para o desenvolvimento local do frontend no Lovable.

### Filtros Comuns (Query Parameters)
Todas as rotas de listagem e BI suportam os seguintes filtros opcionais:

* **Filtros de Período Oficiais (Enviados diretamente ao Pipeimob em Live mode):**
  - `data_inicio_criacao`: Data de criação inicial (`YYYY-MM-DD`).
  - `data_fim_criacao`: Data de criação final (`YYYY-MM-DD`).
  - `data_inicio_ccv`: Data de contrato inicial (`YYYY-MM-DD`).
  - `data_fim_ccv`: Data de contrato final (`YYYY-MM-DD`).
  - `data_arquivamento_inicio`: Data de arquivamento inicial (`YYYY-MM-DD`).
  - `data_arquivamento_fim`: Data de arquivamento final (`YYYY-MM-DD`).

* **Filtros Locais (Aplicados localmente pelo backend após o carregamento):**
  - `agent`: Filtro case-insensitive pelo nome do corretor (`agente_gestor`).
  - `category`: Filtro pela categoria do imóvel (`categoria_crm`).
  - `financing`: Boleano (`true`/`false`) para filtrar se houve financiamento bancário.
### Relação de Endpoints (Exigem autenticação via JWT)

* **Listar Transações:** `GET /api/transactions`
  * Retorna a lista de transações filtradas de forma sanitizada.
* **Detalhar Transação:** `GET /api/transactions/{id}`
  * Detalha uma única transação de forma sanitizada, buscando por `transacao_unique_id_pipeimob` ou `codigo_contrato`.
* **Métricas Gerais (KPIs):** `GET /api/dashboard/summary`
  * Vendas totais, comissões acumuladas, comissão média em % e total de contratos (agregado).
* **Mídias de Origem:** `GET /api/dashboard/origins`
  * Contagem e volume financeiro agrupados por canal de captação (`midia_origem_compradores`).
* **Etapas do Funil:** `GET /api/dashboard/stages`
  * Agrupamento por etapa atual do negócio (`etapa_atual`).
* **Líderes de Equipe:** `GET /api/dashboard/managers`
  * Ranking de corretores por vendas, ticket médio e negócios fechados.
* **Meios de Pagamento:** `GET /api/dashboard/payments`
  * Distribuição de bancos de financiamento, percentual de financiamento vs direto e formas de parcelamento.
* **Análise de Comissões:** `GET /api/dashboard/commissions`
  * Taxas de comissão por contrato (apenas código do contrato e comissão, sem dados de corretor ou e-mails) e média global.
* **Linha do Tempo (Timelines):** `GET /api/dashboard/timeline`
  * Progresso cronológico mensal do volume e quantidade de vendas (ex: `Jan/26`, `Fev/26`).
* **Conciliação de Vendas Pipeimob + Vista:** `GET /api/reconciliation/sales`
  * Considera venda somente quando existe contrato assinado no Pipeimob. O Pipeimob define quantidade, data oficial da venda e VGV.
  * Usa exclusivamente negócios com `Status = Ganho` no Vista para conciliação e atribuição comercial. A etapa `Fechamento`, isoladamente, nunca é tratada como venda.
  * Mantém separados o corretor fiscal do Pipeimob e o corretor comercial do Vista. Quando o corretor comercial não está disponível, o dado fica pendente em vez de ser substituído pelo emissor da nota fiscal.
  * Expõe divergências de vínculo, data e valor para auditoria, sem solicitar ou retornar dados pessoais de clientes.

Configurações server-side necessárias para a conciliação:

```env
VISTA_API_BASE_URL=https://<tenant-vista>/api
VISTA_API_KEY=
VISTA_SALES_PIPE_ID=
VISTA_HTTP_TIMEOUT_SECONDS=12
VISTA_SALES_TEAM_FIELD=
VISTA_DEAL_CREATED_FIELD=DataInicial
VISTA_DEAL_TEAM_FIELD=
VISTA_DEAL_AGENCY_FIELD=
VISTA_DEAL_CAPTURE_SOURCE_FIELD=
VISTA_DEAL_RESPONSIBLE_FIELD=Responsavel
VISTA_FUNNEL_REQUEST_MAX_ATTEMPTS=2
VISTA_FUNNEL_RETRY_BACKOFF_SECONDS=0.25
VISTA_FUNNEL_PAGE_CONCURRENCY=4
VISTA_FUNNEL_CACHE_TTL_SECONDS=180
```

`VISTA_SALES_TEAM_FIELD` é opcional e deve ser preenchido somente depois de
confirmar, no tenant Vista, o campo de equipe do negócio. Quando ausente, a
conciliação tenta a equipe pelos grupos oficiais do responsável no Pipeimob;
o vínculo gerencial por planilha fica reservado à camada de referência com
vigência e nunca substitui quantidade, data ou VGV das APIs.

Exemplo de consulta:

```text
GET /api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-20
```

O endpoint só opera com a fonte Pipeimob ao vivo; dados simulados não podem formar números oficiais.

### Coorte do funil Vista por criação

* **Rota:** `GET /api/vista/funnel/cohort`
* **Fonte:** `Vista /negocios/listar`.
* **Semântica:** negócios distintos criados no período, agrupados pela etapa atual
  e pelo status geral no momento da consulta.
* **Proteção contra divergência:** a quantidade atualmente na etapa `Proposta`
  não é apresentada como "propostas geradas no período". Essa segunda métrica
  permanece `null` até que o contrato de histórico de entrada nas etapas seja
  confirmado no tenant Vista.
* **Distribuição gerencial:** o contrato MCP `1.2` agrega a fotografia atual de
  `Proposta` por equipe. A atribuição usa primeiro a equipe retornada pelo Vista
  e, na ausência dela, o campo `Responsavel` combinado com a referência
  gerencial vigente. A resposta também informa a cobertura e os registros sem
  equipe atribuída.

Exemplo:

```text
GET /api/vista/funnel/cohort?data_inicio=2026-08-01&data_fim=2026-08-30
```

O endpoint retorna somente agregados. Campos pessoais de clientes não são
solicitados nem expostos.

Para reduzir a latência sem alterar a fonte oficial, a primeira página é
consultada antes das demais; quando o Vista informa o total de páginas, as
restantes são buscadas com concorrência limitada. Falhas transitórias recebem
uma retentativa curta e o agregado é reutilizado por até três minutos. Depois
desse prazo, a próxima consulta volta a buscar a API ao vivo.

---

## 🛡️ Política de CORS (Cross-Origin Resource Sharing)

A segurança de origens cruzadas é gerenciada de forma estrita:
* **Origens Permitidas:** Configuradas através da variável de ambiente `ALLOWED_ORIGINS` (separe múltiplos domínios por vírgula).
* **Desenvolvimento:** Quando `APP_ENV=development`, as origens locais `http://localhost:5173` e `http://127.0.0.1:5173` são automaticamente aceitas na lista de origens autorizadas.
* **Wildcards e Credenciais:** Não é utilizada a origem curinga (`*`) e `allow_credentials` está desativado (`False`) nesta etapa, seguindo as diretrizes de segurança.

---

## ⚙️ Testes Automatizados

Para executar os testes de integridade, CORS, catálogo, transações e BI analítico, execute:
```bash
pytest
```

---

## ⚠️ Limitações Atuais e Status de Integração

* **Autenticação:** A autenticação com o CRM Pipeimob está marcada como **Pendente** por padrão até a definição das chaves no `.env`.
* **Endpoint Pipeimob:** O endpoint definitivo de transações foi oficialmente confirmado como `/api/v2/negocios/transacoes`.
