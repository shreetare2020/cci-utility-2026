"""
CCI WORKING CALCULATION UTILITY — Streamlit Version
Run: streamlit run cci_streamlit_app.py
Data stored in cci_masters.json (local file)
"""

import io, json, os, base64
from datetime import datetime, date
import pandas as pd
import streamlit as st

# ─── PAGE CONFIG ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CCI Working Calculation Utility",
    page_icon="🧮",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ─── CUSTOM CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #f4f6fb; }
[data-testid="stHeader"] { background: transparent; }
.main .block-container { padding-top: 1rem; padding-bottom: 2rem; }
.topbar {
    background: linear-gradient(90deg, #1a56db, #1341a8);
    color: white; padding: 14px 24px; border-radius: 10px;
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 20px; box-shadow: 0 2px 12px rgba(26,86,219,.25);
}
.topbar h1 { font-size: 19px; font-weight: 700; margin: 0; }
.topbar .ver { font-size: 11px; background: rgba(255,255,255,.2); padding: 2px 10px; border-radius: 20px; }
.metric-card {
    background: white; border: 1px solid #e5e7eb; border-radius: 10px;
    padding: 16px; text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,.05);
}
.metric-val { font-size: 20px; font-weight: 700; color: #1a56db; }
.metric-lbl { font-size: 11px; color: #6b7280; margin-top: 3px; }
.pill-open { background: #d1fae5; color: #065f46; padding: 2px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.pill-closed { background: #fee2e2; color: #991b1b; padding: 2px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.formula-box {
    background: #f8fafc; border-left: 4px solid #1a56db;
    padding: 10px 14px; border-radius: 0 8px 8px 0;
    font-family: monospace; font-size: 13px; color: #374151;
    margin: 8px 0; white-space: pre-wrap;
}
div[data-testid="stTabs"] button { font-size: 14px; font-weight: 500; }
</style>
""", unsafe_allow_html=True)

# ─── MASTER FILE ──────────────────────────────────────────────────────────────
MASTER_FILE = "cci_masters.json"

def load_masters():
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE) as f:
            return json.load(f)
    return {"projects": [], "contracts": []}

def save_masters(data):
    with open(MASTER_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

if "masters" not in st.session_state:
    st.session_state.masters = load_masters()

# ─── SAFE FLOAT ───────────────────────────────────────────────────────────────
def sf(val, default=0.0):
    try:
        return float(val) if val not in ('', None) else default
    except:
        return default

# ─── PARSE EXCEL ──────────────────────────────────────────────────────────────
def parse_excel(file_bytes):
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    sheets = xl.sheet_names

    cont = pd.read_excel(xl, sheet_name=sheets[0], header=0)
    cont.columns = ["Contract_No","Effective_Date","Bales","Branch"]
    cont = cont.dropna(subset=["Contract_No"])
    cont["Effective_Date"] = pd.to_datetime(cont["Effective_Date"], errors="coerce")
    cont["Bales"] = pd.to_numeric(cont["Bales"], errors="coerce")

    raw2 = pd.read_excel(xl, sheet_name=sheets[1], header=None)
    emd = raw2.iloc[1:,[0,1,2]].copy()
    emd.columns = ["Contract_No","EMD_Date","EMD_Amount"]
    emd = emd.dropna(subset=["Contract_No","EMD_Amount"])
    emd = emd[~emd["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    emd["EMD_Date"]   = pd.to_datetime(emd["EMD_Date"], errors="coerce")
    emd["EMD_Amount"] = pd.to_numeric(emd["EMD_Amount"], errors="coerce")
    emd = emd.dropna(subset=["EMD_Amount"]).reset_index(drop=True)

    pay = raw2.iloc[1:,[4,5,6]].copy()
    pay.columns = ["Contract_No","Payment_Date","Payment_Amount"]
    pay = pay.dropna(subset=["Contract_No","Payment_Amount"])
    pay = pay[~pay["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    pay["Payment_Date"]   = pd.to_datetime(pay["Payment_Date"], errors="coerce")
    pay["Payment_Amount"] = pd.to_numeric(pay["Payment_Amount"], errors="coerce")
    pay = pay.dropna(subset=["Payment_Amount"]).reset_index(drop=True)

    grn = pd.read_excel(xl, sheet_name=sheets[2], header=0)
    grn.columns = ["Contract_No","Party_Bill_Date","GRN_No",
                   "Accepted_Qty_AUM","Accepted_Qty","Material_Amount",
                   "IGST","Party_Bill_Amount","Other_Amount","Final_Indent_Date"]
    grn = grn.dropna(subset=["Contract_No"])
    grn["Party_Bill_Date"]   = pd.to_datetime(grn["Party_Bill_Date"], errors="coerce")
    grn["Final_Indent_Date"] = pd.to_datetime(grn["Final_Indent_Date"], errors="coerce")
    grn["Material_Amount"]   = pd.to_numeric(grn["Material_Amount"], errors="coerce")
    grn["IGST"]              = pd.to_numeric(grn["IGST"], errors="coerce").fillna(0)
    grn["Party_Bill_Amount"] = pd.to_numeric(grn["Party_Bill_Amount"], errors="coerce").fillna(0)
    grn["Accepted_Qty_AUM"]  = pd.to_numeric(grn["Accepted_Qty_AUM"], errors="coerce")
    grn = grn.sort_values("Party_Bill_Date").reset_index(drop=True)
    return cont, emd, pay, grn

# ─── CALCULATIONS ─────────────────────────────────────────────────────────────
def run_calculations(cont, emd, pay, grn, mc):
    emd_rate     = sf(mc.get("emd_percent"), 5.0)
    cd_slabs     = [{"days":sf(s.get("days")),"pct":sf(s.get("pct"))} for s in mc.get("cd_slabs",[])]
    ll_slabs     = [{"days":sf(s.get("days")),"pct":sf(s.get("pct"))} for s in mc.get("ll_slabs",[])]
    ll_gst       = sf(mc.get("ll_gst"), 5.0)
    cc_slabs     = [{"days":sf(s.get("days")),"pct":sf(s.get("pct"))} for s in mc.get("cc_slabs",[])]
    cc_gst       = sf(mc.get("cc_gst"), 5.0)
    cc_free_days = int(sf(mc.get("cc_free_days"), 60))

    total_emd_map = emd.groupby("Contract_No")["EMD_Amount"].sum().to_dict()
    eff_date_map  = cont.set_index("Contract_No")["Effective_Date"].to_dict()
    per_bale_emd  = {}
    for _, r in cont.iterrows():
        cn = r["Contract_No"]; b = r["Bales"] if r["Bales"] > 0 else 1
        per_bale_emd[cn] = total_emd_map.get(cn, 0) / b

    pay_total_map = pay.groupby("Contract_No")["Payment_Amount"].sum().to_dict()

    emd_pool = {}
    for cn, g in emd.groupby("Contract_No"):
        emd_pool[cn] = g[["EMD_Date","EMD_Amount"]].copy().reset_index(drop=True)
        emd_pool[cn]["Remaining"] = emd_pool[cn]["EMD_Amount"].astype(float)

    pay_pool = {}
    for cn, g in pay.groupby("Contract_No"):
        pay_pool[cn] = g[["Payment_Date","Payment_Amount"]].copy().reset_index(drop=True)
        pay_pool[cn]["Remaining"] = pay_pool[cn]["Payment_Amount"].astype(float)

    results = []
    for _, row in grn.iterrows():
        cn    = str(row["Contract_No"]).strip()
        bales = row["Accepted_Qty_AUM"]
        pbe   = per_bale_emd.get(cn, 0)
        mat   = row["Material_Amount"]
        igst  = row["IGST"]
        lift_date = row["Party_Bill_Date"]
        eff_date  = eff_date_map.get(cn, pd.NaT)

        gst_on_mat  = round(igst, 2)
        total_bill  = round(mat + gst_on_mat, 2)
        payment_amt = pay_total_map.get(cn, 0)

        # ── EMD FIFO ──
        emd_need = round(pbe * bales, 2)
        emd_alloc, emd_date = 0.0, pd.NaT
        pool = emd_pool.get(cn)
        if pool is not None and emd_need > 0:
            rem = emd_need
            for idx in pool.index:
                if rem <= 0: break
                avail = pool.at[idx,"Remaining"]
                if avail <= 0: continue
                take = min(avail, rem)
                pool.at[idx,"Remaining"] -= take
                emd_alloc += take; rem -= take
                d = pool.at[idx,"EMD_Date"]
                if pd.isna(emd_date) or d > emd_date: emd_date = d

        # ── PAYMENT FIFO ──
        net_amt = round(mat - emd_alloc, 2)
        pay_alloc, pay_date = 0.0, pd.NaT
        ppool = pay_pool.get(cn)
        if ppool is not None and net_amt > 0:
            rem = net_amt
            for idx in ppool.index:
                if rem <= 0: break
                avail = ppool.at[idx,"Remaining"]
                if avail <= 0: continue
                take = min(avail, rem)
                ppool.at[idx,"Remaining"] -= take
                pay_alloc += take; rem -= take
                d = ppool.at[idx,"Payment_Date"]
                if pd.isna(pay_date) or d > pay_date: pay_date = d

        # ── EMD INTEREST ──
        emd_days, emd_interest = 0, 0.0
        if not pd.isna(emd_date) and not pd.isna(pay_date) and emd_alloc > 0:
            emd_days     = (pay_date - emd_date).days
            emd_interest = round(((emd_alloc * emd_rate / 100) / 365) * emd_days, 2)

        # ── CASH DISCOUNT ──
        cd_amount, cd_days_used, cd_pct_used = 0.0, 0, 0.0
        if cd_slabs and not pd.isna(pay_date) and not pd.isna(lift_date):
            diff_days = (lift_date - pay_date).days
            for slab in sorted(cd_slabs, key=lambda x: -x["days"]):
                if diff_days >= slab["days"] > 0:
                    cd_pct_used  = slab["pct"]
                    cd_days_used = diff_days
                    cd_amount    = round((mat * cd_pct_used / 100) * (diff_days / 365), 2)
                    break

        # ── LATE LIFTING CHARGES ──
        ll_charges, ll_gst_amt, late_lift_days = 0.0, 0.0, 0
        if not pd.isna(pay_date) and not pd.isna(lift_date):
            free_end = pay_date + pd.Timedelta(days=15)
            if lift_date > free_end:
                late_lift_days = (lift_date - free_end).days
                s1 = ll_slabs[0] if len(ll_slabs)>0 else {"days":30,"pct":0.50}
                s2 = ll_slabs[1] if len(ll_slabs)>1 else {"days":30,"pct":0.75}
                s3 = ll_slabs[2] if len(ll_slabs)>2 else {"days":9999,"pct":1.00}
                rem = late_lift_days; ll_base = 0.0
                d1 = min(rem, s1["days"]); ll_base += mat*(s1["pct"]/100)*(d1/30); rem -= d1
                if rem > 0:
                    d2 = min(rem, s2["days"]); ll_base += mat*(s2["pct"]/100)*(d2/30); rem -= d2
                if rem > 0:
                    ll_base += mat*(s3["pct"]/100)*(rem/30)
                ll_charges = round(ll_base, 2)
                ll_gst_amt = round(ll_charges * ll_gst / 100, 2)

        # ── CARRYING CHARGES ──
        # If Payment Date > CC Free End → CC Days = Payment Date - CC Free End
        # Else if Lift Date > CC Free End → CC Days = Lift Date - CC Free End
        cc_charges, cc_gst_amt, cc_days = 0.0, 0.0, 0
        cc_free_end = pd.NaT
        if not pd.isna(eff_date):
            cc_free_end = eff_date + pd.Timedelta(days=cc_free_days)
            if not pd.isna(pay_date) and pay_date > cc_free_end:
                cc_days = min((pay_date - cc_free_end).days, 60)
            elif not pd.isna(lift_date) and lift_date > cc_free_end:
                cc_days = min((lift_date - cc_free_end).days, 60)

            if cc_days > 0:
                s1c = cc_slabs[0] if len(cc_slabs)>0 else {"days":30,"pct":1.25}
                s2c = cc_slabs[1] if len(cc_slabs)>1 else {"days":30,"pct":1.35}
                rem = cc_days; cc_base = 0.0
                d1  = min(rem, s1c["days"])
                cc_base += mat*(s1c["pct"]/100)*(d1/30); rem -= d1
                if rem > 0:
                    cc_base += mat*(s2c["pct"]/100)*(rem/30)
                cc_charges = round(cc_base, 2)
                cc_gst_amt = round(cc_charges * cc_gst / 100, 2)

        results.append({
            "Contract_No"      : cn,
            "GRN_No"           : row["GRN_No"],
            "Effective_Date"   : eff_date,
            "Party_Bill_Date"  : lift_date,
            "Bales"            : int(bales),
            "Material_Amount"  : round(mat, 2),
            "GST_On_Material"  : gst_on_mat,
            "Total_Bill_Amount": total_bill,
            "Payment_Amount"   : round(payment_amt, 2),
            "Per_Bale_EMD"     : round(pbe, 2),
            "EMD_Allocated"    : round(emd_alloc, 2),
            "EMD_Date"         : emd_date,
            "Net_Amount"       : round(net_amt, 2),
            "Payment_Date"     : pay_date,
            "EMD_Days"         : emd_days,
            "EMD_Interest"     : emd_interest,
            "CD_Days"          : cd_days_used,
            "CD_Pct"           : cd_pct_used,
            "Cash_Discount"    : cd_amount,
            "Late_Lift_Days"   : late_lift_days,
            "Late_Lifting_Chg" : ll_charges,
            "Late_Lifting_GST" : ll_gst_amt,
            "CC_Free_End"      : cc_free_end,
            "CC_Days"          : cc_days,
            "Carry_Charges"    : cc_charges,
            "Carry_GST"        : cc_gst_amt,
        })

    return pd.DataFrame(results), per_bale_emd

# ─── EXCEL EXPORT ─────────────────────────────────────────────────────────────
def df_to_excel_bytes(result_df, cont, emd, pay, grn):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        cols = [
            "Contract_No","GRN_No","Effective_Date","Party_Bill_Date","Bales",
            "Material_Amount","GST_On_Material","Total_Bill_Amount","Payment_Amount",
            "Per_Bale_EMD","EMD_Allocated","EMD_Date",
            "Net_Amount","Payment_Date","EMD_Days","EMD_Interest",
            "CD_Days","CD_Pct","Cash_Discount",
            "Late_Lift_Days","Late_Lifting_Chg","Late_Lifting_GST",
            "CC_Free_End","CC_Days","Carry_Charges","Carry_GST"
        ]
        result_df[cols].to_excel(w, sheet_name="GRN Calculation", index=False)
        summary = result_df.groupby("Contract_No").agg(
            GRNs=("GRN_No","count"),
            Total_Bales=("Bales","sum"),
            Total_Material=("Material_Amount","sum"),
            Total_GST_Material=("GST_On_Material","sum"),
            Total_Bill=("Total_Bill_Amount","sum"),
            Total_Payment=("Payment_Amount","first"),
            Total_EMD_Allocated=("EMD_Allocated","sum"),
            Total_EMD_Interest=("EMD_Interest","sum"),
            Total_Cash_Discount=("Cash_Discount","sum"),
            Total_Late_Lifting=("Late_Lifting_Chg","sum"),
            Total_LL_GST=("Late_Lifting_GST","sum"),
            Total_CC_Charges=("Carry_Charges","sum"),
            Total_CC_GST=("Carry_GST","sum"),
        ).reset_index()
        summary.to_excel(w, sheet_name="Summary", index=False)
        cont.to_excel(w, sheet_name="PUR CONT", index=False)
        emd.to_excel(w, sheet_name="EMD Payments", index=False)
        pay.to_excel(w, sheet_name="Final Payments", index=False)
        grn.to_excel(w, sheet_name="GRN Booking", index=False)
    buf.seek(0)
    return buf.getvalue()

def fmt_inr(n):
    if n is None or n == 0: return "—"
    return f"₹{n:,.2f}"

def fmt_date(d):
    if pd.isna(d) or str(d) in ("NaT","nan",""): return "—"
    try: return str(d)[:10]
    except: return str(d)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ═══════════════════════════════════════════════════════════════════════════════

# Top bar
st.markdown("""
<div class="topbar">
  <span style="font-size:24px">🧮</span>
  <h1>CCI Working Calculation Utility</h1>
  <span class="ver">v2.0 — Streamlit</span>
</div>
""", unsafe_allow_html=True)

# Tabs
tab_masters, tab_upload, tab_results, tab_help = st.tabs([
    "📋 Masters", "📤 Upload & Calculate", "📊 Results", "📖 Formula Guide"
])

# ─────────────────────────────────────────────────────────────────────────────
# TAB 1: MASTERS
# ─────────────────────────────────────────────────────────────────────────────
with tab_masters:
    col_left, col_right = st.columns([1, 1], gap="large")

    # ── PROJECT MASTER ──
    with col_left:
        with st.expander("🏗️ **Project Master**", expanded=True):
            with st.form("project_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                proj_name    = c1.text_input("Project Name *", placeholder="e.g. CCI-RAYGADA-2025")
                proj_session = c2.text_input("Session", placeholder="e.g. 2024-25")
                c3, c4 = st.columns(2)
                proj_from   = c3.date_input("From Period *", value=None)
                proj_to     = c4.date_input("To Period *", value=None)
                proj_status = st.selectbox("Status", ["open", "closed"])
                save_proj = st.form_submit_button("💾 Save Project", use_container_width=True, type="primary")

            if save_proj:
                if not proj_name or not proj_from or not proj_to:
                    st.error("Project Name, From & To Period are required.")
                else:
                    st.session_state.masters["projects"].append({
                        "name": proj_name, "session": proj_session,
                        "from_period": str(proj_from), "to_period": str(proj_to),
                        "status": proj_status
                    })
                    save_masters(st.session_state.masters)
                    st.success(f"✅ Project '{proj_name}' saved!")
                    st.rerun()

        # ── CONTRACT MASTER ──
        with st.expander("📄 **Contract Master**", expanded=True):
            open_projects = [p["name"] for p in st.session_state.masters["projects"] if p["status"] == "open"]

            with st.form("contract_form", clear_on_submit=True):
                proj_sel  = st.selectbox("Project *", ["-- Select --"] + open_projects)
                c1, c2 = st.columns(2)
                party_name   = c1.text_input("Party Name *")
                contract_no  = c2.text_input("Contract No *", placeholder="e.g. RAY-110425")
                c3, c4 = st.columns(2)
                cont_date    = c3.date_input("Contract Date", value=None)
                eff_date     = c4.date_input("Effective Date", value=None)
                cont_bales   = st.number_input("Contracted Bales", min_value=0, value=0)

                st.markdown("**📌 EMD Slab**")
                ce1, ce2 = st.columns(2)
                emd_days = ce1.number_input("Days", min_value=0, value=365, key="emd_d")
                emd_pct  = ce2.number_input("Interest % p.a.", min_value=0.0, value=5.0, step=0.01, key="emd_p")

                st.markdown("**💸 Cash Discount Slabs**")
                cd_cols = st.columns(3)
                cd1d = cd_cols[0].number_input("Slab 1 Days", 0, value=30, key="cd1d")
                cd1p = cd_cols[1].number_input("Slab 1 %", 0.0, value=0.50, step=0.01, key="cd1p")
                cd2d = cd_cols[0].number_input("Slab 2 Days", 0, value=60, key="cd2d")
                cd2p = cd_cols[1].number_input("Slab 2 %", 0.0, value=0.75, step=0.01, key="cd2p")
                cd3d = cd_cols[0].number_input("Slab 3 Days", 0, value=90, key="cd3d")
                cd3p = cd_cols[1].number_input("Slab 3 %", 0.0, value=1.00, step=0.01, key="cd3p")
                cd_gst = st.number_input("CD GST %", 0.0, value=18.0, step=0.01)

                st.markdown("**⏰ Late Lifting Slabs**")
                ll_cols = st.columns(3)
                ll1d = ll_cols[0].number_input("Slab 1 Days", 0, value=30, key="ll1d")
                ll1p = ll_cols[1].number_input("Slab 1 %/month", 0.0, value=0.50, step=0.01, key="ll1p")
                ll2d = ll_cols[0].number_input("Slab 2 Days", 0, value=30, key="ll2d")
                ll2p = ll_cols[1].number_input("Slab 2 %/month", 0.0, value=0.75, step=0.01, key="ll2p")
                ll3d = ll_cols[0].number_input("Slab 3 Days", 0, value=9999, key="ll3d")
                ll3p = ll_cols[1].number_input("Slab 3 %/month", 0.0, value=1.00, step=0.01, key="ll3p")
                ll_gst_inp = st.number_input("LL GST %", 0.0, value=5.0, step=0.01)

                st.markdown("**🚛 Carrying Charges Slabs**")
                st.caption("CC applies after: Effective Date + Free Period Days")
                cc_free = st.number_input("Total Lifting Free Period (Days from Eff. Date)", 0, value=60)
                cc_cols = st.columns(3)
                cc1d = cc_cols[0].number_input("Slab 1 Days", 0, value=30, key="cc1d")
                cc1p = cc_cols[1].number_input("Slab 1 %/month", 0.0, value=1.25, step=0.01, key="cc1p")
                cc2d = cc_cols[0].number_input("Slab 2 Days", 0, value=30, key="cc2d")
                cc2p = cc_cols[1].number_input("Slab 2 %/month", 0.0, value=1.35, step=0.01, key="cc2p")
                cc_gst_inp = st.number_input("CC GST %", 0.0, value=5.0, step=0.01)

                save_cont = st.form_submit_button("💾 Save Contract Master", use_container_width=True, type="primary")

            if save_cont:
                if proj_sel == "-- Select --" or not contract_no:
                    st.error("Project and Contract No are required.")
                else:
                    cd_slabs = [s for s in [
                        {"days": cd1d, "pct": cd1p},
                        {"days": cd2d, "pct": cd2p},
                        {"days": cd3d, "pct": cd3p},
                    ] if s["days"] > 0]
                    ll_slabs = [s for s in [
                        {"days": ll1d, "pct": ll1p},
                        {"days": ll2d, "pct": ll2p},
                        {"days": ll3d, "pct": ll3p},
                    ] if s["days"] > 0]
                    cc_slabs = [s for s in [
                        {"days": cc1d, "pct": cc1p},
                        {"days": cc2d, "pct": cc2p},
                    ] if s["days"] > 0]

                    st.session_state.masters["contracts"].append({
                        "project": proj_sel, "party": party_name,
                        "contract_no": contract_no,
                        "contract_date": str(cont_date) if cont_date else "",
                        "effective_date": str(eff_date) if eff_date else "",
                        "bales": cont_bales,
                        "emd_days": emd_days, "emd_percent": emd_pct,
                        "cd_slabs": cd_slabs, "cd_gst": cd_gst,
                        "ll_slabs": ll_slabs, "ll_gst": ll_gst_inp,
                        "cc_free_days": cc_free,
                        "cc_slabs": cc_slabs, "cc_gst": cc_gst_inp,
                    })
                    save_masters(st.session_state.masters)
                    st.success(f"✅ Contract '{contract_no}' saved!")
                    st.rerun()

    # ── RIGHT: SAVED LIST ──
    with col_right:
        st.markdown("#### 🗂️ Saved Projects")
        if not st.session_state.masters["projects"]:
            st.info("No projects saved yet.")
        else:
            for i, p in enumerate(st.session_state.masters["projects"]):
                pill = f'<span class="pill-{"open" if p["status"]=="open" else "closed"}">{p["status"].upper()}</span>'
                c1, c2 = st.columns([4, 1])
                c1.markdown(
                    f"**{p['name']}** &nbsp;{pill}<br>"
                    f"<small style='color:#6b7280'>{p.get('session','')} | {p['from_period']} → {p['to_period']}</small>",
                    unsafe_allow_html=True
                )
                if c2.button("🗑 Delete", key=f"del_proj_{i}"):
                    st.session_state.masters["projects"].pop(i)
                    save_masters(st.session_state.masters)
                    st.rerun()

        st.markdown("---")
        st.markdown("#### 📋 Contract Masters")
        if not st.session_state.masters["contracts"]:
            st.info("No contracts saved yet.")
        else:
            for i, c in enumerate(st.session_state.masters["contracts"]):
                with st.container():
                    col_info, col_del = st.columns([5, 1])
                    col_info.markdown(
                        f"**{c['contract_no']}** — {c.get('party','')} &nbsp;"
                        f"<span style='font-size:11px;background:#eff6ff;color:#1a56db;padding:1px 8px;border-radius:10px'>{c['project']}</span><br>"
                        f"<small style='color:#6b7280'>Eff: {c.get('effective_date','—')} | Bales: {c.get('bales','—')} | CC Free: {c.get('cc_free_days',60)}d | EMD: {c.get('emd_percent','—')}%</small>",
                        unsafe_allow_html=True
                    )
                    if col_del.button("🗑", key=f"del_cont_{i}"):
                        st.session_state.masters["contracts"].pop(i)
                        save_masters(st.session_state.masters)
                        st.rerun()
                    st.markdown("<hr style='margin:6px 0;border-color:#f3f4f6'>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# TAB 2: UPLOAD & CALCULATE
# ─────────────────────────────────────────────────────────────────────────────
with tab_upload:
    col_u1, col_u2 = st.columns([1, 1], gap="large")

    with col_u1:
        st.markdown("#### Select Contract Master")
        contracts = st.session_state.masters["contracts"]
        if not contracts:
            st.warning("⚠️ No contracts saved yet. Please add a contract in the Masters tab first.")
        else:
            cont_options = {f"{c['contract_no']} | {c.get('party','')}": c for c in contracts}
            selected_cont_label = st.selectbox("Contract *", list(cont_options.keys()))
            selected_mc = cont_options[selected_cont_label]

            st.markdown("#### Upload Excel File")
            st.info("📋 Excel must have 3 sheets: **Sheet 1** — PUR CONT DETAILS | **Sheet 2** — EMD PAYMENT DETAILS | **Sheet 3** — GRN BOOKING")

            uploaded_file = st.file_uploader("Choose Excel file (.xlsx / .xls)", type=["xlsx","xls"])

            if uploaded_file:
                st.success(f"✅ {uploaded_file.name}")
                if st.button("⚙️ Run Calculation", type="primary", use_container_width=True):
                    with st.spinner("Calculating..."):
                        try:
                            file_bytes = uploaded_file.read()
                            cont_df, emd_df, pay_df, grn_df = parse_excel(file_bytes)
                            result_df, _ = run_calculations(cont_df, emd_df, pay_df, grn_df, selected_mc)
                            excel_bytes  = df_to_excel_bytes(result_df, cont_df, emd_df, pay_df, grn_df)
                            st.session_state["result_df"]    = result_df
                            st.session_state["excel_bytes"]  = excel_bytes
                            st.success(f"✅ {len(result_df)} GRNs calculated! Go to **📊 Results** tab.")
                        except Exception as e:
                            import traceback
                            st.error(f"❌ Error: {e}")
                            st.code(traceback.format_exc())

    with col_u2:
        st.markdown("#### 📝 Expected Excel Format")
        st.markdown("""
**Sheet 1 – PUR CONT DETAILS**
`Contract No. | EFFECTIVE DATE | BALES | BRANCH-CCI`

---
**Sheet 2 – EMD PAYMENT DETAILS**
`Contract No. | EMD DATE | EMD AMOUNT | [blank] | Contract No. | PAYMENT DATE | PAYMENT AMOUNT`

---
**Sheet 3 – GRN BOOKING**
`contract no | Party Bill Date | GRN | Accepted Qty(AUM) | Accepted Qty | Material Amount | IGST | Party Bill Amount | Other Amount | FINAL INDENT DATE`
""")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 3: RESULTS
# ─────────────────────────────────────────────────────────────────────────────
with tab_results:
    if "result_df" not in st.session_state or st.session_state["result_df"] is None:
        st.info("No results yet. Upload an Excel file and run calculations.")
    else:
        df = st.session_state["result_df"]

        # Metrics
        tot_bill   = df["Total_Bill_Amount"].sum()
        tot_emd    = df["EMD_Interest"].sum()
        tot_ll     = df["Late_Lifting_Chg"].sum()
        tot_cc     = df["Carry_Charges"].sum()
        total_grns = len(df)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.markdown(f'<div class="metric-card"><div class="metric-val">{total_grns}</div><div class="metric-lbl">Total GRNs</div></div>', unsafe_allow_html=True)
        m2.markdown(f'<div class="metric-card"><div class="metric-val">₹{tot_bill:,.0f}</div><div class="metric-lbl">Total Bill Amt</div></div>', unsafe_allow_html=True)
        m3.markdown(f'<div class="metric-card"><div class="metric-val" style="color:#0e9f6e">₹{tot_emd:,.0f}</div><div class="metric-lbl">EMD Interest</div></div>', unsafe_allow_html=True)
        m4.markdown(f'<div class="metric-card"><div class="metric-val" style="color:#e02424">₹{tot_ll:,.0f}</div><div class="metric-lbl">Late Lifting</div></div>', unsafe_allow_html=True)
        m5.markdown(f'<div class="metric-card"><div class="metric-val" style="color:#ff8c00">₹{tot_cc:,.0f}</div><div class="metric-lbl">Carry Charges</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # Download button
        if "excel_bytes" in st.session_state:
            st.download_button(
                label="⬇️ Download Excel",
                data=st.session_state["excel_bytes"],
                file_name="CCI_Calculation_Output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary"
            )

        # Results table — format for display
        disp = df.copy()
        date_cols = ["Effective_Date","Party_Bill_Date","EMD_Date","Payment_Date","CC_Free_End"]
        for col in date_cols:
            disp[col] = disp[col].apply(fmt_date)

        money_cols = ["Material_Amount","GST_On_Material","Total_Bill_Amount","Payment_Amount",
                      "Per_Bale_EMD","EMD_Allocated","Net_Amount","EMD_Interest",
                      "Cash_Discount","Late_Lifting_Chg","Late_Lifting_GST","Carry_Charges","Carry_GST"]
        for col in money_cols:
            disp[col] = disp[col].apply(lambda x: f"₹{x:,.2f}" if x else "—")

        st.markdown("#### GRN-Wise Detail")
        st.dataframe(disp, use_container_width=True, height=450)

# ─────────────────────────────────────────────────────────────────────────────
# TAB 4: FORMULA GUIDE
# ─────────────────────────────────────────────────────────────────────────────
with tab_help:
    st.markdown("### 📖 Formula & Logic Guide")
    c1, c2 = st.columns(2, gap="large")

    with c1:
        st.markdown("**📌 Per Bale EMD**")
        st.markdown('<div class="formula-box">Per Bale EMD = Total EMD Payment ÷ Contracted Bales</div>', unsafe_allow_html=True)

        st.markdown("**💹 EMD Interest**")
        st.markdown('<div class="formula-box">EMD Interest = ((EMD Allocated × EMD % p.a.) ÷ 365) × (Payment Date − EMD Date)</div>', unsafe_allow_html=True)

        st.markdown("**💸 Cash Discount**")
        st.markdown('<div class="formula-box">CD = Material Amount × CD% × (Days ÷ 365)</div>', unsafe_allow_html=True)

        st.markdown("**🧾 Bill Amounts**")
        st.markdown("""<div class="formula-box">GST on Material = IGST column from GRN sheet
Total Bill Amount = Material Amount + GST on Material
Payment Amount = Total Final Payments for contract</div>""", unsafe_allow_html=True)

    with c2:
        st.markdown("**⏰ Late Lifting Charges**")
        st.markdown("""<div class="formula-box">Free Period = Payment Date + 15 days
If Lifting Date > Free Period:
  Late Days = Lifting Date − Free Period
  Slab 1 (first 30d) : Amt × 0.50%/month
  Slab 2 (next  30d) : Amt × 0.75%/month
  Slab 3 (beyond)    : Amt × 1.00%/month
  + GST as applicable</div>""", unsafe_allow_html=True)

        st.markdown("**🚛 Carrying Charges**")
        st.markdown("""<div class="formula-box">CC Free End = Effective Date + Free Period Days

If Payment Date > CC Free End:
    CC Days = Payment Date − CC Free End
Elif Lift Date > CC Free End:
    CC Days = Lift Date − CC Free End
Else: CC Days = 0 (no charges)

  First 30d  : Amt × 1.25%/month
  Beyond 30d : Amt × 1.35%/month
  + GST as applicable
  Max 60 days cap applies</div>""", unsafe_allow_html=True)
