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

## 🛡️ Política de CORS (Cross-Origin Resource Sharing)

A segurança de origens cruzadas é gerenciada de forma estrita:
* **Origens Permitidas:** Configuradas através da variável de ambiente `ALLOWED_ORIGINS` (separe múltiplos domínios por vírgula).
* **Desenvolvimento:** Quando `APP_ENV=development`, as origens locais `http://localhost:5173` e `http://127.0.0.1:5173` são automaticamente aceitas na lista de origens autorizadas.
* **Wildcards e Credenciais:** Não é utilizada a origem curinga (`*`) e `allow_credentials` está desativado (`False`) nesta etapa, seguindo as diretrizes de segurança.

---

## ⚙️ Testes Automatizados

Para executar os testes de integridade, CORS, catálogo e schemas do OpenAPI, utilize o pytest:
```bash
pytest
```

---

## ⚠️ Limitações Atuais e Autenticação Pendente

Nesta primeira etapa, o backend foca em entregar o catálogo e o health check estáveis para o Lovable. 
* **Autenticação:** A autenticação com o CRM Pipeimob está marcada como **Pendente** no catálogo. Nenhuma chamada real é feita nesta etapa.
* **Endpoint Pipeimob:** O endpoint definitivo está marcado como `null` devido a divergências de mapeamento técnico pendentes de resolução entre `/api/v2/negocios/transacoes` e `/api/v2/transacoes`.
