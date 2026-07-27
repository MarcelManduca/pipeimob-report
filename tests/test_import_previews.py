import io
import os
import uuid
import pytest
from typing import Optional, Any
from datetime import datetime, timezone, timedelta

# Set environment variables before main imports to satisfy other tests import order
os.environ["ALLOWED_ORIGINS"] = "https://lovable-test-origin.app"

from openpyxl import Workbook
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from database import Base
from main import app, verify_backend_api_key, get_db_session
from models.contracts_control import (
    ContractsControlResponsible,
    ContractsControlManualData,
    ContractsControlManualDataHistory,
    normalize_responsible_name,
    ContractsControlImportPreview,
    ContractsControlImportPreviewItem
)

# Use isolated test database for imports
test_db_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_preview_temp.db"))
SQLALCHEMY_DATABASE_URL = f"sqlite:///{test_db_file}"
test_engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})

@event.listens_for(test_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    monkeypatch.setenv("CONTRACTS_CONTROL_WRITES_ENABLED", "true")
    monkeypatch.setenv("CONTRACTS_CONTROL_ADMIN_SUBS", "admin_sub_123")

@pytest.fixture(autouse=True)
def setup_test_db():
    if os.path.exists(test_db_file):
        try:
            os.remove(test_db_file)
        except:
            pass
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    
    # Insert test responsibles
    guilherme = ContractsControlResponsible(
        id=uuid.uuid4(),
        name="Guilherme",
        normalized_name=normalize_responsible_name("Guilherme"),
        active=True
    )
    cristina = ContractsControlResponsible(
        id=uuid.uuid4(),
        name="Cristina",
        normalized_name=normalize_responsible_name("Cristina"),
        active=True
    )
    laise = ContractsControlResponsible(
        id=uuid.uuid4(),
        name="Laise",
        normalized_name=normalize_responsible_name("Laise"),
        active=True
    )
    inactive_resp = ContractsControlResponsible(
        id=uuid.uuid4(),
        name="InactiveUser",
        normalized_name=normalize_responsible_name("InactiveUser"),
        active=False
    )
    
    db.add_all([guilherme, cristina, laise, inactive_resp])
    
    # Insert some manual data for existing attributions
    # tx_sync already synchronized
    db.add(ContractsControlManualData(
        transaction_id="tx_sync",
        responsible_id=guilherme.id,
        version=1,
        created_by_sub="test_sub",
        updated_by_sub="test_sub"
    ))
    # tx_to_change will change from Cristina to Guilherme
    db.add(ContractsControlManualData(
        transaction_id="tx_to_change",
        responsible_id=cristina.id,
        version=1,
        created_by_sub="test_sub",
        updated_by_sub="test_sub"
    ))
    # tx_to_clear will clear its responsible
    db.add(ContractsControlManualData(
        transaction_id="tx_to_clear",
        responsible_id=laise.id,
        version=1,
        created_by_sub="test_sub",
        updated_by_sub="test_sub"
    ))
    
    db.commit()
    db.close()
    yield
    test_engine.dispose()
    if os.path.exists(test_db_file):
        try:
            os.remove(test_db_file)
        except:
            pass

# Mock jwt credentials
mock_admin_token = "mock_admin_jwt"
mock_reader_token = "mock_reader_jwt"

from fastapi import Header
async def override_verify_backend_api_key(authorization: Optional[str] = Header(None)):
    if authorization == f"Bearer {mock_admin_token}":
        return {"sub": "admin_sub_123", "role": "authenticated", "email": "admin@gralhaimoveis.com.br"}
    elif authorization == f"Bearer {mock_reader_token}":
        return {"sub": "reader_sub_456", "role": "authenticated", "email": "reader@gralhaimoveis.com.br"}
    raise Exception("Invalid credentials")

def override_get_db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

@pytest.fixture(autouse=True)
def setup_overrides():
    app.dependency_overrides[verify_backend_api_key] = override_verify_backend_api_key
    app.dependency_overrides[get_db_session] = override_get_db_session
    yield
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
    if get_db_session in app.dependency_overrides:
        del app.dependency_overrides[get_db_session]

# Mock Pipeimob CRM dataset
MOCK_PIPEIMOB_DATASET = [
    # 13. Unique match (1 transaction)
    {
        "transacao_unique_id_pipeimob": "tx_unique_101",
        "codigo_imovel": "10001",
        "titulo_nome_negocio": "Apartamento Centro",
        "agente_gestor": "Carlos Silva",
        "data_criacao": "2026-01-10",
        "data_assinatura_ccv": "2026-02-10"
    },
    # Sync targets
    {
        "transacao_unique_id_pipeimob": "tx_sync",
        "codigo_imovel": "10002",
        "titulo_nome_negocio": "Casa Jardim",
        "agente_gestor": "Fernanda"
    },
    {
        "transacao_unique_id_pipeimob": "tx_to_change",
        "codigo_imovel": "10003",
        "titulo_nome_negocio": "Cobertura Centro"
    },
    {
        "transacao_unique_id_pipeimob": "tx_to_clear",
        "codigo_imovel": "10004",
        "titulo_nome_negocio": "Sala Comercial"
    },
    # 14. Multiple candidates resolved by data_criacao
    {
        "transacao_unique_id_pipeimob": "tx_multi_resolved_1",
        "codigo_imovel": "10005",
        "titulo_nome_negocio": "Terreno 1",
        "data_criacao": "2026-03-01",
        "data_assinatura_ccv": "2026-03-15"
    },
    {
        "transacao_unique_id_pipeimob": "tx_multi_resolved_2",
        "codigo_imovel": "10005",
        "titulo_nome_negocio": "Terreno 2",
        "data_criacao": "2026-05-01",
        "data_assinatura_ccv": "2026-05-15"
    },
    # 15. Unresolved ambiguity
    {
        "transacao_unique_id_pipeimob": "tx_ambiguous_1",
        "codigo_imovel": "10006",
        "titulo_nome_negocio": "Studio A"
    },
    {
        "transacao_unique_id_pipeimob": "tx_ambiguous_2",
        "codigo_imovel": "10006",
        "titulo_nome_negocio": "Studio B"
    },
    # Overrides targets
    {
        "transacao_unique_id_pipeimob": "tx_override_39177_1",
        "codigo_imovel": "39177",
        "titulo_nome_negocio": "Negocio 39177"
    },
    {
        "transacao_unique_id_pipeimob": "tx_override_39177_2",
        "codigo_imovel": "39177",
        "titulo_nome_negocio": "Negocio 39177 B"
    },
    # Long alphanumeric property code target
    {
        "transacao_unique_id_pipeimob": "tx_long_alphanumeric_id_xyz_123_abc",
        "codigo_imovel": "IMOV-ALPHA-BETA-9999",
        "titulo_nome_negocio": "Mansão Lago"
    }
]

@pytest.fixture(autouse=True)
def mock_pipeimob_dataset_write(monkeypatch):
    async def mock_write():
        return MOCK_PIPEIMOB_DATASET
    monkeypatch.setattr("main.get_contracts_control_dataset_for_write", mock_write)

def create_in_memory_xlsx(data_sheets: dict) -> bytes:
    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)
    
    for sheet_name, rows in data_sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for r in rows:
            ws.append(r)
            
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

def create_in_memory_csv(rows: list, delimiter=",") -> bytes:
    out = io.StringIO()
    writer = csv_writer = csv_write = None
    import csv
    writer = csv.writer(out, delimiter=delimiter)
    for r in rows:
        writer.writerow(r)
    return out.getvalue().encode("utf-8")

client = TestClient(app)

def test_spreadsheet_import_preview_full_suite():
    # 1. XLSX with three abas & column headers in different positions
    sheets = {
        "Aba 1": [
            ["COD. IMÓVEL", "RESPONSÁVEL", "GERENTE"],
            ["10001", "Guilherme", "Carlos Silva"],
            ["10002", "Guilherme", "Fernanda"],
        ],
        "Aba 2": [
            ["GERENTE", "COD IMÓVEL", "RESPONSAVEL"],
            ["Maria", "10003", "Guilherme"], # ready_to_change
            ["Jose", "10004", ""], # ready_to_clear
        ],
        "Aba 3": [
            ["CÓDIGO IMÓVEL", "RESPONSÁVEL", "DATA DE CADASTRO", "DATA ASSINATURA CCV"],
            [10005, "Laise", "2026-05-01", "2026-05-15"], # multiple resolved deterministically
            ["IMOV-ALPHA-BETA-9999", "Guilherme", "2026-06-01", "2026-06-15"] # long alphanumeric id
        ]
    }
    xlsx_bytes = create_in_memory_xlsx(sheets)
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_test.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    
    assert response.status_code == 200
    res = response.json()
    
    assert "preview_id" in res
    assert res["source_format"] == "xlsx"
    assert res["parser_version"] == "1.0"
    assert res["status"] == "ready"
    
    summary = res["summary"]
    assert summary["source_rows_count"] == 6
    assert summary["unique_property_codes_count"] == 6
    assert summary["to_assign_count"] == 3 # 10001, 10005, IMOV-ALPHA-BETA-9999
    assert summary["already_synchronized_count"] == 1 # 10002
    assert summary["to_change_count"] == 1 # 10003
    assert summary["to_clear_count"] == 1 # 10004
    
    items = res["items"]
    # Check that it returned page 1 items
    assert len(items) <= 25

def test_spreadsheet_import_csv():
    # 3. CSV format with delimiter detection
    csv_rows = [
        ["CÓDIGO DO IMÓVEL", "RESPONSAVEL", "GERENTE"],
        ["10001", "Guilherme", "Carlos Silva"],
        ["10006", "Laise", "Rodrigo"] # Ambiguous
    ]
    csv_bytes = create_in_memory_csv(csv_rows, delimiter=";")
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_test.csv", csv_bytes, "text/csv")}
    )
    
    assert response.status_code == 200
    res = response.json()
    assert res["source_format"] == "csv"
    
    summary = res["summary"]
    assert summary["source_rows_count"] == 2
    assert summary["ambiguous_match_count"] == 1 # 10006

def test_spreadsheet_duplicate_same_and_conflict_rules():
    # 6. Duplicidade com mesmo responsável
    # 7. Duplicidade conflitante
    # 8. Três overrides (39177 -> Guilherme)
    # 5. Duplicidade entre responsável preenchido e vazio (source_conflict)
    sheets = {
        "Sheet1": [
            ["CODIGO IMOVEL", "RESPONSÁVEL"],
            ["10001", "Guilherme"],
            ["10001", "Guilherme"], # duplicate same responsible -> consolidated
            ["10002", "Guilherme"],
            ["10002", "Laise"], # duplicate conflicting responsible -> source_conflict
            ["39177", "Guilherme"],
            ["39177", "Laise"], # duplicate conflicting but overridden -> resolved to Guilherme
            ["10003", "Guilherme"],
            ["10003", ""] # duplicate filled vs empty -> source_conflict
        ]
    }
    xlsx_bytes = create_in_memory_xlsx(sheets)
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_dup_test.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    
    assert response.status_code == 200
    res = response.json()
    summary = res["summary"]
    
    # Overrides 39177 resolves to Guilherme (which matches mock crm candidate tx_override_39177_1)
    # Since 39177 was conflicting but resolved, duplicate_conflict_count handles it
    assert summary["duplicate_conflict_count"] >= 1
    
    # Verify that duplicate conflict items are returned with status source_conflict
    items = res["items"]
    # Verify 10002 is source_conflict
    item_10002 = next(i for i in items if i["codigo_imovel"] == "10002")
    assert item_10002["decisao_proposta"] == "source_conflict"
    assert "source_occurrences" in item_10002
    assert len(item_10002["source_occurrences"]["occurrences"]) == 2

def test_spreadsheet_ana_cristina_invalid_source_row():
    # 9. Ana Cristina registrada como excluída/inválida, sem desaparecer do resumo
    sheets = {
        "Sheet1": [
            ["CODIGO IMOVEL", "RESPONSÁVEL"],
            ["10001", "Ana Cristina"],
            ["10002", "Cristina"]
        ]
    }
    xlsx_bytes = create_in_memory_xlsx(sheets)
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_ana.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    
    assert response.status_code == 200
    res = response.json()
    summary = res["summary"]
    
    assert summary["invalid_source_row_count"] >= 1
    
    items = res["items"]
    item_ana = next(i for i in items if i["codigo_imovel"] == "10001")
    assert item_ana["decisao_proposta"] == "invalid_source_row"
    assert "Ana Cristina" in item_ana["responsavel_planilha"]

def test_unregistered_and_inactive_responsibles():
    # 11. Responsável inexistente
    # 12. Responsável inativo (counts as invalid source row)
    sheets = {
        "Sheet1": [
            ["CODIGO IMOVEL", "RESPONSÁVEL"],
            ["10001", "NonExistent"],
            ["10002", "InactiveUser"]
        ]
    }
    xlsx_bytes = create_in_memory_xlsx(sheets)
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_invalid_resps.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    
    assert response.status_code == 200
    res = response.json()
    
    items = res["items"]
    item_nonexistent = next(i for i in items if i["codigo_imovel"] == "10001")
    assert item_nonexistent["decisao_proposta"] == "responsible_not_registered"
    
    item_inactive = next(i for i in items if i["codigo_imovel"] == "10002")
    assert item_inactive["decisao_proposta"] == "responsible_inactive"

def test_import_unauthorized_user():
    # 22. Usuário sem autorização (reader) -> HTTP 403
    sheets = {
        "Sheet1": [
            ["CODIGO IMOVEL", "RESPONSÁVEL"],
            ["10001", "Guilherme"]
        ]
    }
    xlsx_bytes = create_in_memory_xlsx(sheets)
    headers = {"Authorization": f"Bearer {mock_reader_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_unauth.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert response.status_code == 403

def test_import_expired_preview_410():
    # 11. Para preview expirado, usar HTTP 410 Gone
    db = TestingSessionLocal()
    from models.contracts_control import ContractsControlImportPreview
    
    expired_preview = ContractsControlImportPreview(
        id=uuid.uuid4(),
        source_filename="expired.xlsx",
        source_format="xlsx",
        parser_version="1.0",
        created_by_sub="test_sub",
        status="ready",
        source_hash="expired_hash",
        summary={},
        created_at=datetime.now(timezone.utc) - timedelta(hours=25),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    db.add(expired_preview)
    db.commit()
    expired_id = str(expired_preview.id)
    db.close()
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.get(
        f"/api/contracts-control/imports/responsibles/previews/{expired_id}",
        headers=headers
    )
    assert response.status_code == 410

def test_import_limits_excess():
    # Validation of limits: sheets / rows
    # sheets limit exceeds
    sheets_overflow = {f"Aba_{i}": [["CODIGO IMOVEL", "RESPONSÁVEL"]] for i in range(12)}
    xlsx_bytes = create_in_memory_xlsx(sheets_overflow)
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_limits.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert response.status_code == 400
    assert "exceeds limit" in response.json()["detail"]

def test_import_no_write_and_bi_isolation():
    # 23. Nenhuma escrita durante preview nas tabelas operacionais
    # 24. BI inalterado e isolamento
    sheets = {
        "Sheet1": [
            ["CODIGO IMOVEL", "RESPONSÁVEL"],
            ["10001", "Guilherme"]
        ]
    }
    xlsx_bytes = create_in_memory_xlsx(sheets)
    
    db = TestingSessionLocal()
    manual_data_count_before = db.query(ContractsControlManualData).count()
    history_count_before = db.query(ContractsControlManualDataHistory).count()
    db.close()
    
    headers = {"Authorization": f"Bearer {mock_admin_token}"}
    response = client.post(
        "/api/contracts-control/imports/responsibles/preview",
        headers=headers,
        files={"file": ("import_no_write.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert response.status_code == 200
    
    # Assert counts are exactly the same
    db = TestingSessionLocal()
    manual_data_count_after = db.query(ContractsControlManualData).count()
    history_count_after = db.query(ContractsControlManualDataHistory).count()
    db.close()
    
    assert manual_data_count_before == manual_data_count_after
    assert history_count_before == history_count_after
