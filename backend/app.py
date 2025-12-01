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
    nome_match = re.search(r'Empr\.\:\s*\d*\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ\s\.]+)\s*Situação\:', bloco, re.MULTILINE)
    if nome_match:
        return ' '.join(nome_match.group(1).strip().split())
    nome_match2 = re.search(r'Empr\.\:\s*(.*?)\s*Situação\:', bloco, re.DOTALL)
    if nome_match2:
        return ' '.join(nome_match2.group(1).strip().split())
    return ""


def limpar_cargo(bloco):
    if not bloco:
        return ""
    pattern = r'Cargo:\s*(.+?)(?=(?:\d{1,4}\s*FILIAL:|FILIAL:|SAL[^:]{0,20}:|C\.?B\.?O\.?|CBO\b|Vínculo:|\n|$))'
    m = re.search(pattern, bloco, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        fb = re.search(r'Cargo:\s*(.*?)C\.?\.?B\.?\.?O', bloco, re.IGNORECASE | re.DOTALL)
        if fb:
            cargo_raw = fb.group(1).strip()
        else:
            return ""
    else:
        cargo_raw = m.group(1).strip()

    cargo_raw = re.sub(r'^[\d\.\-:\s]+', '', cargo_raw)
    cargo_clean = re.sub(r'\bC\.?\s*B\.?\s*O\.?\b[:\s\.\-0-9A-Za-z]*', '', cargo_raw, flags=re.IGNORECASE)
    cargo_clean = re.sub(r'\bCBO\b[:\s\.\-0-9A-Za-z]*', '', cargo_clean, flags=re.IGNORECASE)
    cargo_clean = re.sub(r'\bFILIAL[:\s\.\-0-9A-Za-z]*', '', cargo_clean, flags=re.IGNORECASE)
    cargo_clean = re.sub(r'SAL[^:]{0,20}:.*$', '', cargo_clean, flags=re.IGNORECASE)
    cargo_clean = re.sub(r'[:\-\s]+$', '', cargo_clean)
    cargo_clean = re.sub(r'\s+', ' ', cargo_clean).strip()

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
    situacao_match = re.search(r'Situação:\s*([^\n\r]+)', bloco)
    if situacao_match:
        situacao = situacao_match.group(1).strip()
        situacao = situacao.split('CPF')[0].strip()
        return ' '.join(situacao.split())
    return ""


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
    vinculo_match = re.search(r'Vínculo:\s*([^\n\r]+)', bloco, re.IGNORECASE)
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
    sal_match = re.search(r'Salário:\s*([\d\.,]+)', bloco)
    if sal_match:
        return sal_match.group(1).replace('.', '').replace(',', '.')
    return ""


def limpar_admissao(bloco):
    adm_match = re.search(r'Adm:\s*([0-9/]+)', bloco)
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


def _extract_value_from_line_block(bloco: str, code: str, desc_pattern: str) -> str:
    """
    Heurística para retornar o VALOR monetário associado ao item (não a quantidade).
    - Encontra a linha que contém o código (ou a descrição).
    - Coleta os valores monetários nessa linha e nas 1-2 linhas seguintes.
    - Normalmente a ordem é: <descrição> <quantidade> <valor>
      Então pegamos o SEGUNDO valor monetário encontrado na janela (índice 1).
    - Se houver apenas 1 valor na janela, usamos esse valor (fallback).
    - Retorna string com ponto decimal (ex: '417.40') ou "".
    """
    if not bloco:
        return ""
    lines = re.split(r'\r?\n', bloco)
    money_regex = r'(?:\d{1,3}(?:\.\d{3})*|\d+),(?:\d{2})'

    # procura linha que contenha o código ou a descrição
    for idx, line in enumerate(lines):
        if re.search(rf'\b{re.escape(code)}\b', line) or re.search(desc_pattern, line, flags=re.IGNORECASE):
            # janela: linha atual + próximas 2 linhas
            window_text = ' '.join(lines[idx: idx + 3])
            money_matches = re.findall(money_regex, window_text)
            # Se encontrou pelo menos 2 valores (quantidade + valor), pegar o segundo
            if len(money_matches) >= 2:
                chosen = money_matches[1]
                normalized = _normalize_money_str(chosen)
                return normalized or ""
            # Se encontrou apenas 1, provavelmente é o valor (ou não), usar como fallback
            if len(money_matches) == 1:
                chosen = money_matches[0]
                normalized = _normalize_money_str(chosen)
                return normalized or ""
            # Se não encontrou na janela, retornar vazio
            return ""
    # fallback: tentar apenas pela descrição em qualquer linha
    for idx, line in enumerate(lines):
        if re.search(desc_pattern, line, flags=re.IGNORECASE):
            window_text = ' '.join(lines[idx: idx + 3])
            money_matches = re.findall(money_regex, window_text)
            if len(money_matches) >= 2:
                chosen = money_matches[1]
                normalized = _normalize_money_str(chosen)
                return normalized or ""
            if len(money_matches) == 1:
                chosen = money_matches[0]
                normalized = _normalize_money_str(chosen)
                return normalized or ""
            return ""
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

                        # Extrair valores monetários usando heurística que pega o 2º valor na janela
                        adic_noturno_val = _extract_value_from_line_block(bloco, '327', r'ADICIONAL\s*NOTURNO')
                        he_noturna_50_val = _extract_value_from_line_block(bloco, '218', r'NOT.*50%|NOTURNO.*50%|H\.?\s*E\.?\s*NOT')
                        he_noturna_100_val = _extract_value_from_line_block(bloco, '358', r'NOT.*100%|NOTURNO.*100%|HORAS?\s*EXTRAS?\s*NOT.*100%')
                        he_50_val = _extract_value_from_line_block(bloco, '150', r'HORAS\s*EXTRAS\s*50%')
                        he_100_val = _extract_value_from_line_block(bloco, '200', r'HORAS\s*EXTRAS\s*100%')
                        reflexo_adic_noturno_dsr_val = _extract_value_from_line_block(bloco, '854', r'REFLEXO\s*ADIC.*NOTURNO')
                        reflexo_extras_dsr_val = _extract_value_from_line_block(bloco, '250', r'REFLEXO\s*EXTRAS\s*DSR')

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
                            'Reflexo Extras DSR': formato_brasileiro(reflexo_extras_dsr_val)
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
