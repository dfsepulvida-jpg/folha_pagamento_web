from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import re
import os

app = Flask(__name__)
CORS(app)

def limpar_nome(bloco):
    nome_match = re.search(r'Empr\.\:\s*\d*\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ\s\.]+)\s*Situação\:', bloco, re.MULTILINE)
    if nome_match:
        return ' '.join(nome_match.group(1).strip().split())
    nome_match2 = re.search(r'Empr\.\:\s*(.*?)\s*Situação\:', bloco, re.DOTALL)
    if nome_match2:
        return ' '.join(nome_match2.group(1).strip().split())
    return ""

def limpar_cargo(bloco):
    cargo_match = re.search(r'Cargo:\s*\d*\s*([A-ZÇÁÉÍÓÚÃÕÂÊÔ\s\.]+)', bloco)
    if cargo_match:
        return ' '.join(cargo_match.group(1).strip().split())
    cargo_match2 = re.search(r'Cargo:\s*(.*?)C\.B\.O\:', bloco, re.DOTALL)
    if cargo_match2:
        return ' '.join(cargo_match2.group(1).strip().split())
    return ""

def limpar_empresa(texto):
    emp_match = re.search(r'Empresa:\s*([^\n\r]+)', texto)
    if emp_match:
        emp = emp_match.group(1)
        emp = re.sub(r'Página.*', '', emp).strip()
        return emp
    return ""

def limpar_situacao(bloco):
    situacao_match = re.search(r'Situação:\s*([A-Za-zçÇãÃâÂêÊôÔéÉíÍóÓúÚ ]+)', bloco)
    if situacao_match:
        situacao = situacao_match.group(1).strip()
        situacao = situacao.split('CPF')[0].strip()
        return situacao
    return ""

def limpar_vinculo(bloco):
    vinculo_match = re.search(r'Vínculo:\s*([A-Za-zçÇãÃâÂêÊôÔéÉíÍóÓúÚ ]+)', bloco)
    return vinculo_match.group(1).strip() if vinculo_match else ""

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
    pattern = rf"{codigo}\s*{texto}.*?([\d]+[.,]\d+|[\d]+)"
    match = re.search(pattern, bloco, re.IGNORECASE | re.DOTALL)
    return match.group(1).replace(',', '.') if match else ""

def formato_brasileiro(valor):
    try:
        valor_float = float(valor)
        return f"{valor_float:,.2f}".replace(",", "v").replace(".", ",").replace("v", ".")
    except:
        return valor

def extrair_funcionarios(pdf_path):
    dados = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            texto = page.extract_text()
            empresa = limpar_empresa(texto)
            competencia = limpar_competencia(texto)
            funcionarios = re.split(r'Empr\.\:', texto)[1:]
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
                he_noturna = extrair_campo_quantidade_flex(bloco, '218', r'H\.?\s*E\.?\s*NOT\.?\s*50%\s*\+\s*AD\.?\s*20%')
                adic_noturno = extrair_campo_quantidade_flex(bloco, '327', r'ADICIONAL\s*NOTURNO\s*20%')
                reflexo_extras_dsr = extrair_campo_quantidade_flex(bloco, '250', r'REFLEXO\s*EXTRAS\s*DSR')
                reflexo_adic_noturno_dsr = extrair_campo_quantidade_flex(bloco, '854', r'REFLEXO\s*ADIC\.?\s*NOTURNO\s*DSR')
                # Demais campos em branco para preenchimento posterior
                faltas_justificada = ""
                faltas_sem_justificativa = ""
                justif01 = ""
                justif02 = ""
                justif03 = ""
                obs_falta = ""
                justificativa_adm_dem = ""

                dados.append({
                    'Competência': competencia,
                    'Nome': nome,
                    'Cargo': cargo,
                    'Vínculo': vinculo,
                    'Dias Faltas': formato_brasileiro(dias_faltas),
                    'Faltas Justificada': faltas_justificada,
                    'Faltas sem Justificativa': faltas_sem_justificativa,
                    'Justif. 01': justif01,
                    'Justif. 02': justif02,
                    'Justif. 03': justif03,
                    'Observações falta': obs_falta,
                    'Empresa': empresa,
                    'Situação': situacao,
                    'Justificativa preencher em adm e dem': justificativa_adm_dem,
                    'Admissão': admissao,
                    'Salário': formato_brasileiro(salario),
                    'Adicional Noturno 20%': formato_brasileiro(adic_noturno),
                    'HE Noturna 50% + Adic 20%': formato_brasileiro(he_noturna),
                    'Horas Extras 50%': formato_brasileiro(he_50),
                    'Horas Extras 100%': formato_brasileiro(he_100),
                    'Reflexo Adic Noturno DSR': formato_brasileiro(reflexo_adic_noturno_dsr),
                    'Reflexo Extras DSR': formato_brasileiro(reflexo_extras_dsr)
                })
    return dados

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files['file']
    temp_path = 'temp.pdf'
    file.save(temp_path)
    dados = extrair_funcionarios(temp_path)
    os.remove(temp_path)
    return jsonify(dados)

@app.route("/")
def home():
    return "Backend online!"

if __name__ == '__main__':
     app.run(host="0.0.0.0", port=5000)