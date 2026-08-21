"""Privacy-preserving loaders for the controlled Pipeimob/Vista CSV validation."""

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Union


PathLike = Union[str, Path]


def load_pipeimob_transactions(path: PathLike) -> List[Dict[str, Any]]:
    """Load only the Pipeimob fields required by sales reconciliation."""
    rows = _read_semicolon_csv(path)
    return [
        {
            "transacao_unique_id_pipeimob": row.get("ID transação Pipeimob"),
            "codigo_contrato": row.get("Código contrato"),
            "codigo_imovel": row.get("Código imóvel"),
            "data_assinatura_ccv": row.get("Data CCV"),
            "valor_contrato": row.get("Valor contrato"),
            "agente_gestor": row.get("Agente gestor"),
        }
        for row in rows
    ]


def load_vista_gains(path: PathLike) -> List[Dict[str, Any]]:
    """Load Vista gains without retaining client contact or identity fields."""
    gains: List[Dict[str, Any]] = []
    for row in _read_semicolon_csv(path):
        if str(row.get("Status") or "").strip().casefold() != "ganho":
            continue
        title = str(row.get("Título") or "").strip()
        property_code = _property_code_from_title(title)
        gains.append(
            {
                "deal_id": property_code or _opaque_deal_id(title),
                "property_code": property_code,
                "gain_date": row.get("Encerramento do negócio"),
                "deal_value": row.get("Valor potencial"),
                "commercial_broker_name": row.get("Corretores"),
                "stage_name": row.get("Etapa"),
            }
        )
    return gains


def _read_semicolon_csv(path: PathLike) -> List[Dict[str, str]]:
    source = Path(path)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with source.open(encoding=encoding, newline="") as handle:
                return list(csv.DictReader(handle, delimiter=";"))
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, "unsupported CSV encoding")


def _property_code_from_title(title: str) -> str | None:
    match = re.match(r"\s*(\d+)\b", title)
    return match.group(1) if match else None


def _opaque_deal_id(title: str) -> str:
    """Create a stable local identifier without returning the original title."""
    import hashlib

    return "vista-local-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
