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
Os endpoints de dados e dashboard da API (`/api/transactions`, `/api/transactions/{id}` e `/api/dashboard/*`) exigem autenticação segura por meio de uma chave interna definida no ambiente do servidor.
* **Cabeçalho Obrigatório:** `X-Backend-API-Key`
* **Configuração:** O valor do token de acesso deve ser configurado na variável de ambiente `BACKEND_API_KEY`. Caso não esteja configurado no servidor, as requisições retornarão status HTTP 401 Unauthorized.

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

### Relação de Endpoints (Exigem cabeçalho `X-Backend-API-Key`)

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

