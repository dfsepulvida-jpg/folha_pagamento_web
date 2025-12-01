from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import re
import os
import tempfile
import logging
from werkzeug.utils import secure_filename
from datetime import datetime
from typing import Optional

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_date_ddmmyyyy(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def limpar_nome(bloco):
    nome_match = re.search(r'Empr\.\:\s*\d*\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ\s\.\-]+?)\s*Situa', bloco, re.MULTILINE | re.IGNORECASE)
    if nome_match:
        return ' '.join(nome_match.group(1).strip().split())
    nome_match2 = re.search(r'Empr\.\:\s*(.*?)\s*Situação\:', bloco, re.DOTALL | re.IGNORECASE)
    if nome_match2:
        return ' '.join(nome_match2.group(1).strip().split())
    # fallback: try after Empr.:
    m = re.search(r'Empr\.\:\s*(\d+)?\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ][A-Z0-9ÇÁÉÍÓÚÃÕÂÊÔ\s\.\-]+)', bloco, re.IGNORECASE)
    if m:
        return ' '.join(m.group(2).strip().split())
    return ""


def limpar_cargo(bloco):
    if not bloco:
        return ""
    # try to find Cargo: ... up to C.B.O or Filial or Salário
    pattern = r'Cargo[:\s]*([^\n\r]+?)(?=(?:C\.?B\.?O\.?|CBO\b|Filial:|Filial\b|Sal[aáàâãä]rio|Salßrio|\n))'
    m = re.search(pattern, bloco, flags=re.IGNORECASE | re.DOTALL)
    if m:
        cargo_raw = m.group(1).strip()
    else:
        # looser fallback
        fb = re.search(r'Cargo[:\s]*(.*?)\s{2,}', bloco, re.IGNORECASE | re.DOTALL)
        cargo_raw = fb.group(1).strip() if fb else ""
    cargo_clean = re.sub(r'\s+', ' ', cargo_raw).strip()
    return cargo_clean.upper() if cargo_clean else ""


def limpar_empresa(texto):
    if not texto:
        return ""
    emp_match = re.search(r'Empresa:\s*([^\n\r]+)', texto)
    if emp_match:
        emp = emp_match.group(1).strip()
        emp = re.sub(r'^\d{4}\s-\s', '', emp)
        emp = re.sub(r'Página.*', '', emp, flags=re.IGNORECASE).strip()
        return ' '.join(emp.split())
    return ""


def limpar_situacao_raw(bloco):
    situacao_match = re.search(r'Situa(?:ç|c)[aã]o\:?[\s]*([^\n\r]+)', bloco, flags=re.IGNORECASE)
    if situacao_match:
        situacao = situacao_match.group(1).strip()
        situacao = situacao.split('CPF')[0].strip()
        return ' '.join(situacao.split())
    # fallback
    m = re.search(r'Situa.*?:\s*([A-Za-z ]+)', bloco, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def normalize_situacao(situacao_raw: str, admissao_str: str, competencia_str: str) -> str:
    admissao_date = _parse_date_ddmmyyyy(admissao_str)
    competencia_date = _parse_date_ddmmyyyy(competencia_str)  # '01/MM/YYYY'
    if admissao_date and competencia_date:
        if admissao_date.year == competencia_date.year and admissao_date.month == competencia_date.month:
            return "Admissão"

    s = (situacao_raw or "").strip()
    s_lower = s.lower()

    if "demit" in s_lower or "deslig" in s_lower or "rescind" in s_lower:
        return "Demissão"

    if "trabalh" in s_lower or "ativo" in s_lower or "empreg" in s_lower or "contrat" in s_lower:
        return "Ativo"

    return s.title() if s else ""


def limpar_vinculo(bloco):
    vinculo_match = re.search(r'V[ií]nculo:\s*([^\n\r]+)', bloco, re.IGNORECASE)
    if not vinculo_match:
        return ""
    vinculo_raw = vinculo_match.group(1).strip()
    vinculo_raw = re.sub(r'\s+', ' ', vinculo_raw)
    first_token_match = re.match(r'([A-Za-zÇçÁáÉéÍíÓóÚúÃãÕõÂâÊêÔô\-]+)', vinculo_raw)
    if not first_token_match:
        return vinculo_raw
    token = first_token_match.group(1)
    if token.lower() == 'celetista':
        return 'CLT'
    return token


def limpar_salario(bloco):
    # accept multiple possible corruptions of "Salário"
    sal_patterns = [
        r'(?:Sal[aáàâãä]rio|Salßrio|Salario|SALARIO|SALÁRIO)\s*[:\-]?\s*([\d\.,]+)',
        r'\bSal\b[:\s]*([\d\.,]+)'
    ]
    for p in sal_patterns:
        m = re.search(p, bloco, flags=re.IGNORECASE)
        if m:
            return m.group(1).replace('.', '').replace(',', '.')
    # fallback: search first monetary value after the header lines where salary usually appears
    m2 = re.search(r'8781\s*DIAS\s*NORMAIS.*?([\d\.,]+)', bloco
