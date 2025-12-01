from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import re
import os
import tempfile
import logging
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def limpar_nome(bloco):
    nome_match = re.search(r'Empr\.\:\s*\d*\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ\s\.]+)\s*Situação\:', bloco, re.MULTILINE)
    if nome_match:
        return ' '.join(nome_match.group(1).strip().split())
    nome_match2 = re.search(r'Empr\.\:\s*(.*?)\s*Situação\:', bloco, re.DOTALL)
    if nome_match2:
        return ' '.join(nome_match2.group(1).strip().split())
    return ""


def limpar_cargo(bloco):
    """
    Extrai o cargo evitando que números de código, 'FILIAL', 'SALÁRIO', 'C.B.O' ou campos adjacentes
    sejam incorporados ao texto do cargo. Retorna o cargo em MAIÚSCULAS (compatível com saída anterior).
    """
    if not bloco:
        return ""
    # Captura o conteúdo depois de 'Cargo:' até um delimitador previsível:
    # - número + FILIAL:, FILIAL:, SAL...:, C.B.O, CBO, Vínculo:, nova linha ou fim
    pattern = r'Cargo:\s*(.+?)(?=(?:\d{1,4}\s*FILIAL:|FILIAL:|SAL[^:]{0,20}:|C\.?B\.?O\.?|CBO\b|Vínculo:|\n|$))'
    m = re.search(pattern, bloco, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    cargo_raw = m.group(1).strip()

    # Remover prefixos numéricos grudados (ex: "269ANALISTA ..." ou "115ASSISTENTE ...")
    cargo_raw = re.sub(r'^[\d\.\-:\s]+', '', cargo_raw)

    # Remover variantes de C.B.O e quaisquer dígitos/pontos/traços que venham depois
    cargo_clean = re.sub(r'\bC\.?\s*B\.?\s*O\.?\b[:\s\.\-0-9]*', '', cargo_raw, flags=re.IGNORECASE)
    cargo_clean = re.sub(r'\bCBO\b[:\s\.\-0-9]*', '', cargo_clean, flags=re.IGNORECASE)

    # Remover possíveis porções 'FILIAL:1' que fiquem misturadas ao cargo
    cargo_clean = re.sub(r'\bFILIAL[:\s\.\-0-9A-Za-z]*', '', cargo_clean, flags=re.IGNORECASE)

    # Remover porção de salário caso tenha sobrado (ex: 'SALÁRIO: 4.160,00')
    cargo_clean = re.sub(r'SAL[^:]{0,20}:.*$', '', cargo_clean, flags=re.IGNORECASE)

    # Limpeza final de espaços e caracteres remanescentes
    cargo_clean = re.sub(r'[:\-\s]+$', '', cargo_clean)
    cargo_clean = re.sub(r'\s+', ' ', cargo_clean).strip()

    return cargo_clean.upper() if cargo_clean else ""


def limpar_empresa(texto):
    """
    Extrai o nome da empresa e remove prefixos do tipo '1234 - ' quando presentes.
    - Exemplo: 'Empresa: 1234 - ACME Ltda' -> 'ACME Ltda'
    - Também remove sufixos como 'Página ...' se houver.
    """
    if not texto:
        return ""
    emp_match = re.search(r'Empresa:\s*([^\n\r]+)', texto)
    if emp_match:
        emp = emp_match.group(1).strip()
        # Remover prefixo formado por 4 números, espaço, traço, espaço (ex: '1234 - ')
        emp = re.sub(r'^\d{4}\s-\s', '', emp)
        # Remover elementos de paginação que possam ter sido colados
        emp = re.sub(r'Página.*', '', emp, flags=re.IGNORECASE).strip()
        return ' '.join(emp.split())
    return ""


def limpar_situacao(bloco):
    situacao_match = re.search(r'Situação:\s*([A-Za-zçÇãÃâÂêÊôÔéÉíÍóÓúÚ\-\s]+)', bloco)
    if situacao_match:
        situacao = situacao_match.group(1).strip()
        situacao = situacao.split('CPF')[0].strip()
        return ' '.join(situacao.split())
    return ""


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
                        situacao = limpar_situacao(bloco)
                        vinculo = limpar_vinculo(bloco)
                        salario = limpar_salario(bloco)
                        admissao = limpar_admissao(bloco)
                        dias_faltas = extrair_campo_quantidade_flex(bloco, '8792', r'DIAS\s*FALTAS')
                        he_50 = extrair_campo_quantidade_flex(bloco, '150', r'HORAS\s*EXTRAS\s*50%')
                        he_100 = extrair_campo_quantidade_flex(bloco, '200', r'HORAS\s*EXTRAS\s*100%')
                        he_noturna = extrair_campo_quantidade_flex(
                            bloco,
                            '218',
                            r'H\.?\s*E\.?\s*NOT\.?\s*50%\s*\+\s*AD\.?\s*20%')
                        adic_noturno = extrair_campo_quantidade_flex(bloco, '327', r'ADICIONAL\s*NOTURNO\s*20%')
                        reflexo_extras_dsr = extrair_campo_quantidade_flex(bloco, '250', r'REFLEXO\s*EXTRAS\s*DSR')
                        reflexo_adic_noturno_dsr = extrair_campo_quantidade_flex(
                            bloco, '854', r'REFLEXO\s*ADIC\.?\s*NOTURNO\s*DSR')

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
                            'Adicional Noturno 20%': formato_brasileiro(adic_noturno),
                            'HE Noturna 50% + Adic 20%': formato_brasileiro(he_noturna),
                            'Horas Extras 50%': formato_brasileiro(he_50),
                            'Horas Extras 100%': formato_brasileiro(he_100),
                            'Reflexo Adic Noturno DSR': formato_brasileiro(reflexo_adic_noturno_dsr),
                            'Reflexo Extras DSR': formato_brasileiro(reflexo_extras_dsr)
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
