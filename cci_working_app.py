import streamlit as st
import pandas as pd
import os
from datetime import datetime
import io
from fpdf import FPDF
import firebase_admin
from firebase_admin import credentials, firestore
import json

# --- 1. UI CONFIG (Aboli & Pista - LOCKED) ---
st.set_page_config(page_title="CCI Supreme Utility", layout="wide")

st.markdown("""
    <style>
    .stApp { background: linear-gradient(135deg, #fff5ec 0%, #ffe0cc 100%) !important; }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #e6fffa 0%, #b2f5ea 100%) !important;
        border-right: 2px solid #d4af37;
    }
    .sidebar-title {
        background: linear-gradient(90deg, #d4af37, #aa8833);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        font-size: 22px; font-weight: 900; text-transform: uppercase;
        text-align: center; margin-bottom: 20px; border-bottom: 2px solid #d4af37;
        padding-bottom: 10px;
    }
    .summary-card {
        background: white; border: 1px solid #d4af37;
        border-radius: 12px; padding: 15px; text-align: center;
        box-shadow: 2px 2px 10px rgba(0,0,0,0.05);
    }
    .stButton>button, .stDownloadButton>button {
        background: #81e6d9 !important; color: black !important;
        font-weight: 900 !important; border: 1px solid #d4af37 !important;
        border-radius: 8px !important; width: 100% !important;
    }
    .executive-label {
        background: #ffe0cc; border-left: 5px solid #d4af37;
        color: #aa8833 !important; padding: 10px; margin: 10px 0;
        font-weight: 800; text-transform: uppercase;
    }
    </style>
""", unsafe_allow_html=True)

# --- 2. FIREBASE CONNECTION (NEW) ---
def init_db():
    if not firebase_admin._apps:
        try:
            sc_json = st.secrets["firebase"]["service_account_json"]
            import ast
            try:
                info = json.loads(sc_json, strict=False)
            except:
                info = ast.literal_eval(sc_json)
            
            # --- YE SABSE ZAROORI LINE HAI (PEM ERROR FIX) ---
            if "private_key" in info:
                info["private_key"] = info["private_key"].replace("\\n", "\n")
            # ------------------------------------------------
                
            cred = credentials.Certificate(info)
            firebase_admin.initialize_app(cred)
        except Exception as e:
            st.error(f"Database Connection Error: {e}")
            return None
    return firestore.client()

db = init_db()

# --- 3. ENGINE (LOCKED - NO CHANGE) ---
def calculate_compound_prorata(amt, days, d1, p1, d2, p2, d3, p3):
    if days <= 0: return 0
    curr_bal, total_int = amt, 0
    s1_d = min(days, d1); i1 = (curr_bal * p1 / 100) * (s1_d / 365); total_int += i1
    if days > d1:
        curr_bal += i1; s2_d = min(days, d2) - d1; i2 = (curr_bal * p2 / 100) * (s2_d / 365); total_int += i2
        if days > d2:
            curr_bal += i2; s3_d = min(days, d3) - d2; i3 = (curr_bal * p3 / 100) * (s3_d / 365); total_int += i3
    return total_int

# --- 4. DATABASE OPERATIONS (FIREBASE) ---
def load_master_data():
    if db:
        docs = db.collection("cci_master").stream()
        data = [doc.to_dict() for doc in docs]
        return pd.DataFrame(data)
    return pd.DataFrame()

master_df = load_master_data()

# --- 5. SIDEBAR ---
with st.sidebar:
    st.markdown('<div class="sidebar-title">⚜️ CCI CALCULATION<br>WORKING UTILITY ⚜️</div>', unsafe_allow_html=True)
    st.markdown("<h4 style='text-align:center; color:#aa8833;'>MASTER SETUP</h4>", unsafe_allow_html=True)
    
    with st.form("master_form_v100"):
        p_name = st.text_input("PARTY NAME")
        p_valid = st.date_input("PROJECT VALID TILL")
        s_no = st.text_input("SAUDA NO", "DEFAULT").upper()
        c_date = st.date_input("CONTRACT DATE")
        e_dt = st.date_input("EFFECTIVE DATE")
        
        with st.expander("💰 CD & TAX"):
            c1, c2 = st.columns(2)
            cd_d = c1.number_input("CD DAYS", 30); cd_p = c2.number_input("CD %", 5.0); gst = st.number_input("GST %", 18.0)
        with st.expander("📉 LL SLABS"):
            l1a, l1b = st.columns(2); l1d = l1a.number_input("LL D1", 30); l1p = l1b.number_input("LL P1", 0.5)
            l2a, l2b = st.columns(2); l2d = l2a.number_input("LL D2", 60); l2p = l2b.number_input("LL P2", 0.75)
            l3a, l3b = st.columns(2); l3d = l3a.number_input("LL D3", 360); l3p = l3b.number_input("LL P3", 1.0)
        with st.expander("🚚 CC SLABS"):
            c1a, c1b = st.columns(2); c1d = c1a.number_input("CC D1", 30); c1p = c1b.number_input("CC P1", 1.25)
            c2a, c2b = st.columns(2); c2d = c2a.number_input("CC D2", 60); c2p = c2b.number_input("CC P2", 1.35)
            c3a, c3b = st.columns(2); c3d = c3a.number_input("CC D3", 360); c3p = c3b.number_input("CC P3", 1.5)

        if st.form_submit_button("🔱 SAVE TO MASTER"):
            new_row = {
                "Party_Name": p_name, "Project_Valid_Till": p_valid.strftime('%d-%m-%Y'),
                "Sauda_No": s_no, "Contract_Date": c_date.strftime('%d-%m-%Y'),
                "Interest_Effective_Date": e_dt.strftime('%d-%m-%Y'), 
                "CD_Free_Days": cd_d, "CD_Percentage": cd_p, "GST": gst, 
                "LL_D1": l1d, "LL_P1": l1p, "LL_D2": l2d, "LL_P2": l2p, "LL_D3": l3d, "LL_P3": l3p, 
                "CC_D1": c1d, "CC_P1": c1p, "CC_D2": c2d, "CC_P2": c2p, "CC_D3": c3d, "CC_P3": c3p
            }
            if db:
                db.collection("cci_master").document(s_no).set(new_row)
                st.success(f"Sauda {s_no} Saved Permanently!")
                st.rerun()

# --- 6. MAIN CONTENT ---
st.markdown('<div class="executive-label">💎 MASTER HISTORY PREVIEW</div>', unsafe_allow_html=True)
if not master_df.empty:
    st.dataframe(master_df, use_container_width=True)
else:
    st.info("No Master Data Found. Add from sidebar.")

u_file = st.file_uploader("UPLOAD DATA", type=["xlsx", "csv"])
if u_file:
    if st.button("🚀 EXECUTE CALCULATIONS"):
        df_xl = pd.read_csv(u_file) if u_file.name.endswith('.csv') else pd.read_excel(u_file)
        results = []
        for _, row in df_xl.iterrows():
            xl_sno = str(row.get('Sauda_No', row.get('Sauda No', ''))).split('.')[0].strip().upper()
            m = master_df[master_df['Sauda_No'] == xl_sno] if not master_df.empty else pd.DataFrame()
            if m.empty and not master_df.empty: m = master_df[master_df['Sauda_No'] == 'DEFAULT']
            
            if not m.empty:
                m = m.iloc[0]
                p_dt = pd.to_datetime(row.get('Payment_Date', row.get('Payment Date'))).date()
                e_dt = pd.to_datetime(m['Interest_Effective_Date'], dayfirst=True).date()
                amt = float(row.get('Amount', 0)); diff = (p_dt - e_dt).days
                cd = abs((amt * m['CD_Percentage']/100)*(diff/365)) if diff <= m['CD_Free_Days'] else 0
                ll = calculate_compound_prorata(amt, diff, m['LL_D1'], m['LL_P1'], m['LL_D2'], m['LL_P2'], m['LL_D3'], m['LL_P3'])
                cc = calculate_compound_prorata(amt, diff, m['CC_D1'], m['CC_P1'], m['CC_D2'], m['CC_P2'], m['CC_D3'], m['CC_P3'])
                tax = (ll + cc) * (m['GST']/100)
                results.append({"Sauda": xl_sno, "Days": diff, "Amt": amt, "CD": round(cd,2), "LL": round(ll,2), "CC": round(cc,2), "GST": round(tax,2), "Net": round(amt-cd+ll+cc+tax,2)})
        
        if results:
            st.session_state.audit_results = pd.DataFrame(results)

# --- 7. RESULTS & EXPORT (LOCKED - NO CHANGE) ---
if 'audit_results' in st.session_state and st.session_state.audit_results is not None:
    res = st.session_state.audit_results
    st.markdown('<div class="executive-label">📊 RESULTS SUMMARY</div>', unsafe_allow_html=True)
    s1, s2, s3, s4 = st.columns(4)
    s1.markdown(f'<div class="summary-card"><div style="font-size:11px; font-weight:800;">TOTAL CD</div><div style="font-size:20px; font-weight:900; color:#aa8833;">₹ {res["CD"].sum():,.2f}</div></div>', unsafe_allow_html=True)
    s2.markdown(f'<div class="summary-card"><div style="font-size:11px; font-weight:800;">TOTAL LL</div><div style="font-size:20px; font-weight:900; color:#aa8833;">₹ {res["LL"].sum():,.2f}</div></div>', unsafe_allow_html=True)
    s3.markdown(f'<div class="summary-card"><div style="font-size:11px; font-weight:800;">TOTAL CC</div><div style="font-size:20px; font-weight:900; color:#aa8833;">₹ {res["CC"].sum():,.2f}</div></div>', unsafe_allow_html=True)
    s4.markdown(f'<div class="summary-card" style="border:2px solid #d4af37;"><div style="font-size:11px; font-weight:800;">NET PAYABLE</div><div style="font-size:20px; font-weight:900; color:#aa8833;">₹ {res["Net"].sum():,.2f}</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="executive-label">📑 DETAILED WORKING DATA</div>', unsafe_allow_html=True)
    st.dataframe(res, use_container_width=True)

    st.markdown('<div class="executive-label">📥 EXPORT REPORTS</div>', unsafe_allow_html=True)
    d1, d2 = st.columns(2)
    buf_xl = io.BytesIO(); res.to_excel(buf_xl, index=False); d1.download_button("📥 DOWNLOAD EXCEL", buf_xl.getvalue(), "CCI_Final_Report.xlsx")
    
    pdf = FPDF(); pdf.add_page(); pdf.set_font("Arial", 'B', 10); pdf.cell(0, 10, "CCI CALCULATION REPORT", ln=True, align='C')
    pdf.ln(5); pdf.set_font("Arial", size=7)
    for col in res.columns: pdf.cell(23, 7, str(col), 1)
    pdf.ln()
    for row in res.values:
        for val in row: pdf.cell(23, 7, str(val), 1)
        pdf.ln()
    d2.download_button("📄 DOWNLOAD PDF", pdf.output(dest='S').encode('latin-1'), "CCI_Final_Report.pdf", "application/pdf")
