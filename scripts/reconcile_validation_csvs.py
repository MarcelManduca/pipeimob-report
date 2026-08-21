#!/usr/bin/env python3
"""Run the controlled August validation without emitting client PII."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sales_reconciliation import reconcile_sales
from services.validation_csv_loader import load_pipeimob_transactions, load_vista_gains


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pipeimob_csv")
    parser.add_argument("vista_csv")
    args = parser.parse_args()

    result = reconcile_sales(
        load_pipeimob_transactions(args.pipeimob_csv),
        load_vista_gains(args.vista_csv),
    )
    safe_report = {
        "summary": result["summary"],
        "status_counts": dict(Counter(item["status"] for item in result["items"])),
        "matched_property_codes": sorted(
            item["property_code"]
            for item in result["items"]
            if item.get("official_sale_date") and item.get("vista_deal_id")
        ),
        "unmatched_pipeimob_property_codes": sorted(
            item["property_code"]
            for item in result["items"]
            if item["status"] == "PIPEIMOB_SEM_GANHO_VISTA"
        ),
    }
    print(json.dumps(safe_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
