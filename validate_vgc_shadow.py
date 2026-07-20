import csv
import sys
import os
from decimal import Decimal
from typing import Optional, List, Dict, Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from main import fetch_all_pipeimob_transactions

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
        print("Usage: python3 validate_vgc_shadow.py <path_to_csv_export>")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"Error: CSV file not found at {csv_path}")
        sys.exit(1)

    # 1. Read CSV
    with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as fh:
        sample = fh.read(2048)
        delim = ";" if ";" in sample else ","

    with open(csv_path, mode="r", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=delim)
        csv_rows = list(reader)

    # 2. Fetch API Transactions
    api_key = os.environ.get("PIPEIMOB_API_KEY")
    api_secret = os.environ.get("PIPEIMOB_SECRET_KEY")
    
    if not api_key or not api_secret:
        print("Error: PIPEIMOB_API_KEY and PIPEIMOB_SECRET_KEY must be configured in environment.")
        sys.exit(1)

    try:
        print("Fetching transactions from Pipeimob API H1 2026...")
        txs, pages = fetch_all_pipeimob_transactions(
            api_key=api_key,
            api_secret=api_secret,
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30"
        )
    except Exception as e:
        print(f"Failed to fetch API transactions: {e}")
        sys.exit(1)

    # 3. Analyze IDs & Duplications
    missing_api_id_count = 0
    duplicate_api_id_count = 0
    api_ids = []
    api_by_id = {}
    for tx in txs:
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
    print(f"  total_api_transactions: {len(txs)}")
    print(f"  total_export_rows: {len(csv_rows)}")
    print(f"  missing_api_id_count: {missing_api_id_count}")
    print(f"  missing_export_id_count: {missing_export_id_count}")
    print(f"  duplicate_api_id_count: {duplicate_api_id_count}")
    print(f"  duplicate_export_id_count: {duplicate_export_id_count}")
    print(f"  matched_transaction_count: {matched_transaction_count}")
    print(f"  unmatched_api_transaction_count: {unmatched_api_transaction_count}")
    print(f"  unmatched_export_transaction_count: {unmatched_export_transaction_count}")

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

        print(f"\nCandidate: {cand.name}")
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

    # Determine validation approval
    approved_cand = None
    for cand in candidates:
        if (matched_transaction_count == len(txs) and 
            unmatched_api_transaction_count == 0 and 
            duplicate_api_id_count == 0 and 
            duplicate_export_id_count == 0 and 
            cand.value_mismatch_count == 0 and 
            cand.missing_status_mismatch_count == 0 and 
            cand.multiple_candidate_count == 0 and 
            cand.maximum_difference <= Decimal("0.01") and 
            cand.absolute_aggregate_difference <= Decimal("0.01")):
            approved_cand = cand
            break

    if approved_cand:
        print(f"\nCONCLUSION: APPROVED rule '{approved_cand.name}' for activation!")
        sys.exit(0)
    else:
        print("\nCONCLUSION: REJECTED! No candidate rule met all criteria for promotion.")
        sys.exit(1)

if __name__ == "__main__":
    main()
