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
    m = re.search(r'Empr\.\:\s*(\d+)?\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ][A-Z0-9ÇÁÉÍÓÚÃÕÂÊÔ\s\.\-]+)', bloco, re.IGNORECASE)
    if m:
        return ' '.join(m.group(2).strip().split())
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
    m = re.search(r'Situa.*?:\s*([A-Za-z ]+)', bloco, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def normalize_situacao(situacao_raw: str, admissao_str: str, competencia_str: str) -> str:
    admissao_date = _parse_date_ddmmyyyy(admissao_str)
    competencia_date = _parse_date_ddmmyyyy(competencia_str)
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
    sal_patterns = [
        r'(?:Sal[aáàâãä]rio|Salßrio|Salario|SALARIO|SALÁRIO)\s*[:\-]?\s*([\d\.,]+)',
        r'\bSal\b[:\s]*([\d\.,]+)'
    ]
    for p in sal_patterns:
        m = re.search(p, bloco, flags=re.IGNORECASE)
        if m:
            return m.group(1).replace('.', '').replace(',', '.')
    m2 = re.search(r'8781\s*DIAS\s*NORMAIS.*?([\d\.,]+)', bloco, flags=re.IGNORECASE | re.DOTALL)
    if m2:
        return m2.group(1).replace('.', '').replace(',', '.')
    return ""


def limpar_admissao(bloco):
    adm_match = re.search(r'Adm\:\s*([0-9/]+)', bloco)
    return adm_match.group(1) if adm_match else ""


def limpar_competencia(texto):
    comp_match = re.search(r'Competência:\s*(\d{2}/\d{4})', texto)
    if comp_match:
        comp = comp_match.group(1)
        mes, ano = comp.split('/')
        return f"01/{mes}/{ano}"
    return ""


def extrair_campo_quantidade_flex(bloco, codigo, texto):
    pattern = rf"{re.escape(str(codigo))}\s*{texto}.*?([\d]+[.,]\d+|[\d]+)"
    match = re.search(pattern, bloco, re.IGNORECASE | re.DOTALL)
    return match.group(1).replace(',', '.') if match else ""


def _normalize_money_str(s: str) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    s = s.replace('.', '').replace(',', '.')
    try:
        v = float(s)
        return f"{v:.2f}"
    except Exception:
        return None


def _extract_value_from_line_block_improved(bloco: str, code: str, desc_pattern: str, salary_str: Optional[str] = None, require_both: bool = False) -> str:
    """
    Improved extractor with:
    - guard against summary section (don't read below 'Resumo...' / 'Líquido Geral'),
    - prefer same-line code+qty+val matches before summary,
    - when scanning windows, select monetary value that appears AFTER the code or AFTER the description match (prefer the nearest following money),
      instead of picking an arbitrary value from the whole window.
    - require_both: when True, only accept matches where both code and description appear in the same window/line (used for Noturna).
    """
    if not bloco:
        return ""

    # Insert missing spaces like "150HORAS" -> "150 HORAS" (helps regex)
    bloco_norm = re.sub(r'(?P<code>\b\d{2,4})(?=[A-ZÁÉÍÓÚÃÕÂÊÔÇ])', r'\g<code> ', bloco)

    # Find summary header position (if any) to avoid reading aggregated tables below it
    summary_header_regex = re.compile(r'(?mi)^\s*(Resumo por Rubricas|Resumo por Rubricas do Centro|Resumo por Rubricas do Centro de Custo|L[ií]quido Geral)\b')
    summary_match = summary_header_regex.search(bloco_norm)
    summary_pos = summary_match.start() if summary_match else None

    # 1) same-line pattern: code ... qty ... value
    same_line_regex = re.compile(
        rf'\b{re.escape(code)}\b[^\d\n\r]*?([\d]{{1,3}}(?:[.,]\d{{3}})*(?:[.,]\d{{2}})?)\D+([\d]{{1,3}}(?:[.,]\d{{3}})*(?:[.,]\d{{2}})?)',
        flags=re.IGNORECASE
    )
    same_matches = list(same_line_regex.finditer(bloco_norm))
    if same_matches:
        chosen = None
        if summary_pos is not None:
            for m in same_matches:
                if m.start() < summary_pos:
                    chosen = m
                    break
        if chosen is None:
            chosen = same_matches[0]
        if chosen:
            span_text = bloco_norm[max(0, chosen.start()-30): chosen.end()+30]
            if require_both and not re.search(desc_pattern, span_text, flags=re.IGNORECASE):
                # don't accept this same-line match if description not present nearby
                pass
            else:
                val = chosen.group(2)
                normalized = _normalize_money_str(val)
                if normalized and salary_str:
                    try:
                        if float(normalized) == float(salary_str):
                            pass
                        else:
                            return normalized
                    except Exception:
                        return normalized
                elif normalized:
                    return normalized

    # 2) windowed search (line-level), but only up to the summary section
    lines = re.split(r'\r?\n', bloco_norm)
    line_limit = len(lines)
    if summary_pos is not None:
        # find the line index where summary starts (approx by cumulative length)
        cum = 0
        for i, ln in enumerate(lines):
            cum += len(ln) + 1
            if cum >= summary_pos:
                line_limit = i
                break

    money_regex = r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})'

    for idx, line in enumerate(lines[:line_limit]):
        # Determine whether to consider this line/window:
        has_code_line = bool(re.search(rf'\b{re.escape(code)}\b', line))
        has_desc_line = bool(re.search(desc_pattern, line, flags=re.IGNORECASE))
        # Also check in window (line + next 2 lines)
        window = ' '.join(lines[idx: idx + 3])
        has_code_window = bool(re.search(rf'\b{re.escape(code)}\b', window))
        has_desc_window = bool(re.search(desc_pattern, window, flags=re.IGNORECASE))

        if require_both:
            if not (has_code_window and has_desc_window):
                continue
        else:
            if not (has_code_line or has_desc_line or has_code_window or has_desc_window):
                continue

        # Find money occurrences and pick the one that comes AFTER the code (preferred)
        code_pos = None
        mcode = re.search(rf'\b{re.escape(code)}\b', window)
        if mcode:
            code_pos = mcode.start()

        # If description pattern present, prefer money after description if code not found
        desc_pos = None
        mdesc = re.search(desc_pattern, window, flags=re.IGNORECASE)
        if mdesc:
            desc_pos = mdesc.end()

        # Find money matches with positions
        money_iter = list(re.finditer(money_regex, window))
        chosen_candidate = None
        if money_iter:
            # Prefer first money after code_pos
            if code_pos is not None:
                for mm in money_iter:
                    if mm.start() > code_pos:
                        chosen_candidate = mm.group(0)
                        break
            # Else prefer first money after desc_pos
            if chosen_candidate is None and desc_pos is not None:
                for mm in money_iter:
                    if mm.start() > desc_pos:
                        chosen_candidate = mm.group(0)
                        break
            # Else fallback: choose last money in window (most likely the rubrica value)
            if chosen_candidate is None:
                chosen_candidate = money_iter[-1].group(0)

            # Normalize and ensure not salary
            normalized = _normalize_money_str(chosen_candidate)
            if normalized:
                if salary_str:
                    try:
                        if float(normalized) == float(salary_str):
                            continue
                        else:
                            return normalized
                    except Exception:
                        return normalized
                else:
                    return normalized

    # 3) final fallback: description-only search before summary
    for idx, line in enumerate(lines[:line_limit]):
        if re.search(desc_pattern, line, flags=re.IGNORECASE):
            window_text = ' '.join(lines[idx: idx + 3])
            money_matches = re.findall(money_regex, window_text)
            if money_matches:
                chosen = money_matches[-1]
                normalized = _normalize_money_str(chosen)
                return normalized or ""
    return ""


def formato_brasileiro(valor):
    try:
        if valor is None or valor == "":
            return ""
        valor_float = float(valor)
        return f"{valor_float:,.2f}".replace(",", "v").replace(".", ",").replace("v", ".")
    except:
        return valor


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
                        situacao_raw = limpar_situacao_raw(bloco)
                        situacao = normalize_situacao(situacao_raw, admissao, competencia)
                        vinculo = limpar_vinculo(bloco)
                        salario = limpar_salario(bloco)

                        dias_faltas = extrair_campo_quantidade_flex(bloco, '8792', r'DIAS\s*FALTAS')

                        adic_noturno_val = _extract_value_from_line_block_improved(bloco, '327', r'ADICIONAL\s*NOTURNO', salary_str=salario)
                        # For night extras require both code and description to be present nearby to avoid false positives
                        he_noturna_50_val = _extract_value_from_line_block_improved(bloco, '218', r'NOTURNO|NOT\.|NOT\s*URNO|H\.?\s*E\.?\s*NOT', salary_str=salario, require_both=True)
                        he_noturna_100_val = _extract_value_from_line_block_improved(bloco, '358', r'NOTURNO|NOT\.|NOT\s*URNO|H\.?\s*E\.?\s*NOT', salary_str=salario, require_both=True)
                        he_50_val = _extract_value_from_line_block_improved(bloco, '150', r'HORAS\s*EXTRAS\s*50%|HORAS\s*EXTRAS\s*50', salary_str=salario)
                        he_100_val = _extract_value_from_line_block_improved(bloco, '200', r'HORAS\s*EXTRAS\s*100%|HORAS\s*EXTRAS\s*100', salary_str=salario)
                        reflexo_adic_noturno_dsr_val = _extract_value_from_line_block_improved(bloco, '854', r'REFLEXO\s*ADIC.*NOTURNO', salary_str=salario)
                        reflexo_extras_dsr_val = _extract_value_from_line_block_improved(bloco, '250', r'REFLEXO\s*EXTRAS\s*DSR', salary_str=salario)

                        dados.append({
                            'Competência': competencia,
                            'Nome': nome,
                            'Cargo': cargo,
                            'Vínculo': vinculo,
                            'Dias Faltas': formato_brasileiro(dias_faltas),
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
                            'Adicional Noturno 20%': formato_brasileiro(adic_noturno_val),
                            'HE Noturna 50% + Adic 20%': formato_brasileiro(he_noturna_50_val),
                            'HE Noturna 100% + Adic 20%': formato_brasileiro(he_noturna_100_val),
                            'Horas Extras 50%': formato_brasileiro(he_50_val),
                            'Horas Extras 100%': formato_brasileiro(he_100_val),
                            'Reflexo Adic Noturno DSR': formato_brasileiro(reflexo_adic_noturno_dsr_val),
                            'Reflexo Extras DSR': formato_brasileiro(reflexo_extras_dsr_val),
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
