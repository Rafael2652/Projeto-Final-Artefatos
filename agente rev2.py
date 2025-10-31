# -*- coding: utf-8 -*-
"""
App Streamlit: Controle de Notas Fiscais + Assistente I.A. (Ollama - llama3.2)

Como executar localmente:
1) Instale dependências:  pip install streamlit pandas openpyxl requests python-dateutil
2) Garanta o Ollama em execução com o modelo llama3.2:  https://ollama.com 
   - ollama pull llama3.2
   - ollama serve (por padrão em http://localhost:11434)
3) Rode o app:  streamlit run streamlit_ollama_notas.py

O app implementa o fluxo solicitado (itens 1 a 6), valida dados,
orienta o usuário, consulta a I.A. para recomendações com base em
mudanças de processo/legislação e registra na planilha
"Planilha_Controle_Notas_Fiscais.xlsx".
"""

from __future__ import annotations
import os
import re
import io
import json
import time
import uuid
import requests
import pandas as pd
from datetime import datetime, date
from dateutil.parser import parse as dateparse
import streamlit as st

# =============================
# Configuração básica do app
# =============================
st.set_page_config(
    page_title="Controle de Notas Fiscais + I.A.",
    page_icon="📑",
    layout="wide",
)

PLANILHA_ARQUIVO = "Planilha_Controle_Notas_Fiscais.xlsx"
PLANILHA_ABA = "Notas"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

COLUNAS = [
    "Data de Emissão",
    "Nº da NF",
    "Tipo (Entrada/Saída)",
    "Fornecedor ou Cliente",
    "Descrição / Observação",
    "CFOP",
    "Categoria",
    "Valor Total (R$)",
    "Departamento Responsável",
    "Situação (Paga / Pendente / Recebida / Entregue)",
    "Chave de Acesso (44 dígitos)",
]

# =============================
# Utilidades
# =============================
@st.cache_data(show_spinner=False)
def carregar_planilha(caminho: str) -> pd.DataFrame:
    if os.path.exists(caminho):
        try:
            df = pd.read_excel(caminho, sheet_name=PLANILHA_ABA, dtype=str)
            # padroniza colunas
            faltantes = [c for c in COLUNAS if c not in df.columns]
            for c in faltantes:
                df[c] = ""
            df = df[COLUNAS]
            return df
        except Exception:
            st.warning("Arquivo existente não possui a aba esperada; será criado um novo.")
    # inicia vazio
    return pd.DataFrame(columns=COLUNAS)


def salvar_planilha(caminho: str, df: pd.DataFrame) -> None:
    with pd.ExcelWriter(caminho, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=PLANILHA_ABA, index=False)


def df_para_download(df: pd.DataFrame, nome_arquivo: str) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=PLANILHA_ABA, index=False)
    return buffer.getvalue()


# =============================
# Regras de negócio (itens 1 a 5)
# =============================
CFOP_SETOR_MAP = {
    "1": "Compras / Almoxarifado / Contabilidade",
    "2": "Compras / Almoxarifado / Contabilidade",
    "5": "Vendas / Financeiro / Fiscal",
    "6": "Vendas / Financeiro / Fiscal",
}

CATEGORIAS = [
    "Materiais / Insumos",
    "Serviços",
    "Vendas de Produtos",
    "Despesas administrativas",
]

CATEGORIA_SETOR_SUG = {
    "Materiais / Insumos": "Produção / Almoxarifado",
    "Serviços": "Manutenção / Financeiro",
    "Vendas de Produtos": "Comercial / Fiscal",
    "Despesas administrativas": "Administrativo / Financeiro",
}

SITUACOES = [
    "Paga",
    "Pendente",
    "Recebida",
    "Entregue",
]

TIPOS = ["Entrada", "Saída"]


def normalizar_cfop(cfop: str) -> str:
    if not cfop:
        return ""
    # remove tudo que não é dígito e mantém ponto decimal simples
    cfop = cfop.replace(",", ".")
    # aceita formato 1.102 ou 1102; mantém com ponto no padrão N.NNN
    dig = re.sub(r"[^0-9]", "", cfop)
    if len(dig) == 4:
        return f"{dig[0]}.{dig[1:]}"  # N.NNN
    return cfop.strip()


def inferir_tipo_por_cfop(cfop_norm: str) -> str | None:
    if not cfop_norm:
        return None
    m = re.match(r"^(\d)\.?\d{3}$", cfop_norm)
    if not m:
        return None
    inicial = m.group(1)
    if inicial in {"1", "2"}:
        return "Entrada"
    if inicial in {"5", "6"}:
        return "Saída"
    return None


def setor_por_cfop(cfop_norm: str) -> str:
    if not cfop_norm:
        return ""
    m = re.match(r"^(\d)", cfop_norm)
    if not m:
        return ""
    return CFOP_SETOR_MAP.get(m.group(1), "")


def validar_chave_acesso(chave: str) -> bool:
    return bool(re.fullmatch(r"\d{44}", (chave or "").strip()))


def validar_data_emissao(data_str: str) -> bool:
    try:
        _ = dateparse(data_str, dayfirst=True).date()
        return True
    except Exception:
        return False


def validar_valor_total(valor: str) -> bool:
    try:
        valor = str(valor).replace(".", "").replace(",", ".")
        float(valor)
        return True
    except Exception:
        return False


def formatar_valor(valor: str) -> str:
    v = str(valor).replace(".", "").replace(",", ".")
    return f"{float(v):.2f}"


# =============================
# Integração com Ollama
# =============================

def ollama_disponivel() -> bool:
    try:
        r = requests.get(OLLAMA_URL)
        return r.status_code < 500
    except Exception:
        return False


def consultar_ollama(mensagem_usuario: str, top_p: float = 0.9, temperature: float = 0.2) -> str:
    """Consulta o endpoint /api/chat do Ollama para orientar o usuário.
    Usa um prompt de sistema para manter o contexto do fluxo (itens 1 a 6).
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Assuma a função de um assistente especializado em legislação tributária e processos de negócios. Sua principal responsabilidade é fornecer informações precisas, detalhadas e atualizadas sobre a legislação corporativa, regulamentações fiscais e os processos operacionais obrigatórios para empresas."
                    " Responda de forma objetiva, cite cuidados com CFOP, impostos (ICMS/ISS/IPI),"
                    " e sugira ações quando houver mudanças gerenciais ou legislações relevantes."
                    " Se não tiver certeza, peça documentação (legislação, nota, contrato)."
                ),
            },
            {
                "role": "user",
                "content": mensagem_usuario.strip(),
            },
        ],
        "stream": False,
        "options": {"top_p": top_p, "temperature": temperature},
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        return data.get("message", {}).get("content", "")
    except Exception as e:
        return f"[I.A. indisponível ou erro na consulta: {e}]"


# =============================
# Estado inicial
# =============================
if "df" not in st.session_state:
    st.session_state.df = carregar_planilha(PLANILHA_ARQUIVO)

if "chat" not in st.session_state:
    st.session_state.chat = []  # lista de (role, text)

# =============================
# Sidebar
# =============================
with st.sidebar:
    st.header("⚙️ Configurações")
    st.caption("Parâmetros da I.A. (Ollama)")
    model = st.text_input("Modelo", value=OLLAMA_MODEL)
    OLLAMA_MODEL = model or OLLAMA_MODEL
    url = st.text_input("Endpoint", value=OLLAMA_URL)
    OLLAMA_URL = url or OLLAMA_URL

    st.divider()
    st.subheader("Planilha")
    st.write(f"Arquivo: **{PLANILHA_ARQUIVO}** / Aba: **{PLANILHA_ABA}**")
    if st.button("💾 Salvar agora"):
        salvar_planilha(PLANILHA_ARQUIVO, st.session_state.df)
        st.success("Planilha salva no disco.")

    # Carregar exemplos prontos (itens do enunciado)
    if st.button("📥 Carregar exemplos do enunciado"):
        exemplos = [
            ["10/10/2025","1023","Entrada","Ferro & Cia Ltda","Compra de barras de ferro","1.102","Materiais / Insumos","8500","Almoxarifado / Contabilidade","Recebida","35241111879788000123550000001023123456789012"],
            ["11/10/2025","1589","Saída","Oficina Mecânica Pires","Venda de eixos montados","5.101","Vendas de Produtos","12900","Fiscal / Financeiro","Entregue","35241111879788000123550000001589123456789012"],
            ["15/10/2025","2045","Entrada","Servmaq Serviços Ltda","Manutenção de torno mecânico","1.401","Serviços","3500","Manutenção / Financeiro","Paga","35241111879788000123550000002045123456789012"],
            ["18/10/2025","1780","Saída","Auto Peças Silva","Venda de cubos e flanges","5.102","Vendas de Produtos","24700","Fiscal / Financeiro","Entregue","35241111879788000123550000001780123456789012"],
        ]
        df_ex = pd.DataFrame(exemplos, columns=COLUNAS)
        st.session_state.df = pd.concat([st.session_state.df, df_ex], ignore_index=True)
        st.success("Exemplos adicionados à tabela.")

# =============================
# Layout principal
# =============================
st.title("📑 Controle de Notas Fiscais + 💬 Assistente I.A. (Ollama)")

aba_cadastro, aba_assistente, aba_tabela = st.tabs(["Cadastro da Nota", "Assistente I.A.", "Planilha / Exportar"]) 

# -----------------------------
# Aba 1: Cadastro
# -----------------------------
with aba_cadastro:
    st.subheader("Fluxo guiado (Itens 1 a 6)")
    with st.form("form_nota", clear_on_submit=False):
        c1, c2, c3, c4 = st.columns([1,1,1,1])
        with c1:
            data_emissao = st.text_input("Data de Emissão (dd/mm/aaaa)")
        with c2:
            numero_nf = st.text_input("Nº da NF")
        with c3:
            cfop_in = st.text_input("CFOP (ex.: 1.102 ou 1102)")
        with c4:
            tipo_escolhido = st.selectbox("Tipo (Entrada/Saída)", options=["(auto)"] + TIPOS, index=0)

        cfop_norm = normalizar_cfop(cfop_in)
        tipo_inferido = inferir_tipo_por_cfop(cfop_norm)

        # Item 1: Identificar o tipo de nota
        st.markdown("**1) Tipo de nota**")
        tipo_mostrar = tipo_inferido or "Não identificado"
        st.info(f"CFOP normalizado: **{cfop_norm or '—'}** | Tipo inferido: **{tipo_mostrar}**")
        if tipo_escolhido != "(auto)":
            tipo_final = tipo_escolhido
            if tipo_inferido and tipo_escolhido != tipo_inferido:
                st.warning("O tipo selecionado diverge do CFOP. Verifique a consistência.")
        else:
            tipo_final = tipo_inferido or ""

        # Item 2: Analisar o tipo de operação (CFOP -> Setor)
        st.markdown("**2) Tipo de operação / Setor**")
        setor_cfop = setor_por_cfop(cfop_norm)
        st.write(f"Setor sugerido pelo CFOP: **{setor_cfop or '—'}**")

        # Item 3: Classificar por categoria
        st.markdown("**3) Categoria**")
        categoria = st.selectbox("Categoria", options=["(selecione)"] + CATEGORIAS, index=0)
        setor_categoria = CATEGORIA_SETOR_SUG.get(categoria, "") if categoria in CATEGORIAS else ""
        st.caption(f"Setor indicado pela categoria: {setor_categoria or '—'}")

        # Item 4: Conferir dados principais
        st.markdown("**4) Dados principais**")
        c5, c6 = st.columns([2,2])
        with c5:
            parceiro = st.text_input("Fornecedor ou Cliente")
            descricao = st.text_area("Descrição / Observação")
            situacao = st.selectbox("Situação", options=SITUACOES)
        with c6:
            valor_total = st.text_input("Valor Total (R$)")
            departamento_final = st.text_input(
                "Departamento Responsável",
                value=(setor_cfop or setor_categoria or ""),
            )
            chave_acesso = st.text_input("Chave de Acesso (44 dígitos)")

        # Botões do formulário
        submitted = st.form_submit_button("➕ Adicionar registro à planilha")

    if submitted:
        erros = []
        if not validar_data_emissao(data_emissao):
            erros.append("Data de emissão inválida (use dd/mm/aaaa).")
        if not numero_nf:
            erros.append("Número da NF é obrigatório.")
        if not cfop_norm or not re.fullmatch(r"\d\.\d{3}", cfop_norm or ""):
            erros.append("CFOP inválido (use 1.102 ou similar).")
        if not tipo_final:
            erros.append("Tipo não definido (selecione manualmente ou corrija o CFOP).")
        if categoria not in CATEGORIAS:
            erros.append("Categoria não selecionada.")
        if not validar_valor_total(valor_total):
            erros.append("Valor total inválido.")
        if not departamento_final:
            erros.append("Departamento responsável não informado.")
        if not validar_chave_acesso(chave_acesso):
            erros.append("Chave de acesso deve conter 44 dígitos numéricos.")

        if erros:
            st.error("\n".join([f"• {e}" for e in erros]))
        else:
            registro = {
                "Data de Emissão": dateparse(data_emissao, dayfirst=True).strftime("%d/%m/%Y"),
                "Nº da NF": numero_nf.strip(),
                "Tipo (Entrada/Saída)": tipo_final,
                "Fornecedor ou Cliente": parceiro.strip(),
                "Descrição / Observação": descricao.strip(),
                "CFOP": cfop_norm,
                "Categoria": categoria,
                "Valor Total (R$)": formatar_valor(valor_total),
                "Departamento Responsável": departamento_final.strip(),
                "Situação (Paga / Pendente / Recebida / Entregue)": situacao,
                "Chave de Acesso (44 dígitos)": chave_acesso.strip(),
            }
            st.session_state.df = pd.concat(
                [st.session_state.df, pd.DataFrame([registro])], ignore_index=True
            )
            salvar_planilha(PLANILHA_ARQUIVO, st.session_state.df)
            st.success("Registro adicionado e planilha atualizada (Item 6 concluído).")

            with st.expander("📄 Registro inserido"):
                st.dataframe(pd.DataFrame([registro]), use_container_width=True)

# -----------------------------
# Aba 2: Assistente I.A.
# -----------------------------
with aba_assistente:
    st.subheader("Pergunte sobre processos, CFOPs, impostos e mudanças de legislação")
    if not ollama_disponivel():
        st.warning(
            "Ollama não detectado em **%s**. Ajuste o endpoint no menu lateral e garanta que o modelo '%s' está disponível."
            % (OLLAMA_URL, OLLAMA_MODEL)
        )

    # Caixa de prompt
    prompt = st.text_area(
        "Sua pergunta ou descreva a mudança de processo/legislação para receber recomendações:",
        placeholder=(
            "Exemplos: 'Nova alíquota de ISS para serviços de manutenção mudou no município X, como adaptar?\n"
            "CFOP 5.101 vs 5.102 para venda interna: diferenças práticas?'"
        ),
        height=120,
    )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        temp = st.slider("Temperature", 0.0, 1.0, 0.2, 0.05)
    with col_b:
        top_p = st.slider("Top-p", 0.1, 1.0, 0.9, 0.05)

    if st.button("🧠 Consultar I.A."):
        if not prompt.strip():
            st.error("Digite algo para consultar a I.A.")
        else:
            st.session_state.chat.append(("user", prompt))
            resposta = consultar_ollama(prompt, top_p=top_p, temperature=temp)
            st.session_state.chat.append(("assistant", resposta))

    # Histórico simples
    if st.session_state.chat:
        for role, msg in st.session_state.chat[-12:]:
            if role == "user":
                st.markdown(f"**Você:** {msg}")
            else:
                st.markdown(f"**I.A.:** {msg}")

# -----------------------------
# Aba 3: Planilha / Exportar (Item 6)
# -----------------------------
with aba_tabela:
    st.subheader("Registros na planilha (Item 6)")
    st.dataframe(st.session_state.df, use_container_width=True, hide_index=True)

    col1, col2, col3 = st.columns([1,1,1])
    with col1:
        if st.button("🧹 Remover linhas selecionadas"):
            st.info("Use o filtro abaixo para exportar um subconjunto. Para remoção física, ajuste manualmente no Excel se preferir.")
    with col2:
        st.write("")
    with col3:
        if st.button("💽 Salvar planilha no disco"):
            salvar_planilha(PLANILHA_ARQUIVO, st.session_state.df)
            st.success(f"Arquivo salvo: {PLANILHA_ARQUIVO}")

    st.markdown("### Exportar")
    df = st.session_state.df.copy()
    excel_bytes = df_para_download(df, PLANILHA_ARQUIVO)
    st.download_button(
        label="⬇️ Baixar Planilha_Controle_Notas_Fiscais.xlsx",
        data=excel_bytes,
        file_name=PLANILHA_ARQUIVO,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# =============================
# Rodapé
# =============================
st.caption(
    "Dica: preencha o CFOP corretamente para que o sistema infira o tipo e o setor.\n"
    "Use a aba de Assistente I.A. para validar regras fiscais e se manter atualizado sobre eventuais mudanças."
)
