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
    """
    Extrai o vínculo e normaliza:
    - Retorna apenas o primeiro token do campo 'Vínculo'
    - Se o token for 'celetista' (qualquer caixa), retorna 'CLT'
    - Caso contrário retorna o token tal qual (limpo)
    Exemplo:
      'Vínculo: Celetista CC' -> 'CLT'
      'Vínculo: Celetista' -> 'CLT'
      'Vínculo: Estatutário' -> 'Estatutário'
    """
    vinculo_match = re.search(r'Vínculo:\s*([^\n\r]+)', bloco, re.IGNORECASE)
    if not vinculo_match:
        return ""
    vinculo_raw = vinculo_match.group(1).strip()
    # normaliza espaços e pega o primeiro token "palavra"
    vinculo_raw = re.sub(r'\s+', ' ', vinculo_raw)
    first_token_match = re.match(r'([A-Za-zçÇãÃâÂêÊôÔéÉíÍóÓúÚ\-]+)', vinculo_raw)
    if not first_token_match:
        return vinculo_raw  # fallback: retorna tudo limpo
    token = first_token_match.group(1)
    if token.lower() == 'celetista':
        return 'CLT'
    return token

def limpar_salario(bloco):
    sal_match = re.search(r'Salário:\s*([\d\.,]+
