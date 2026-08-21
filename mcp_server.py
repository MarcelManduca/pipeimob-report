"""Read-only MCP server for validated Pipeimob + Vista management indicators."""

import asyncio
import os
from typing import Any, Callable

import jwt
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.request_state import get_access_token
from mcp.types import ToolAnnotations

from services.director_metrics import (
    DirectorMetricsClient,
    broker_sales,
    funnel_summary,
    quality_summary,
    sales_divergences,
    sales_summary,
)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


class SupabaseDirectorTokenVerifier(TokenVerifier):
    """Validate individual Supabase JWTs and the authorized director audience."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            payload = await asyncio.to_thread(self._decode, token)
        except Exception:
            return None

        email = str(payload.get("email") or "").strip().lower()
        subject = str(payload.get("sub") or "").strip()
        role = str(payload.get("role") or "").strip()
        if not email or not subject or role != "authenticated":
            return None
        if not _authorized_email(email):
            return None

        return AccessToken(
            token=token,
            client_id=str(payload.get("client_id") or "supabase-chatgpt"),
            scopes=["openid", "email"],
            expires_at=int(payload["exp"]) if payload.get("exp") else None,
            subject=subject,
            claims={"email": email, "role": role},
        )

    @staticmethod
    def _decode(token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        audience = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
        issuer = _required_env("SUPABASE_ISSUER")
        options = {"require": ["exp", "sub", "email"]}

        if algorithm == "HS256":
            secret = _required_env("SUPABASE_JWT_SECRET")
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=audience,
                issuer=issuer,
                options=options,
            )
        if algorithm not in {"RS256", "ES256"}:
            raise ValueError("Unsupported JWT algorithm")
        jwks_url = _required_env("SUPABASE_JWKS_URL")
        signing_key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            options=options,
        )


def create_server(
    *,
    auth_required: bool | None = None,
    client_factory: Callable[[], DirectorMetricsClient] | None = None,
) -> MCPServer:
    if auth_required is None:
        auth_required = os.getenv("MCP_AUTH_REQUIRED", "true").lower() != "false"

    auth = None
    token_verifier = None
    if auth_required:
        issuer = _required_env("SUPABASE_ISSUER")
        public_url = _required_env("MCP_PUBLIC_URL").rstrip("/")
        auth = AuthSettings(
            issuer_url=issuer,
            resource_server_url=f"{public_url}/mcp",
            required_scopes=["openid", "email"],
        )
        token_verifier = SupabaseDirectorTokenVerifier()

    server = MCPServer(
        name="gralha-management-validation",
        title="Gralha — Indicadores Pipeimob × Vista",
        description="Consultas gerenciais validadas, somente leitura e sem PII de clientes.",
        version="0.1.0-validation",
        instructions=(
            "Use Pipeimob as the official source for sale existence, date and VGV. "
            "Use Vista Status=Ganho for commercial ownership. Never count Fechamento "
            "as a sale. State the period, sources and data-quality limitations."
        ),
        auth=auth,
        token_verifier=token_verifier,
    )
    make_client = client_factory or _client_from_env

    @server.tool(
        name="consultar_resumo_vendas",
        title="Consultar resumo de vendas",
        description="Retorna vendas oficiais, VGV e situação da conciliação no período.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def consultar_resumo_vendas(data_inicio: str, data_fim: str) -> dict[str, Any]:
        payload = await make_client().sales_reconciliation(
            data_inicio, data_fim, _backend_token()
        )
        return sales_summary(payload)

    @server.tool(
        name="consultar_conciliacao_vendas",
        title="Consultar conciliação de vendas",
        description="Retorna o resumo e os registros conciliados ou pendentes, sem PII de clientes.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def consultar_conciliacao_vendas(
        data_inicio: str, data_fim: str
    ) -> dict[str, Any]:
        payload = await make_client().sales_reconciliation(
            data_inicio, data_fim, _backend_token()
        )
        return {
            **sales_summary(payload),
            "divergences": sales_divergences(payload),
        }

    @server.tool(
        name="listar_divergencias_vendas",
        title="Listar divergências de vendas",
        description="Lista contratos pendentes, datas/valores divergentes e vínculos incompletos.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def listar_divergencias_vendas(
        data_inicio: str, data_fim: str
    ) -> dict[str, Any]:
        payload = await make_client().sales_reconciliation(
            data_inicio, data_fim, _backend_token()
        )
        return sales_divergences(payload)

    @server.tool(
        name="listar_corretores_com_vendas",
        title="Listar corretores com vendas",
        description="Agrupa vendas por corretor comercial informado no Vista, sem substituir pelo fiscal.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def listar_corretores_com_vendas(
        data_inicio: str, data_fim: str
    ) -> dict[str, Any]:
        payload = await make_client().sales_reconciliation(
            data_inicio, data_fim, _backend_token()
        )
        return broker_sales(payload)

    @server.tool(
        name="consultar_funil",
        title="Consultar funil",
        description="Retorna as etapas do funil, mantendo Fechamento separado de venda ganha.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def consultar_funil(data_inicio: str, data_fim: str) -> dict[str, Any]:
        payload = await make_client().dashboard_full(
            data_inicio, data_fim, _backend_token()
        )
        return funnel_summary(payload)

    @server.tool(
        name="consultar_qualidade_dados",
        title="Consultar qualidade dos dados",
        description="Retorna lacunas e limitações das fontes Pipeimob e Vista no período.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def consultar_qualidade_dados(
        data_inicio: str, data_fim: str
    ) -> dict[str, Any]:
        client = make_client()
        token = _backend_token()
        reconciliation, dashboard = await asyncio.gather(
            client.sales_reconciliation(data_inicio, data_fim, token),
            client.dashboard_full(data_inicio, data_fim, token),
        )
        return quality_summary(reconciliation, dashboard)

    return server


def _backend_token() -> str:
    access_token = get_access_token()
    if access_token and access_token.token:
        return access_token.token
    fallback = os.getenv("MCP_BACKEND_BEARER_TOKEN", "").strip()
    if fallback:
        return fallback
    raise RuntimeError("Authenticated director access is required")


def _client_from_env() -> DirectorMetricsClient:
    return DirectorMetricsClient(
        _required_env("PIPEIMOB_BI_BACKEND_URL"),
        int(os.getenv("MCP_BACKEND_TIMEOUT_SECONDS", "35")),
    )


def _authorized_email(email: str) -> bool:
    allowed_emails = {
        value.strip().lower()
        for value in os.getenv("MCP_ALLOWED_DIRECTOR_EMAILS", "").split(",")
        if value.strip()
    }
    allowed_domains = {
        value.strip().lower()
        for value in os.getenv(
            "MCP_ALLOWED_DIRECTOR_DOMAINS", "gralhaimoveis.com.br"
        ).split(",")
        if value.strip()
    }
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return email in allowed_emails or domain in allowed_domains


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


if __name__ == "__main__":
    mcp = create_server()
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("MCP_PORT", "8000"))),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
