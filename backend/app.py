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
    s = re.sub(r'[^\d,\.]', '', s)  # remove P/D and other chars
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


def _tokenize(line: str):
    # Return list of tokens with their start positions
    tokens = []
    for m in re.finditer(r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})|[\d]+(?:[.,]\d+)?|\b[A-Z0-9%\.]+\b', line, flags=re.IGNORECASE):
        tokens.append((m.group(0), m.start()))
    return tokens


def _extract_value_by_code_near_qty(bloco: str, code: str, salary_str: Optional[str] = None, max_tokens_between=6) -> str:
    """
    New conservative extractor:
    - find line windows containing the 3-digit code
    - tokenize the line/window and look for sequence: [code] ... [quantity] ... [money]
      where distance (in tokens) between quantity and money <= max_tokens_between
    - prefer the FIRST money that follows the quantity
    - if not found, fallback: first money after code within max_tokens_between tokens
    - ignore values equal to salary and values after "Resumo por Rubricas"
    """
    if not bloco:
        return ""

    bloco_norm = re.sub(r'(?P<code>\b\d{2,4})(?=[A-ZÁÉÍÓÚÃÕÂÊÔÇ])', r'\g<code> ', bloco)
    summary_header_regex = re.compile(r'(?mi)^\s*(Resumo por Rubricas|Resumo por Rubricas do Centro|Resumo por Rubricas do Centro de Custo|L[ií]quido Geral)\b')
    summary_match = summary_header_regex.search(bloco_norm)
    summary_pos = summary_match.start() if summary_match else None

    lines = re.split(r'\r?\n', bloco_norm)
    line_limit = len(lines)
    if summary_pos is not None:
        cum = 0
        for i, ln in enumerate(lines):
            cum += len(ln) + 1
            if cum >= summary_pos:
                line_limit = i
                break

    money_re = re.compile(r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})')
    qty_re = re.compile(r'^[\d]+(?:[.,]\d+)?$')

    for idx, line in enumerate(lines[:line_limit]):
        if re.search(rf'\b{re.escape(code)}\b', line):
            # build tokens (simple split by whitespace + money tokens)
            # we keep sequence of tokens (words/numbers/money)
            raw_tokens = re.findall(r'\S+', line)
            # map to normalized tokens with types
            norm_tokens = []
            for t in raw_tokens:
                if money_re.match(t):
                    norm_tokens.append(('MONEY', t))
                elif re.match(r'^[\d]+(?:[.,]\d+)?$', t):
                    norm_tokens.append(('QTY', t))
                else:
                    norm_tokens.append(('WORD', t))

            # find code index in raw tokens (first token that contains the code)
            code_idx = None
            for i, t in enumerate(raw_tokens):
                if re.search(rf'\b{re.escape(code)}\b', t):
                    code_idx = i
                    break
            if code_idx is None:
                continue

            # search for QTY after code and then MONEY after qty within max_tokens_between
            for i in range(code_idx + 1, min(len(norm_tokens), code_idx + 20)):
                if norm_tokens[i][0] == 'QTY':
                    # look for money within next max_tokens_between tokens
                    for j in range(i + 1, min(len(norm_tokens), i + 1 + max_tokens_between)):
                        if norm_tokens[j][0] == 'MONEY':
                            val = _normalize_money_str(norm_tokens[j][1])
                            if val:
                                if salary_str:
                                    try:
                                        if float(val) == float(salary_str):
                                            break
                                    except:
                                        pass
                                logger.info("Code %s: qty->money matched on line -> %s", code, val)
                                return val
                    # if qty found but no money close, continue searching further qtys
            # fallback: pick first MONEY after code within max_tokens_between tokens
            for k in range(code_idx + 1, min(len(norm_tokens), code_idx + 1 + max_tokens_between)):
                if norm_tokens[k][0] == 'MONEY':
                    val = _normalize_money_str(norm_tokens[k][1])
                    if val:
                        if salary_str:
                            try:
                                if float(val) == float(salary_str):
                                    break
                            except:
                                pass
                        logger.info("Code %s: picked first money after code on line -> %s", code, val)
                        return val
            # try window (current line + next 2 lines) if not found on same line
            window = ' '.join(lines[idx: idx + 3])
            raw_tokens = re.findall(r'\S+', window)
            norm_tokens = []
            for t in raw_tokens:
                if money_re.match(t):
                    norm_tokens.append(('MONEY', t))
                elif re.match(r'^[\d]+(?:[.,]\d+)?$', t):
                    norm_tokens.append(('QTY', t))
                else:
                    norm_tokens.append(('WORD', t))
            # find first qty after first code occurrence in window
            code_pos = None
            for i, t in enumerate(raw_tokens):
                if re.search(rf'\b{re.escape(code)}\b', t):
                    code_pos = i
                    break
            if code_pos is not None:
                # locate qty after code_pos (in window tokens)
                for i in range(code_pos + 1, min(len(norm_tokens), code_pos + 40)):
                    if norm_tokens[i][0] == 'QTY':
                        for j in range(i + 1, min(len(norm_tokens), i + 1 + max_tokens_between)):
                            if norm_tokens[j][0] == 'MONEY':
                                val = _normalize_money_str(norm_tokens[j][1])
                                if val:
                                    if salary_str:
                                        try:
                                            if float(val) == float(salary_str):
                                                break
                                        except:
                                            pass
                                    logger.info("Code %s: qty->money matched in window -> %s", code, val)
                                    return val
                for k in range(code_pos + 1, min(len(norm_tokens), code_pos + 1 + max_tokens_between)):
                    if norm_tokens[k][0] == 'MONEY':
                        val = _normalize_money_str(norm_tokens[k][1])
                        if val:
                            if salary_str:
                                try:
                                    if float(val) == float(salary_str):
                                        break
                                except:
                                    pass
                            logger.info("Code %s: picked first money after code in window -> %s", code, val)
                            return val
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

                    empresa = limpar_cargo(texto)  # small reuse (ok)
                    competencia = limpar_competencia(texto)
                    funcionarios = re.split(r'Empr\.\:', texto)[1:] if texto else []

                    for func_raw in funcionarios:
                        bloco = "Empr.:" + func_raw

                        nome = limpar_nome(bloco)
                        cargo = limpar_cargo(bloco)
                        admissao = limpar_admissao(bloco)
                        salario = limpar_salario(bloco)

                        # extract by code near qty (VALORES)
                        reflexo_extras = _extract_value_by_code_near_qty(bloco, '250', salary_str=salario)
                        reflexo_adic_not = _extract_value_by_code_near_qty(bloco, '854', salary_str=salario)
                        he_50 = _extract_value_by_code_near_qty(bloco, '150', salary_str=salario)
                        he_100 = _extract_value_by_code_near_qty(bloco, '200', salary_str=salario)
                        he_not_50 = _extract_value_by_code_near_qty(bloco, '218', salary_str=salario)
                        he_not_100 = _extract_value_by_code_near_qty(bloco, '358', salary_str=salario)
                        adic_not = _extract_value_by_code_near_qty(bloco, '327', salary_str=salario)

                        dados.append({
                            'Competência': competencia,
                            'Nome': nome,
                            'Cargo': cargo,
                            'Vínculo': "",
                            'Dias Faltas': "",
                            'Empresa': "",
                            'Situação': "",
                            'Admissão': admissao,
                            'Salário': formato_brasileiro(salario),
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
