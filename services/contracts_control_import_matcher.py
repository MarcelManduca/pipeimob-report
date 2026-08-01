import re
import unicodedata
from datetime import datetime, date, timezone
from typing import List, Dict, Any, Optional, Tuple

def normalize_text(t: str) -> str:
    if not t:
        return ""
    # strip, collapse spaces, lowercase
    t = t.strip()
    t = " ".join(t.split())
    t = t.lower()
    # remove accents
    t = "".join(
        c for c in unicodedata.normalize("NFKD", t)
        if not unicodedata.combining(c)
    )
    return t

def parse_date(d_val: Any) -> Optional[date]:
    if d_val is None:
        return None
    if isinstance(d_val, date):
        if isinstance(d_val, datetime):
            return d_val.date()
        return d_val
        
    s = str(d_val).strip()
    if not s:
        return None
        
    # Standard BR format: DD/MM/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
            
    # Standard BR format two digit year: DD/MM/YY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})$", s)
    if m:
        try:
            yr = int(m.group(3))
            yr = 2000 + yr if yr < 50 else 1900 + yr
            return date(yr, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
            
    # ISO format: YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:T.*)?$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
            
    return None

class ContractsControlImportMatcher:
    @staticmethod
    def match_transaction(
        codigo_imovel: str,
        transactions_by_code: List[Dict[str, Any]],
        gerente: Optional[str] = None,
        nome_imovel: Optional[str] = None,
        data_cadastro: Optional[str] = None,
        data_assinatura_ccv: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
        
        # If no candidates at all
        if not transactions_by_code:
            return None, "not_found", "Código do imóvel não localizado no Pipeimob."

        # If exactly one candidate
        if len(transactions_by_code) == 1:
            return transactions_by_code[0], "unique_match", None

        # Multiple candidates found -> Apply deterministic filtering
        candidates = list(transactions_by_code)
        
        # 1. Filter by data_cadastro / data_criacao
        p_data_cadastro = parse_date(data_cadastro)
        if p_data_cadastro is not None:
            matched = []
            for c in candidates:
                # check data_criacao
                p_criacao = parse_date(c.get("data_criacao"))
                if p_criacao == p_data_cadastro:
                    matched.append(c)
            if len(matched) == 1:
                return matched[0], "unique_match", None
            elif len(matched) > 1:
                candidates = matched

        # 2. Filter by data_assinatura_ccv / data_assinatura_ccv / data_contrato / data_ccv
        p_data_ccv = parse_date(data_assinatura_ccv)
        if p_data_ccv is not None:
            matched = []
            for c in candidates:
                # check data_assinatura_ccv or data_ccv or data_contrato
                p_c_ccv = parse_date(c.get("data_assinatura_ccv") or c.get("data_ccv") or c.get("data_contrato"))
                if p_c_ccv == p_data_ccv:
                    matched.append(c)
            if len(matched) == 1:
                return matched[0], "unique_match", None
            elif len(matched) > 1:
                candidates = matched

        # 3. Filter by nome_imovel / titulo_nome_negocio
        if nome_imovel:
            norm_name = normalize_text(nome_imovel)
            matched = []
            for c in candidates:
                norm_c_name = normalize_text(c.get("titulo_nome_negocio"))
                if norm_name == norm_c_name and norm_name != "":
                    matched.append(c)
            if len(matched) == 1:
                return matched[0], "unique_match", None
            elif len(matched) > 1:
                candidates = matched

        # 4. Filter by gerente / agente_gestor
        if gerente:
            norm_gerente = normalize_text(gerente)
            matched = []
            for c in candidates:
                norm_c_mgr = normalize_text(c.get("agente_gestor"))
                if norm_gerente == norm_c_mgr and norm_gerente != "":
                    matched.append(c)
            if len(matched) == 1:
                return matched[0], "unique_match", None
            elif len(matched) > 1:
                candidates = matched

        # If we still have multiple candidates or filters over-constrained to 0, return ambiguous
        return None, "ambiguous_match", f"Múltiplos negócios ({len(transactions_by_code)}) com o mesmo código e sem resolução determinística."
