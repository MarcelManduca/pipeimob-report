import json
import http.client
import threading
import time
import urllib.error
import urllib.parse
from datetime import date

import pytest

from services.vista_funnel_client import (
    VistaFunnelAPIError,
    VistaFunnelClient,
    summarize_created_deal_cohort,
)
from services.vista_sales_client import (
    VistaSalesAPIError,
    VistaSalesConfigurationError,
)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


def test_fetch_created_deals_uses_creation_date_and_all_statuses():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(
            {
                "1": {
                    "Codigo": "deal-1",
                    "DataInicial": "2026-08-01",
                    "Status": "Aberto",
                    "NomeEtapa": "Proposta",
                    "NomeCliente": "must-not-be-exposed",
                },
                "2": {
                    "Codigo": "deal-2",
                    "DataInicial": "2026-08-02",
                    "Status": "Perdido",
                    "NomeEtapa": "Proposta",
                },
                "total": 2,
                "paginas": 1,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com", "secret-key", "pipe-1", opener=opener
    )
    deals = client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
    pesquisa = json.loads(query["pesquisa"][0])
    assert pesquisa["filter"] == {"DataInicial": ["2026-08-01", "2026-08-30"]}
    assert "Status" not in pesquisa["filter"]
    assert "NomeCliente" not in pesquisa["fields"]
    assert "NomeCliente" not in deals[0]
    assert {deal["status"] for deal in deals} == {"Aberto", "Perdido"}


def test_fetch_created_deals_deduplicates_by_vista_deal_id():
    def opener(request, timeout):
        return FakeResponse(
            {
                "1": {"Codigo": "deal-1", "NomeEtapa": "Proposta"},
                "2": {"Codigo": "deal-1", "NomeEtapa": "Fechamento"},
                "total": 2,
                "paginas": 1,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com", "secret-key", "pipe-1", opener=opener
    )
    deals = client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert len(deals) == 1
    assert deals[0]["stage_name"] == "Fechamento"


def test_fetch_created_deals_retries_transient_failure_once():
    attempts = 0

    def opener(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("temporary")
        return FakeResponse(
            {
                "1": {"Codigo": "deal-1", "NomeEtapa": "Proposta"},
                "total": 1,
                "paginas": 1,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        opener=opener,
        request_attempts=2,
        retry_backoff_seconds=0,
    )

    deals = client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert attempts == 2
    assert [deal["deal_id"] for deal in deals] == ["deal-1"]


@pytest.mark.parametrize(
    "transport_error",
    [
        http.client.RemoteDisconnected("remote closed connection"),
        ConnectionResetError("connection reset"),
    ],
)
def test_fetch_created_deals_retries_low_level_transport_failures(
    transport_error,
):
    attempts = 0

    def opener(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise transport_error
        return FakeResponse(
            {
                "1": {"Codigo": "deal-1", "NomeEtapa": "Proposta"},
                "total": 1,
                "paginas": 1,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        opener=opener,
        request_attempts=2,
        retry_backoff_seconds=0,
    )

    deals = client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert attempts == 2
    assert [deal["deal_id"] for deal in deals] == ["deal-1"]


def test_fetch_created_deals_fetches_known_remaining_pages_concurrently():
    lock = threading.Lock()
    active = 0
    max_active = 0

    def opener(request, timeout):
        nonlocal active, max_active
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(request.full_url).query
        )
        pesquisa = json.loads(query["pesquisa"][0])
        page = pesquisa["paginacao"]["pagina"]
        if page > 1:
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
        return FakeResponse(
            {
                "1": {
                    "Codigo": f"deal-{page}",
                    "NomeEtapa": "Proposta",
                },
                "total": 4,
                "paginas": 4,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        opener=opener,
        page_concurrency=4,
    )

    deals = client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert {deal["deal_id"] for deal in deals} == {
        "deal-1",
        "deal-2",
        "deal-3",
        "deal-4",
    }
    assert max_active > 1


def test_fetch_created_deals_recovers_failed_parallel_page_serially():
    lock = threading.Lock()
    page_attempts = {}

    def opener(request, timeout):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(request.full_url).query
        )
        pesquisa = json.loads(query["pesquisa"][0])
        page = pesquisa["paginacao"]["pagina"]
        with lock:
            page_attempts[page] = page_attempts.get(page, 0) + 1
            attempt = page_attempts[page]
        if page == 3 and attempt == 1:
            raise urllib.error.URLError("temporary parallel refusal")
        return FakeResponse(
            {
                "1": {"Codigo": f"deal-{page}", "NomeEtapa": "Proposta"},
                "total": 4,
                "paginas": 4,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        opener=opener,
        request_attempts=1,
        retry_backoff_seconds=0,
        page_concurrency=4,
    )

    deals = client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert {deal["deal_id"] for deal in deals} == {
        "deal-1",
        "deal-2",
        "deal-3",
        "deal-4",
    }
    assert page_attempts[3] == 2


def test_summary_keeps_current_proposal_stage_separate_from_generated_proposals():
    summary = summarize_created_deal_cohort(
        [
            {"deal_id": "1", "created_at": "2026-08-01", "status": "Aberto", "stage_name": "Proposta", "responsible": "Gerente Um"},
            {"deal_id": "2", "created_at": "2026-08-02", "status": "Perdido", "stage_name": "Proposta", "team": "Equipe Direta"},
            {"deal_id": "3", "created_at": "2026-08-03", "status": "Ganho", "stage_name": "Fechamento"},
            {"deal_id": "4", "created_at": "2026-08-04", "status": "Em aberto", "stage_name": "Captação"},
        ]
    )

    assert summary["created_deals"] == 4
    assert summary["proposal"]["created_deals_currently_in_proposal"] == 2
    assert (
        summary["proposal"]["created_deals_in_proposal_stage_with_open_status"]
        == 1
    )
    proposal_matrix = next(
        row
        for row in summary["stage_status_breakdown"]
        if row["stage"] == "Proposta"
    )
    assert proposal_matrix == {
        "stage": "Proposta",
        "deals_count": 2,
        "status_breakdown": [
            {"status": "Aberto", "deals_count": 1},
            {"status": "Perdido", "deals_count": 1},
        ],
    }
    assert summary["proposal"]["proposals_generated_in_period"] is None
    assert (
        summary["proposal"]["proposals_generated_status"]
        == "requires_stage_event_history"
    )
    assert summary["proposal"]["assignment_breakdown"] == [
        {
            "team": None,
            "responsible": "Gerente Um",
            "created_date": "2026-08-01",
            "current_stage_deals_count": 1,
            "open_deals_count": 1,
        },
        {
            "team": "Equipe Direta",
            "responsible": None,
            "created_date": "2026-08-02",
            "current_stage_deals_count": 1,
            "open_deals_count": 0,
        },
    ]
    assert summary["data_quality"]["proposal_open_without_direct_team"] == 1
    assert (
        summary["data_quality"]["proposal_open_without_assignment_identity"]
        == 0
    )


def test_funnel_error_never_contains_api_key():
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "denied", None, None)

    client = VistaFunnelClient(
        "https://tenant.example.com", "never-expose-me", "pipe-1", opener=opener
    )

    with pytest.raises(VistaSalesAPIError) as error:
        client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert "never-expose-me" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "vista_http_401"),
        (403, "vista_http_403"),
        (429, "vista_http_429"),
        (422, "vista_http_422"),
        (503, "vista_http_503"),
    ],
)
def test_funnel_http_failures_are_safely_classified(status, expected_code):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "denied", None, None)

    client = VistaFunnelClient(
        "https://tenant.example.com",
        "never-expose-me",
        "pipe-1",
        request_attempts=1,
        opener=opener,
    )

    with pytest.raises(VistaFunnelAPIError) as error:
        client.fetch_created_deals(date(2026, 8, 1), date(2026, 8, 30))

    assert error.value.error_code == expected_code
    assert "never-expose-me" not in str(error.value)


def test_optional_dimension_field_names_are_validated():
    with pytest.raises(
        VistaSalesConfigurationError, match="VISTA_DEAL_AGENCY_FIELD"
    ):
        VistaFunnelClient(
            "https://tenant.example.com",
            "secret-key",
            "pipe-1",
            agency_field="Agency.Name",
        )


def test_from_env_does_not_assume_an_unconfirmed_responsible_field(monkeypatch):
    monkeypatch.setenv("VISTA_API_BASE_URL", "https://tenant.example.com")
    monkeypatch.setenv("VISTA_API_KEY", "secret-key")
    monkeypatch.setenv("VISTA_SALES_PIPE_ID", "pipe-1")
    monkeypatch.delenv("VISTA_DEAL_RESPONSIBLE_FIELD", raising=False)

    client = VistaFunnelClient.from_env()

    assert client.responsible_field is None
    assert "Responsavel" not in client.fields


def test_http_400_from_optional_field_retries_with_confirmed_core_fields():
    requested_fields = []

    def opener(request, timeout):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(request.full_url).query
        )
        fields = json.loads(query["pesquisa"][0])["fields"]
        requested_fields.append(fields)
        if "Responsavel" in fields:
            raise urllib.error.HTTPError(
                request.full_url, 400, "invalid field", None, None
            )
        return FakeResponse(
            {
                "1": {
                    "Codigo": "deal-1",
                    "DataInicial": "2026-08-01",
                    "Status": "Em aberto",
                    "NomeEtapa": "Proposta",
                },
                "total": 1,
                "paginas": 1,
            }
        )

    client = VistaFunnelClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        responsible_field="Responsavel",
        request_attempts=1,
        opener=opener,
    )

    deals = client.fetch_created_deals(
        date(2026, 8, 1), date(2026, 8, 31)
    )

    assert len(requested_fields) == 2
    assert "Responsavel" in requested_fields[0]
    assert "Responsavel" not in requested_fields[1]
    assert client.optional_fields_rejected is True
    assert deals[0]["stage_name"] == "Proposta"
