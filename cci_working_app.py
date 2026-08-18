"""
CCI WORKING CALCULATION UTILITY
Softview Technologies | Streamlit + Firebase
Run: streamlit run cci_working_app.py
"""

import io, json, os, base64 as b64lib
from datetime import date
import pandas as pd
import streamlit as st

# ─── PAGE CONFIG (Must be the first Streamlit command) ────────────────────────
st.set_page_config(
    page_title="CCI Working Calculation Utility",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─── STYLING / CSS TO FIX WHITE BACKGROUND & FIELD ISSUES ─────────────────────
st.markdown("""
    <style>
    .stApp {
        background-color: #0e1117;
        color: #fafafa;
    }
    .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"], .stDateInput input {
        background-color: #262730 !important;
        color: #fafafa !important;
    }
    .stDataFrame {
        background-color: #1e1e1e;
    }
    </style>
""", unsafe_allow_html=True)

# ─── FIREBASE ────────────────────────────────────────────────────────────────
import firebase_admin
from firebase_admin import credentials, firestore

# ─── FIREBASE INIT (works both locally and on Streamlit Cloud) ───────────────
FIREBASE_KEY_FILE = "firebase_key.json"   # local JSON file path

def init_firebase():
    if not firebase_admin._apps:
        try:
            cred_dict = dict(st.secrets["firebase"])
            cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(cred_dict)
        except Exception:
            if not os.path.exists(FIREBASE_KEY_FILE):
                st.error(
                    f"❌ Firebase key not found!\n\n"
                    f"**Localhost fix:** Place your Firebase service account JSON "
                    f"as **`firebase_key.json`** in the same folder as `cci_working_app.py`"
                )
                st.stop()
            cred = credentials.Certificate(FIREBASE_KEY_FILE)
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = init_firebase()

def fs_load():
    try:
        doc = db.collection("cci_utility").document("masters").get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        st.warning(f"Firebase read error: {e}")
    return {"projects": [], "contracts": []}

def fs_save(data):
    try:
        db.collection("cci_utility").document("masters").set(data)
    except Exception as e:
        st.error(f"Firebase save error: {e}")

# ─── LOAD DATA & STATE MANAGEMENT ────────────────────────────────────────────
if "data" not in st.session_state:
    st.session_state.data = fs_load()

data = st.session_state.data

st.title("📊 CCI Working Calculation Utility")
st.sidebar.header("Navigation")
menu = st.sidebar.radio("Go to", ["Dashboard", "Contract Management", "Summary & Details Report"])

# ─── CONTRACT MANAGEMENT (Fixed Edit Bug) ────────────────────────────────────
if menu == "Contract Management":
    st.subheader("Manage Contracts")
    
    contracts = data.get("contracts", [])
    
    # Use a safe mechanism for selection to prevent state collision during edits
    contract_names = [c.get("name", "Unnamed") for c in contracts]
    selected_contract_name = st.selectbox("Select Contract to Edit/View", ["-- Add New Contract --"] + contract_names)
    
    if selected_contract_name == "-- Add New Contract --":
        c_name = st.text_input("Contract Name")
        c_value = st.number_input("Contract Value", value=0.0)
        if st.button("Save New Contract"):
            if c_name:
                contracts.append({"name": c_name, "value": c_value})
                data["contracts"] = contracts
                fs_save(data)
                st.success("Contract added successfully!")
                st.rerun()
            else:
                st.error("Contract name cannot be empty.")
    else:
        # Edit existing contract safely
        contract_idx = contract_names.index(selected_contract_name)
        curr_contract = contracts[contract_idx]
        
        edit_name = st.text_input("Contract Name", value=curr_contract.get("name", ""))
        edit_value = st.number_input("Contract Value", value=float(curr_contract.get("value", 0.0)))
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Update Contract"):
                contracts[contract_idx]["name"] = edit_name
                contracts[contract_idx]["value"] = edit_value
                data["contracts"] = contracts
                fs_save(data)
                st.success("Contract updated successfully!")
                st.rerun()
        with col2:
            if st.button("Delete Contract"):
                contracts.pop(contract_idx)
                data["contracts"] = contracts
                fs_save(data)
                st.success("Contract deleted successfully!")
                st.rerun()

# ─── DASHBOARD ───────────────────────────────────────────────────────────────
elif menu == "Dashboard":
    st.subheader("Overview Dashboard")
    contracts = data.get("contracts", [])
    st.metric("Total Contracts", len(contracts))
    total_val = sum([float(c.get("value", 0)) for c in contracts])
    st.metric("Total Contract Value", f"₹ {total_val:,.2f}")

# ─── SUMMARY & DETAILS REPORT (With Automatic Column Totals) ─────────────────
elif menu == "Summary & Details Report":
    st.subheader("Summary & Details Sheet")
    
    contracts = data.get("contracts", [])
    if contracts:
        df = pd.DataFrame(contracts)
        
        # Calculate totals for numerical columns dynamically
        numeric_cols = df.select_dtypes(include=['number']).columns
        if not numeric_cols.empty:
            totals = df[numeric_cols].sum(numeric_only=True)
            total_row = {col: totals[col] for col in numeric_cols}
            # Set label for non-numeric first column as 'Total'
            for col in df.columns:
                if col not in numeric_cols:
                    total_row[col] = "Total"
                    break
            
            # Append total row safely using pandas concat
            df_total = pd.DataFrame([total_row])
            df_display = pd.concat([df, df_total], ignore_index=True)
        else:
            df_display = df
            
        st.dataframe(df_display, use_container_width=True)
    else:
        st.info("No data available to generate report.")
