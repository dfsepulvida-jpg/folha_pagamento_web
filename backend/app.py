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
    s = re.sub(r'[^\d,\.]', '', s)
    s = s.replace('.', '').replace(',', '.')
    try:
        v = float(s)
        return f"{v:.2f}"
    except Exception:
        return None


def formato_brasileiro(valor: Optional[str]) -> str:
    try:
        if valor is None or valor == "":
            return ""
        f = float(valor)
        return f"{f:,.2f}".replace(",", "v").replace(".", ",").replace("v", ".")
    except:
        return valor or ""


# ---------- campo básicos/metadata ----------
def limpar_empresa(texto: str) -> str:
    if not texto:
        return ""
    m = re.search(r'Empresa:\s*([^\n\r]+)', texto)
    if m:
        emp = m.group(1).strip()
        emp = re.sub(r'^\d{4}\s-\s', '', emp)
        emp = re.sub(r'Página.*', '', emp, flags=re.IGNORECASE).strip()
        return ' '.join(emp.split())
    return ""


def limpar_competencia(texto: str) -> str:
    m = re.search(r'Competência:\s*(\d{2}/\d{4})', texto)
    if m:
        mes, ano = m.group(1).split('/')
        return f"01/{mes}/{ano}"
    return ""


def limpar_salario(bloco: str) -> str:
    patterns = [r'(?:Sal[aáàâãä]rio|SALARIO|SALÁRIO)\s*[:\-]?\s*([\d\.,]+)', r'\bSal\b[:\s]*([\d\.,]+)']
    for p in patterns:
        m = re.search(p, bloco, flags=re.IGNORECASE)
        if m:
            return m.group(1).replace('.', '').replace(',', '.')
    m2 = re.search(r'8781\s*DIAS\s*NORMAIS.*?([\d\.,]+)', bloco, flags=re.IGNORECASE | re.DOTALL)
    if m2:
        return m2.group(1).replace('.', '').replace(',', '.')
    return ""


def limpar_nome(bloco: str) -> str:
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


def limpar_cargo(bloco: str) -> str:
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


def limpar_admissao(bloco: str) -> str:
    m = re.search(r'Adm\:\s*([0-9/]+)', bloco)
    return m.group(1) if m else ""


def limpar_situacao_raw(bloco: str) -> str:
    m = re.search(r'Situa(?:ç|c)[aã]o\:?[\s]*([^\n\r]+)', bloco, flags=re.IGNORECASE)
    if m:
        s = m.group(1).strip().split('CPF')[0].strip()
        return ' '.join(s.split())
    return ""


def normalize_situacao(situacao_raw: str, admissao_str: str, competencia_str: str) -> str:
    admissao_date = _parse_date_ddmmyyyy(admissao_str) if admissao_str else None
    competencia_date = _parse_date_ddmmyyyy(competencia_str) if competencia_str else None
    if admissao_date and competencia_date:
        if admissao_date.year == competencia_date.year and admissao_date.month == competencia_date.month:
            return "Admissão"
    s = (situacao_raw or "").strip().lower()
    if "demit" in s or "deslig" in s or "rescind" in s:
        return "Demissão"
    if "trabalh" in s or "ativo" in s or "empreg" in s or "contrat" in s:
        return "Ativo"
    return (situacao_raw or "").strip().title()


def limpar_vinculo(bloco: str) -> str:
    m = re.search(r'V[ií]nculo:\s*([^\n\r]+)', bloco, re.IGNORECASE)
    if not m:
        return ""
    raw = m.group(1).strip()
    raw = re.sub(r'\s+', ' ', raw)
    first = re.match(r'([A-Za-zÇçÁáÉéÍíÓóÚúÃãÕõÂâÊêÔô\-]+)', raw)
    if first and first.group(1).lower() == 'celetista':
        return 'CLT'
    return first.group(1) if first else raw


def extrair_campo_quantidade_flex(bloco: str, codigo: str, texto: str) -> str:
    pattern = rf"{re.escape(str(codigo))}\s*{texto}.*?([\d]+[.,]\d+|[\d]+)"
    m = re.search(pattern, bloco, re.IGNORECASE | re.DOTALL)
    return m.group(1).replace(',', '.') if m else ""


# ---------- extractor P-based (estrito) ----------
def _extract_value_by_code_using_P(bloco: str, code: str, salary_str: Optional[str] = None) -> str:
    if not bloco:
        return ""
    if not re.search(rf'\b{re.escape(code)}\b', bloco):
        logger.debug("Code %s not present in block -> empty", code)
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
    money_p_re = re.compile(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})\s*P\b', flags=re.IGNORECASE)
    money_re = re.compile(r'(\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})')
    qty_re = re.compile(r'^[\d]+(?:[.,]\d+)?$')
    for idx, line in enumerate(lines[:line_limit]):
        if re.search(rf'\b{re.escape(code)}\b', line):
            code_pos = re.search(rf'\b{re.escape(code)}\b', line).end()
            for m in money_p_re.finditer(line):
                if m.start() >= code_pos:
                    val_raw = m.group(0)
                    norm = _normalize_money_str(val_raw)
                    if norm and salary_str:
                        try:
                            if float(norm) == float(salary_str):
                                continue
                        except:
                            pass
                    if norm:
                        logger.info("Code %s: matched money+P on same line -> %s", code, norm)
                        return norm
            window = ' '.join(lines[idx: idx + 3])
            code_pos_w_match = re.search(rf'\b{re.escape(code)}\b', window)
            code_pos_w = code_pos_w_match.end() if code_pos_w_match else 0
            for m in money_p_re.finditer(window):
                if m.start() >= code_pos_w:
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
            # fallback: qty -> money on same line
            raw_tokens = re.findall(r'\S+', line)
            norm_tokens = []
            for t in raw_tokens:
                if money_re.match(t):
                    norm_tokens.append(('MONEY', t))
                elif re.match(r'^[\d]+(?:[.,]\d+)?$', t):
                    norm_tokens.append(('QTY', t))
                else:
                    norm_tokens.append(('WORD', t))
            code_token_idx = None
            for i, t in enumerate(raw_tokens):
                if re.search(rf'\b{re.escape(code)}\b', t):
                    code_token_idx = i
                    break
            if code_token_idx is not None:
                for i in range(code_token_idx + 1, min(len(norm_tokens), code_token_idx + 20)):
                    if norm_tokens[i][0] == 'QTY':
                        for j in range(i + 1, min(len(norm_tokens), i + 1 + 6)):
                            if norm_tokens[j][0] == 'MONEY':
                                val_raw = norm_tokens[j][1]
                                norm = _normalize_money_str(val_raw)
                                if norm and salary_str:
                                    try:
                                        if float(norm) == float(salary_str):
                                            break
                                    except:
                                        pass
                                if norm:
                                    logger.info("Code %s: fallback qty->money on line -> %s", code, norm)
                                    return norm
            # fallback on window tokens
            window_tokens = re.findall(r'\S+', window)
            norm_tokens = []
            for t in window_tokens:
                if money_re.match(t):
                    norm_tokens.append(('MONEY', t))
                elif re.match(r'^[\d]+(?:[.,]\d+)?$', t):
                    norm_tokens.append(('QTY', t))
                else:
                    norm_tokens.append(('WORD', t))
            code_idx_w = None
            for i, t in enumerate(window_tokens):
                if re.search(rf'\b{re.escape(code)}\b', t):
                    code_idx_w = i
                    break
            if code_idx_w is not None:
                for i in range(code_idx_w + 1, min(len(norm_tokens), code_idx_w + 40)):
                    if norm_tokens[i][0] == 'QTY':
                        for j in range(i + 1, min(len(norm_tokens), i + 1 + 6)):
                            if norm_tokens[j][0] == 'MONEY':
                                val_raw = norm_tokens[j][1]
                                norm = _normalize_money_str(val_raw)
                                if norm and salary_str:
                                    try:
                                        if float(norm) == float(salary_str):
                                            break
                                    except:
                                        pass
                                if norm:
                                    logger.info("Code %s: fallback qty->money in window -> %s", code, norm)
                                    return norm
            logger.debug("Code %s present but no reliable value found -> empty", code)
            return ""
    return ""


def extrair_funcionarios(pdf_path: str):
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
                        situacao_raw = limpar_situacao_raw(bloco)
                        situacao = normalize_situacao(situacao_raw, admissao, competencia)
                        vinculo = limpar_vinculo(bloco)
                        salario = limpar_salario(bloco)
                        dias_faltas = extrair_campo_quantidade_flex(bloco, '8792', r'DIAS\s*FALTAS')
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
                            'Vínculo': vinculo,
                            'Dias Faltas': dias_faltas,
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
                            'Reflexo Adic Noturno DSR': formato_brasileiro(reflexo_adic_not),
                            'Reflexo Extras DSR': formato_brasileiro(reflexo_extras),
                            'raw_block': bloco
                        })
                except Exception as e_page:
                    logger.exception("Erro ao processar página %s: %s", page_num, e_page)
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
