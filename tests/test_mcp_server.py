import asyncio
from unittest.mock import AsyncMock

from mcp_server import SupabaseDirectorTokenVerifier
from mcp_server import create_server
from services.director_metrics import DirectorMetricsClient


class FakeDirectorClient:
    async def sales_reconciliation(self, start, end, bearer_token):
        assert bearer_token == "validation-token"
        return {
            "period": {"start": start, "end": end},
            "official_source": "pipeimob_api_v2",
            "commercial_source": "vista_negocio_ganho",
            "summary": {"official_sales": 19, "official_vgv": "24196504"},
            "items": [],
        }

    async def dashboard_full(self, start, end, bearer_token):
        assert bearer_token == "validation-token"
        return {"period": {"start": start, "end": end}, "stages": []}


def test_mcp_advertises_only_read_only_director_tools(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_BEARER_TOKEN", "validation-token")
    server = create_server(
        auth_required=False, client_factory=lambda: FakeDirectorClient()
    )

    async def inspect_tools():
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == {
            "consultar_resumo_vendas",
            "consultar_conciliacao_vendas",
            "listar_divergencias_vendas",
            "listar_corretores_com_vendas",
            "consultar_funil",
            "consultar_qualidade_dados",
        }
        assert all(tool.annotations.read_only_hint is True for tool in tools)
        assert all(tool.annotations.destructive_hint is False for tool in tools)
        assert all(
            tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is True
            for tool in tools
        )

        result = await server.call_tool(
            "consultar_resumo_vendas",
            {"data_inicio": "2026-08-01", "data_fim": "2026-08-20"},
        )
        assert result.is_error is False
        assert result.structured_content["sales"] == 19
        assert result.structured_content["vgv"] == "24196504"

    asyncio.run(inspect_tools())


def test_director_token_uses_scopes_supported_by_supabase(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("SUPABASE_ISSUER", "https://example.supabase.co/auth/v1")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://validation.example.com")
    server = create_server(auth_required=True)

    assert server.settings.auth.required_scopes == ["openid", "email"]

    verifier = SupabaseDirectorTokenVerifier()
    monkeypatch.setattr(
        verifier,
        "_decode",
        lambda token: {
            "sub": "director-1",
            "email": "director@gralhaimoveis.com.br",
            "role": "authenticated",
            "exp": 9999999999,
        },
    )

    access_token = asyncio.run(verifier.verify_token("token"))
    assert access_token.scopes == ["openid", "email"]


def test_sales_can_use_isolated_validation_endpoint():
    client = DirectorMetricsClient(
        "https://existing-backend.example.com",
        sales_url="https://validation.example.com/director-validation",
    )
    client._get_url = AsyncMock(return_value={"summary": {"official_sales": 19}})

    result = asyncio.run(
        client.sales_reconciliation("2026-08-01", "2026-08-20", "director-token")
    )

    assert result["summary"]["official_sales"] == 19
    client._get_url.assert_awaited_once_with(
        "https://validation.example.com/director-validation",
        {"data_inicio_ccv": "2026-08-01", "data_fim_ccv": "2026-08-20"},
        "director-token",
    )
