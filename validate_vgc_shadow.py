import csv
import sys
import os
import urllib.request
import urllib.error
import ssl
import json
from decimal import Decimal
from typing import Optional, List, Dict, Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from main import get_auth_token

class CandidateRule:
    def __init__(self, name: str):
        self.name = name
        # metrics
        self.candidate_present_count = 0
        self.candidate_missing_count = 0
        self.exact_value_match_count = 0
        self.within_one_cent_count = 0
        self.value_mismatch_count = 0
        self.missing_status_match_count = 0
        self.missing_status_mismatch_count = 0
        self.no_candidate_count = 0
        self.multiple_candidate_count = 0
        self.maximum_difference = Decimal("0.0")
        self.aggregate_difference = Decimal("0.0")
        self.absolute_aggregate_difference = Decimal("0.0")

    def evaluate(self, c_list: List[Dict[str, Any]]) -> tuple[Optional[Decimal], int]:
        # Returns (summed_val, match_count)
        matched_items = []
        for c in c_list:
            if not isinstance(c, dict):
                continue
            
            is_imob = c.get("comissionado_imobiliaria")
            if is_imob is None:
                is_imob = c.get("comissionado_imobiliária")
            is_imob_bool = is_imob is True or str(is_imob).lower() in ["true", "1"]
            
            is_filial = c.get("comissionado_filial")
            is_filial_bool = is_filial is True or str(is_filial).lower() in ["true", "1"]
            
            is_agencia = c.get("comissionado_agencia")
            is_agencia_bool = is_agencia is True or str(is_agencia).lower() in ["true", "1"]
            
            tipo = str(c.get("comissionado_tipo_participacao") or "").lower()
            is_tipo_bool = tipo in ["imobiliária", "imobiliaria"]

            matched = False
            if self.name == "candidate_imobiliaria_flag":
                matched = is_imob_bool
            elif self.name == "candidate_filial_flag":
                matched = is_filial_bool
            elif self.name == "candidate_agencia_flag":
                matched = is_agencia_bool or is_imob_bool
            elif self.name == "candidate_tipo_imobiliaria":
                matched = is_tipo_bool
            elif self.name == "candidate_combined_flags":
                matched = is_imob_bool or is_filial_bool or is_agencia_bool or is_tipo_bool
            elif self.name == "candidate_current_backend_rule":
                matched = is_imob_bool

            if matched:
                matched_items.append(c)

        if not matched_items:
            return None, 0

        total_val = Decimal("0.0")
        for item in matched_items:
            val_raw = item.get("comissionado_valor")
            if val_raw is None:
                val_raw = item.get("valor")
            if val_raw is not None and str(val_raw).strip() != "":
                try:
                    val_str = str(val_raw).replace("R$", "").replace(".", "").replace(",", ".").strip()
                    total_val += Decimal(val_str)
                except Exception:
                    pass
        return total_val, len(matched_items)

def clean_decimal(val_raw: Any) -> Optional[Decimal]:
    if val_raw is None or str(val_raw).strip() == "":
        return None
    try:
        val_str = str(val_raw).replace("R$", "").replace(".", "").replace(",", ".").strip()
        return Decimal(val_str)
    except Exception:
        return None

def main():
    if len(sys.argv) < 2:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print("Error: Missing CSV path argument.")
        sys.exit(2)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print(f"Error: CSV file not found at {csv_path}")
        sys.exit(2)

    # 1. Read CSV
    try:
        with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as fh:
            sample = fh.read(2048)
            delim = ";" if ";" in sample else ","

        with open(csv_path, mode="r", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, delimiter=delim)
            csv_rows = list(reader)
            headers = reader.fieldnames or []
    except Exception as e:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print(f"Error: Failed to read CSV: {e}")
        sys.exit(2)

    if "transacao_uniqueID" not in headers or "comissionado_valor" not in headers:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print("Error: Missing required columns 'transacao_uniqueID' or 'comissionado_valor' in CSV.")
        sys.exit(2)

    # 2. Fetch API Transactions
    api_key = os.environ.get("PIPEIMOB_API_KEY")
    api_secret = os.environ.get("PIPEIMOB_SECRET_KEY")
    
    if not api_key or not api_secret:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print("Error: PIPEIMOB_API_KEY and PIPEIMOB_SECRET_KEY must be configured in environment.")
        sys.exit(2)

    # Fetch pages
    base_url = "https://api.pipeimob.com.br/v2"
    url_prefix = "&data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30"
    
    all_transactions = []
    seen_ids = set()
    current_page = 1
    pages_fetched = 0
    total_reported = None
    pagination_finished_normally = False

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    print("Authenticating with Pipeimob API...")
    try:
        token = get_auth_token(api_key, api_secret)
        if not token:
            print("run_status: operational_error")
            print("validation_status = operational_error")
            print("Error: Authentication succeeded but returned empty token.")
            sys.exit(2)
    except Exception as e:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print(f"Error: Authentication failed: {e}")
        sys.exit(2)

    print("Fetching transaction pages from Pipeimob API...")
    try:
        while True:
            if current_page > 100:  # Infinite loop protection
                break
                
            url = f"{base_url}/negocios/transacoes?pagina={current_page}{url_prefix}"
            req = urllib.request.Request(
                url,
                headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Mozilla/5.0'}
            )
            
            with urllib.request.urlopen(req, context=ssl_context, timeout=15) as response:
                res_body = json.loads(response.read().decode('utf-8'))
                if not res_body.get("success"):
                    print("run_status: operational_error")
                    print("validation_status = operational_error")
                    print("Error: API response success=False.")
                    sys.exit(2)
                
                pages_fetched += 1
                txs = res_body.get("data", {}).get("transacoes", []) if isinstance(res_body.get("data"), dict) else []
                
                for tx in txs:
                    tx_id = tx.get("transacao_unique_id_pipeimob")
                    if tx_id:
                        if tx_id not in seen_ids:
                            seen_ids.add(tx_id)
                            all_transactions.append(tx)
                    else:
                        all_transactions.append(tx)

                # Pagination metadata
                meta_p = None
                if "meta" in res_body and isinstance(res_body["meta"], dict) and "pagination" in res_body["meta"]:
                    meta_p = res_body["meta"]["pagination"]
                elif "data" in res_body and isinstance(res_body["data"], dict) and "meta" in res_body["data"] and isinstance(res_body["data"]["meta"], dict) and "pagination" in res_body["data"]["meta"]:
                    meta_p = res_body["data"]["meta"]["pagination"]
                
                if meta_p is None:
                    print("run_status: operational_error")
                    print("validation_status = operational_error")
                    print("Error: Pagination metadata not found in response.")
                    sys.exit(2)
                
                if total_reported is None:
                    total_reported = meta_p.get("total")
                
                last_page = meta_p.get("total_pages") or 1
                if current_page >= last_page:
                    pagination_finished_normally = True
                    break
                    
                current_page += 1
    except Exception as e:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print(f"Error during API pagination: {e}")
        sys.exit(2)

    # Check pagination consistency
    if total_reported is not None and len(all_transactions) != total_reported:
        print("run_status: operational_error")
        print("validation_status = operational_error")
        print(f"Error: Fetched transactions count ({len(all_transactions)}) does not match reported total ({total_reported}).")
        sys.exit(2)

    # 3. Analyze IDs & Duplications
    missing_api_id_count = 0
    duplicate_api_id_count = 0
    api_ids = []
    api_by_id = {}
    for tx in all_transactions:
        tx_id = tx.get("transacao_unique_id_pipeimob")
        if not tx_id:
            missing_api_id_count += 1
            continue
        tx_id_str = str(tx_id).strip()
        api_ids.append(tx_id_str)
        if tx_id_str in api_by_id:
            duplicate_api_id_count += 1
        api_by_id[tx_id_str] = tx

    missing_export_id_count = 0
    duplicate_export_id_count = 0
    export_ids = []
    export_by_id = {}
    for r in csv_rows:
        exp_id = r.get("transacao_uniqueID")
        if not exp_id:
            missing_export_id_count += 1
            continue
        exp_id_str = str(exp_id).strip()
        export_ids.append(exp_id_str)
        if exp_id_str in export_by_id:
            duplicate_export_id_count += 1
        export_by_id[exp_id_str] = r

    matched_ids = set(api_by_id.keys()).intersection(set(export_by_id.keys()))
    matched_transaction_count = len(matched_ids)
    
    unmatched_api_transaction_count = len(set(api_by_id.keys()) - matched_ids)
    unmatched_export_transaction_count = len(set(export_by_id.keys()) - matched_ids)

    print("\nID Mapping Metrics:")
    print(f"  total_api_transactions: {len(all_transactions)}")
    print(f"  total_export_rows: {len(csv_rows)}")
    print(f"  missing_api_id_count: {missing_api_id_count}")
    print(f"  missing_export_id_count: {missing_export_id_count}")
    print(f"  duplicate_api_id_count: {duplicate_api_id_count}")
    print(f"  duplicate_export_id_count: {duplicate_export_id_count}")
    print(f"  matched_transaction_count: {matched_transaction_count}")
    print(f"  unmatched_api_transaction_count: {unmatched_api_transaction_count}")
    print(f"  unmatched_export_transaction_count: {unmatched_export_transaction_count}")

    # Pagination Validation Print
    print("\nPagination Metrics:")
    print(f"  pages_consultadas: {pages_fetched}")
    print(f"  total_informado_pela_api: {total_reported}")
    print(f"  quantidade_processada: {len(all_transactions)}")
    print(f"  paginacao_terminou_normalmente: {pagination_finished_normally}")

    # 4. Evaluate Candidates
    candidates = [
        CandidateRule("candidate_imobiliaria_flag"),
        CandidateRule("candidate_filial_flag"),
        CandidateRule("candidate_agencia_flag"),
        CandidateRule("candidate_tipo_imobiliaria"),
        CandidateRule("candidate_combined_flags"),
        CandidateRule("candidate_current_backend_rule")
    ]

    for cand in candidates:
        export_present_count = 0
        export_missing_count = 0
        
        for m_id in matched_ids:
            tx = api_by_id[m_id]
            r = export_by_id[m_id]
            
            csv_val = clean_decimal(r.get("comissionado_valor"))
            if csv_val is None:
                export_missing_count += 1
            else:
                export_present_count += 1

            c_list = tx.get("comissionados") or []
            cand_val, match_count = cand.evaluate(c_list)
            
            if match_count == 0:
                cand.no_candidate_count += 1
            elif match_count > 1:
                cand.multiple_candidate_count += 1

            if cand_val is None:
                cand.candidate_missing_count += 1
            else:
                cand.candidate_present_count += 1

            # Comparison
            if csv_val is None and cand_val is None:
                cand.missing_status_match_count += 1
                diff = Decimal("0.0")
            elif csv_val is None and cand_val is not None:
                cand.missing_status_mismatch_count += 1
                diff = Decimal("0.0") - cand_val
            elif csv_val is not None and cand_val is None:
                cand.missing_status_mismatch_count += 1
                diff = csv_val
            else:
                diff = csv_val - cand_val
                if diff == Decimal("0.0"):
                    cand.exact_value_match_count += 1
                elif abs(diff) <= Decimal("0.01"):
                    cand.within_one_cent_count += 1
                else:
                    cand.value_mismatch_count += 1

            cand.maximum_difference = max(cand.maximum_difference, abs(diff))
            cand.aggregate_difference += diff
            cand.absolute_aggregate_difference += abs(diff)

        # Approved calculation check
        approved = (matched_transaction_count == len(all_transactions) and 
                    unmatched_api_transaction_count == 0 and 
                    duplicate_api_id_count == 0 and 
                    duplicate_export_id_count == 0 and 
                    cand.value_mismatch_count == 0 and 
                    cand.missing_status_mismatch_count == 0 and 
                    cand.multiple_candidate_count == 0 and 
                    cand.maximum_difference <= Decimal("0.01") and 
                    cand.absolute_aggregate_difference <= Decimal("0.01"))

        print(f"\nCandidate: {cand.name}")
        print(f"  transaction_count: {len(all_transactions)}")
        print(f"  matched_transaction_count: {matched_transaction_count}")
        print(f"  unmatched_api_transaction_count: {unmatched_api_transaction_count}")
        print(f"  unmatched_export_transaction_count: {unmatched_export_transaction_count}")
        print(f"  duplicate_api_id_count: {duplicate_api_id_count}")
        print(f"  duplicate_export_id_count: {duplicate_export_id_count}")
        print(f"  export_present_count: {export_present_count}")
        print(f"  export_missing_count: {export_missing_count}")
        print(f"  candidate_present_count: {cand.candidate_present_count}")
        print(f"  candidate_missing_count: {cand.candidate_missing_count}")
        print(f"  exact_value_match_count: {cand.exact_value_match_count}")
        print(f"  within_one_cent_count: {cand.within_one_cent_count}")
        print(f"  value_mismatch_count: {cand.value_mismatch_count}")
        print(f"  missing_status_match_count: {cand.missing_status_match_count}")
        print(f"  missing_status_mismatch_count: {cand.missing_status_mismatch_count}")
        print(f"  no_candidate_count: {cand.no_candidate_count}")
        print(f"  multiple_candidate_count: {cand.multiple_candidate_count}")
        print(f"  maximum_difference: R$ {cand.maximum_difference}")
        print(f"  aggregate_difference: R$ {cand.aggregate_difference}")
        print(f"  absolute_aggregate_difference: R$ {cand.absolute_aggregate_difference}")
        print(f"  approved: {approved}")

    # Determine validation approval
    approved_cand = None
    for cand in candidates:
        approved = (matched_transaction_count == len(all_transactions) and 
                    unmatched_api_transaction_count == 0 and 
                    duplicate_api_id_count == 0 and 
                    duplicate_export_id_count == 0 and 
                    cand.value_mismatch_count == 0 and 
                    cand.missing_status_mismatch_count == 0 and 
                    cand.multiple_candidate_count == 0 and 
                    cand.maximum_difference <= Decimal("0.01") and 
                    cand.absolute_aggregate_difference <= Decimal("0.01"))
        if approved:
            approved_cand = cand
            break

    if approved_cand:
        print("\nrun_status: validated_candidate")
        print(f"CONCLUSION: APPROVED rule '{approved_cand.name}' for activation!")
        sys.exit(0)
    else:
        print("\nrun_status: no_valid_candidate")
        print("CONCLUSION: REJECTED! No candidate rule met all criteria for promotion.")
        sys.exit(1)

if __name__ == "__main__":
    main()
