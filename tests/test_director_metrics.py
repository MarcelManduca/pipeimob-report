from services.director_metrics import (
    broker_sales,
    funnel_summary,
    sales_divergences,
    sales_summary,
)


def _payload():
    return {
        "period": {"start": "2026-08-01", "end": "2026-08-20"},
        "generated_at": "2026-08-20T21:00:00Z",
        "official_source": "pipeimob_api_v2",
        "commercial_source": "vista_negocio_ganho",
        "summary": {
            "official_sales": 2,
            "official_vgv": "900000",
            "matched": 1,
            "pipeimob_without_vista_gain": 1,
        },
        "items": [
            {
                "status": "CONCILIADO",
                "issues": [],
                "official_sale_date": "2026-08-10",
                "official_value": "600000",
                "vista_deal_id": "vista-1",
                "commercial_broker": "Corretor A",
                "client_name": "must never leave the backend",
            },
            {
                "status": "PIPEIMOB_SEM_GANHO_VISTA",
                "issues": [],
                "official_sale_date": "2026-08-11",
                "official_value": "300000",
                "pipeimob_transaction_id": "pipe-2",
                "property_code": "200",
                "client_email": "must-never-leak@example.com",
            },
        ],
    }


def test_read_only_views_preserve_source_rules_and_remove_pii():
    summary = sales_summary(_payload())
    divergences = sales_divergences(_payload())
    brokers = broker_sales(_payload())

    assert summary["sales"] == 2
    assert summary["pending_pipeimob_without_vista_gain"] == 1
    assert divergences["count"] == 1
    assert "client" not in str(divergences).lower()
    assert brokers["brokers"] == [
        {"commercial_broker": "Corretor A", "sales": 1, "vgv": "600000"}
    ]


def test_funnel_view_keeps_only_aggregate_stage_fields():
    result = funnel_summary(
        {
            "period": {"start": "2026-08-01", "end": "2026-08-20"},
            "stages": [
                {"name": "Fechamento", "count": 4, "client_name": "private"}
            ],
        }
    )

    assert result["stages"] == [{"name": "Fechamento", "count": 4}]
    assert "only Vista Status=Ganho" in result["rule"]
