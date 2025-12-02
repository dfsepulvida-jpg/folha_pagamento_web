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


def _extract_value_by_code_near_qty(bloco: str, code: str, salary_str: Optional[str] = None, max_tokens_between=6) -> str:
    """
    Conservative extractor with P-preference:
    - prefer money tokens that are followed by 'P' (e.g. '417,40 P' or '417,40P')
    - search for pattern: CODE -> QTY -> MONEY (with P preferred)
    - fallback: first MONEY after CODE within token window (prefer MONEY+P)
    - ignore values after summary header and values equal to salary
    """
    if not bloco:
        return ""

    # Normalize glued numbers and text
    bloco_norm = re.sub(r'(?P<code>\b\d{2,4})(?=[A-ZÁÉÍÓÚÃÕÂÊÔÇ])', r'\g<code> ', bloco)

    # detect summary header and limit search before it
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

    # regexes
    money_re = re.compile(r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})')
    money_with_p_re = re.compile(r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})\s*P\b', flags=re.IGNORECASE)
    qty_re = re.compile(r'^[\d]+(?:[.,]\d+)?$')

    # Scan lines
    for idx, line in enumerate(lines[:line_limit]):
        if re.search(rf'\b{re.escape(code)}\b', line):
            # token sequence (split preserving tokens)
            raw_tokens = re.findall(r'\S+', line)
            # map token types
            norm_tokens = []
            for t in raw_tokens:
                if money_re.match(t):
                    # keep original token for P-check
                    has_p = bool(re.search(r'\d{1,3}(?:\.\d{3})*,\d{2}\s*P\b', t + ' ', flags=re.IGNORECASE))
                    norm_tokens.append(('MONEY', t, has_p))
                elif re.match(r'^[\d]+(?:[.,]\d+)?$', t):
                    norm_tokens.append(('QTY', t, False))
                else:
                    norm_tokens.append(('WORD', t, False))

            # find first token index that contains the code
            code_idx = None
            for i, t in enumerate(raw_tokens):
                if re.search(rf'\b{re.escape(code)}\b', t):
                    code_idx = i
                    break
            if code_idx is None:
                continue

            # 1) prefer QTY -> MONEY where MONEY has trailing P
            for i in range(code_idx + 1, min(len(norm_tokens), code_idx + 20)):
                if norm_tokens[i][0] == 'QTY':
                    # search for MONEY with P within window after qty
                    for j in range(i + 1, min(len(norm_tokens), i + 1 + max_tokens_between)):
                        if norm_tokens[j][0] == 'MONEY' and norm_tokens[j][2]:
                            val = _normalize_money_str(norm_tokens[j][1])
                            if val:
                                if salary_str:
                                    try:
                                        if float(val) == float(salary_str):
                                            break
                                    except:
                                        pass
                                logger.info("Code %s: qty->money+P matched on line -> %s", code, val)
                                return val
                    # if none with P, look for any MONEY after qty (first)
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
            # 2) If no qty found or no money after qty, prefer first MONEY with P after code
            for k in range(code_idx + 1, min(len(norm_tokens), code_idx + 1 + max_tokens_between)):
                if norm_tokens[k][0] == 'MONEY' and norm_tokens[k][2]:
                    val = _normalize_money_str(norm_tokens[k][1])
                    if val:
                        if salary_str:
                            try:
                                if float(val) == float(salary_str):
                                    break
                            except:
                                pass
                        logger.info("Code %s: picked first MONEY+P after code on line -> %s", code, val)
                        return val
            # 3) fallback: first MONEY after code (without P)
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
                        logger.info("Code %s: picked first MONEY after code on line -> %s", code, val)
                        return val

            # 4) try window (line + next 2 lines) with same P-preference
            window = ' '.join(lines[idx: idx + 3])
            raw_tokens = re.findall(r'\S+', window)
            norm_tokens = []
            for t in raw_tokens:
                if money_re.match(t):
                    has_p = bool(re.search(r'\d{1,3}(?:\.\d{3})*,\d{2}\s*P\b', t + ' ', flags=re.IGNORECASE))
                    norm_tokens.append(('MONEY', t, has_p))
                elif re.match(r'^[\d]+(?:[.,]\d+)?$', t):
                    norm_tokens.append(('QTY', t, False))
                else:
                    norm_tokens.append(('WORD', t, False))
            # find first code position in window
            code_pos = None
            for i, t in enumerate(raw_tokens):
                if re.search(rf'\b{re.escape(code)}\b', t):
                    code_pos = i
                    break
            if code_pos is not None:
                # qty->money+P
                for i in range(code_pos + 1, min(len(norm_tokens), code_pos + 40)):
                    if norm_tokens[i][0] == 'QTY':
                        for j in range(i + 1, min(len(norm_tokens), i + 1 + max_tokens_between)):
                            if norm_tokens[j][0] == 'MONEY' and norm_tokens[j][2]:
                                val = _normalize_money_str(norm_tokens[j][1])
                                if val:
                                    if salary_str:
                                        try:
                                            if float(val) == float(salary_str):
                                                break
                                        except:
                                            pass
                                    logger.info("Code %s: qty->money+P matched in window -> %s", code, val)
                                    return val
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
                # first MONEY+P after code in window
                for k in range(code_pos + 1, min(len(norm_tokens), code_pos + 1 + max_tokens_between)):
                    if norm_tokens[k][0] == 'MONEY' and norm_tokens[k][2]:
                        val = _normalize_money_str(norm_tokens[k][1])
                        if val:
                            if salary_str:
                                try:
                                    if float(val) == float(salary_str):
                                        break
                                except:
                                    pass
                            logger.info("Code %s: picked MONEY+P after code in window -> %s", code, val)
                            return val
                # fallback: first money after code in window
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

                    empresa = limpar_empresa(texto) if 'limpar_empresa' in globals() else ""
                    competencia = limpar_competencia(texto)
                    funcionarios = re.split(r'Empr\.\:', texto)[1:] if texto else []

                    for func_raw in funcionarios:
                        bloco = "Empr.:" + func_raw

                        nome = limpar_nome(bloco)
                        cargo = limpar_cargo(bloco)
                        admissao = limpar_admissao(bloco)
                        salario = limpar_salario(bloco)

                        # extract values by code, preferring MONEY+P tokens
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
