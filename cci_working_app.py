"""
CCI WORKING CALCULATION UTILITY
================================
Run:  python cci_working_app.py
Then open:  http://localhost:5000
"""

import io, json, base64, webbrowser, threading, os
from datetime import datetime, date

import pandas as pd
from flask import Flask, render_template_string, request, jsonify, send_file

app = Flask(__name__)

# ─── MASTER STORE (in-memory, JSON file for persistence) ──────────────────────
MASTER_FILE = "cci_masters.json"

def load_masters():
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE) as f:
            return json.load(f)
    return {"projects": [], "contracts": []}

def save_masters(data):
    with open(MASTER_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ─── CALCULATION LOGIC ─────────────────────────────────────────────────────────

def parse_excel(file_bytes):
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    sheets = xl.sheet_names

    # Sheet 1: PUR CONT DETAILS
    cont = pd.read_excel(xl, sheet_name=sheets[0], header=0)
    cont.columns = ["Contract_No", "Effective_Date", "Bales", "Branch"]
    cont = cont.dropna(subset=["Contract_No"])
    cont["Effective_Date"] = pd.to_datetime(cont["Effective_Date"], errors="coerce")
    cont["Bales"] = pd.to_numeric(cont["Bales"], errors="coerce")

    # Sheet 2: EMD PAYMENT DETAILS
    raw2 = pd.read_excel(xl, sheet_name=sheets[1], header=None)
    emd = raw2.iloc[1:, [0,1,2]].copy()
    emd.columns = ["Contract_No","EMD_Date","EMD_Amount"]
    emd = emd.dropna(subset=["Contract_No","EMD_Amount"])
    emd = emd[~emd["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    emd["EMD_Date"]   = pd.to_datetime(emd["EMD_Date"], errors="coerce")
    emd["EMD_Amount"] = pd.to_numeric(emd["EMD_Amount"], errors="coerce")
    emd = emd.dropna(subset=["EMD_Amount"]).reset_index(drop=True)

    pay = raw2.iloc[1:, [4,5,6]].copy()
    pay.columns = ["Contract_No","Payment_Date","Payment_Amount"]
    pay = pay.dropna(subset=["Contract_No","Payment_Amount"])
    pay = pay[~pay["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    pay["Payment_Date"]   = pd.to_datetime(pay["Payment_Date"], errors="coerce")
    pay["Payment_Amount"] = pd.to_numeric(pay["Payment_Amount"], errors="coerce")
    pay = pay.dropna(subset=["Payment_Amount"]).reset_index(drop=True)

    # Sheet 3: GRN BOOKING
    grn = pd.read_excel(xl, sheet_name=sheets[2], header=0)
    grn.columns = ["Contract_No","Party_Bill_Date","GRN_No",
                   "Accepted_Qty_AUM","Accepted_Qty","Material_Amount",
                   "IGST","Party_Bill_Amount","Other_Amount","Final_Indent_Date"]
    grn = grn.dropna(subset=["Contract_No"])
    grn["Party_Bill_Date"]   = pd.to_datetime(grn["Party_Bill_Date"], errors="coerce")
    grn["Final_Indent_Date"] = pd.to_datetime(grn["Final_Indent_Date"], errors="coerce")
    grn["Material_Amount"]   = pd.to_numeric(grn["Material_Amount"], errors="coerce")
    grn["Accepted_Qty_AUM"]  = pd.to_numeric(grn["Accepted_Qty_AUM"], errors="coerce")
    grn = grn.sort_values("Party_Bill_Date").reset_index(drop=True)

    return cont, emd, pay, grn

def run_calculations(cont, emd, pay, grn, master_contract):
    emd_rate   = float(master_contract.get("emd_percent", 5.0))
    cd_slabs   = master_contract.get("cd_slabs", [])
    ll_slabs   = master_contract.get("ll_slabs", [])
    ll_gst     = float(master_contract.get("ll_gst", 5.0))
    cc_slabs   = master_contract.get("cc_slabs", [])
    cc_gst     = float(master_contract.get("cc_gst", 5.0))

    # per bale EMD
    total_emd_map = emd.groupby("Contract_No")["EMD_Amount"].sum().to_dict()
    per_bale_emd  = {}
    for _, r in cont.iterrows():
        cn = r["Contract_No"]
        b  = r["Bales"] if r["Bales"] > 0 else 1
        per_bale_emd[cn] = total_emd_map.get(cn, 0) / b

    # EMD pool
    emd_pool = {}
    for cn, g in emd.groupby("Contract_No"):
        emd_pool[cn] = g[["EMD_Date","EMD_Amount"]].copy().reset_index(drop=True)
        emd_pool[cn]["Remaining"] = emd_pool[cn]["EMD_Amount"].astype(float)

    # Payment pool
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
        # CD based on: how many free days remain between payment_date and lifting_date
        cd_amount, cd_days_used, cd_pct_used = 0.0, 0, 0.0
        if cd_slabs and not pd.isna(pay_date) and not pd.isna(row["Party_Bill_Date"]):
            lift_date = row["Party_Bill_Date"]
            diff_days = (lift_date - pay_date).days   # days before lifting payment was made
            for slab in sorted(cd_slabs, key=lambda x: -x["days"]):  # highest slab first
                if diff_days >= slab["days"] and slab["days"] > 0:
                    cd_pct_used  = slab["pct"]
                    cd_days_used = diff_days
                    cd_amount    = round((mat * cd_pct_used / 100) * (diff_days / 365), 2)
                    break

        # ── LATE LIFTING CHARGES ──
        # If lifting (Party_Bill_Date) > payment_date + 15 days → late lifting
        ll_charges, ll_gst_amt = 0.0, 0.0
        if not pd.isna(pay_date) and not pd.isna(row["Party_Bill_Date"]):
            free_end    = pay_date + pd.Timedelta(days=15)
            lift_date   = row["Party_Bill_Date"]
            if lift_date > free_end:
                late_days = (lift_date - free_end).days
                ll_base   = 0.0
                remaining = late_days
                # Slab 1: first 30 days
                s1 = ll_slabs[0] if len(ll_slabs) > 0 else {"days":30,"pct":0.50}
                s2 = ll_slabs[1] if len(ll_slabs) > 1 else {"days":30,"pct":0.75}
                s3 = ll_slabs[2] if len(ll_slabs) > 2 else {"days":9999,"pct":1.00}
                d1 = min(remaining, s1["days"])
                ll_base  += mat * (s1["pct"]/100) * (d1/30); remaining -= d1
                if remaining > 0:
                    d2 = min(remaining, s2["days"])
                    ll_base += mat * (s2["pct"]/100) * (d2/30); remaining -= d2
                if remaining > 0:
                    ll_base += mat * (s3["pct"]/100) * (remaining/30)
                ll_charges  = round(ll_base, 2)
                ll_gst_amt  = round(ll_charges * ll_gst / 100, 2)

        # ── CARRYING CHARGES ──
        cc_charges, cc_gst_amt = 0.0, 0.0
        if not pd.isna(row["Final_Indent_Date"]) and not pd.isna(row["Party_Bill_Date"]):
            lifting_period_end = row["Final_Indent_Date"]
            lift_date          = row["Party_Bill_Date"]
            if lift_date > lifting_period_end:
                carry_days = (lift_date - lifting_period_end).days
                carry_days = min(carry_days, 60)   # max 60 days as per contract
                s1c = cc_slabs[0] if len(cc_slabs) > 0 else {"days":30,"pct":1.25}
                s2c = cc_slabs[1] if len(cc_slabs) > 1 else {"days":30,"pct":1.35}
                cc_base = 0.0
                rem = carry_days
                d1  = min(rem, s1c["days"])
                cc_base += mat * (s1c["pct"]/100) * (d1/30); rem -= d1
                if rem > 0:
                    cc_base += mat * (s2c["pct"]/100) * (rem/30)
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
            "Late_Lift_Days"   : int((row["Party_Bill_Date"] - (pay_date + pd.Timedelta(days=15))).days)
                                  if (not pd.isna(pay_date) and not pd.isna(row["Party_Bill_Date"])
                                      and row["Party_Bill_Date"] > pay_date + pd.Timedelta(days=15)) else 0,
            "Late_Lifting_Chg" : ll_charges,
            "Late_Lifting_GST" : ll_gst_amt,
            "Carry_Charges"    : cc_charges,
            "Carry_GST"        : cc_gst_amt,
        })

    return pd.DataFrame(results), per_bale_emd


def df_to_excel_bytes(result_df, cont, emd, pay, grn):
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
            Total_EMD=("EMD_Allocated","sum"),
            Total_Payment=("Net_Amount","sum"),
            Total_EMD_Interest=("EMD_Interest","sum"),
            Total_Cash_Discount=("Cash_Discount","sum"),
            Total_LL_Charges=("Late_Lifting_Chg","sum"),
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


# ─── HTML TEMPLATE ─────────────────────────────────────────────────────────────

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CCI Working Calculation Utility</title>
<style>
:root{
  --bg:#f4f6fb;--card:#fff;--primary:#1a56db;--primary-dark:#1341a8;
  --success:#0e9f6e;--danger:#e02424;--warning:#ff8c00;
  --border:#e5e7eb;--text:#111827;--muted:#6b7280;--radius:10px;
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',Arial,sans-serif}
body{background:var(--bg);color:var(--text);min-height:100vh}
/* ── TOP BAR ── */
.topbar{background:var(--primary);color:#fff;display:flex;align-items:center;
  gap:12px;padding:0 24px;height:52px;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.topbar h1{font-size:17px;font-weight:600;flex:1}
.topbar .ver{font-size:11px;opacity:.7;background:rgba(255,255,255,.15);
  padding:2px 8px;border-radius:20px}
/* ── NAV ── */
.nav{background:#fff;border-bottom:1px solid var(--border);display:flex;
  gap:4px;padding:0 24px;position:sticky;top:0;z-index:100}
.nav button{background:none;border:none;cursor:pointer;font-size:14px;
  padding:14px 16px;color:var(--muted);border-bottom:2px solid transparent;
  font-weight:500;transition:.2s}
.nav button.active{color:var(--primary);border-bottom-color:var(--primary)}
.nav button:hover:not(.active){color:var(--text)}
/* ── PANELS ── */
.panel{display:none;padding:24px;max-width:1200px;margin:0 auto}
.panel.active{display:block}
/* ── CARDS ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;margin-bottom:18px}
.card-title{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:14px}
/* ── GRID ── */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
/* ── FORM ── */
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:12px;color:var(--muted);font-weight:500}
.field input,.field select{padding:8px 10px;border:1px solid var(--border);
  border-radius:7px;font-size:14px;background:#fff;color:var(--text);width:100%}
.field input:focus,.field select:focus{outline:none;border-color:var(--primary);
  box-shadow:0 0 0 3px rgba(26,86,219,.08)}
.slab-block{background:#f9fafb;border:1px solid var(--border);border-radius:8px;
  padding:12px;margin-bottom:10px}
.slab-title{font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px}
.slab-row{display:grid;grid-template-columns:120px 1fr 1fr;gap:8px;margin-bottom:6px;
  align-items:center}
.slab-row label{font-size:12px;color:var(--muted)}
/* ── BUTTONS ── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border-radius:8px;
  font-size:14px;font-weight:500;cursor:pointer;border:1px solid transparent;
  transition:.15s}
.btn-primary{background:var(--primary);color:#fff;border-color:var(--primary)}
.btn-primary:hover{background:var(--primary-dark)}
.btn-success{background:var(--success);color:#fff}
.btn-danger{background:var(--danger);color:#fff}
.btn-outline{background:#fff;border-color:var(--border);color:var(--text)}
.btn-outline:hover{background:#f9fafb}
.actions{display:flex;gap:10px;margin-top:14px}
/* ── STATUS PILLS ── */
.pill{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600}
.pill-open{background:#d1fae5;color:#065f46}
.pill-closed{background:#fee2e2;color:#991b1b}
/* ── METRIC CARDS ── */
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.metric{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px;text-align:center}
.metric .val{font-size:22px;font-weight:700;color:var(--primary)}
.metric .lbl{font-size:11px;color:var(--muted);margin-top:3px}
/* ── TABLE ── */
.tbl-wrap{overflow-x:auto;border-radius:var(--radius);border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}
th{background:#f9fafb;padding:10px 12px;text-align:left;font-size:11px;
  font-weight:600;color:var(--muted);border-bottom:1px solid var(--border);
  white-space:nowrap;text-transform:uppercase}
td{padding:9px 12px;border-bottom:1px solid #f3f4f6;white-space:nowrap}
tr:hover td{background:#fafbff}
tr:last-child td{border-bottom:none}
.num{text-align:right}
/* ── UPLOAD ZONE ── */
.upload-zone{border:2px dashed var(--border);border-radius:var(--radius);
  padding:36px;text-align:center;cursor:pointer;transition:.2s;
  color:var(--muted);font-size:14px}
.upload-zone:hover,.upload-zone.drag{border-color:var(--primary);
  background:rgba(26,86,219,.03)}
.upload-zone input{display:none}
/* ── ALERT ── */
.alert{padding:12px 16px;border-radius:8px;font-size:13px;margin-bottom:14px}
.alert-info{background:#eff6ff;border:1px solid #bfdbfe;color:#1e40af}
.alert-success{background:#f0fdf4;border:1px solid #bbf7d0;color:#166534}
.alert-danger{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}
.alert-warning{background:#fffbeb;border:1px solid #fde68a;color:#92400e}
/* ── SECTION HEADING ── */
.sec-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.sec-head h2{font-size:18px;font-weight:600}
/* ── SPINNER ── */
.spinner{display:none;width:20px;height:20px;border:3px solid #e5e7eb;
  border-top-color:var(--primary);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* ── BADGE ── */
.badge{background:var(--primary);color:#fff;font-size:10px;padding:1px 6px;
  border-radius:10px;font-weight:600;margin-left:4px}
/* ── FORMULA BOX ── */
.formula{background:#f8fafc;border-left:4px solid var(--primary);
  padding:10px 14px;font-size:13px;border-radius:0 8px 8px 0;margin:8px 0;
  font-family:monospace;color:#374151}
</style>
</head>
<body>

<div class="topbar">
  <span style="font-size:22px">🧮</span>
  <h1>CCI Working Calculation Utility</h1>
  <span class="ver">v1.0</span>
</div>

<div class="nav">
  <button class="active" onclick="tab('masters')">📋 Masters</button>
  <button onclick="tab('upload')">📤 Upload & Calculate</button>
  <button onclick="tab('results')">📊 Results <span class="badge" id="grn-badge">0</span></button>
  <button onclick="tab('help')">📖 Formula Guide</button>
</div>

<!-- ═══════════════════════════════ MASTERS ═══════════════════════════════ -->
<div class="panel active" id="panel-masters">

  <div class="g2" style="gap:20px">

    <!-- LEFT: Project + Contract Master Form -->
    <div>
      <div class="card">
        <div class="card-title">🏗️ Project Master</div>
        <div class="g2" style="margin-bottom:12px">
          <div class="field">
            <label>Project Name *</label>
            <input id="proj-name" placeholder="e.g. CCI-RAYGADA-2025">
          </div>
          <div class="field">
            <label>Session</label>
            <input id="proj-session" placeholder="e.g. 2024-25">
          </div>
        </div>
        <div class="g2" style="margin-bottom:12px">
          <div class="field">
            <label>From Period *</label>
            <input id="proj-from" type="date">
          </div>
          <div class="field">
            <label>To Period *</label>
            <input id="proj-to" type="date">
          </div>
        </div>
        <div class="field" style="margin-bottom:12px">
          <label>Status</label>
          <select id="proj-status">
            <option value="open">Open</option>
            <option value="closed">Closed</option>
          </select>
        </div>
        <div class="actions">
          <button class="btn btn-primary" onclick="saveProject()">💾 Save Project</button>
          <button class="btn btn-outline" onclick="clearProjectForm()">✕ Clear</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">📄 Contract Master</div>
        <div class="g2" style="margin-bottom:12px">
          <div class="field">
            <label>Project *</label>
            <select id="cont-project"><option value="">-- Select Project --</option></select>
          </div>
          <div class="field">
            <label>Party Name *</label>
            <input id="cont-party" placeholder="Party name">
          </div>
        </div>
        <div class="g2" style="margin-bottom:12px">
          <div class="field">
            <label>Contract No *</label>
            <input id="cont-no" placeholder="e.g. RAY-110425">
          </div>
          <div class="field">
            <label>Contract Date</label>
            <input id="cont-date" type="date">
          </div>
        </div>
        <div class="g2" style="margin-bottom:12px">
          <div class="field">
            <label>Effective Date</label>
            <input id="cont-effective" type="date">
          </div>
          <div class="field">
            <label>Contracted Bales</label>
            <input id="cont-bales" type="number" placeholder="600">
          </div>
        </div>

        <!-- EMD Slab -->
        <div class="slab-block">
          <div class="slab-title">📌 EMD Slab</div>
          <div class="g2">
            <div class="field"><label>Days</label><input id="emd-days" type="number" placeholder="365"></div>
            <div class="field"><label>Interest % p.a.</label><input id="emd-pct" type="number" step="0.01" placeholder="5.00"></div>
          </div>
        </div>

        <!-- Cash Discount Slabs -->
        <div class="slab-block">
          <div class="slab-title">💸 Cash Discount Slabs</div>
          <div class="g3" style="margin-bottom:6px">
            <div class="field"><label>Slab 1 Days</label><input id="cd1-days" type="number" placeholder="30"></div>
            <div class="field"><label>Slab 1 %</label><input id="cd1-pct" type="number" step="0.01" placeholder="0.50"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="g3" style="margin-bottom:6px">
            <div class="field"><label>Slab 2 Days</label><input id="cd2-days" type="number" placeholder="60"></div>
            <div class="field"><label>Slab 2 %</label><input id="cd2-pct" type="number" step="0.01" placeholder="0.75"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="g3" style="margin-bottom:8px">
            <div class="field"><label>Slab 3 Days</label><input id="cd3-days" type="number" placeholder="90"></div>
            <div class="field"><label>Slab 3 %</label><input id="cd3-pct" type="number" step="0.01" placeholder="1.00"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="field"><label>GST %</label><input id="cd-gst" type="number" step="0.01" placeholder="18.00" style="max-width:160px"></div>
        </div>

        <!-- Late Lifting Slabs -->
        <div class="slab-block">
          <div class="slab-title">⏰ Late Lifting Slabs</div>
          <div class="g3" style="margin-bottom:6px">
            <div class="field"><label>Slab 1 Days</label><input id="ll1-days" type="number" value="30"></div>
            <div class="field"><label>Slab 1 %/month</label><input id="ll1-pct" type="number" step="0.01" value="0.50"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="g3" style="margin-bottom:6px">
            <div class="field"><label>Slab 2 Days</label><input id="ll2-days" type="number" value="30"></div>
            <div class="field"><label>Slab 2 %/month</label><input id="ll2-pct" type="number" step="0.01" value="0.75"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="g3" style="margin-bottom:8px">
            <div class="field"><label>Slab 3 Days</label><input id="ll3-days" type="number" value="9999"></div>
            <div class="field"><label>Slab 3 %/month</label><input id="ll3-pct" type="number" step="0.01" value="1.00"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="field"><label>GST %</label><input id="ll-gst" type="number" step="0.01" placeholder="5.00" style="max-width:160px"></div>
        </div>

        <!-- Carrying Charges Slabs -->
        <div class="slab-block">
          <div class="slab-title">🚛 Carrying Charges Slabs</div>
          <div class="g3" style="margin-bottom:6px">
            <div class="field"><label>Slab 1 Days</label><input id="cc1-days" type="number" value="30"></div>
            <div class="field"><label>Slab 1 %/month</label><input id="cc1-pct" type="number" step="0.01" value="1.25"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="g3" style="margin-bottom:8px">
            <div class="field"><label>Slab 2 Days</label><input id="cc2-days" type="number" value="30"></div>
            <div class="field"><label>Slab 2 %/month</label><input id="cc2-pct" type="number" step="0.01" value="1.35"></div>
            <div class="field" style="visibility:hidden"></div>
          </div>
          <div class="field"><label>GST %</label><input id="cc-gst" type="number" step="0.01" placeholder="5.00" style="max-width:160px"></div>
        </div>

        <div class="actions">
          <button class="btn btn-primary" onclick="saveContract()">💾 Save Contract Master</button>
          <button class="btn btn-outline" onclick="clearContractForm()">✕ Clear</button>
        </div>
      </div>
    </div>

    <!-- RIGHT: Saved Masters Preview -->
    <div>
      <div class="card">
        <div class="card-title">🗂️ Saved Projects</div>
        <div id="proj-list"><p style="color:var(--muted);font-size:13px">No projects saved yet.</p></div>
      </div>
      <div class="card">
        <div class="card-title">📋 Contract Masters Preview</div>
        <div id="cont-list"><p style="color:var(--muted);font-size:13px">No contracts saved yet.</p></div>
      </div>
    </div>

  </div>
</div>

<!-- ═══════════════════════════════ UPLOAD ═══════════════════════════════ -->
<div class="panel" id="panel-upload">
  <div class="sec-head"><h2>📤 Upload Excel & Run Calculations</h2></div>

  <div class="g2" style="gap:20px">
    <div>
      <div class="card">
        <div class="card-title">Select Contract Master for this Upload</div>
        <div class="field" style="margin-bottom:14px">
          <label>Contract *</label>
          <select id="upload-contract"><option value="">-- Select Contract --</option></select>
        </div>

        <div class="card-title" style="margin-top:4px">Upload Excel File</div>
        <div class="alert alert-info">
          📋 Excel must have 3 sheets:<br>
          <strong>Sheet 1</strong> — PUR CONT DETAILS &nbsp;|&nbsp;
          <strong>Sheet 2</strong> — EMD PAYMENT DETAILS &nbsp;|&nbsp;
          <strong>Sheet 3</strong> — GRN BOOKING
        </div>

        <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-inp').click()"
          ondragover="event.preventDefault();this.classList.add('drag')"
          ondragleave="this.classList.remove('drag')"
          ondrop="handleDrop(event)">
          <input type="file" id="file-inp" accept=".xlsx,.xls" onchange="fileSelected(this)">
          <div style="font-size:32px;margin-bottom:8px">📂</div>
          <div>Click to browse or drag & drop your Excel file</div>
          <div style="font-size:12px;color:var(--muted);margin-top:4px">Supports .xlsx / .xls</div>
          <div id="file-name" style="margin-top:10px;font-weight:600;color:var(--primary)"></div>
        </div>

        <div class="actions" style="margin-top:14px">
          <button class="btn btn-primary" onclick="runCalc()" id="calc-btn">⚙️ Run Calculation</button>
          <div class="spinner" id="spinner"></div>
        </div>
        <div id="upload-msg"></div>
      </div>
    </div>

    <div>
      <div class="card">
        <div class="card-title">📝 Expected Excel Format</div>
        <p style="font-size:13px;color:var(--muted);margin-bottom:10px">
          Your Excel file should have exactly these 3 sheets:
        </p>
        <div style="font-size:13px">
          <div style="margin-bottom:8px"><strong>Sheet 1 – PUR CONT DETAILS</strong></div>
          <code style="font-size:11px;color:#374151">Contract No. | EFFECTIVE DATE | BALES | BRANCH-CCI</code>
          <hr style="margin:10px 0;border-color:#f3f4f6">
          <div style="margin-bottom:8px"><strong>Sheet 2 – EMD PAYMENT DETAILS</strong></div>
          <code style="font-size:11px;color:#374151">Contract No. | EMD DATE | EMD AMOUNT | [blank] | Contract No. | PAYMENT | PAYMENT DATE</code>
          <hr style="margin:10px 0;border-color:#f3f4f6">
          <div style="margin-bottom:8px"><strong>Sheet 3 – GRN BOOKING</strong></div>
          <code style="font-size:11px;color:#374151">contract no | Party Bill Date | GRN | Accepted Qty(AUM) | Accepted Qty | Material Amount | IGST | Party Bill Amount | Other Amount | FINAL INDENT DATE</code>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════ RESULTS ═══════════════════════════════ -->
<div class="panel" id="panel-results">
  <div class="sec-head">
    <h2>📊 Calculation Results</h2>
    <button class="btn btn-success" onclick="downloadExcel()" id="dl-btn" style="display:none">
      ⬇️ Download Excel Report
    </button>
  </div>

  <div class="metrics" id="metrics-row">
    <div class="metric"><div class="val" id="m-grns">—</div><div class="lbl">Total GRNs</div></div>
    <div class="metric"><div class="val" id="m-mat">—</div><div class="lbl">Total Material Amt (₹)</div></div>
    <div class="metric"><div class="val" id="m-emd-int">—</div><div class="lbl">EMD Interest (₹)</div></div>
    <div class="metric"><div class="val" id="m-ll">—</div><div class="lbl">Late Lifting Chg (₹)</div></div>
  </div>

  <div id="result-alert"></div>

  <div class="card">
    <div class="card-title">GRN-Wise Detail</div>
    <div class="tbl-wrap">
      <table id="result-table">
        <thead>
          <tr>
            <th>Contract</th><th>GRN No</th><th>Lift Date</th><th class="num">Bales</th>
            <th class="num">Material Amt</th><th class="num">Per Bale EMD</th>
            <th class="num">EMD Alloc</th><th>EMD Date</th>
            <th class="num">Net Amt</th><th>Pay Date</th>
            <th class="num">Days</th><th class="num">EMD Interest</th>
            <th class="num">Cash Disc</th><th class="num">Late Lift Chg</th>
            <th class="num">LL GST</th><th class="num">Carry Chg</th><th class="num">CC GST</th>
          </tr>
        </thead>
        <tbody id="result-body">
          <tr><td colspan="17" style="text-align:center;color:var(--muted);padding:30px">
            No results yet. Upload an Excel file to calculate.
          </td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════ HELP ═══════════════════════════════ -->
<div class="panel" id="panel-help">
  <div class="sec-head"><h2>📖 Formula & Logic Guide</h2></div>
  <div class="g2" style="gap:18px">
    <div>
      <div class="card">
        <div class="card-title">📌 Per Bale EMD</div>
        <div class="formula">Per Bale EMD = Total EMD Payment ÷ Contracted Bales</div>
        <p style="font-size:13px;color:var(--muted)">FIFO allocated against each GRN based on bales received.</p>
      </div>
      <div class="card">
        <div class="card-title">💹 EMD Interest</div>
        <div class="formula">EMD Interest = ((EMD Allocated × EMD % p.a.) ÷ 365) × (Payment Date − EMD Date)</div>
        <p style="font-size:13px;color:var(--muted)">EMD date = max voucher date of EMD used. Payment date = max voucher date of payment used.</p>
      </div>
      <div class="card">
        <div class="card-title">💸 Cash Discount</div>
        <div class="formula">If Payment Date &lt; Lifting Date − CD Days:
  CD = Material Amount × CD% × (Free Days Remaining ÷ 365)</div>
        <p style="font-size:13px;color:var(--muted)">Slab with most free days remaining is applied.</p>
      </div>
    </div>
    <div>
      <div class="card">
        <div class="card-title">⏰ Late Lifting Charges</div>
        <div class="formula">Free Period = Payment Date + 15 days
If Lifting Date &gt; Free Period:
  Late Days = Lifting Date − Free Period
  Slab 1 (first 30d): Amount × 0.50%/month
  Slab 2 (next 30d) : Amount × 0.75%/month
  Slab 3 (beyond)   : Amount × 1.00%/month
  + GST as applicable</div>
      </div>
      <div class="card">
        <div class="card-title">🚛 Carrying Charges</div>
        <div class="formula">If Lifting Date &gt; Final Indent Date:
  Carry Days = min(Lifting − Indent Date, 60)
  First 30d : Amount × 1.25%/month
  Beyond 30d: Amount × 1.35%/month
  + GST as applicable</div>
        <p style="font-size:13px;color:var(--muted)">Maximum 60 days carrying period as per contract.</p>
      </div>
    </div>
  </div>
</div>

<script>
// ─── STATE ────────────────────────────────────────────────────────────────────
let masters = {projects:[], contracts:[]};
let uploadedFile = null;
let resultData = null;
let excelBase64 = null;

// ─── TABS ─────────────────────────────────────────────────────────────────────
function tab(name) {
  document.querySelectorAll('.nav button').forEach((b,i)=>{
    b.classList.toggle('active', ['masters','upload','results','help'][i]===name);
  });
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  if(name==='upload') refreshUploadDropdown();
}

// ─── MASTERS ──────────────────────────────────────────────────────────────────
function loadMasters() {
  fetch('/api/masters').then(r=>r.json()).then(d=>{
    masters=d; renderProjectList(); renderContractList(); refreshProjectDropdown(); refreshUploadDropdown();
  });
}

function saveProject() {
  const name=v('proj-name'), session=v('proj-session'),
        from=v('proj-from'), to=v('proj-to'), status=v('proj-status');
  if(!name||!from||!to){alert('Project Name, From Period and To Period are required.');return;}
  fetch('/api/save_project',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,session,from_period:from,to_period:to,status})})
  .then(r=>r.json()).then(d=>{masters=d;renderProjectList();refreshProjectDropdown();clearProjectForm();});
}

function saveContract() {
  const proj=v('cont-project');
  if(!proj){alert('Please select a project.');return;}
  const no=v('cont-no');
  if(!no){alert('Contract No is required.');return;}
  const data={
    project:proj, party:v('cont-party'), contract_no:no,
    contract_date:v('cont-date'), effective_date:v('cont-effective'),
    bales:v('cont-bales'),
    emd_days:v('emd-days'), emd_percent:v('emd-pct'),
    cd_slabs:[
      {days:n('cd1-days'),pct:n('cd1-pct')},
      {days:n('cd2-days'),pct:n('cd2-pct')},
      {days:n('cd3-days'),pct:n('cd3-pct')}
    ].filter(s=>s.days>0),
    cd_gst:n('cd-gst'),
    ll_slabs:[
      {days:n('ll1-days'),pct:n('ll1-pct')},
      {days:n('ll2-days'),pct:n('ll2-pct')},
      {days:n('ll3-days'),pct:n('ll3-pct')}
    ].filter(s=>s.days>0),
    ll_gst:n('ll-gst'),
    cc_slabs:[
      {days:n('cc1-days'),pct:n('cc1-pct')},
      {days:n('cc2-days'),pct:n('cc2-pct')}
    ].filter(s=>s.days>0),
    cc_gst:n('cc-gst'),
  };
  fetch('/api/save_contract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(r=>r.json()).then(d=>{masters=d;renderContractList();refreshUploadDropdown();clearContractForm();});
}

function deleteProject(i){if(confirm('Delete project?'))
  fetch('/api/delete_project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})})
  .then(r=>r.json()).then(d=>{masters=d;renderProjectList();refreshProjectDropdown();});}

function deleteContract(i){if(confirm('Delete contract master?'))
  fetch('/api/delete_contract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})})
  .then(r=>r.json()).then(d=>{masters=d;renderContractList();refreshUploadDropdown();});}

function toggleProjectStatus(i){
  const proj=masters.projects[i];
  proj.status = proj.status==='open'?'closed':'open';
  fetch('/api/save_masters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(masters)})
  .then(r=>r.json()).then(d=>{masters=d;renderProjectList();});}

function renderProjectList(){
  const el=document.getElementById('proj-list');
  if(!masters.projects.length){el.innerHTML='<p style="color:var(--muted);font-size:13px">No projects saved yet.</p>';return;}
  el.innerHTML=masters.projects.map((p,i)=>`
    <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;display:flex;align-items:center;gap:10px">
      <div style="flex:1">
        <div style="font-weight:600;font-size:14px">${p.name}</div>
        <div style="font-size:12px;color:var(--muted)">${p.session||''} &nbsp;|&nbsp; ${p.from_period} → ${p.to_period}</div>
      </div>
      <span class="pill ${p.status==='open'?'pill-open':'pill-closed'}" onclick="toggleProjectStatus(${i})" style="cursor:pointer" title="Click to toggle">${p.status.toUpperCase()}</span>
      <button class="btn btn-outline" style="font-size:12px;padding:4px 10px" onclick="editProjToDate(${i})">✏️ Extend</button>
      <button class="btn" style="font-size:12px;padding:4px 10px;background:#fff5f5;color:var(--danger);border-color:#fecaca" onclick="deleteProject(${i})">🗑</button>
    </div>`).join('');
}

function editProjToDate(i){
  const p=masters.projects[i];
  const nd=prompt('New To Date (YYYY-MM-DD):',p.to_period);
  if(nd){p.to_period=nd;
    fetch('/api/save_masters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(masters)})
    .then(r=>r.json()).then(d=>{masters=d;renderProjectList();});}
}

function renderContractList(){
  const el=document.getElementById('cont-list');
  if(!masters.contracts.length){el.innerHTML='<p style="color:var(--muted);font-size:13px">No contracts saved yet.</p>';return;}
  el.innerHTML=masters.contracts.map((c,i)=>`
    <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <div style="font-weight:600;font-size:14px">${c.contract_no}</div>
        <span style="font-size:12px;color:var(--muted)">| ${c.party||''}</span>
        <span style="font-size:12px;background:#eff6ff;color:var(--primary);padding:1px 8px;border-radius:10px;margin-left:auto">${c.project}</span>
        <button class="btn" style="font-size:11px;padding:3px 8px;background:#fff5f5;color:var(--danger);border-color:#fecaca" onclick="deleteContract(${i})">🗑</button>
      </div>
      <div style="font-size:12px;color:var(--muted);display:grid;grid-template-columns:1fr 1fr;gap:4px">
        <span>Effective: ${c.effective_date||'—'}</span>
        <span>Bales: ${c.bales||'—'}</span>
        <span>EMD: ${c.emd_days||'—'}d @ ${c.emd_percent||'—'}%</span>
        <span>CD GST: ${c.cd_gst||'—'}%</span>
        <span>LL GST: ${c.ll_gst||'—'}%</span>
        <span>CC GST: ${c.cc_gst||'—'}%</span>
      </div>
    </div>`).join('');
}

function refreshProjectDropdown(){
  const sel=document.getElementById('cont-project');
  sel.innerHTML='<option value="">-- Select Project --</option>'+
    masters.projects.filter(p=>p.status==='open').map(p=>`<option>${p.name}</option>`).join('');
}
function refreshUploadDropdown(){
  const sel=document.getElementById('upload-contract');
  sel.innerHTML='<option value="">-- Select Contract --</option>'+
    masters.contracts.map(c=>`<option value="${c.contract_no}">${c.contract_no} | ${c.party||''}</option>`).join('');
}

function clearProjectForm(){['proj-name','proj-session','proj-from','proj-to'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('proj-status').value='open';}
function clearContractForm(){['cont-project','cont-party','cont-no','cont-date','cont-effective','cont-bales',
  'emd-days','emd-pct','cd1-days','cd1-pct','cd2-days','cd2-pct','cd3-days','cd3-pct','cd-gst',
  'll1-days','ll1-pct','ll2-days','ll2-pct','ll3-days','ll3-pct','ll-gst',
  'cc1-days','cc1-pct','cc2-days','cc2-pct','cc-gst'].forEach(id=>document.getElementById(id).value='');}

// ─── UPLOAD ───────────────────────────────────────────────────────────────────
function fileSelected(inp){
  if(inp.files[0]){
    uploadedFile=inp.files[0];
    document.getElementById('file-name').textContent='✅ '+uploadedFile.name;
  }
}
function handleDrop(e){
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag');
  const f=e.dataTransfer.files[0];
  if(f){uploadedFile=f;document.getElementById('file-name').textContent='✅ '+f.name;}
}

function runCalc(){
  const contract_no=v('upload-contract');
  if(!contract_no){alert('Please select a contract master first.');return;}
  if(!uploadedFile){alert('Please upload an Excel file first.');return;}

  const master_contract=masters.contracts.find(c=>c.contract_no===contract_no);
  if(!master_contract){alert('Contract master not found.');return;}

  const fd=new FormData();
  fd.append('file',uploadedFile);
  fd.append('master',JSON.stringify(master_contract));

  document.getElementById('spinner').style.display='inline-block';
  document.getElementById('calc-btn').disabled=true;
  document.getElementById('upload-msg').innerHTML='';

  fetch('/api/calculate',{method:'POST',body:fd})
  .then(r=>r.json())
  .then(d=>{
    document.getElementById('spinner').style.display='none';
    document.getElementById('calc-btn').disabled=false;
    if(d.error){
      document.getElementById('upload-msg').innerHTML=`<div class="alert alert-danger" style="margin-top:12px">❌ ${d.error}</div>`;
      return;
    }
    document.getElementById('upload-msg').innerHTML=`<div class="alert alert-success" style="margin-top:12px">✅ Calculation complete! ${d.rows} GRNs processed.</div>`;
    resultData=d.results;
    excelBase64=d.excel;
    renderResults();
    tab('results');
  })
  .catch(e=>{
    document.getElementById('spinner').style.display='none';
    document.getElementById('calc-btn').disabled=false;
    document.getElementById('upload-msg').innerHTML=`<div class="alert alert-danger" style="margin-top:12px">❌ Error: ${e}</div>`;
  });
}

// ─── RESULTS ──────────────────────────────────────────────────────────────────
function fmt(n){if(n==null||n===''||n===0)return '—';return Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fmtD(s){if(!s||s==='NaT')return '—';return s.split('T')[0];}

function renderResults(){
  if(!resultData||!resultData.length)return;
  const rows=resultData;
  document.getElementById('grn-badge').textContent=rows.length;

  let totMat=0,totEmdInt=0,totLL=0;
  rows.forEach(r=>{totMat+=r.Material_Amount||0;totEmdInt+=r.EMD_Interest||0;totLL+=r.Late_Lifting_Chg||0;});
  document.getElementById('m-grns').textContent=rows.length;
  document.getElementById('m-mat').textContent='₹'+fmt(totMat);
  document.getElementById('m-emd-int').textContent='₹'+fmt(totEmdInt);
  document.getElementById('m-ll').textContent='₹'+fmt(totLL);

  const tbody=document.getElementById('result-body');
  tbody.innerHTML=rows.map(r=>`<tr>
    <td>${r.Contract_No}</td>
    <td><strong>${r.GRN_No}</strong></td>
    <td>${fmtD(r.Party_Bill_Date)}</td>
    <td class="num">${r.Bales}</td>
    <td class="num">₹${fmt(r.Material_Amount)}</td>
    <td class="num">₹${fmt(r.Per_Bale_EMD)}</td>
    <td class="num">₹${fmt(r.EMD_Allocated)}</td>
    <td>${fmtD(r.EMD_Date)}</td>
    <td class="num">₹${fmt(r.Net_Amount)}</td>
    <td>${fmtD(r.Payment_Date)}</td>
    <td class="num">${r.EMD_Days}</td>
    <td class="num" style="color:var(--success);font-weight:600">₹${fmt(r.EMD_Interest)}</td>
    <td class="num" style="color:#0891b2">₹${fmt(r.Cash_Discount)}</td>
    <td class="num" style="color:var(--danger)">₹${fmt(r.Late_Lifting_Chg)}</td>
    <td class="num" style="color:var(--danger)">₹${fmt(r.Late_Lifting_GST)}</td>
    <td class="num" style="color:var(--warning)">₹${fmt(r.Carry_Charges)}</td>
    <td class="num" style="color:var(--warning)">₹${fmt(r.Carry_GST)}</td>
  </tr>`).join('');

  document.getElementById('dl-btn').style.display='inline-flex';
  document.getElementById('result-alert').innerHTML=
    `<div class="alert alert-success">✅ ${rows.length} GRNs calculated successfully.</div>`;
}

function downloadExcel(){
  if(!excelBase64)return;
  const a=document.createElement('a');
  a.href='data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,'+excelBase64;
  a.download='CCI_Calculation_Output.xlsx';
  a.click();
}

// ─── UTILS ────────────────────────────────────────────────────────────────────
function v(id){return document.getElementById(id).value.trim();}
function n(id){return parseFloat(document.getElementById(id).value)||0;}

// ─── INIT ─────────────────────────────────────────────────────────────────────
loadMasters();
</script>
</body>
</html>
"""

# ─── FLASK ROUTES ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/masters")
def get_masters():
    return jsonify(load_masters())

@app.route("/api/save_project", methods=["POST"])
def save_project():
    data = load_masters()
    proj = request.json
    data["projects"].append(proj)
    save_masters(data)
    return jsonify(data)

@app.route("/api/save_contract", methods=["POST"])
def save_contract():
    data = load_masters()
    cont = request.json
    data["contracts"].append(cont)
    save_masters(data)
    return jsonify(data)

@app.route("/api/delete_project", methods=["POST"])
def delete_project():
    data = load_masters()
    idx  = request.json["index"]
    data["projects"].pop(idx)
    save_masters(data)
    return jsonify(data)

@app.route("/api/delete_contract", methods=["POST"])
def delete_contract():
    data = load_masters()
    idx  = request.json["index"]
    data["contracts"].pop(idx)
    save_masters(data)
    return jsonify(data)

@app.route("/api/save_masters", methods=["POST"])
def save_masters_route():
    data = request.json
    save_masters(data)
    return jsonify(data)

@app.route("/api/calculate", methods=["POST"])
def calculate():
    try:
        file_bytes     = request.files["file"].read()
        master_contract = json.loads(request.form["master"])

        cont, emd, pay, grn = parse_excel(file_bytes)
        result_df, per_bale_emd = run_calculations(cont, emd, pay, grn, master_contract)

        # Serialize dates to string for JSON
        def serialize(df):
            d = df.copy()
            for col in d.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns:
                d[col] = d[col].astype(str)
            return d.to_dict(orient="records")

        excel_bytes = df_to_excel_bytes(result_df, cont, emd, pay, grn)
        excel_b64   = base64.b64encode(excel_bytes).decode()

        return jsonify({
            "rows"   : len(result_df),
            "results": serialize(result_df),
            "excel"  : excel_b64,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e) + "\n" + traceback.format_exc()})


# ─── LAUNCH ────────────────────────────────────────────────────────────────────
def open_browser():
    import time; time.sleep(1)
    webbrowser.open("http://localhost:5000")

if __name__ == "__main__":
    print("=" * 55)
    print("  CCI Working Calculation Utility")
    print("  Opening: http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, port=5000)
