import React, { useState } from 'react';
import axios from 'axios';
import * as XLSX from 'xlsx';
import { saveAs } from 'file-saver';

const COLUNAS = [
  "Competência",
  "Nome",
  "Cargo", // <-- agora logo após Nome
  "Vínculo",
  "Dias Faltas",
  "Faltas Justificada",
  "Faltas sem Justificativa",
  "Justif. 01",
  "Justif. 02",
  "Justif. 03",
  "Observações falta",
  "Empresa",
  "Situação",
  "Justificativa preencher em adm e dem",
  "Admissão",
  "Salário",
  "Adicional Noturno 20%",
  "HE Noturna 50% + Adic 20%",
  "Horas Extras 50%",
  "Horas Extras 100%",
  "Reflexo Adic Noturno DSR",
  "Reflexo Extras DSR"
];

function App() {
  const [dados, setDados] = useState([]);
  const [file, setFile] = useState(null);

  const handleUpload = async () => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await axios.post('http://localhost:5000/upload', formData);
    setDados(res.data);
  };

  const exportToExcel = () => {
    // Garante que todas as colunas existem e estão na ordem correta
    const arr = dados.map((row) => {
      const newRow = {};
      COLUNAS.forEach((col) => {
        newRow[col] = row[col] || "";
      });
      return newRow;
    });
    const ws = XLSX.utils.json_to_sheet(arr, { header: COLUNAS });
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Funcionarios");
    const excelBuffer = XLSX.write(wb, { bookType: "xlsx", type: "array" });
    const blob = new Blob([excelBuffer], { type: "application/octet-stream" });
    saveAs(blob, "funcionarios.xlsx");
  };

  return (
    <div style={{ padding: '2rem' }}>
      <h2>Upload de Folha de Pagamento em PDF</h2>
      <input type="file" accept="application/pdf" onChange={e => setFile(e.target.files[0])} />
      <button onClick={handleUpload} disabled={!file}>Enviar PDF</button>
      <button onClick={exportToExcel} disabled={dados.length === 0} style={{ marginLeft: "1rem" }}>
        Exportar para Excel
      </button>
      <hr/>
      <table border="1" cellPadding="5" style={{marginTop: '2rem', width: '100%'}}>
        <thead>
          <tr>
            {COLUNAS.map(col => <th key={col}>{col}</th>)}
          </tr>
        </thead>
        <tbody>
          {dados.map((linha, idx) => (
            <tr key={idx}>
              {COLUNAS.map(col => <td key={col}>{linha[col]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default App;