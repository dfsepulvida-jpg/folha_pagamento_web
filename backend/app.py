# (substitua o app.py do seu projeto por este conteúdo)
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


def _normalize_money_str(s: str) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    # remove trailing P/D or non-numeric suffixes
    s = re.sub(r'[^\d,\.]', '', s)
    s = s.replace('.', '').replace(',', '.')
    try:
        v = float(s)
        return f"{v:.2f}"
    except Exception:
        return None


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
    if not bloco:
        return ""
    pattern = r'Cargo[:\s]*([^\n\r]+?)(?=(?:C\.?B\.?O\.?|CBO\b|Filial:|Filial\b|Sal[aáàâãä]rio|Salßrio|\n))'
    m = re.search(pattern, bloco, flags=re.IGNORECASE | re.DOTALL)
    if m:
        cargo_raw = m.group(1).strip()
    else:
        fb = re.search(r'Cargo[:\s]*(.*?)\s{2,}', bloco, re.IGNORECASE | re.DOTALL)
        cargo_raw = fb.group(1).strip() if fb else ""
    return re.sub(r'\s+', ' ', cargo_raw).strip().upper()


def limpar_empresa(texto):
    if not texto:
        return ""
    m = re.search(r'Empresa:\s*([^\n\r]+)', texto)
    if m:
        emp = m.group(1).strip()
        emp = re.sub(r'^\d{4}\s-\s', '', emp)
        emp = re.sub(r'Página.*', '', emp, flags=re.IGNORECASE).strip()
        return ' '.join(emp.split())
    return ""


def limpar_admissao(bloco):
    m = re.search(r'Adm\:\s*([0-9/]+)', bloco)
    return m.group(1) if m else ""


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


def formato_brasileiro(valor):
    try:
        if valor is None or valor == "":
            return ""
        f = float(valor)
        return f"{f:,.2f}".replace(",", "v").replace(".", ",").replace("v", ".")
    except:
        return valor


def _extract_value_strict_by_code(bloco: str, code: str, salary_str: Optional[str] = None) -> str:
    """
    Strict extraction by 3-digit code:
    1) limit search to text before 'Resumo por Rubricas' / 'Líquido Geral'
    2) find line (or window of 3 lines) containing the code
    3) prefer pattern: code ... QUANTITY ... VALUE -> return the VALUE after QUANTITY (first such)
    4) if not found, return the FIRST monetary token AFTER the code in the same line/window
    5) ignore monetary tokens equal to salary
    """
    if not bloco:
        return ""

    # normalize cases where numbers are glued to text
    bloco_norm = re.sub(r'(?P<code>\b\d{2,4})(?=[A-ZÁÉÍÓÚÃÕÂÊÔÇ])', r'\g<code> ', bloco)

    # find position of summary header to avoid aggregated values
    summary_header_regex = re.compile(r'(?mi)^\s*(Resumo por Rubricas|Resumo por Rubricas do Centro|Resumo por Rubricas do Centro de Custo|L[ií]quido Geral)\b')
    summary_match = summary_header_regex.search(bloco_norm)
    summary_pos = summary_match.start() if summary_match else None

    lines = re.split(r'\r?\n', bloco_norm)
    # limit to lines before summary
    line_limit = len(lines)
    if summary_pos is not None:
        cum = 0
        for i, ln in enumerate(lines):
            cum += len(ln) + 1
            if cum >= summary_pos:
                line_limit = i
                break

    money_re = r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})(?:\s*[PD])?'
    qty_re = r'[\d]+(?:[.,]\d+)?'

    # Scan lines for the code, prefer pattern code + qty + money (value after qty)
    for idx, line in enumerate(lines[:line_limit]):
        if re.search(rf'\b{re.escape(code)}\b', line):
            # 1) same-line qty+value pattern
            patt = re.compile(rf'\b{re.escape(code)}\b[^\d\n\r]{{0,80}}(?:[A-Z0-9\%\.\,\-\s]{{0,80}})?({qty_re})\D+({money_re})', flags=re.IGNORECASE)
            m = patt.search(line)
            if m:
                val = m.group(2)
                norm = _normalize_money_str(val)
                if norm and salary_str:
                    try:
                        if float(norm) == float(salary_str):
                            pass
                        else:
                            logger.info("Code %s: matched qty+val SAME-LINE -> %s", code, norm)
                            return norm
                    except Exception:
                        return norm
                elif norm:
                    logger.info("Code %s: matched qty+val SAME-LINE -> %s", code, norm)
                    return norm

            # 2) try window (line + next 2) for qty+value
            window = ' '.join(lines[idx: idx + 3])
            mwin = patt.search(window)
            if mwin:
                val = mwin.group(2)
                norm = _normalize_money_str(val)
                if norm and salary_str:
                    try:
                        if float(norm) == float(salary_str):
                            pass
                        else:
                            logger.info("Code %s: matched qty+val WINDOW -> %s", code, norm)
                            return norm
                    except Exception:
                        return norm
                elif norm:
                    logger.info("Code %s: matched qty+val WINDOW -> %s", code, norm)
                    return norm

            # 3) fallback: first money token AFTER the code on the same line
            # find the position of the code and pick first money token whose start > code_pos
            code_m = re.search(rf'\b{re.escape(code)}\b', line)
            code_pos = code_m.end() if code_m else 0
            money_iter = list(re.finditer(money_re, line))
            for mm in money_iter:
                if mm.start() > code_pos:
                    norm = _normalize_money_str(mm.group(0))
                    if norm:
                        if salary_str:
                            try:
                                if float(norm) == float(salary_str):
                                    continue
                            except:
                                pass
                        logger.info("Code %s: picked first money after code on same line -> %s", code, norm)
                        return norm
            # 4) fallback: first money token AFTER the code in window
            money_iter = list(re.finditer(money_re, window))
            # find code position in window (relative)
            code_mw = re.search(rf'\b{re.escape(code)}\b', window)
            code_pos_w = code_mw.end() if code_mw else 0
            for mm in money_iter:
                if mm.start() > code_pos_w:
                    norm = _normalize_money_str(mm.group(0))
                    if norm:
                        if salary_str:
                            try:
                                if float(norm) == float(salary_str):
                                    continue
                            except:
                                pass
                        logger.info("Code %s: picked first money after code in window -> %s", code, norm)
                        return norm
            # 5) last resort on this line: last money token (but normally not needed)
            money_matches = re.findall(money_re, line)
            if money_matches:
                norm = _normalize_money_str(money_matches[-1])
                if norm:
                    if salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    logger.info("Code %s: fallback last money on line -> %s", code, norm)
                    return norm

    # global fallback: last money after any occurrence of code but before summary
    code_positions = [m.start() for m in re.finditer(rf'\b{re.escape(code)}\b', bloco_norm)]
    if code_positions:
        money_all = list(re.finditer(money_re, bloco_norm))
        for mm in reversed(money_all):
            if summary_pos is not None and mm.start() >= summary_pos:
                continue
            if any(cp < mm.start() for cp in code_positions):
                norm = _normalize_money_str(mm.group(0))
                if norm:
                    if salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    logger.info("Code %s: global fallback -> %s", code, norm)
                    return norm

    return ""


def extrair_funcionarios(pdf_path):
    dados = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                try:
                    texto = page.extract_text()
                    if not texto:
                        logger.debug("Página %s sem texto detectada, pulando", page_num)
                        continue

                    empresa = limpar_empresa(texto)
                    competencia = limpar_competencia(texto)
                    funcionarios = re.split(r'Empr\.\:', texto)[1:] if texto else []

                    for func_raw in funcionarios:
                        bloco = "Empr.:" + func_raw

                        nome = limpar_nome(bloco)
                        cargo = limpar_cargo(bloco)
                        admissao = limpar_admissao(bloco)
                        situacao = ""  # se necessário, pode reusar normalize_situacao
                        vinculo = ""
                        salario = limpar_salario(bloco)

                        # Extração estrita por códigos (pega VALOR, não quantidade)
                        reflexo_extras_dsr = _extract_value_strict_by_code(bloco, '250', salary_str=salario)
                        reflexo_adic_not_dsr = _extract_value_strict_by_code(bloco, '854', salary_str=salario)
                        he_50 = _extract_value_strict_by_code(bloco, '150', salary_str=salario)
                        he_100 = _extract_value_strict_by_code(bloco, '200', salary_str=salario)
                        he_not_50 = _extract_value_strict_by_code(bloco, '218', salary_str=salario)
                        he_not_100 = _extract_value_strict_by_code(bloco, '358', salary_str=salario)
                        adic_not = _extract_value_strict_by_code(bloco, '327', salary_str=salario)

                        dados.append({
                            'Competência': competencia,
                            'Nome': nome,
                            'Cargo': cargo,
                            'Vínculo': vinculo,
                            'Dias Faltas': "",
                            'Faltas Justificada': "",
                            'Faltas sem Justificativa': "",
                            'Justif. 01': "",
                            'Justif. 02': "",
                            'Justif. 03': "",
                            'Observações falta': "",
                            'Empresa': empresa,
                            'Situação': situacao,
                            'Justificativa preencher em adm e dem': "",
                            'Admissão': admissao,
                            'Salário': formato_brasileiro(salario),
                            'Adicional Noturno 20%': formato_brasileiro(adic_not),
                            'HE Noturna 50% + Adic 20%': formato_brasileiro(he_not_50),
                            'HE Noturna 100% + Adic 20%': formato_brasileiro(he_not_100),
                            'Horas Extras 50%': formato_brasileiro(he_50),
                            'Horas Extras 100%': formato_brasileiro(he_100),
                            'Reflexo Adic Noturno DSR': formato_brasileiro(reflexo_adic_not_dsr),
                            'Reflexo Extras DSR': formato_brasileiro(reflexo_extras_dsr),
                            'raw_block': bloco
                        })

                except Exception as e:
                    logger.exception("Erro na página %s: %s", page_num, e)
    except Exception as e:
        logger.exception("Erro ao abrir/processar PDF %s: %s", pdf_path, e)
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
        logger.info("Arquivo salvo temporariamente em %s", tmp_file)
        dados = extrair_funcionarios(tmp_file)
        return jsonify(dados)
    except Exception as e:
        logger.exception("Erro no endpoint /upload: %s", e)
        return jsonify({"error": "Erro ao processar o arquivo", "detail": str(e)}), 500
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
                logger.info("Arquivo temporário removido: %s", tmp_file)
            except Exception:
                logger.debug("Falha ao remover arquivo temporário: %s", tmp_file)


@app.route("/")
def home():
    return "Backend online!"

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
