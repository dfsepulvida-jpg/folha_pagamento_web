from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import re
import os
import tempfile
import logging
from werkzeug.utils import secure_filename
from typing import Optional

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _normalize_money_str(s: str) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    s = re.sub(r'[^\d,\.]', '', s)
    s = s.replace('.', '').replace(',', '.')
    try:
        v = float(s)
        return f"{v:.2f}"
    except Exception:
        return None


def limpar_competencia(texto):
    m = re.search(r'Competência:\s*(\d{2}/\d{4})', texto)
    if m:
        mes, ano = m.group(1).split('/')
        return f"01/{mes}/{ano}"
    return ""


def limpar_salario(bloco):
    patterns = [r'(?:Sal[aáàâãä]rio|SALARIO|SALÁRIO)\s*[:\-]?\s*([\d\.,]+)', r'\bSal\b[:\s]*([\d\.,]+)']
    for p in patterns:
        m = re.search(p, bloco, flags=re.IGNORECASE)
        if m:
            return m.group(1).replace('.', '').replace(',', '.')
    m2 = re.search(r'8781\s*DIAS\s*NORMAIS.*?([\d\.,]+)', bloco, flags=re.IGNORECASE | re.DOTALL)
    if m2:
        return m2.group(1).replace('.', '').replace(',', '.')
    return ""


def limpar_nome(bloco):
    m = re.search(r'Empr\.\:\s*\d*\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ0-9\s\.\-]+?)\s*Situa', bloco, re.MULTILINE | re.IGNORECASE)
    if m:
        return ' '.join(m.group(1).strip().split())
    m2 = re.search(r'Empr\.\:\s*(.*?)\s*Situação\:', bloco, re.DOTALL | re.IGNORECASE)
    if m2:
        return ' '.join(m2.group(1).strip().split())
    m3 = re.search(r'Empr\.\:\s*(\d+)?\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ][A-Z0-9ÇÁÉÍÓÚÃÕÂÊÔ\s\.\-]+)', bloco, re.IGNORECASE)
    if m3:
        return ' '.join(m3.group(2).strip().split())
    return ""


def limpar_cargo(bloco):
    pattern = r'Cargo[:\s]*([^\n\r]+?)(?=(?:C\.?B\.?O\.?|CBO\b|Filial:|Filial\b|Sal[aáàâãä]rio|Salßrio|\n))'
    m = re.search(pattern, bloco, flags=re.IGNORECASE | re.DOTALL)
    if m:
        cargo_raw = m.group(1).strip()
        return re.sub(r'\s+', ' ', cargo_raw).strip().upper()
    return ""


def limpar_admissao(bloco):
    m = re.search(r'Adm\:\s*([0-9/]+)', bloco)
    return m.group(1) if m else ""


def _find_money_before_P_on_line(line: str):
    """
    Return first money token that is immediately followed by 'P' (allowing optional spaces),
    searching left-to-right. Money token format: 1.234,56 or 1234,56 or 1.234,56P or 1234,56P
    """
    # match money optionally followed by spaces and P
    for m in re.finditer(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})\s*P\b', line, flags=re.IGNORECASE):
        return m.group(1) + ',' + line[m.end() - 2:m.end()].strip().replace('P', '')
    return None


def _extract_value_by_code_using_P(bloco: str, code: str, salary_str: Optional[str] = None) -> str:
    """
    Strict and simple: for each line (and fallback on a 3-line window) that contains the code,
    return the first monetary value which is followed by 'P' on that same line/window that occurs
    after the code. This follows the rule you specified: the value needed appears before 'P'.
    If not found, fallback to searching for the first monetary value after the code (without P).
    """
    if not bloco:
        return ""

    # normalize glue like "150HORAS" -> "150 HORAS"
    bloco_norm = re.sub(r'(?P<code>\b\d{2,4})(?=[A-ZÁÉÍÓÚÃÕÂÊÔÇ])', r'\g<code> ', bloco)

    # find where summary starts and limit search before it
    summary_header_regex = re.compile(r'(?mi)^\s*(Resumo por Rubricas|Resumo por Rubricas do Centro|Resumo por Rubricas do Centro de Custo|L[ií]quido Geral)\b')
    summary_match = summary_header_regex.search(bloco_norm)
    summary_pos = summary_match.start() if summary_match else None

    lines = re.split(r'\r?\n', bloco_norm)
    # determine line limit
    line_limit = len(lines)
    if summary_pos is not None:
        cum = 0
        for i, ln in enumerate(lines):
            cum += len(ln) + 1
            if cum >= summary_pos:
                line_limit = i
                break

    money_re = re.compile(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})')
    # iterate lines before summary
    for idx, line in enumerate(lines[:line_limit]):
        if re.search(rf'\b{re.escape(code)}\b', line):
            # 1) prefer money followed by P on same line after code
            code_pos = re.search(rf'\b{re.escape(code)}\b', line)
            code_end = code_pos.end() if code_pos else 0
            # search for money+P occurrences and pick the first one whose start is after code_end
            for m in re.finditer(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})\s*P\b', line, flags=re.IGNORECASE):
                if m.start() >= code_end:
                    val_raw = m.group(0)
                    norm = _normalize_money_str(val_raw)
                    if norm and salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    if norm:
                        logger.info("Code %s: matched money+P on line -> %s", code, norm)
                        return norm
            # 2) search money+P in the short window (line + next 2 lines)
            window = ' '.join(lines[idx: idx + 3])
            code_pos_w = re.search(rf'\b{re.escape(code)}\b', window)
            code_end_w = code_pos_w.end() if code_pos_w else 0
            for m in re.finditer(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})\s*P\b', window, flags=re.IGNORECASE):
                if m.start() >= code_end_w:
                    val_raw = m.group(0)
                    norm = _normalize_money_str(val_raw)
                    if norm and salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    if norm:
                        logger.info("Code %s: matched money+P in window -> %s", code, norm)
                        return norm
            # 3) fallback: first money after code on same line (no P)
            for m in re.finditer(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})', line):
                if m.start() >= code_end:
                    val_raw = m.group(0)
                    norm = _normalize_money_str(val_raw)
                    if norm and salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    if norm:
                        logger.info("Code %s: fallback money on line -> %s", code, norm)
                        return norm
            # 4) fallback window money (no P)
            for m in re.finditer(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})', window):
                if m.start() >= code_end_w:
                    val_raw = m.group(0)
                    norm = _normalize_money_str(val_raw)
                    if norm and salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    if norm:
                        logger.info("Code %s: fallback money in window -> %s", code, norm)
                        return norm
    return ""


def extrair_funcionarios(pdf_path):
    dados = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                texto = page.extract_text()
                if not texto:
                    continue
                competencia = limpar_competencia(texto)
                funcionarios = re.split(r'Empr\.\:', texto)[1:] if texto else []
                for func_raw in funcionarios:
                    bloco = "Empr.:" + func_raw
                    nome = limpar_nome(bloco)
                    cargo = limpar_cargo(bloco)
                    admissao = limpar_admissao(bloco)
                    salario = limpar_salario(bloco)

                    # map rubrica codes -> fields, and use strict P-based extractor
                    reflexo_extras = _extract_value_by_code_using_P(bloco, '250', salary_str=salario)
                    reflexo_adic_not = _extract_value_by_code_using_P(bloco, '854', salary_str=salario)
                    he_50 = _extract_value_by_code_using_P(bloco, '150', salary_str=salario)
                    he_100 = _extract_value_by_code_using_P(bloco, '200', salary_str=salario)
                    he_not_50 = _extract_value_by_code_using_P(bloco, '218', salary_str=salario)
                    he_not_100 = _extract_value_by_code_using_P(bloco, '358', salary_str=salario)
                    adic_not = _extract_value_by_code_using_P(bloco, '327', salary_str=salario)

                    dados.append({
                        'Competência': competencia,
                        'Nome': nome,
                        'Cargo': cargo,
                        'Admissão': admissao,
                        'Salário': _normalize_money_str(salario) if salario else "",
                        'Adicional Noturno 20%': formato_brasileiro(adic_not),
                        'HE Noturna 50% + Adic 20%': formato_brasileiro(he_not_50),
                        'HE Noturna 100% + Adic 20%': formato_brasileiro(he_not_100),
                        'Horas Extras 50%': formato_brasileiro(he_50),
                        'Horas Extras 100%': formato_brasileiro(he_100),
                        'Reflexo Adic Noturno DSR': formato_brasileiro(reflexo_adic_not),
                        'Reflexo Extras DSR': formato_brasileiro(reflexo_extras),
                        'raw_block': bloco
                    })
    except Exception as e:
        logger.exception("Erro ao processar PDF: %s", e)
    return dados


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado com a chave 'file'."}), 400
    file = request.files['file']
    filename = secure_filename(file.filename or "upload.pdf")
    if not filename.lower().endswith('.pdf'):
        return jsonify({"error": "Apenas arquivos PDF são aceitos."}), 400
    tmp_file = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as f:
            tmp_file = f.name
            file.save(tmp_file)
        dados = extrair_funcionarios(tmp_file)
        return jsonify(dados)
    except Exception as e:
        logger.exception("Erro no endpoint /upload: %s", e)
        return jsonify({"error": "Erro ao processar o arquivo", "detail": str(e)}), 500
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass


@app.route("/")
def home():
    return "Backend online!"


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
