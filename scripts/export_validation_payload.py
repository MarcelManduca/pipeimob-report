#!/usr/bin/env python3
"""Build the private homologation payload without retaining client PII."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sales_reconciliation import (  # noqa: E402
    _normalize_pipe_sale,
    _normalize_vista_gain,
    reconcile_sales,
)
from services.validation_csv_loader import (  # noqa: E402
    load_pipeimob_transactions,
    load_vista_gains,
)


def _encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _record_hash(value):
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def main():
    pipe_path = Path(sys.argv[1])
    vista_path = Path(sys.argv[2])
    pipe_rows = load_pipeimob_transactions(pipe_path)
    vista_rows = load_vista_gains(vista_path)
    normalized_pipe = [_normalize_pipe_sale(row) for row in pipe_rows]
    normalized_vista = [_normalize_vista_gain(row) for row in vista_rows]
    payload = {
        "pipe_file": pipe_path.name,
        "vista_file": vista_path.name,
        "pipe_sha": hashlib.sha256(pipe_path.read_bytes()).hexdigest(),
        "vista_sha": hashlib.sha256(vista_path.read_bytes()).hexdigest(),
        "pipe": [
            {**row, "source_record_hash": _record_hash(row)}
            for row in normalized_pipe
        ],
        "vista": [
            {**row, "source_record_hash": _record_hash(row)}
            for row in normalized_vista
        ],
        "result": reconcile_sales(pipe_rows, vista_rows),
    }
    print(_encoded(payload))


if __name__ == "__main__":
    main()
