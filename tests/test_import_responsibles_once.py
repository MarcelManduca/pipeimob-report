import os
import sys
import uuid
import json
import csv
import hashlib
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database
from models.contracts_control import (
    Base,
    ContractsControlResponsible,
    ContractsControlManualData,
    ContractsControlManualDataHistory,
    normalize_responsible_name,
)
from repositories.contracts_control_repository import ContractsControlRepository
from scripts.import_responsibles_once import (
    normalize_code,
    parse_consolidated_csv,
    calculate_file_sha256,
)

# Use isolated SQLite database file for tests
test_db_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_import_once_temp.db"))
SQLALCHEMY_DATABASE_URL = f"sqlite:///{test_db_file}"
test_engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})

@event.listens_for(test_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Mock deal dataset for testing property code matching
MOCK_DEALS = [
    {"transaction_id": "tx_41170", "codigo_imovel": "41170"},
    {"transaction_id": "tx_40947", "codigo_imovel": "40947"},
    {"transaction_id": "tx_39177", "codigo_imovel": "39177"},
    {"transaction_id": "tx_42623", "codigo_imovel": "42623"},
    {"transaction_id": "tx_42625", "codigo_imovel": "42625"},
    {"transaction_id": "tx_41386", "codigo_imovel": "41386"},
    {"transaction_id": "tx_39726", "codigo_imovel": "39726"},
    {"transaction_id": "tx_ambiguous_1", "codigo_imovel": "99999"},
    {"transaction_id": "tx_ambiguous_2", "codigo_imovel": "99999"} # 2 deals for same code 99999 -> ambiguous!
]

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(database, "engine", test_engine)
    
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    
    db = TestingSessionLocal()
    resps = ["Guilherme", "Cristina", "Carol", "Laise"]
    for r in resps:
        obj = ContractsControlResponsible(
            id=uuid.uuid4(),
            name=r,
            normalized_name=normalize_responsible_name(r),
            active=True
        )
        db.add(obj)
    db.commit()
    db.close()

    def mock_list_deals(session, code):
        return [d for d in MOCK_DEALS if d["codigo_imovel"] == code]

    monkeypatch.setattr(ContractsControlRepository, "list_deals_by_property_code", mock_list_deals)

    yield

    Base.metadata.drop_all(bind=test_engine)


# -----------------------------------------------------------------------------
# 1. Test Normalization
# -----------------------------------------------------------------------------
def test_code_normalization():
    assert normalize_code("41170.0") == "41170"
    assert normalize_code(" 40947 ") == "40947"
    assert normalize_code("39177.000") == "39177"
    assert normalize_code("") == ""


# -----------------------------------------------------------------------------
# 2. Test Exact Responsible Matching
# -----------------------------------------------------------------------------
def test_exact_responsible_matching(tmp_path):
    csv_file = tmp_path / "test.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n"
        "1° TRIMESTRE,3,40947,Guilherme,GERENTE 2\n",
        encoding="utf-8"
    )
    parsed = parse_consolidated_csv(str(csv_file))
    assert parsed["proposed_by_code"]["41170"] == "Carol"
    assert parsed["proposed_by_code"]["40947"] == "Guilherme"
    assert len(parsed["invalid_responsible_ignored"]) == 0


# -----------------------------------------------------------------------------
# 3. Test Reject Ana Cristina
# -----------------------------------------------------------------------------
def test_reject_ana_cristina(tmp_path):
    csv_file = tmp_path / "test_ana.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Ana Cristina,GERENTE 1\n",
        encoding="utf-8"
    )
    parsed = parse_consolidated_csv(str(csv_file))
    assert "41170" not in parsed["proposed_by_code"]
    assert len(parsed["ana_cristina_ignored"]) == 1
    assert parsed["ana_cristina_ignored"][0]["codigo_imovel"] == "41170"


# -----------------------------------------------------------------------------
# 4. Test Explicit Overrides (39177, 42623, 42625 -> Guilherme)
# -----------------------------------------------------------------------------
def test_explicit_overrides_application(tmp_path):
    csv_file = tmp_path / "test_override.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,39177,Cristina,GERENTE 1\n"
        "2° TRIMESTRE,3,42623,Cristina,GERENTE 2\n"
        "2° TRIMESTRE,4,42625,Cristina,GERENTE 3\n",
        encoding="utf-8"
    )
    parsed = parse_consolidated_csv(str(csv_file))
    assert parsed["proposed_by_code"]["39177"] == "Guilherme"
    assert parsed["proposed_by_code"]["42623"] == "Guilherme"
    assert parsed["proposed_by_code"]["42625"] == "Guilherme"
    assert len(parsed["explicit_overrides_applied"]) == 3


# -----------------------------------------------------------------------------
# 5. Test Duplicate Codes Consolidation (Same Responsible)
# -----------------------------------------------------------------------------
def test_duplicate_codes_consolidation(tmp_path):
    csv_file = tmp_path / "test_dup_same.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n"
        "2° TRIMESTRE,5,41170,Carol,GERENTE 1\n",
        encoding="utf-8"
    )
    parsed = parse_consolidated_csv(str(csv_file))
    assert parsed["proposed_by_code"]["41170"] == "Carol"
    assert len(parsed["conflict_codes"]) == 0
    assert parsed["source_counts"]["duplicate_occurrences"] == 1


# -----------------------------------------------------------------------------
# 6. Test Conflict Codes Blocking (Different Responsibles)
# -----------------------------------------------------------------------------
def test_conflict_codes_blocking(tmp_path):
    csv_file = tmp_path / "test_conflict.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n"
        "2° TRIMESTRE,5,41170,Cristina,GERENTE 2\n",
        encoding="utf-8"
    )
    parsed = parse_consolidated_csv(str(csv_file))
    assert "41170" not in parsed["proposed_by_code"]
    assert len(parsed["conflict_codes"]) == 1
    assert parsed["conflict_codes"][0]["codigo_imovel"] == "41170"


# -----------------------------------------------------------------------------
# 7. Test Ambiguous Property Code Blocking (>1 Deal in DB)
# -----------------------------------------------------------------------------
def test_ambiguous_property_code_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    csv_file = tmp_path / "test_ambig.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,99999,Carol,GERENTE 1\n",
        encoding="utf-8"
    )
    from scripts.import_responsibles_once import main as run_import
    with patch("sys.argv", ["import_responsibles_once.py", "--file", str(csv_file), "--target", "staging"]):
        run_import()

    # Check generated report in reports/
    report_dirs = [d for d in os.listdir("reports") if d.startswith("import_once_")]
    latest_report_dir = sorted(report_dirs)[-1]
    with open(os.path.join("reports", latest_report_dir, "report.json")) as f:
        data = json.load(f)
    
    assert data["summary"]["database_matching_results"]["unique_codes_ambiguous"] == 1
    assert data["summary"]["database_matching_results"]["deals_eligible"] == 0


# -----------------------------------------------------------------------------
# 8. Test Dry-Run Zero Writes
# -----------------------------------------------------------------------------
def test_dry_run_zero_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    csv_file = tmp_path / "test_dry.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n",
        encoding="utf-8"
    )
    from scripts.import_responsibles_once import main as run_import
    with patch("sys.argv", ["import_responsibles_once.py", "--file", str(csv_file), "--target", "staging"]):
        run_import()

    db = TestingSessionLocal()
    manual_data = db.query(ContractsControlManualData).all()
    history = db.query(ContractsControlManualDataHistory).all()
    db.close()

    assert len(manual_data) == 0
    assert len(history) == 0


# -----------------------------------------------------------------------------
# 9. Test APP_ENV Mismatch Abortion
# -----------------------------------------------------------------------------
def test_app_env_mismatch_abortion(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    csv_file = tmp_path / "test_env.csv"
    csv_file.write_text("source_sheet,source_row,codigo_imovel,responsavel,gerente\n", encoding="utf-8")

    from scripts.import_responsibles_once import main as run_import
    with patch("sys.argv", ["import_responsibles_once.py", "--file", str(csv_file), "--target", "staging"]):
        with pytest.raises(SystemExit) as exc:
            run_import()
        assert exc.value.code == 1


# -----------------------------------------------------------------------------
# 10. Test Hash Mismatch Abortion
# -----------------------------------------------------------------------------
def test_hash_mismatch_abortion(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    csv_file = tmp_path / "test_hash.csv"
    csv_file.write_text("source_sheet,source_row,codigo_imovel,responsavel,gerente\n", encoding="utf-8")

    from scripts.import_responsibles_once import main as run_import
    with patch("sys.argv", ["import_responsibles_once.py", "--file", str(csv_file), "--target", "staging", "--apply", "--expected-source-sha256", "wrong_hash"]):
        with pytest.raises(SystemExit) as exc:
            run_import()
        assert exc.value.code == 1


# -----------------------------------------------------------------------------
# 11. Test Rollback Monotonic Version & History
# -----------------------------------------------------------------------------
def test_rollback_monotonic_version(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    csv_file = tmp_path / "test_apply_rollback.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n",
        encoding="utf-8"
    )
    file_sha256 = calculate_file_sha256(str(csv_file))

    from scripts.import_responsibles_once import main as run_import
    from scripts.rollback_responsibles_once import main as run_rollback

    # 1. Apply import
    with patch("sys.argv", [
        "import_responsibles_once.py",
        "--file", str(csv_file),
        "--target", "staging",
        "--apply",
        "--expected-source-sha256", file_sha256
    ]):
        run_import()

    db = TestingSessionLocal()
    md = db.query(ContractsControlManualData).filter_by(transaction_id="tx_41170").first()
    assert md is not None
    assert md.version == 1 # Initial version 1
    applied_resp_id = str(md.responsible_id)
    db.close()

    # Get execution_id from generated backup
    backup_dirs = [d for d in os.listdir("backups") if d.startswith("import_once_")]
    exec_id = sorted(backup_dirs)[-1].replace("import_once_", "")

    # 2. Run Rollback
    with patch("sys.argv", [
        "rollback_responsibles_once.py",
        "--execution-id", exec_id,
        "--target", "staging",
        "--apply"
    ]):
        run_rollback()

    db = TestingSessionLocal()
    md_after = db.query(ContractsControlManualData).filter_by(transaction_id="tx_41170").first()
    assert md_after.responsible_id is None # Restored to previous null
    assert md_after.version == 2 # Monotonic increment (1 -> 2)

    hist_entries = db.query(ContractsControlManualDataHistory).filter_by(transaction_id="tx_41170").order_by(ContractsControlManualDataHistory.new_version).all()
    assert len(hist_entries) == 2
    assert hist_entries[1].previous_version == 1
    assert hist_entries[1].new_version == 2
    assert hist_entries[1].previous_value == applied_resp_id
    assert hist_entries[1].new_value is None
    assert hist_entries[1].changed_by_sub == f"rollback:{exec_id}"
    db.close()


# -----------------------------------------------------------------------------
# 12. Test Rollback Conflict On Subsequent Change
# -----------------------------------------------------------------------------
def test_rollback_conflict_on_subsequent_change(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    csv_file = tmp_path / "test_conflict_rb.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n",
        encoding="utf-8"
    )
    file_sha256 = calculate_file_sha256(str(csv_file))

    from scripts.import_responsibles_once import main as run_import
    from scripts.rollback_responsibles_once import main as run_rollback

    # 1. Apply import
    with patch("sys.argv", [
        "import_responsibles_once.py",
        "--file", str(csv_file),
        "--target", "staging",
        "--apply",
        "--expected-source-sha256", file_sha256
    ]):
        run_import()

    # 2. Simulate user modifying responsible in UI after import
    db = TestingSessionLocal()
    guilherme_resp = db.query(ContractsControlResponsible).filter_by(name="Guilherme").first()
    guilherme_resp_id = guilherme_resp.id
    md = db.query(ContractsControlManualData).filter_by(transaction_id="tx_41170").first()
    md.responsible_id = guilherme_resp_id
    md.version = 2 # User bumped version to 2
    db.commit()
    db.close()

    backup_dirs = [d for d in os.listdir("backups") if d.startswith("import_once_")]
    exec_id = sorted(backup_dirs)[-1].replace("import_once_", "")

    # 3. Run Rollback -> Must detect conflict and preserve post-import user change!
    with patch("sys.argv", [
        "rollback_responsibles_once.py",
        "--execution-id", exec_id,
        "--target", "staging",
        "--apply"
    ]):
        run_rollback()

    db = TestingSessionLocal()
    md_after = db.query(ContractsControlManualData).filter_by(transaction_id="tx_41170").first()
    assert md_after.responsible_id == guilherme_resp_id # Preserved Guilherme!
    assert md_after.version == 2 # Unchanged version!
    db.close()


# -----------------------------------------------------------------------------
# 13. Test Atomic Rollback On Write Failure
# -----------------------------------------------------------------------------
def test_atomic_rollback_on_write_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    csv_file = tmp_path / "test_atomic_fail.csv"
    csv_file.write_text(
        "source_sheet,source_row,codigo_imovel,responsavel,gerente\n"
        "1° TRIMESTRE,2,41170,Carol,GERENTE 1\n"
        "1° TRIMESTRE,3,40947,Guilherme,GERENTE 2\n",
        encoding="utf-8"
    )
    file_sha256 = calculate_file_sha256(str(csv_file))

    from scripts.import_responsibles_once import main as run_import

    # Patch create_history_record to raise exception on second item
    orig_create_history = ContractsControlRepository.create_history_record
    call_count = 0

    def failing_create_history(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ValueError("Simulated DB write failure on 2nd item")
        return orig_create_history(*args, **kwargs)

    monkeypatch.setattr(ContractsControlRepository, "create_history_record", failing_create_history)

    with patch("sys.argv", [
        "import_responsibles_once.py",
        "--file", str(csv_file),
        "--target", "staging",
        "--apply",
        "--expected-source-sha256", file_sha256
    ]):
        with pytest.raises(ValueError, match="Simulated DB write failure"):
            run_import()

    # Confirm DB rollback left 0 items written
    db = TestingSessionLocal()
    manual_data = db.query(ContractsControlManualData).all()
    history = db.query(ContractsControlManualDataHistory).all()
    db.close()

    assert len(manual_data) == 0
    assert len(history) == 0
