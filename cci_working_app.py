"""
CCI WORKING CALCULATION UTILITY - Streamlit Version
Run locally:  streamlit run cci_working_app.py
"""

import io, json, os
import pandas as pd
import streamlit as st
from datetime import datetime, date

st.set_page_config(
    page_title="CCI Working Calculation Utility",
    page_icon="🧮",
    layout="wide",
    initial_sidebar_state="expanded"
)

MASTER_FILE = "cci_masters.json"

# ─── STYLES ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.stTabs [data-baseweb="tab-list"] { gap: 8px; }
.stTabs [data-baseweb="tab"] { padding: 8px 20px; border-radius: 8px 8px 0 0; }
div[data-testid="metric-container"] {
    background:#f0f4ff; border:1px solid #c7d7fd;
    border-radius:10px; padding:12px;
}
.pill-open  { background:#d1fae5; color:#065f46; padding:3px 10px;
               border-radius:20px; font-size:12px; font-weight:600; }
.pill-closed{ background:#fee2e2; color:#991b1b; padding:3px 10px;
               border-radius:20px; font-size:12px; font-weight:600; }
.formula-box{ background:#f8fafc; border-left:4px solid #1a56db;
               padding:10px 14px; border-radius:0 8px 8px 0;
               font-family:monospace; font-size:13px; color:#374151; }
</style>
""", unsafe_allow_html=True)

# ─── MASTER PERSISTENCE ──────────────────────────────────────────────────────
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

def persist():
    save_masters(st.session_state.masters)

# ─── SAFE FLOAT ──────────────────────────────────────────────────────────────
def sf(val, default=0.0):
    try:
        return float(val) if val not in ('', None) else default
    except:
        return default

# ─── CALCULATION ENGINE ──────────────────────────────────────────────────────
def parse_excel(file_bytes):
    xl  = pd.ExcelFile(io.BytesIO(file_bytes))
    sn  = xl.sheet_names

    cont = pd.read_excel(xl, sheet_name=sn[0], header=0)
    cont.columns = ["Contract_No","Effective_Date","Bales","Branch"]
    cont = cont.dropna(subset=["Contract_No"])
    cont["Effective_Date"] = pd.to_datetime(cont["Effective_Date"], errors="coerce")
    cont["Bales"] = pd.to_numeric(cont["Bales"], errors="coerce")

    raw2 = pd.read_excel(xl, sheet_name=sn[1], header=None)
    emd  = raw2.iloc[1:,[0,1,2]].copy()
    emd.columns = ["Contract_No","EMD_Date","EMD_Amount"]
    emd  = emd.dropna(subset=["Contract_No","EMD_Amount"])
    emd  = emd[~emd["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    emd["EMD_Date"]   = pd.to_datetime(emd["EMD_Date"], errors="coerce")
    emd["EMD_Amount"] = pd.to_numeric(emd["EMD_Amount"], errors="coerce")
    emd  = emd.dropna(subset=["EMD_Amount"]).reset_index(drop=True)

    pay  = raw2.iloc[1:,[4,5,6]].copy()
    pay.columns = ["Contract_No","Payment_Date","Payment_Amount"]
    pay  = pay.dropna(subset=["Contract_No","Payment_Amount"])
    pay  = pay[~pay["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    pay["Payment_Date"]   = pd.to_datetime(pay["Payment_Date"], errors="coerce")
    pay["Payment_Amount"] = pd.to_numeric(pay["Payment_Amount"], errors="coerce")
    pay  = pay.dropna(subset=["Payment_Amount"]).reset_index(drop=True)

    grn  = pd.read_excel(xl, sheet_name=sn[2], header=0)
    grn.columns = ["Contract_No","Party_Bill_Date","GRN_No",
                   "Accepted_Qty_AUM","Accepted_Qty","Material_Amount",
                   "IGST","Party_Bill_Amount","Other_Amount","Final_Indent_Date"]
    grn  = grn.dropna(subset=["Contract_No"])
    grn["Party_Bill_Date"]   = pd.to_datetime(grn["Party_Bill_Date"], errors="coerce")
    grn["Final_Indent_Date"] = pd.to_datetime(grn["Final_Indent_Date"], errors="coerce")
    grn["Material_Amount"]   = pd.to_numeric(grn["Material_Amount"], errors="coerce")
    grn["Accepted_Qty_AUM"]  = pd.to_numeric(grn["Accepted_Qty_AUM"], errors="coerce")
    grn  = grn.sort_values("Party_Bill_Date").reset_index(drop=True)
    return cont, emd, pay, grn

def run_calculations(cont, emd, pay, grn, mc):
    emd_rate = sf(mc.get("emd_percent"), 5.0)
    cd_slabs = [{"days":sf(s.get("days")), "pct":sf(s.get("pct"))} for s in mc.get("cd_slabs",[])]
    ll_slabs = [{"days":sf(s.get("days")), "pct":sf(s.get("pct"))} for s in mc.get("ll_slabs",[])]
    cc_slabs = [{"days":sf(s.get("days")), "pct":sf(s.get("pct"))} for s in mc.get("cc_slabs",[])]
    ll_gst   = sf(mc.get("ll_gst"), 5.0)
    cc_gst   = sf(mc.get("cc_gst"), 5.0)

    total_emd_map = emd.groupby("Contract_No")["EMD_Amount"].sum().to_dict()
    per_bale_emd  = {}
    for _, r in cont.iterrows():
        cn = r["Contract_No"]; b = r["Bales"] if r["Bales"] > 0 else 1
        per_bale_emd[cn] = total_emd_map.get(cn, 0) / b

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

        # EMD FIFO
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

        # Payment FIFO
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

        # EMD Interest
        emd_days, emd_interest = 0, 0.0
        if not pd.isna(emd_date) and not pd.isna(pay_date) and emd_alloc > 0:
            emd_days     = (pay_date - emd_date).days
            emd_interest = round(((emd_alloc * emd_rate / 100) / 365) * emd_days, 2)

        # Cash Discount
        cd_amount, cd_days_used, cd_pct_used = 0.0, 0, 0.0
        if cd_slabs and not pd.isna(pay_date) and not pd.isna(row["Party_Bill_Date"]):
            diff_days = (row["Party_Bill_Date"] - pay_date).days
            for slab in sorted(cd_slabs, key=lambda x: -x["days"]):
                if diff_days >= slab["days"] > 0:
                    cd_pct_used  = slab["pct"]
                    cd_days_used = diff_days
                    cd_amount    = round((mat * cd_pct_used / 100) * (diff_days / 365), 2)
                    break

        # Late Lifting
        ll_charges, ll_gst_amt = 0.0, 0.0
        late_lift_days = 0
        if not pd.isna(pay_date) and not pd.isna(row["Party_Bill_Date"]):
            free_end = pay_date + pd.Timedelta(days=15)
            if row["Party_Bill_Date"] > free_end:
                late_lift_days = (row["Party_Bill_Date"] - free_end).days
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

        # Carrying Charges
        cc_charges, cc_gst_amt = 0.0, 0.0
        if not pd.isna(row["Final_Indent_Date"]) and not pd.isna(row["Party_Bill_Date"]):
            if row["Party_Bill_Date"] > row["Final_Indent_Date"]:
                carry_days = min((row["Party_Bill_Date"] - row["Final_Indent_Date"]).days, 60)
                s1c = cc_slabs[0] if len(cc_slabs)>0 else {"days":30,"pct":1.25}
                s2c = cc_slabs[1] if len(cc_slabs)>1 else {"days":30,"pct":1.35}
                rem = carry_days; cc_base = 0.0
                d1 = min(rem, s1c["days"]); cc_base += mat*(s1c["pct"]/100)*(d1/30); rem -= d1
                if rem > 0: cc_base += mat*(s2c["pct"]/100)*(rem/30)
                cc_charges = round(cc_base, 2)
                cc_gst_amt = round(cc_charges * cc_gst / 100, 2)

        results.append({
            "Contract_No"      : cn,
            "GRN_No"           : row["GRN_No"],
            "Party_Bill_Date"  : row["Party_Bill_Date"],
            "Bales"            : int(bales),
            "Material_Amount"  : round(mat, 2),
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
            "Carry_Charges"    : cc_charges,
            "Carry_GST"        : cc_gst_amt,
        })

    return pd.DataFrame(results), per_bale_emd

def to_excel_bytes(result_df, cont, emd, pay, grn):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        cols = ["Contract_No","GRN_No","Party_Bill_Date","Bales",
                "Material_Amount","Per_Bale_EMD","EMD_Allocated","EMD_Date",
                "Net_Amount","Payment_Date","EMD_Days","EMD_Interest",
                "CD_Days","CD_Pct","Cash_Discount",
                "Late_Lift_Days","Late_Lifting_Chg","Late_Lifting_GST",
                "Carry_Charges","Carry_GST"]
        result_df[cols].to_excel(w, sheet_name="GRN Calculation", index=False)
        summary = result_df.groupby("Contract_No").agg(
            GRNs=("GRN_No","count"),
            Total_Bales=("Bales","sum"),
            Total_Material=("Material_Amount","sum"),
            Total_EMD_Allocated=("EMD_Allocated","sum"),
            Total_EMD_Interest=("EMD_Interest","sum"),
            Total_Cash_Discount=("Cash_Discount","sum"),
            Total_Late_Lifting=("Late_Lifting_Chg","sum"),
            Total_LL_GST=("Late_Lifting_GST","sum"),
            Total_Carry_Chg=("Carry_Charges","sum"),
            Total_CC_GST=("Carry_GST","sum"),
        ).reset_index()
        summary.to_excel(w, sheet_name="Summary", index=False)
        cont.to_excel(w, sheet_name="PUR CONT", index=False)
        emd.to_excel(w, sheet_name="EMD Payments", index=False)
        pay.to_excel(w, sheet_name="Final Payments", index=False)
        grn.to_excel(w, sheet_name="GRN Booking", index=False)
    buf.seek(0)
    return buf.getvalue()

# ─── UI ──────────────────────────────────────────────────────────────────────

st.title("🧮 CCI Working Calculation Utility")

tab1, tab2, tab3, tab4 = st.tabs(["📋 Masters", "📤 Upload & Calculate", "📊 Results", "📖 Formula Guide"])

# ══════════════════════════════ TAB 1: MASTERS ═══════════════════════════════
with tab1:
    left_col, right_col = st.columns([1.1, 0.9])

    with left_col:
        # ── PROJECT MASTER ──
        with st.expander("🏗️ Project Master", expanded=True):
            with st.form("project_form"):
                c1, c2 = st.columns(2)
                pname   = c1.text_input("Project Name *", placeholder="e.g. CCI-RAYGADA-2025")
                psess   = c2.text_input("Session", placeholder="e.g. 2024-25")
                c3, c4  = st.columns(2)
                pfrom   = c3.date_input("From Period *", value=None)
                pto     = c4.date_input("To Period *", value=None)
                pstatus = st.selectbox("Status", ["open","closed"])
                if st.form_submit_button("💾 Save Project", type="primary"):
                    if not pname or not pfrom or not pto:
                        st.error("Project Name, From and To Period required.")
                    else:
                        st.session_state.masters["projects"].append({
                            "name": pname, "session": psess,
                            "from_period": str(pfrom), "to_period": str(pto),
                            "status": pstatus
                        })
                        persist()
                        st.success(f"✅ Project '{pname}' saved!")
                        st.rerun()

        # ── CONTRACT MASTER ──
        with st.expander("📄 Contract Master", expanded=True):
            open_projects = [p["name"] for p in st.session_state.masters["projects"] if p["status"]=="open"]
            if not open_projects:
                st.warning("⚠️ Please add an Open project first.")
            else:
                with st.form("contract_form"):
                    c1, c2 = st.columns(2)
                    cproj  = c1.selectbox("Project *", open_projects)
                    cparty = c2.text_input("Party Name", placeholder="Party name")
                    c3, c4 = st.columns(2)
                    cno    = c3.text_input("Contract No *", placeholder="e.g. RAY-110425")
                    cdt    = c4.date_input("Contract Date", value=None)
                    c5, c6 = st.columns(2)
                    ceff   = c5.date_input("Effective Date", value=None)
                    cbales = c6.number_input("Contracted Bales", min_value=0, value=0)

                    st.markdown("**📌 EMD Slab**")
                    e1, e2 = st.columns(2)
                    emd_d  = e1.number_input("EMD Days", min_value=0, value=365)
                    emd_p  = e2.number_input("EMD Interest % p.a.", min_value=0.0, value=5.0, step=0.01)

                    st.markdown("**💸 Cash Discount Slabs**")
                    for i in range(1,4):
                        ca, cb, _ = st.columns([1,1,1])
                        ca.number_input(f"CD Slab {i} Days", min_value=0, value=0, key=f"cd{i}d")
                        cb.number_input(f"CD Slab {i} %", min_value=0.0, value=0.0, step=0.01, key=f"cd{i}p")
                    cd_gst = st.number_input("CD GST %", min_value=0.0, value=18.0, step=0.01)

                    st.markdown("**⏰ Late Lifting Slabs**")
                    ll_defaults = [(30,0.50),(30,0.75),(9999,1.00)]
                    for i,(dd,dp) in enumerate(ll_defaults,1):
                        la, lb, _ = st.columns([1,1,1])
                        la.number_input(f"LL Slab {i} Days", min_value=0, value=dd, key=f"ll{i}d")
                        lb.number_input(f"LL Slab {i} %/month", min_value=0.0, value=dp, step=0.01, key=f"ll{i}p")
                    ll_gst = st.number_input("LL GST %", min_value=0.0, value=5.0, step=0.01)

                    st.markdown("**🚛 Carrying Charges Slabs**")
                    cc_defaults = [(30,1.25),(30,1.35)]
                    for i,(dd,dp) in enumerate(cc_defaults,1):
                        ca2, cb2, _ = st.columns([1,1,1])
                        ca2.number_input(f"CC Slab {i} Days", min_value=0, value=dd, key=f"cc{i}d")
                        cb2.number_input(f"CC Slab {i} %/month", min_value=0.0, value=dp, step=0.01, key=f"cc{i}p")
                    cc_gst = st.number_input("CC GST %", min_value=0.0, value=5.0, step=0.01)

                    if st.form_submit_button("💾 Save Contract Master", type="primary"):
                        if not cno:
                            st.error("Contract No required.")
                        else:
                            contract = {
                                "project": cproj, "party": cparty,
                                "contract_no": cno,
                                "contract_date": str(cdt) if cdt else "",
                                "effective_date": str(ceff) if ceff else "",
                                "bales": cbales,
                                "emd_days": emd_d, "emd_percent": emd_p,
                                "cd_slabs": [{"days":st.session_state[f"cd{i}d"],"pct":st.session_state[f"cd{i}p"]} for i in range(1,4) if st.session_state[f"cd{i}d"]>0],
                                "cd_gst": cd_gst,
                                "ll_slabs": [{"days":st.session_state[f"ll{i}d"],"pct":st.session_state[f"ll{i}p"]} for i in range(1,4)],
                                "ll_gst": ll_gst,
                                "cc_slabs": [{"days":st.session_state[f"cc{i}d"],"pct":st.session_state[f"cc{i}p"]} for i in range(1,3)],
                                "cc_gst": cc_gst,
                            }
                            st.session_state.masters["contracts"].append(contract)
                            persist()
                            st.success(f"✅ Contract '{cno}' saved!")
                            st.rerun()

    with right_col:
        st.markdown("#### 🗂️ Saved Projects")
        projs = st.session_state.masters["projects"]
        if not projs:
            st.info("No projects yet.")
        else:
            for i, p in enumerate(projs):
                pill = "🟢 OPEN" if p["status"]=="open" else "🔴 CLOSED"
                with st.container(border=True):
                    r1, r2 = st.columns([3,1])
                    r1.markdown(f"**{p['name']}** &nbsp; {pill}  \n`{p['session'] or ''}` &nbsp;|&nbsp; {p['from_period']} → {p['to_period']}")
                    with r2:
                        if st.button("✏️", key=f"epj{i}", help="Extend To Date"):
                            st.session_state[f"edit_proj_{i}"] = True
                        if st.button("🗑", key=f"dpj{i}", help="Delete"):
                            st.session_state.masters["projects"].pop(i); persist(); st.rerun()
                        tog = "Close" if p["status"]=="open" else "Open"
                        if st.button(tog, key=f"tpj{i}"):
                            st.session_state.masters["projects"][i]["status"] = "closed" if p["status"]=="open" else "open"
                            persist(); st.rerun()
                    if st.session_state.get(f"edit_proj_{i}"):
                        nd = st.date_input("New To Date", key=f"nd_{i}")
                        if st.button("Save", key=f"spj{i}"):
                            st.session_state.masters["projects"][i]["to_period"] = str(nd)
                            persist(); st.session_state[f"edit_proj_{i}"] = False; st.rerun()

        st.markdown("#### 📋 Contract Masters")
        conts = st.session_state.masters["contracts"]
        if not conts:
            st.info("No contracts yet.")
        else:
            for i, c in enumerate(conts):
                with st.container(border=True):
                    rc1, rc2 = st.columns([4,1])
                    rc1.markdown(
                        f"**{c['contract_no']}** | {c.get('party','')}  \n"
                        f"`{c['project']}` &nbsp;|&nbsp; Bales: {c.get('bales','—')}  \n"
                        f"EMD: {c.get('emd_days','—')}d @ {c.get('emd_percent','—')}%  &nbsp;|&nbsp; "
                        f"LL GST: {c.get('ll_gst','—')}%"
                    )
                    if rc2.button("🗑", key=f"dc{i}", help="Delete"):
                        st.session_state.masters["contracts"].pop(i); persist(); st.rerun()

# ════════════════════════════ TAB 2: UPLOAD ══════════════════════════════════
with tab2:
    st.subheader("📤 Upload Excel & Run Calculations")
    conts = st.session_state.masters["contracts"]

    if not conts:
        st.warning("⚠️ Please save at least one Contract Master in the Masters tab first.")
    else:
        col1, col2 = st.columns([1.2, 0.8])
        with col1:
            cont_options = {f"{c['contract_no']} | {c.get('party','')}": c for c in conts}
            selected_lbl = st.selectbox("Select Contract Master *", list(cont_options.keys()))
            master_contract = cont_options[selected_lbl]

            st.info("📋 Excel must have 3 sheets:\n- **Sheet 1** — PUR CONT DETAILS\n- **Sheet 2** — EMD PAYMENT DETAILS\n- **Sheet 3** — GRN BOOKING")

            uploaded = st.file_uploader("Upload Excel File", type=["xlsx","xls"])

            if st.button("⚙️ Run Calculation", type="primary", disabled=(uploaded is None)):
                try:
                    with st.spinner("Calculating... please wait"):
                        file_bytes = uploaded.read()
                        cont_df, emd_df, pay_df, grn_df = parse_excel(file_bytes)
                        result_df, pbe = run_calculations(cont_df, emd_df, pay_df, grn_df, master_contract)
                        st.session_state["result_df"]   = result_df
                        st.session_state["result_cont"] = cont_df
                        st.session_state["result_emd"]  = emd_df
                        st.session_state["result_pay"]  = pay_df
                        st.session_state["result_grn"]  = grn_df
                    st.success(f"✅ Done! {len(result_df)} GRNs calculated. Go to **Results** tab.")
                except Exception as e:
                    st.error(f"❌ Error: {e}")
                    import traceback; st.code(traceback.format_exc())

        with col2:
            st.markdown("**Expected Column Headers**")
            st.markdown("""
**Sheet 1 – PUR CONT DETAILS**
`Contract No. | EFFECTIVE DATE | BALES | BRANCH-CCI`

**Sheet 2 – EMD PAYMENT DETAILS**
`Contract No. | EMD DATE | EMD AMOUNT | [blank] | Contract No. | PAYMENT | PAYMENT DATE`

**Sheet 3 – GRN BOOKING**
`contract no | Party Bill Date | GRN | Accepted Qty(AUM) | Accepted Qty | Material Amount | IGST | Party Bill Amount | Other Amount | FINAL INDENT DATE`
""")

# ════════════════════════════ TAB 3: RESULTS ══════════════════════════════════
with tab3:
    st.subheader("📊 Calculation Results")

    if "result_df" not in st.session_state:
        st.info("No results yet. Please upload an Excel file in the Upload tab.")
    else:
        result_df = st.session_state["result_df"]

        # Metrics
        m1,m2,m3,m4,m5 = st.columns(5)
        m1.metric("Total GRNs",      len(result_df))
        m2.metric("Material Amt",    f"₹{result_df['Material_Amount'].sum():,.0f}")
        m3.metric("EMD Interest",    f"₹{result_df['EMD_Interest'].sum():,.2f}")
        m4.metric("Late Lifting",    f"₹{result_df['Late_Lifting_Chg'].sum():,.2f}")
        m5.metric("Carry Charges",   f"₹{result_df['Carry_Charges'].sum():,.2f}")

        # Download button
        excel_bytes = to_excel_bytes(
            result_df,
            st.session_state["result_cont"],
            st.session_state["result_emd"],
            st.session_state["result_pay"],
            st.session_state["result_grn"],
        )
        st.download_button(
            "⬇️ Download Excel Report",
            data=excel_bytes,
            file_name="CCI_Calculation_Output.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.markdown("---")
        st.markdown("#### GRN-Wise Detail")

        disp = result_df.copy()
        for dc in ["Party_Bill_Date","EMD_Date","Payment_Date"]:
            disp[dc] = pd.to_datetime(disp[dc], errors="coerce").dt.strftime("%d-%b-%Y").fillna("—")

        disp = disp.rename(columns={
            "Contract_No":"Contract","GRN_No":"GRN No","Party_Bill_Date":"Lift Date",
            "Bales":"Bales","Material_Amount":"Material Amt","Per_Bale_EMD":"Per Bale EMD",
            "EMD_Allocated":"EMD Alloc","EMD_Date":"EMD Date","Net_Amount":"Net Amt",
            "Payment_Date":"Pay Date","EMD_Days":"Days","EMD_Interest":"EMD Interest",
            "Cash_Discount":"Cash Disc","Late_Lift_Days":"Late Days",
            "Late_Lifting_Chg":"LL Chg","Late_Lifting_GST":"LL GST",
            "Carry_Charges":"Carry Chg","Carry_GST":"CC GST"
        })
        show_cols = ["Contract","GRN No","Lift Date","Bales","Material Amt",
                     "Per Bale EMD","EMD Alloc","EMD Date","Net Amt","Pay Date",
                     "Days","EMD Interest","Cash Disc","Late Days","LL Chg","LL GST",
                     "Carry Chg","CC GST"]
        st.dataframe(disp[show_cols], use_container_width=True, hide_index=True)

        # Summary
        st.markdown("#### Contract-wise Summary")
        summary = result_df.groupby("Contract_No").agg(
            GRNs=("GRN_No","count"),
            Total_Bales=("Bales","sum"),
            Total_Material=("Material_Amount","sum"),
            Total_EMD_Allocated=("EMD_Allocated","sum"),
            Total_EMD_Interest=("EMD_Interest","sum"),
            Total_Cash_Disc=("Cash_Discount","sum"),
            Total_LL_Chg=("Late_Lifting_Chg","sum"),
            Total_LL_GST=("Late_Lifting_GST","sum"),
            Total_CC_Chg=("Carry_Charges","sum"),
            Total_CC_GST=("Carry_GST","sum"),
        ).reset_index()
        st.dataframe(summary, use_container_width=True, hide_index=True)

# ════════════════════════════ TAB 4: HELP ════════════════════════════════════
with tab4:
    st.subheader("📖 Formula & Logic Guide")
    col1, col2 = st.columns(2)
    with col1:
        with st.expander("📌 Per Bale EMD", expanded=True):
            st.code("Per Bale EMD = Total EMD Payment ÷ Contracted Bales", language="text")
            st.caption("FIFO allocated against each GRN based on bales received.")
        with st.expander("💹 EMD Interest", expanded=True):
            st.code("EMD Interest = ((EMD Allocated × EMD % p.a.) ÷ 365) × (Payment Date − EMD Date)", language="text")
            st.caption("EMD Date = max voucher date of EMD used. Payment Date = max payment voucher date used.")
        with st.expander("💸 Cash Discount", expanded=True):
            st.code("If Payment is made before Lifting Date − CD Days:\n  CD = Material Amt × CD% × (Free Days / 365)", language="text")
    with col2:
        with st.expander("⏰ Late Lifting Charges", expanded=True):
            st.code("""Free Period = Payment Date + 15 days
If Lifting Date > Free Period:
  Late Days = Lifting Date − Free Period
  Slab 1 (first 30d) : Amt × 0.50%/month
  Slab 2 (next  30d) : Amt × 0.75%/month
  Slab 3 (beyond)    : Amt × 1.00%/month
  + GST as applicable""", language="text")
        with st.expander("🚛 Carrying Charges", expanded=True):
            st.code("""If Lifting Date > Final Indent Date:
  Carry Days = min(days, 60 days max)
  First 30d  : Amt × 1.25%/month
  Beyond 30d : Amt × 1.35%/month
  + GST as applicable""", language="text")
