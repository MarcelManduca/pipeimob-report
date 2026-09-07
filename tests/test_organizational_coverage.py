"""Automated tests for Vista organizational coverage diagnostic and client (v3)."""

import io
import json
import urllib.error
from datetime import date
import pytest

from services.vista_organization_client import (
    VistaOrganizationAPIError,
    VistaOrganizationClient,
)
from services.organizational_coverage import evaluate_organizational_coverage
from services.vista_sales_client import VistaSalesConfigurationError


class MockHTTPResponse:
    def __init__(self, data: dict, code: int = 200) -> None:
        self.raw = json.dumps(data).encode("utf-8")
        self.code = code

    def read(self) -> bytes:
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


# ==============================================================================
# 1. Separating IDs from Labels & Nested Object IDs
# ==============================================================================

def test_text_label_never_becomes_team_id_produces_name_only():
    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        deal_team_field="EquipeNegocio",
    )
    # Plain string label "Equipe Alpha"
    deal_string = {"Codigo": "1", "EquipeNegocio": "Equipe Alpha", "CorretorNegocio": "100"}
    sanitized_str = client._sanitize_deal_record(deal_string)
    assert sanitized_str["team_id"] is None
    assert sanitized_str["team_name_only"] is True

    # User plain string label
    user_string = {"Codigo": "100", "Equipe": "Equipe Sul"}
    sanitized_user = client._sanitize_user_record(user_string)
    assert sanitized_user["team_id"] is None
    assert sanitized_user["team_name_only"] is True


def test_nested_object_ids_accepted():
    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        deal_team_field="EquipeNegocio",
    )
    # Object with Codigo field
    deal_obj = {"Codigo": "2", "EquipeNegocio": {"Codigo": "24", "Nome": "Equipe 24"}, "CorretorNegocio": "100"}
    sanitized_deal = client._sanitize_deal_record(deal_obj)
    assert sanitized_deal["team_id"] == "24"
    assert sanitized_deal["team_name_only"] is False

    # Object with id field
    user_obj = {"Codigo": "100", "Equipe": {"id": "24", "Nome": "Equipe 24"}}
    sanitized_user = client._sanitize_user_record(user_obj)
    assert sanitized_user["team_id"] == "24"
    assert sanitized_user["team_name_only"] is False


# ==============================================================================
# 2. Historical Transfers vs Simultaneous Conflicts
# ==============================================================================

def test_historical_transfers_classified_as_evidence_not_conflict():
    # Broker 101 has current user team 24, and past deals in team 10 on 2026-06-01 and team 24 on 2026-08-01
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "broker_id": "101", "team_id": "10", "deal_date": "2026-06-01"},
        {"deal_id": "d2", "broker_id": "101", "team_id": "24", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe"], "rejected": []},
        circuit_broken=False,
    )
    b2t = result["dimensions"]["broker_to_team"]
    assert b2t["metrics"]["conflict"] == 0
    assert b2t["metrics"]["historical_multi_team_evidence"] == 1
    assert b2t["metrics"]["resolved"] == 1
    assert b2t["status"] == "ready"


def test_simultaneous_same_date_competing_teams_produces_conflict():
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
    ]
    # Two deals on the exact same date with conflicting teams
    deals = [
        {"deal_id": "d1", "broker_id": "101", "team_id": "10", "deal_date": "2026-08-01"},
        {"deal_id": "d2", "broker_id": "101", "team_id": "99", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe"], "rejected": []},
        circuit_broken=False,
    )
    b2t = result["dimensions"]["broker_to_team"]
    assert b2t["metrics"]["conflict"] == 1
    assert b2t["status"] == "partial"


# ==============================================================================
# 3. Completeness & Truncated Snapshot Handling
# ==============================================================================

def test_truncated_snapshot_never_returns_ready():
    users = [
        {"user_id": "1", "role_type": "broker", "team_id": "24", "is_active": True},
        {"user_id": "5", "role_type": "manager", "team_id": "24", "agency_id": "1", "is_active": True},
    ]
    # Even if 100% of fetched records have IDs, if truncated=True status must be partial
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoGerente", "CodigoAgencia"], "rejected": []},
        circuit_broken=False,
        completeness={
            "pages_reported": 10,
            "pages_fetched": 3,
            "records_fetched": 150,
            "truncated": True,
            "complete": False,
        },
    )
    assert result["completeness"]["truncated"] is True
    assert result["dimensions"]["broker_to_team"]["status"] == "partial"
    assert result["overall_status"] == "partial"
    assert "snapshot_truncated_by_pagination_limit" in result["blocks_found"]


# ==============================================================================
# 4. Unconfigured Fields Are Not Speculatively Queried
# ==============================================================================

def test_unconfigured_fields_not_queried():
    queried_fields = []

    def probe_opener(req, *args, **kwargs):
        url = req.full_url
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        if "pesquisa" in params:
            pesquisa = json.loads(params["pesquisa"][0])
            queried_fields.extend(pesquisa.get("fields", []))
        return MockHTTPResponse({"total": 1, "paginas": 1, "1": {"Codigo": "1", "Status": "Ativo"}})

    # Client without optional user env vars configured
    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        opener=probe_opener,
    )
    client.fetch_anonymized_users(max_pages=1)
    # Check that random speculative fields like Codigo_Equipe or CodigoGestor were NOT queried
    assert "Codigo_Equipe" not in queried_fields
    assert "CodigoGestor" not in queried_fields


def test_user_field_catalog_drives_organizational_field_negotiation():
    requested_paths = []
    queried_fields = []

    def catalog_opener(req, *args, **kwargs):
        parsed = urllib.parse.urlparse(req.full_url)
        requested_paths.append(parsed.path)
        if parsed.path.endswith("/usuarios/listarcampos"):
            return MockHTTPResponse({
                "fields": {
                    "Codigo": "Codigo",
                    "CodigoEquipeComercial": "CodigoEquipeComercial",
                    "CodigoFilial": "CodigoFilial",
                    "CampoSemRelacao": "CampoSemRelacao",
                }
            })
        params = urllib.parse.parse_qs(parsed.query)
        pesquisa = json.loads(params["pesquisa"][0])
        queried_fields.extend(pesquisa.get("fields", []))
        return MockHTTPResponse({"total": 1, "paginas": 1, "1": {"Codigo": "1"}})

    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        opener=catalog_opener,
    )
    client.fetch_anonymized_users(max_pages=1)

    assert requested_paths[0].endswith("/usuarios/listarcampos")
    assert "CodigoEquipeComercial" in queried_fields
    assert "CodigoFilial" in queried_fields
    assert "CampoSemRelacao" not in queried_fields
    assert "Cargo" not in queried_fields


def test_broker_directory_classifies_users_without_retaining_names():
    def broker_opener(req, *args, **kwargs):
        path = urllib.parse.urlparse(req.full_url).path
        if path.endswith("/usuarios/listarcampos"):
            return MockHTTPResponse({"fields": {"Codigo": "Codigo"}})
        if path.endswith("/corretores/listar"):
            return MockHTTPResponse({
                "total": 1,
                "paginas": 1,
                "1": {"Codigo": "77", "Nome": "Nome não deve sair do cliente"},
            })
        return MockHTTPResponse({
            "total": 1,
            "paginas": 1,
            "1": {"Codigo": "77"},
        })

    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        opener=broker_opener,
    )
    users, _ = client.fetch_anonymized_users(max_pages=1)

    assert users == [{
        "user_id": "77",
        "role_type": "broker",
        "team_id": None,
        "manager_id": None,
        "agency_id": None,
        "team_name_only": False,
        "manager_name_only": False,
        "agency_name_only": False,
        "is_active": True,
        "is_inactive": False,
        "is_deleted": False,
    }]


def test_user_field_catalog_accepts_list_contract():
    def list_catalog_opener(req, *args, **kwargs):
        path = urllib.parse.urlparse(req.full_url).path
        if path.endswith("/usuarios/listarcampos"):
            return MockHTTPResponse(["Codigo", "CodigoEquipeComercial"])
        return MockHTTPResponse({"total": 1, "paginas": 1, "1": {"Codigo": "9"}})

    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        opener=list_catalog_opener,
    )
    users, _ = client.fetch_anonymized_users(max_pages=1)

    assert users[0]["user_id"] == "9"
    assert "CodigoEquipeComercial" in client.get_probed_fields()["accepted"]


def test_broker_directory_failure_does_not_discard_user_catalog_result():
    def optional_broker_opener(req, *args, **kwargs):
        path = urllib.parse.urlparse(req.full_url).path
        if path.endswith("/usuarios/listarcampos"):
            return MockHTTPResponse({"fields": {"Codigo": "Codigo"}})
        if path.endswith("/corretores/listar"):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}")
            )
        return MockHTTPResponse({"total": 1, "paginas": 1, "1": {"Codigo": "88"}})

    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        opener=optional_broker_opener,
    )
    users, _ = client.fetch_anonymized_users(max_pages=1)

    assert users[0]["user_id"] == "88"
    assert users[0]["role_type"] == "unknown"


# ==============================================================================
# 5. Role Categorization: Unknown Must Be 'unknown' and Never Count as Broker
# ==============================================================================

def test_unknown_role_classification():
    client = VistaOrganizationClient(base_url="https://api.vista.com", api_key="test")
    assert client._sanitize_user_record({"Codigo": "1"})["role_type"] == "unknown"
    assert client._sanitize_user_record({"Codigo": "2", "Cargo": "Random Title"})["role_type"] == "unknown"
    assert client._sanitize_user_record({"Codigo": "3", "Cargo": "Corretor"})["role_type"] == "broker"
    assert client._sanitize_user_record({"Codigo": "4", "Cargo": "Gerente"})["role_type"] == "manager"


def test_unknown_role_does_not_inflate_total_brokers_nor_generate_ready_coverage():
    # User with unknown role has team_id=24, and deals have CorretorNegocio="99"
    users = [
        {"user_id": "99", "role_type": "unknown", "team_id": "24", "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "broker_id": "99", "team_id": "24", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe"], "rejected": []},
        circuit_broken=False,
    )
    assert result["role_summary"]["unknown_role_count"] == 1
    assert result["role_summary"]["brokers_count"] == 0
    b2t = result["dimensions"]["broker_to_team"]
    assert b2t["metrics"]["total_brokers"] == 0
    assert b2t["metrics"]["resolved"] == 0
    assert b2t["status"] == "blocked"
    assert result["overall_status"] == "blocked"


# ==============================================================================
# 6. Temporal Semantics on Team->Manager and Team->Agency
# ==============================================================================

def test_temporal_semantics_team_to_manager():
    # Team 24 has current manager 5 from user record, past deal on 2026-06-01 had manager 6
    users = [
        {"user_id": "5", "role_type": "manager", "team_id": "24", "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "broker_id": "101", "team_id": "24", "manager_id": "6", "deal_date": "2026-06-01"},
        {"deal_id": "d2", "broker_id": "101", "team_id": "24", "manager_id": "5", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoGerente"], "rejected": []},
        circuit_broken=False,
    )
    t2m = result["dimensions"]["team_to_manager"]
    assert t2m["metrics"]["conflict"] == 0
    assert t2m["metrics"]["historical_change_evidence"] == 1
    assert t2m["metrics"]["resolved"] == 1
    assert t2m["status"] == "ready"


def test_temporal_semantics_team_to_manager_simultaneous_conflict():
    # Team 24 has two different managers on the exact same date
    deals = [
        {"deal_id": "d1", "team_id": "24", "manager_id": "6", "deal_date": "2026-08-01"},
        {"deal_id": "d2", "team_id": "24", "manager_id": "7", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=[],
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoGerente"], "rejected": []},
        circuit_broken=False,
    )
    t2m = result["dimensions"]["team_to_manager"]
    assert t2m["metrics"]["conflict"] == 1
    assert t2m["status"] == "partial"


def test_temporal_semantics_team_to_agency():
    # Team 24 has current agency 1, past deal on 2026-05-01 had agency 2
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "agency_id": "1", "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "team_id": "24", "agency_id": "2", "deal_date": "2026-05-01"},
        {"deal_id": "d2", "team_id": "24", "agency_id": "1", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoAgencia"], "rejected": []},
        circuit_broken=False,
    )
    t2a = result["dimensions"]["team_to_agency"]
    assert t2a["metrics"]["conflict"] == 0
    assert t2a["metrics"]["historical_change_evidence"] == 1
    assert t2a["metrics"]["resolved"] == 1
    assert t2a["status"] == "ready"


def test_temporal_semantics_team_to_agency_simultaneous_conflict():
    # Users simultaneously assign two different agencies to the same team in the current snapshot
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "agency_id": "1", "is_active": True},
        {"user_id": "102", "role_type": "broker", "team_id": "24", "agency_id": "2", "is_active": True},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoAgencia"], "rejected": []},
        circuit_broken=False,
    )
    t2a = result["dimensions"]["team_to_agency"]
    assert t2a["metrics"]["conflict"] == 1
    assert t2a["status"] == "partial"


# ==============================================================================
# 7. Zero Fields Negotiated Support & Complete Flag
# ==============================================================================

def test_zero_fields_negotiated_returns_unsupported_and_incomplete():
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        probed_fields_summary={"accepted": [], "rejected": ["BadField1", "BadField2"]},
        circuit_broken=False,
    )
    assert result["field_negotiation"]["supported"] is False
    assert result["completeness"]["supported"] is False
    assert result["completeness"]["complete"] is False
    assert result["overall_status"] == "blocked"
    assert "no_supported_fields_negotiated" in result["blocks_found"]


# ==============================================================================
# 8. Per-Source Metadata Tracking
# ==============================================================================

def test_per_source_metadata_tracking():
    sources = {
        "users": {
            "requested": True,
            "successful": True,
            "complete": True,
            "truncated": False,
            "error_code": None,
            "pages_reported": 1,
            "pages_fetched": 1,
            "records_fetched": 5,
        },
        "deals": {
            "requested": True,
            "successful": False,
            "complete": False,
            "truncated": False,
            "error_code": "vista_deal_query_failed",
            "pages_reported": 0,
            "pages_fetched": 0,
            "records_fetched": 0,
        },
    }
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
        {"user_id": "5", "role_type": "manager", "team_id": "24", "agency_id": "1", "is_active": True},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=[],
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoGerente", "CodigoAgencia"], "rejected": []},
        circuit_broken=False,
        sources=sources,
    )
    assert result["sources"]["users"]["successful"] is True
    assert result["sources"]["deals"]["successful"] is False
    assert result["sources"]["deals"]["error_code"] == "vista_deal_query_failed"
    assert result["completeness"]["complete"] is False
    assert result["overall_status"] == "partial"
    assert "one_or_more_requested_sources_failed" in result["blocks_found"]


# ==============================================================================
# 9. Adversarial Tests for v5
# ==============================================================================

def test_adversarial_source_complete_false_caps_diagnostic():
    sources = {
        "users": {
            "requested": True,
            "successful": True,
            "complete": False,
            "truncated": False,
            "error_code": None,
            "pages_reported": 1,
            "pages_fetched": 1,
            "records_fetched": 2,
        },
    }
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
        {"user_id": "5", "role_type": "manager", "team_id": "24", "agency_id": "1", "is_active": True},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        probed_fields_summary={"accepted": ["Codigo", "CodigoEquipe", "CodigoGerente", "CodigoAgencia"], "rejected": []},
        circuit_broken=False,
        sources=sources,
    )
    assert result["sources"]["users"]["complete"] is False
    assert result["completeness"]["complete"] is False
    assert result["overall_status"] == "partial"
    assert "one_or_more_requested_sources_incomplete" in result["blocks_found"]


def test_adversarial_only_generic_fields_accepted_yields_unsupported():
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": None, "is_active": True},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        probed_fields_summary={"accepted": ["Codigo", "Status", "Ativo", "Cargo", "Funcao", "Perfil"], "rejected": []},
        circuit_broken=False,
    )
    assert result["field_negotiation"]["supported"] is False
    assert result["completeness"]["supported"] is False
    assert result["completeness"]["complete"] is False
    assert result["overall_status"] == "blocked"
    assert result["dimensions"]["broker_to_team"]["supported"] is False
    assert result["dimensions"]["team_to_manager"]["supported"] is False
    assert result["dimensions"]["manager_to_agency"]["supported"] is False
    assert result["dimensions"]["team_to_agency"]["supported"] is False
    assert "no_supported_fields_negotiated" in result["blocks_found"]


def test_adversarial_opaque_organizational_field_does_not_claim_support():
    result = evaluate_organizational_coverage(
        anonymized_users=[
            {"user_id": "101", "role_type": "broker", "team_id": None, "is_active": True},
        ],
        probed_fields_summary={"accepted": ["CampoOrganizacionalX"], "rejected": []},
        circuit_broken=False,
    )

    assert result["field_negotiation"]["supported"] is False
    assert result["overall_status"] == "blocked"
    assert all(
        dimension["supported"] is False
        for dimension in result["dimensions"].values()
    )


def test_current_snapshot_and_same_day_deal_disagreement_is_conflict():
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "broker_id": "101", "team_id": "99", "deal_date": "2026-09-06"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["CodigoEquipe"], "rejected": []},
        circuit_broken=False,
        snapshot_date="2026-09-06",
    )

    dimension = result["dimensions"]["broker_to_team"]
    assert dimension["status"] == "partial"
    assert dimension["metrics"]["conflict"] == 1
    assert dimension["metrics"]["historical_multi_team_evidence"] == 0
    assert "broker_to_team_contains_simultaneous_conflicts" in result["blocks_found"]


def test_current_snapshot_and_undated_deal_disagreement_is_conflict():
    result = evaluate_organizational_coverage(
        anonymized_users=[
            {"user_id": "101", "role_type": "broker", "team_id": "24", "is_active": True},
        ],
        anonymized_deals=[
            {"deal_id": "d1", "broker_id": "101", "team_id": "99", "deal_date": None},
        ],
        probed_fields_summary={"accepted": ["CodigoEquipe"], "rejected": []},
        circuit_broken=False,
        snapshot_date="2026-09-06",
    )

    dimension = result["dimensions"]["broker_to_team"]
    assert dimension["metrics"]["conflict"] == 1
    assert dimension["status"] == "partial"


def test_adversarial_broker_without_team_in_snapshot_but_stable_deal_team_resolves_ready():
    users = [
        {"user_id": "101", "role_type": "broker", "team_id": None, "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "broker_id": "101", "team_id": "24", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "Cargo"], "rejected": []},
        circuit_broken=False,
    )
    b2t = result["dimensions"]["broker_to_team"]
    assert b2t["supported"] is True
    assert b2t["metrics"]["total_brokers"] == 1
    assert b2t["metrics"]["resolved"] == 1
    assert b2t["coverage_ratio"] == 1.0
    assert b2t["status"] == "ready"


def test_adversarial_manager_without_agency_in_snapshot_but_stable_deal_agency_resolves_ready():
    users = [
        {"user_id": "5", "role_type": "manager", "agency_id": None, "is_active": True},
    ]
    deals = [
        {"deal_id": "d1", "manager_id": "5", "agency_id": "1", "deal_date": "2026-08-01"},
    ]
    result = evaluate_organizational_coverage(
        anonymized_users=users,
        anonymized_deals=deals,
        probed_fields_summary={"accepted": ["Codigo", "Cargo"], "rejected": []},
        circuit_broken=False,
    )
    m2a = result["dimensions"]["manager_to_agency"]
    assert m2a["supported"] is True
    assert m2a["metrics"]["total_managers"] == 1
    assert m2a["metrics"]["resolved"] == 1
    assert m2a["coverage_ratio"] == 1.0
    assert m2a["status"] == "ready"


# ==============================================================================
# 10. Circuit Breaker
# ==============================================================================

def test_rejected_probe_fields_do_not_open_circuit_breaker():
    call_count = 0

    def failing_opener(req, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error": "bad"}'))

    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        max_failure_threshold=2,
        opener=failing_opener,
    )
    with pytest.raises(VistaOrganizationAPIError) as exc_info:
        client.fetch_anonymized_users()

    assert exc_info.value.error_code == "vista_http_400"
    assert client.is_circuit_broken() is False
    # One rejected catalog request followed by the bounded compatibility probe.
    assert call_count == 2


def test_operational_failures_still_open_circuit_breaker():
    call_count = 0

    def failing_opener(req, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise urllib.error.URLError("temporary transport failure")

    client = VistaOrganizationClient(
        base_url="https://api.vista.com",
        api_key="secret-key-123",
        max_failure_threshold=2,
        opener=failing_opener,
    )
    for _ in range(2):
        with pytest.raises(VistaOrganizationAPIError):
            client._execute_user_query(["Codigo"], is_probe=False)

    assert client.is_circuit_broken() is True
    before = call_count
    with pytest.raises(VistaOrganizationAPIError) as exc_info:
        client._execute_user_query(["Codigo"], is_probe=False)
    assert exc_info.value.error_code == "vista_circuit_broken"
    assert call_count == before
