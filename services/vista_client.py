"""
Cliente oficial de integração com o CRM Vista (api.vistahost.com.br).
Consulta negócios com status 'Ganho' e enriquece com dados do corretor comercial.
"""

import os
import json
import ssl
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Union


# ==============================================================================
# Exceções Tipadas do Cliente CRM Vista
# ==============================================================================

class VistaClientError(Exception):
    """Exceção base do cliente CRM Vista."""
    pass


class VistaConfigurationError(VistaClientError):
    """Configuração ausente ou inválida do CRM Vista (ex.: VISTA_API_KEY)."""
    pass


class VistaAuthenticationError(VistaClientError):
    """Erro 401/403 de autenticação na API do CRM Vista."""
    pass


class VistaTimeoutError(VistaClientError):
    """Tempo limite de conexão ou leitura excedido na API do CRM Vista."""
    pass


class VistaResponseError(VistaClientError):
    """Resposta inválida, estrutura inesperada ou erro 5xx da API do CRM Vista."""
    pass


class VistaIncompleteQueryError(VistaClientError):
    """Limite defensivo de paginação atingido com registros pendentes."""
    pass


# ==============================================================================
# Cliente de Vendas do CRM Vista
# ==============================================================================

class VistaSalesClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        sales_pipe_id: Optional[Union[str, int]] = None,
        timeout_seconds: Optional[int] = None
    ):
        # 1. Base URL
        env_base = os.getenv("VISTA_API_BASE_URL")
        self.base_url = (base_url or env_base or "https://gralhaim-rest.vistahost.com.br").rstrip("/")

        # 2. API Key
        self.api_key = api_key or os.getenv("VISTA_API_KEY")

        # 3. Funil de Vendas (codigo_pipe)
        pipe_raw = sales_pipe_id if sales_pipe_id is not None else os.getenv("VISTA_SALES_PIPE_ID")
        self.sales_pipe_id = str(pipe_raw).strip() if pipe_raw is not None and str(pipe_raw).strip() != "" else None

        # 4. Timeout com limites seguros (1 a 30s)
        if timeout_seconds is not None:
            self.timeout = max(1, min(30, int(timeout_seconds)))
        else:
            try:
                env_t = int(os.getenv("VISTA_HTTP_TIMEOUT_SECONDS", "12"))
                self.timeout = max(1, min(30, env_t))
            except ValueError:
                self.timeout = 12

        self.ssl_context = ssl.create_default_context()

    def _api_get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executa requisição GET autenticada na API REST do CRM Vista.
        Lança exceções tipadas em caso de falha (não mascara erros como lista vazia).
        """
        if not self.api_key:
            raise VistaConfigurationError("VISTA_API_KEY não configurada no ambiente.")

        merged_params = {"key": self.api_key, **params}
        query_string = urllib.parse.urlencode(merged_params)
        url = f"{self.base_url}/{endpoint.lstrip('/')}?{query_string}"

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Pipeimob-Report-VistaClient/2.0"
            }
        )

        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=self.timeout) as resp:
                raw_data = resp.read().decode("utf-8")
                try:
                    parsed = json.loads(raw_data)
                except Exception as json_err:
                    raise VistaResponseError(f"Resposta não é um JSON válido do CRM Vista: {json_err}")

                if not isinstance(parsed, dict):
                    raise VistaResponseError("Resposta da API Vista não possui estrutura de objeto/dicionário.")

                # Verificar se a resposta contém mensagem de erro da própria API Vista
                if parsed.get("status") in (401, 403) or parsed.get("message") == "Invalid API key":
                    raise VistaAuthenticationError(f"Autenticação recusada pelo CRM Vista: {parsed.get('message')}")
                if "error" in parsed and parsed["error"]:
                    raise VistaResponseError(f"Erro retornado pela API Vista: {parsed['error']}")

                return parsed

        except urllib.error.HTTPError as http_err:
            if http_err.code in (401, 403):
                raise VistaAuthenticationError(f"Falha de autenticação na API do CRM Vista (HTTP {http_err.code}).")
            raise VistaResponseError(f"API do CRM Vista retornou HTTP {http_err.code}.")

        except urllib.error.URLError as url_err:
            reason_str = str(url_err.reason).lower() if hasattr(url_err, "reason") else ""
            if "timeout" in reason_str or "timed out" in reason_str:
                raise VistaTimeoutError(f"Tempo limite de conexão ({self.timeout}s) com o CRM Vista excedido.")
            raise VistaResponseError(f"Erro de rede ao conectar com o CRM Vista: {url_err.reason}")

        except (VistaClientError, VistaConfigurationError, VistaAuthenticationError, VistaTimeoutError, VistaResponseError):
            raise
        except Exception as generic_err:
            raise VistaResponseError(f"Erro inesperado na chamada ao CRM Vista: {generic_err}")

    def fetch_users_map(self) -> Dict[str, str]:
        """
        Consulta /usuarios/listar com paginação e retorna mapeamento codigo -> nome do corretor.
        """
        if not self.api_key:
            raise VistaConfigurationError("VISTA_API_KEY não configurada.")

        users_map = {}
        page = 1
        page_size = 50
        max_pages = 20

        while page <= max_pages:
            params = {
                "showtotal": "1",
                "pesquisa": json.dumps({
                    "fields": ["codigo", "nome", "email", "status"]
                }),
                "paginacao": json.dumps({
                    "pagina": page,
                    "quantidade": page_size
                })
            }

            res = self._api_get("usuarios/listar", params)
            if not res:
                break

            # Identificar quantidade bruta de registros no payload retornado
            raw_records = [
                v for k, v in res.items()
                if k not in ("total", "paginas", "pagina", "quantidade") and isinstance(v, dict)
            ]

            for v in raw_records:
                code = str(v.get("codigo") or v.get("Codigo") or "").strip()
                name = str(v.get("nome") or v.get("Nome") or "").strip()
                if code and name:
                    users_map[code] = name

            total_str = str(res.get("total") or "").strip()
            total_records = int(total_str) if total_str.isdigit() else None

            # Critério de parada de paginação baseado nos registros brutos e metadados
            if len(raw_records) < page_size:
                break
            if total_records is not None and len(users_map) >= total_records:
                break

            page += 1

        return users_map

    def fetch_won_deals(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Consulta /negocios/listar filtrando com a semântica canônica exata:
        - filter.Status = "Ganho"
        - filter.DataFinal = [data_inicial, data_final]
        - codigo_pipe = VISTA_SALES_PIPE_ID (se configurado)
        Garante limites inclusivos e paginação baseada nos registros brutos.
        """
        if not self.api_key:
            raise VistaConfigurationError("VISTA_API_KEY não configurada.")

        fields = [
            "codigo", "Status", "status", "Etapa", "etapa", "Valor", "valor",
            "DataFechamento", "data_fechamento", "DataGanho", "data_ganho",
            "Imovel", "codigo_imovel", "imovel", "Corretor", "corretor",
            "CorretorNome", "corretor_nome"
        ]

        filter_dict: Dict[str, Any] = {"Status": "Ganho"}

        # Filtro de período dinâmico e inclusivo em DataFinal
        if start_date and end_date:
            filter_dict["DataFinal"] = [start_date, end_date]
        elif start_date:
            filter_dict["DataFinal"] = [start_date, start_date]
        elif end_date:
            filter_dict["DataFinal"] = [end_date, end_date]

        won_deals = []
        page = 1
        page_size = 50
        max_pages = 50  # Suporta até 2.500 negócios no período

        while page <= max_pages:
            params: Dict[str, Any] = {
                "showtotal": "1",
                "pesquisa": json.dumps({
                    "fields": fields,
                    "filter": filter_dict
                }),
                "paginacao": json.dumps({
                    "pagina": page,
                    "quantidade": page_size
                })
            }

            if self.sales_pipe_id:
                params["codigo_pipe"] = self.sales_pipe_id

            res = self._api_get("negocios/listar", params)
            if not res:
                break

            # Identificar registros brutos retornados na página
            raw_records = [
                v for k, v in res.items()
                if k not in ("total", "paginas", "pagina", "quantidade") and isinstance(v, dict)
            ]

            for v in raw_records:
                # Leitura defensiva com tolerância a variações de capitalização
                status_raw = str(v.get("Status") or v.get("status") or "").strip().lower()
                
                # Somente 'Ganho' representa venda; Fechamento não é venda
                if status_raw != "ganho":
                    continue

                won_deals.append(v)

            total_str = str(res.get("total") or "").strip()
            total_records = int(total_str) if total_str.isdigit() else None

            # Controle de paginação baseado estritamente nos itens brutos da página
            if len(raw_records) < page_size:
                break
            if total_records is not None and (page * page_size) >= total_records:
                break

            if page == max_pages and len(raw_records) == page_size:
                raise VistaIncompleteQueryError(
                    f"Limite defensivo de {max_pages * page_size} negócios atingido com dados pendentes no CRM Vista."
                )

            page += 1

        return won_deals

    def get_enriched_won_deals(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Retorna lista de negócios 'Ganho' enriquecidos com o nome do corretor comercial.
        """
        deals = self.fetch_won_deals(start_date=start_date, end_date=end_date)
        if not deals:
            return []

        users_map = self.fetch_users_map()

        enriched = []
        for d in deals:
            deal_id = str(d.get("codigo") or d.get("Codigo") or d.get("id") or "").strip()
            prop_code = str(d.get("codigo_imovel") or d.get("CodigoImovel") or d.get("imovel") or d.get("Imovel") or "").strip() or None
            status_val = d.get("Status") or d.get("status") or "Ganho"
            etapa_val = str(d.get("Etapa") or d.get("etapa") or "").strip()
            valor_val = d.get("Valor") if d.get("Valor") is not None else d.get("valor")
            data_fech = d.get("DataFechamento") or d.get("data_fechamento") or d.get("DataGanho") or d.get("data_ganho")
            
            broker_code = str(d.get("Corretor") or d.get("corretor") or "").strip()
            broker_name = str(d.get("CorretorNome") or d.get("corretor_nome") or users_map.get(broker_code) or "").strip() or None

            enriched.append({
                "deal_id": deal_id,
                "codigo_imovel": prop_code,
                "status": status_val,
                "etapa": etapa_val,
                "valor": valor_val,
                "data_fechamento": data_fech,
                "corretor_nome": broker_name,
                "corretor_codigo": broker_code or None
            })

        return enriched
