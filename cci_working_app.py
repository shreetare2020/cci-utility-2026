"""
CCI WORKING CALCULATION UTILITY
Softview Technologies | Streamlit + Firebase
Run: streamlit run cci_working_app.py
"""

import io, json, os, base64 as b64lib, secrets, hashlib, smtplib, socket, re
from datetime import date, datetime, timedelta
from email.message import EmailMessage
import pandas as pd
import streamlit as st

# ─── FIREBASE / FIRESTORE (REST) ──────────────────────────────────────────────
# Use Firestore REST so the app can explicitly discover the existing database
# instead of getting the SDK error: Invalid database id %28default%29.
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

FIREBASE_KEY_FILE = "firebase_key.json"
_FIRESTORE_COLLECTION = "cci_utility"
_FIRESTORE_SCOPE = "https://www.googleapis.com/auth/datastore"


def _load_firebase_credentials():
    try:
        cred_dict = dict(st.secrets["firebase"])
        if "private_key" in cred_dict:
            cred_dict["private_key"] = str(cred_dict["private_key"]).replace("\\n", "\n")
        cred = service_account.Credentials.from_service_account_info(
            cred_dict, scopes=[_FIRESTORE_SCOPE]
        )
        project_id = cred_dict.get("project_id") or cred.project_id
        if not project_id:
            raise ValueError("Firebase service account does not contain project_id")
        return cred, project_id
    except Exception:
        if not os.path.exists(FIREBASE_KEY_FILE):
            st.error(
                "❌ Firebase credentials not found. Configure [firebase] in Streamlit Secrets "
                "or place firebase_key.json beside the app."
            )
            st.stop()
        cred = service_account.Credentials.from_service_account_file(
            FIREBASE_KEY_FILE, scopes=[_FIRESTORE_SCOPE]
        )
        if not cred.project_id:
            st.error("❌ Firebase service account does not contain project_id.")
            st.stop()
        return cred, cred.project_id


class _RestSnapshot:
    def __init__(self, exists, data=None):
        self.exists = bool(exists)
        self._data = data or {}
    def to_dict(self):
        return dict(self._data)


def _fs_encode(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_fs_encode(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {str(k): _fs_encode(v) for k, v in value.items()}}}
    try:
        import numpy as np
        if isinstance(value, np.integer):
            return {"integerValue": str(int(value))}
        if isinstance(value, np.floating):
            return {"doubleValue": float(value)}
    except Exception:
        pass
    return {"stringValue": str(value)}


def _fs_decode(value):
    if not isinstance(value, dict):
        return None
    if "nullValue" in value: return None
    if "stringValue" in value: return value["stringValue"]
    if "booleanValue" in value: return value["booleanValue"]
    if "integerValue" in value:
        try: return int(value["integerValue"])
        except Exception: return value["integerValue"]
    if "doubleValue" in value: return value["doubleValue"]
    if "timestampValue" in value: return value["timestampValue"]
    if "bytesValue" in value: return value["bytesValue"]
    if "referenceValue" in value: return value["referenceValue"]
    if "geoPointValue" in value: return value["geoPointValue"]
    if "arrayValue" in value:
        return [_fs_decode(v) for v in value.get("arrayValue", {}).get("values", [])]
    if "mapValue" in value:
        return {k: _fs_decode(v) for k, v in value.get("mapValue", {}).get("fields", {}).items()}
    return None


cred, _firebase_project_id = _load_firebase_credentials()
_firebase_session = AuthorizedSession(cred)


def _discover_firestore_database():
    """Return an actual database id from this Firebase project.
    Prefer (default); otherwise use the only available Firestore database.
    """
    from urllib.parse import quote
    url = f"https://firestore.googleapis.com/v1/projects/{quote(_firebase_project_id, safe='')}/databases"
    r = _firebase_session.get(url, timeout=25)
    r.raise_for_status()
    items = r.json().get("databases", [])
    db_ids = []
    for item in items:
        name = str(item.get("name", ""))
        if "/databases/" in name:
            db_ids.append(name.split("/databases/", 1)[1])
    if "(default)" in db_ids:
        return "(default)"
    if len(db_ids) == 1:
        return db_ids[0]
    if db_ids:
        # Prefer a database explicitly named default-ish; otherwise first listed.
        for preferred in ("default", "firestore", "cci"):
            for dbid in db_ids:
                if dbid.lower() == preferred:
                    return dbid
        return db_ids[0]
    raise RuntimeError("No Firestore database was found in the Firebase project.")


_FIRESTORE_DB_ID = _discover_firestore_database()


def _document_url(collection, document):
    from urllib.parse import quote
    # IMPORTANT: keep parentheses in the default database id unescaped.
    db_id = quote(_FIRESTORE_DB_ID, safe="()")
    return (
        f"https://firestore.googleapis.com/v1/projects/{quote(_firebase_project_id, safe='')}"
        f"/databases/{db_id}/documents/{quote(collection, safe='')}/{quote(document, safe='')}"
    )


class _RestDocument:
    def __init__(self, collection, document):
        self.collection = collection
        self.document = document
    def get(self):
        response = _firebase_session.get(_document_url(self.collection, self.document), timeout=25)
        if response.status_code == 404:
            return _RestSnapshot(False, {})
        response.raise_for_status()
        payload = response.json()
        return _RestSnapshot(True, {k: _fs_decode(v) for k, v in payload.get("fields", {}).items()})
    def set(self, data):
        payload = {"fields": {str(k): _fs_encode(v) for k, v in dict(data).items()}}
        response = _firebase_session.patch(_document_url(self.collection, self.document), json=payload, timeout=25)
        response.raise_for_status()
        return response.json()


class _RestCollection:
    def __init__(self, collection): self.collection = collection
    def document(self, document): return _RestDocument(self.collection, document)


class _RestFirestore:
    def collection(self, collection): return _RestCollection(collection)


db = _RestFirestore()


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _safe_doc_id(value):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
    return s[:120] or "unknown"


def _get_doc(doc_name):
    try:
        snap = db.collection(_FIRESTORE_COLLECTION).document(doc_name).get()
        return snap.to_dict() if snap.exists else {}
    except Exception:
        return {}


def _set_doc(doc_name, data):
    db.collection(_FIRESTORE_COLLECTION).document(doc_name).set(data)


def _ensure_xyz_migration():
    """One-time migration: old single masters become firm XYZ Company data."""
    try:
        reg = _get_doc("firm_registry")
        firms = reg.get("firms", {}) or {}
        if "XYZ" not in firms:
            old = _get_doc("masters")
            legacy = {
                "projects": old.get("projects", []) if isinstance(old, dict) else [],
                "contracts": old.get("contracts", []) if isinstance(old, dict) else [],
            }
            _set_doc("firm_data_XYZ", legacy)
            firms["XYZ"] = {
                "firm_id": "XYZ",
                "firm_name": "XYZ Company",
                "status": "ACTIVE",
                "created_at": _now_iso(),
                "owner_username": "admin",
                "mobile": "",
                "email": "",
                "registration_key": "",
                "activation_key": "LEGACY_XYZ",
                "subscription_status": "ACTIVE",
                "trial_start": "",
                "trial_end": "",
                "subscription_start": "",
                "subscription_end": "2099-12-31T23:59:59",
                "included_users": _system_policy()["included_users"],
                "extra_users": 0,
            }
            _set_doc("firm_registry", {"firms": firms})
    except Exception as e:
        st.warning(f"Firm migration warning: {e}")


def fs_load():
    try:
        _ensure_xyz_migration()
        firm_id = st.session_state.get("_firm_id", "XYZ")
        doc = db.collection(_FIRESTORE_COLLECTION).document(f"firm_data_{_safe_doc_id(firm_id)}").get()
        if doc.exists:
            data = doc.to_dict()
            data.setdefault("projects", [])
            data.setdefault("contracts", [])
            return data
    except Exception as e:
        st.warning(f"Firebase read error: {e}")
    return {"projects": [], "contracts": []}


def fs_save(data):
    try:
        firm_id = st.session_state.get("_firm_id", "XYZ")
        db.collection(_FIRESTORE_COLLECTION).document(f"firm_data_{_safe_doc_id(firm_id)}").set(data)
    except Exception as e:
        st.error(f"Firebase save error: {e}")

# ─── LOGO ────────────────────────────────────────────────────────────────────
LOGO_B64 = "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"

# ─── PAGE CONFIG ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="CCI Calculation Utility | Softview Technologies",
    page_icon="🧮",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ─── PREMIUM CSS ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

* { font-family: 'Inter', sans-serif; font-size: 14px; }

[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(circle at 8% 0%, rgba(249,115,22,0.06) 0%, transparent 42%),
        radial-gradient(circle at 100% 10%, rgba(234,88,12,0.05) 0%, transparent 38%),
        #f4f0ea;
    min-height: 100vh;
}
[data-testid="stHeader"] { background: transparent !important; }
.main .block-container { padding: 0.5rem 1.5rem 2rem 1.5rem; max-width: 1400px; }

/* ── TOP HEADER ── */
.top-header {
    background: linear-gradient(115deg, #f97316 0%, #ea580c 45%, #9a3412 100%);
    border-radius: 18px;
    padding: 0px 28px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    box-shadow: 0 10px 32px rgba(154,52,18,0.38), 0 0 0 1px rgba(253,186,116,0.28);
    min-height: 80px;
    position: relative;
    overflow: hidden;
}
.top-header::before {
    content:'';
    position:absolute; top:-60px; right:-40px;
    width:220px; height:220px;
    background: radial-gradient(circle, rgba(255,255,255,0.14) 0%, transparent 70%);
    border-radius:50%; pointer-events:none;
}
.top-header::after {
    content:'';
    position:absolute; bottom:-90px; left:20%;
    width:260px; height:260px;
    background: radial-gradient(circle, rgba(255,200,100,0.14) 0%, transparent 65%);
    border-radius:50%; pointer-events:none;
}
.top-header-left {
    display: flex; align-items: center; gap: 18px; z-index:1; position:relative;
}
.top-header-title {
    color: #ffffff;
    font-size: 23px;
    font-weight: 800;
    letter-spacing: -0.3px;
    line-height: 1.2;
    text-shadow: 0 2px 10px rgba(0,0,0,0.18);
}
.top-header-sub {
    color: rgba(255,255,255,0.85);
    font-size: 12.5px;
    font-weight: 600;
    margin-top: 3px;
    letter-spacing: .04em;
    text-transform: uppercase;
}
.top-header-badge {
    background: rgba(255,255,255,0.22);
    color: #fff;
    font-size: 11px;
    padding: 4px 12px;
    border-radius: 20px;
    font-weight: 800;
    letter-spacing: .08em;
    border: 1px solid rgba(255,255,255,0.35);
    box-shadow: 0 2px 8px rgba(0,0,0,0.10);
}
.logo-img {
    height: 68px; width: auto;
    border-radius: 12px; background: rgba(255,255,255,0.97);
    padding: 6px 10px; object-fit: contain;
    box-shadow: 0 4px 16px rgba(0,0,0,0.18); flex-shrink: 0;
}
.top-header-left { display: flex !important; align-items: center !important; gap: 16px !important; }

/* ── TABS ── */
div[data-testid="stTabs"] {
    background: #ffffff;
    border-radius: 16px; padding: 6px;
    border: 1px solid #e7ddd0;
    margin-bottom: 20px;
    box-shadow: 0 4px 16px rgba(154,52,18,0.06);
}
div[data-testid="stTabs"] button {
    font-size: 14px !important; font-weight: 600 !important;
    color: #78716c !important;
    border-radius: 11px !important; padding: 10px 22px !important;
    transition: all 0.2s !important;
}
div[data-testid="stTabs"] button[aria-selected="true"] {
    background: linear-gradient(135deg, #c2410c, #f97316) !important;
    color: #ffffff !important; font-weight: 800 !important;
    box-shadow: 0 4px 14px rgba(234,88,12,0.40) !important;
    transform: translateY(-1px);
}
div[data-testid="stTabs"] button:hover:not([aria-selected="true"]) {
    color: #c2410c !important; background: rgba(234,88,12,0.08) !important;
}
div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
div[data-testid="stTabs"] [data-baseweb="tab-border"] { display:none !important; }

/* ── CARDS ── */
.sv-card {
    background: #ffffff; border: 1px solid #ece4d8;
    border-radius: 16px; padding: 22px; margin-bottom: 16px;
    box-shadow: 0 4px 18px rgba(154,52,18,0.07);
    transition: box-shadow 0.2s ease;
}
.sv-card:hover { box-shadow: 0 8px 26px rgba(154,52,18,0.11); }
.sv-card-title {
    font-size: 13px; font-weight: 800; color: #c2410c;
    text-transform: uppercase; letter-spacing: .1em;
    margin-bottom: 16px; display: flex; align-items: center; gap: 8px;
}
.sv-card-title::before {
    content:''; width:6px; height:18px; border-radius:3px;
    background: linear-gradient(180deg,#f97316,#c2410c); display:inline-block; flex-shrink:0;
}
.sv-card-title::after {
    content: ''; flex: 1; height: 1px;
    background: linear-gradient(90deg, rgba(234,88,12,0.3), transparent);
}

/* ── METRIC CARDS ── */
.metric-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 22px; }
.metric-card {
    background: #ffffff; border: 1px solid #ece4d8;
    border-radius: 14px; padding: 18px 10px; text-align: center;
    position: relative; overflow: hidden;
    box-shadow: 0 4px 16px rgba(154,52,18,0.07);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.metric-card:hover { transform: translateY(-3px); box-shadow: 0 10px 24px rgba(154,52,18,0.14); }
.metric-card::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0;
    height: 4px; border-radius: 14px 14px 0 0;
}
.metric-card.green::before  { background: linear-gradient(90deg, #c2410c, #fdba74); }
.metric-card.blue::before   { background: linear-gradient(90deg, #1a56db, #3b82f6); }
.metric-card.teal::before   { background: linear-gradient(90deg, #0891b2, #06b6d4); }
.metric-card.red::before    { background: linear-gradient(90deg, #dc2626, #ef4444); }
.metric-card.orange::before { background: linear-gradient(90deg, #d97706, #f59e0b); }
.metric-card.purple::before { background: linear-gradient(90deg, #7e22ce, #a855f7); }
.metric-val { font-size: 18px; font-weight: 800; color: #1c1917; margin-bottom: 4px; letter-spacing: -0.3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.metric-lbl { font-size: 9.5px; color: #a8a29e; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.metric-icon {
    font-size: 20px; margin-bottom: 8px; display:inline-flex; align-items:center; justify-content:center;
    width:38px; height:38px; border-radius:50%;
    background: linear-gradient(135deg, rgba(249,115,22,0.12), rgba(194,65,12,0.06));
}

@media (max-width: 900px) {
    .metric-row { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 560px) {
    .metric-row { grid-template-columns: repeat(2, 1fr); }
}

/* ── FORM ELEMENTS ── */
div[data-testid="stTextInput"] input,
div[data-testid="stNumberInput"] input,
div[data-testid="stDateInput"] input,
div[data-testid="stTextArea"] textarea,
input[type="text"], input[type="number"], input[type="date"] {
    background-color: #ffffff !important;
    border: 1.5px solid #d1d5db !important;
    border-radius: 8px !important; color: #111827 !important;
    caret-color: #c2410c !important;
}
div[data-testid="stTextInput"] input:focus,
div[data-testid="stNumberInput"] input:focus,
div[data-testid="stDateInput"] input:focus {
    border-color: #f97316 !important;
    box-shadow: 0 0 0 3px rgba(249,115,22,0.15) !important; outline: none !important;
}
div[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
div[data-baseweb="select"] > div {
    background-color: #ffffff !important;
    border: 1.5px solid #d1d5db !important;
    border-radius: 8px !important; color: #111827 !important;
}
div[data-baseweb="select"] span { color: #111827 !important; background: transparent !important; }
div[data-baseweb="popover"] ul, div[data-baseweb="menu"] {
    background-color: #ffffff !important; border: 1px solid #d1d5db !important;
    box-shadow: 0 4px 16px rgba(0,0,0,0.1) !important;
}
div[data-baseweb="menu"] li, div[data-baseweb="option"] {
    color: #111827 !important; background-color: #ffffff !important;
}
div[data-baseweb="option"]:hover { background-color: rgba(234,88,12,0.08) !important; }
div[data-testid="stTextInput"] label,
div[data-testid="stNumberInput"] label,
div[data-testid="stSelectbox"] label,
div[data-testid="stDateInput"] label,
div[data-testid="stTextArea"] label {
    color: #374151 !important; font-size: 13px !important; font-weight: 600 !important;
}
div[data-testid="stTextInput"] input::placeholder,
div[data-testid="stNumberInput"] input::placeholder { color: #9ca3af !important; }
div[data-testid="stNumberInput"] button {
    color: #374151 !important; background: #f3f4f6 !important;
    border: 1px solid #d1d5db !important;
}

/* ── BUTTONS ── */
div[data-testid="stButton"] button[kind="primary"] {
    background: linear-gradient(135deg, #c2410c, #f97316) !important;
    color: #ffffff !important; border: none !important;
    border-radius: 11px !important; font-weight: 700 !important; font-size: 14px !important;
    padding: 11px 26px !important; box-shadow: 0 6px 18px rgba(234,88,12,0.35) !important;
    transition: all 0.2s !important; letter-spacing: .01em !important;
}
div[data-testid="stButton"] button[kind="primary"]:hover {
    transform: translateY(-2px) !important; box-shadow: 0 10px 24px rgba(234,88,12,0.45) !important;
}
div[data-testid="stButton"] button[kind="secondary"] {
    background: #ffffff !important; border: 1.5px solid #e7ddd0 !important;
    color: #57534e !important; border-radius: 11px !important; font-weight: 600 !important;
}
div[data-testid="stButton"] button[kind="secondary"]:hover {
    background: #fff7ed !important; border-color: #f97316 !important; color: #c2410c !important;
}

/* ── DOWNLOAD BUTTON ── */
div[data-testid="stDownloadButton"] button {
    background: linear-gradient(135deg, #1a56db, #2563eb) !important;
    color: white !important; border: none !important; border-radius: 11px !important;
    font-weight: 700 !important; box-shadow: 0 6px 18px rgba(26,86,219,0.32) !important;
    transition: all 0.2s !important;
}
div[data-testid="stDownloadButton"] button:hover {
    transform: translateY(-2px) !important; box-shadow: 0 10px 24px rgba(26,86,219,0.42) !important;
}

/* ── ALERTS ── */
div[data-testid="stAlert"] {
    background: #fff7ed !important; border: 1px solid #fdba74 !important;
    border-radius: 11px !important; color: #9a3412 !important;
    box-shadow: 0 3px 10px rgba(154,52,18,0.06) !important;
}
div[data-testid="stInfo"] {
    background: #eff6ff !important; border: 1px solid #93c5fd !important; border-radius: 11px !important;
}
div[data-testid="stWarning"] {
    background: #fffbeb !important; border: 1px solid #fcd34d !important; border-radius: 11px !important;
}

/* ── DATAFRAME ── */
div[data-testid="stDataFrame"] {
    border-radius: 14px !important; overflow: hidden;
    border: 1px solid #ece4d8 !important; box-shadow: 0 4px 16px rgba(154,52,18,0.07) !important;
}
div[data-testid="stDataFrame"] [role="columnheader"] {
    background: linear-gradient(135deg, #fff7ed, #ffedd5) !important;
    color: #9a3412 !important; font-weight: 800 !important;
}

/* ── EXPANDER ── */
div[data-testid="stExpander"] {
    background: #ffffff !important; border: 1px solid #e7ddd0 !important;
    border-radius: 14px !important; box-shadow: 0 4px 14px rgba(154,52,18,0.06) !important;
    margin-bottom: 12px !important; overflow: hidden;
}
div[data-testid="stExpander"] summary,
div[data-testid="stExpander"] details > summary {
    background: #fafaf9 !important; color: #1c1917 !important;
    font-weight: 700 !important; border-radius: 14px !important; font-size: 14px !important;
    padding: 4px 6px !important;
}
div[data-testid="stExpander"] details[open] > summary {
    background: linear-gradient(135deg,rgba(234,88,12,0.10),rgba(249,115,22,0.05)) !important;
    color: #c2410c !important; border-radius: 14px 14px 0 0 !important;
    border-bottom: 1px solid #e7ddd0 !important;
}
div[data-testid="stExpander"] summary span,
div[data-testid="stExpander"] summary p,
div[data-testid="stExpander"] summary * { color: inherit !important; }
div[data-testid="stExpander"] summary:hover {
    background: rgba(234,88,12,0.07) !important; color: #c2410c !important;
}
div[data-testid="stExpander"] > details,
div[data-testid="stExpander"] details { background: #ffffff !important; border-radius: 14px !important; }
div[data-testid="stExpander"] details > div,
div[data-testid="stExpander"] [data-testid="stExpanderDetails"] {
    background: #ffffff !important; color: #374151 !important;
}
div[data-testid="stExpander"] * { color: #374151 !important; }
div[data-testid="stExpander"] div, div[data-testid="stExpander"] section { background-color: transparent !important; }

/* File uploader inside expander */
div[data-testid="stExpander"] div[data-testid="stFileUploader"] {
    background: #f9fafb !important; border: 2px dashed rgba(234,88,12,0.4) !important; border-radius: 10px !important;
}
div[data-testid="stExpander"] div[data-testid="stFileUploader"] * { color: #374151 !important; }
div[data-testid="stExpander"] div[data-testid="stFileUploader"] button,
div[data-testid="stExpander"] div[data-testid="stFileUploader"] button:focus {
    background: linear-gradient(135deg, #c2410c, #f97316) !important;
    background-color: #f97316 !important; color: #ffffff !important; border: none !important; opacity: 1 !important;
}
div[data-testid="stExpander"] div[data-testid="stFileUploader"] button:hover {
    background: linear-gradient(135deg, #f97316, #ea580c) !important;
    color: #ffffff !important; opacity: 1 !important;
}

/* ── STATUS PILLS ── */
.pill-open { background: linear-gradient(135deg,#c2410c,#f97316); color:#fff; padding:4px 14px; border-radius:20px; font-size:11.5px; font-weight:800; letter-spacing:.04em; box-shadow:0 2px 8px rgba(234,88,12,0.35); }
.pill-closed { background: linear-gradient(135deg,#dc2626,#ef4444); color:#fff; padding:4px 14px; border-radius:20px; font-size:11.5px; font-weight:800; letter-spacing:.04em; box-shadow:0 2px 8px rgba(220,38,38,0.30); }

/* ── SECTION DIVIDER ── */
.sv-divider { height: 1px; background: linear-gradient(90deg,transparent,rgba(234,88,12,0.3),transparent); margin: 18px 0; }

/* ── FORMULA BOX ── */
.formula-box {
    background: #fff7ed; border-left: 3px solid #f97316;
    border-radius: 0 8px 8px 0; padding: 10px 14px;
    font-family: 'Courier New', monospace; font-size: 12px;
    color: #9a3412; margin: 4px 0 10px 0; white-space: pre-wrap; line-height: 1.6;
    border: 1px solid #fed7aa; border-left: 3px solid #f97316;
}

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #f3f4f6; }
::-webkit-scrollbar-thumb { background: rgba(234,88,12,0.3); border-radius: 3px; }

/* ── PRORATA LABELS ── */
.prorata-label { font-size: 13px; font-weight: 700; color: #c2410c; text-transform:uppercase; letter-spacing:.1em; margin-top:12px; margin-bottom:4px; }
.prorata-hint { font-size: 13px; color: #6b7280; margin-bottom: 6px; line-height: 1.5; }

/* ── RADIO BUTTON ── */
div[data-testid="stRadio"] > div { background: transparent !important; }
div[data-testid="stRadio"] label { color: #374151 !important; background: transparent !important; }
div[data-testid="stRadio"] span { color: #374151 !important; }
div[data-testid="stRadio"] input[type="radio"] + div { border-color: rgba(234,88,12,0.5) !important; }
div[data-testid="stRadio"] input[type="radio"]:checked + div { background: #f97316 !important; border-color: #f97316 !important; }

/* ── GLOBAL LIGHT OVERRIDES ── */
.stApp, .main, [data-testid="stAppViewContainer"] > .main { background: transparent !important; }
div[data-testid="stRadio"], div[data-testid="stRadio"] div[role="radiogroup"],
div[data-testid="stNumberInput"], div[data-testid="column"],
div[data-testid="stVerticalBlock"], div[data-testid="stHorizontalBlock"] { background: transparent !important; }
div[data-baseweb="popover"] [role="listbox"], div[data-baseweb="popover"] { background-color: #ffffff !important; }
div[data-baseweb="popover"] [role="option"] { background-color: #ffffff !important; color: #111827 !important; }
div[data-baseweb="popover"] [role="option"]:hover,
div[data-baseweb="popover"] [aria-selected="true"] { background-color: rgba(234,88,12,0.08) !important; }
div[data-baseweb="popover"] [aria-selected="true"] {
    background-color: rgba(251,146,60,0.2) !important;
}
/* Checkbox */
div[data-testid="stCheckbox"] label {
    color: rgba(255,255,255,0.8) !important;
}
/* Warning / info / error boxes */
div[data-testid="stAlert"] {
    border-radius: 8px !important;
}
/* Caption */
div[data-testid="stCaptionContainer"] p {
    color: rgba(255,255,255,0.45) !important;
}

/* ── CONTRACT CARD ── */
.contract-card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 10px;
    transition: border-color 0.2s;
}
.contract-card:hover { border-color: rgba(251,146,60,0.3); }

/* ── SECTION LABEL ── */
.sec-label {
    font-size: 14px;
    font-weight: 700;
    color: #fdba74;
    text-transform: uppercase;
    letter-spacing: .1em;
    margin-bottom: 10px;
    margin-top: 16px;
}

/* ── FILE UPLOADER ── */
div[data-testid="stFileUploader"] {
    background: rgba(255,255,255,0.03) !important;
    border: 2px dashed rgba(251,146,60,0.3) !important;
    border-radius: 10px !important;
    padding: 8px !important;
}
div[data-testid="stFileUploader"]:hover {
    border-color: rgba(253,186,116,0.6) !important;
    background: rgba(234,88,12,0.05) !important;
}
/* File uploader button — all states locked to parrot green */
div[data-testid="stFileUploader"] button,
div[data-testid="stFileUploaderDropzone"] button,
[data-testid="stFileUploader"] button[kind],
[data-testid="stFileUploader"] button {
    background: linear-gradient(135deg, #c2410c, #f97316) !important;
    background-color: #f97316 !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    opacity: 1 !important;
}
div[data-testid="stFileUploader"] button:hover,
div[data-testid="stFileUploader"] button:focus,
div[data-testid="stFileUploader"] button:active,
div[data-testid="stFileUploaderDropzone"] button:hover,
[data-testid="stFileUploader"] button:hover {
    background: linear-gradient(135deg, #f97316, #fdba74) !important;
    background-color: #fdba74 !important;
    color: #0a1a0a !important;
    border: none !important;
    opacity: 1 !important;
    box-shadow: 0 0 12px rgba(253,186,116,0.4) !important;
}
div[data-testid="stFileUploader"] small,
div[data-testid="stFileUploader"] span,
div[data-testid="stFileUploader"] p {
    color: rgba(255,255,255,0.65) !important;
}
div[data-testid="stFileUploaderDropzone"] {
    background: transparent !important;
}
div[data-testid="stFileUploaderDropzone"] * {
    color: #374151 !important;
}

/* ── CAPTION ── */
div[data-testid="stCaptionContainer"] { color: #6b7280 !important; }

/* ── MARKDOWN TEXT ── */
.stMarkdown p, .stMarkdown li { color: #374151 !important; }
.stMarkdown strong { color: #111827 !important; }
h1,h2,h3,h4 { color: #111827 !important; }

/* ── SPINNER ── */
div[data-testid="stSpinner"] { color: #f97316 !important; }

/* ── SUCCESS MSG ── */
.success-msg {
    background: #fff7ed; border: 1px solid #fdba74;
    border-radius: 8px; padding: 10px 16px;
    color: #9a3412; font-weight: 500; font-size: 14px; margin: 8px 0;
}

/* ── FOOTER ── */
.sv-footer {
    text-align: center; color: #9ca3af; font-size: 12px;
    padding: 20px 0 8px; border-top: 1px solid #e5e7eb; margin-top: 30px;
}

/* ── CONTRACT CARD ── */
.contract-card {
    background: #ffffff; border: 1px solid #ece4d8; border-left: 4px solid #f97316;
    border-radius: 12px; padding: 14px 18px; margin-bottom: 12px;
    transition: all 0.2s ease; box-shadow: 0 3px 12px rgba(154,52,18,0.06);
}
.contract-card:hover { border-left-color: #c2410c; box-shadow: 0 8px 22px rgba(154,52,18,0.13); transform: translateX(2px); }

/* ── SECTION LABEL ── */
.sec-label {
    font-size: 12.5px; font-weight: 800; color: #c2410c;
    text-transform: uppercase; letter-spacing: .1em; margin-bottom: 12px; margin-top: 18px;
    display: flex; align-items: center; gap: 8px;
}
.sec-label::before {
    content: ''; width: 6px; height: 16px; border-radius: 3px;
    background: linear-gradient(180deg, #f97316, #c2410c); display: inline-block; flex-shrink: 0;
}
.sec-label::after {
    content: ''; flex: 1; height: 1px;
    background: linear-gradient(90deg, rgba(234,88,12,0.25), transparent);
}

/* ── CHECKBOX ── */
div[data-testid="stCheckbox"] label { color: #374151 !important; }

/* ── FILE UPLOADER (main) ── */
div[data-testid="stFileUploader"] {
    background: #f9fafb !important;
    border: 2px dashed rgba(234,88,12,0.35) !important;
    border-radius: 10px !important; padding: 8px !important;
}
div[data-testid="stFileUploader"]:hover {
    border-color: #f97316 !important; background: rgba(234,88,12,0.03) !important;
}
div[data-testid="stFileUploader"] button,
div[data-testid="stFileUploaderDropzone"] button,
[data-testid="stFileUploader"] button {
    background: linear-gradient(135deg, #c2410c, #f97316) !important;
    background-color: #f97316 !important; color: #ffffff !important;
    border: none !important; border-radius: 6px !important; font-weight: 600 !important; opacity: 1 !important;
}
div[data-testid="stFileUploader"] button:hover,
div[data-testid="stFileUploaderDropzone"] button:hover,
[data-testid="stFileUploader"] button:hover {
    background: linear-gradient(135deg, #f97316, #ea580c) !important;
    background-color: #ea580c !important; color: #ffffff !important;
    border: none !important; opacity: 1 !important;
    box-shadow: 0 3px 10px rgba(249,115,22,0.3) !important;
}
div[data-testid="stFileUploader"] small,
div[data-testid="stFileUploader"] span,
div[data-testid="stFileUploader"] p { color: #6b7280 !important; }
div[data-testid="stFileUploaderDropzone"] { background: transparent !important; }

/* ── FINAL EXECUTIVE LIGHT THEME OVERRIDES ── */
body, .stApp, [data-testid="stAppViewContainer"] {
    color: #1f2937 !important;
}
.contract-card {
    background: #ffffff !important;
    color: #1f2937 !important;
    border: 1px solid #ece4d8 !important;
    border-left: 4px solid #f97316 !important;
    box-shadow: 0 4px 14px rgba(154,52,18,0.07) !important;
}
.contract-card * { color: #374151 !important; }
.contract-card strong { color: #9a3412 !important; }
.contract-card [style*="color:#fff"], .contract-card [style*="color: #fff"] { color:#374151 !important; }
.pill-open, .pill-closed { color:#ffffff !important; }
.sec-label { color: #9a3412 !important; }
div[data-testid="stCaptionContainer"] p,
.stCaption, .stMarkdown p, .stMarkdown li { color: #4b5563 !important; }
div[data-testid="stCheckbox"] label,
div[data-testid="stRadio"] label,
div[data-testid="stRadio"] span { color: #374151 !important; }
div[data-testid="stButton"] button {
    min-height: 40px !important;
    color: #1f2937 !important;
    background: #ffffff !important;
    border: 1px solid #cbd5e1 !important;
}
div[data-testid="stButton"] button[kind="primary"] {
    color: #ffffff !important;
    background: linear-gradient(135deg,#9a3412,#f97316) !important;
    border: none !important;
}
div[data-testid="stButton"] button:hover {
    color: #7c2d12 !important;
    border-color: #f97316 !important;
    background: #fff7ed !important;
}
div[data-testid="stButton"] button[kind="primary"]:hover {
    color: #ffffff !important;
    background: linear-gradient(135deg,#7c2d12,#f97316) !important;
}
div[data-testid="stDownloadButton"] button {
    color: #ffffff !important;
    background: linear-gradient(135deg,#1d4ed8,#2563eb) !important;
}
div[data-testid="stExpander"] *,
div[data-testid="stExpander"] summary,
div[data-testid="stExpander"] summary * { color: #374151 !important; }
div[data-testid="stExpander"] summary { background:#f8fafc !important; }
div[data-testid="stExpander"] details[open] > summary {
    color:#9a3412 !important;
    background:#fff7ed !important;
}
div[data-testid="stTextInput"] input,
div[data-testid="stNumberInput"] input,
div[data-testid="stDateInput"] input,
div[data-testid="stTextArea"] textarea,
div[data-baseweb="select"] > div {
    color:#111827 !important;
    background:#ffffff !important;
}
div[data-testid="stFileUploader"] {
    background:#f8fafc !important;
}
div[data-testid="stFileUploader"] * { color:#475569 !important; }
div[data-testid="stFileUploader"] button {
    color:#ffffff !important;
    background:linear-gradient(135deg,#9a3412,#f97316) !important;
}


/* UPLOAD BUTTON HIGH CONTRAST */
[data-testid="stFileUploader"] button,
[data-testid="stFileUploader"] [role="button"] { background:linear-gradient(135deg,#f59e0b,#ea580c)!important; color:#fff!important; border:2px solid #c2410c!important; font-weight:800!important; text-shadow:0 1px 1px rgba(0,0,0,.25)!important; }
[data-testid="stFileUploader"] button:hover,
[data-testid="stFileUploader"] [role="button"]:hover { background:linear-gradient(135deg,#ea580c,#c2410c)!important; color:#fff!important; }
/* LOGIN EMPTY BOX */
.login-page .empty-box,.login-page .logo-placeholder,.login-page .top-placeholder,.login-page .blank-box {display:none!important;}
.login-page .login-card {background:transparent!important;border:none!important;box-shadow:none!important;}

</style>
""", unsafe_allow_html=True)

# ─── MULTI-TENANT AUTH / REGISTRATION / SUBSCRIPTION ─────────────────────────
_DEFAULT_USERS = {"admin": "cci@2025", "softview": "sv@admin"}
_KEY_SPECIALS = "@#$%&*!+-_?"

def _get_setting(name, default=""):
    # Deployment secrets are intentionally only a one-time bootstrap/
    # infrastructure configuration. Per-firm and operating settings are
    # controlled from the Super User UI and stored in Firestore.
    try:
        v = st.secrets.get(name, default)
        return str(v) if v is not None else default
    except Exception:
        return default


_SYSTEM_DEFAULTS = {
    "superuser_id": "superuser",
    "superuser_password_hash": "cff792fb0046b07d3b968bcc88c2648c9ea52d184773d96cf16c6873c862fc73",
    "registration_key_hours": 2,
    "trial_days": 3,
    "included_users": 3,
}

def _load_system_settings():
    raw = _get_doc("system_settings")
    if not isinstance(raw, dict):
        raw = {}
    out = dict(_SYSTEM_DEFAULTS)
    out.update(raw)
    return out

def _save_system_settings(settings):
    _set_doc("system_settings", settings)

def _superuser_credentials():
    settings = _load_system_settings()
    sid = str(settings.get("superuser_id") or _get_setting("SUPERUSER_ID", "superuser"))
    ph = str(settings.get("superuser_password_hash") or "")
    if not ph:
        return sid, _get_setting("SUPERUSER_PASSWORD", "SV@Super2026!")
    return sid, ph

def _superuser_login_valid(username, password):
    settings = _load_system_settings()
    sid = str(settings.get("superuser_id") or _get_setting("SUPERUSER_ID", "superuser"))
    supplied_hash = _hash_secret(password)
    stored_hash = str(settings.get("superuser_password_hash") or "")
    if stored_hash:
        return username == sid and supplied_hash == stored_hash
    return username == sid and password == _get_setting("SUPERUSER_PASSWORD", "SV@Super2026!")

def _system_policy():
    s = _load_system_settings()
    try:
        reg_hours = max(1, int(s.get("registration_key_hours", 2)))
    except Exception:
        reg_hours = 2
    try:
        trial_days = max(1, int(s.get("trial_days", 3)))
    except Exception:
        trial_days = 3
    try:
        included_users = max(1, int(s.get("included_users", 3)))
    except Exception:
        included_users = 3
    return {
        "registration_key_hours": reg_hours,
        "trial_days": trial_days,
        "included_users": included_users,
    }


def _generate_key(length):
    if length not in (12, 24):
        raise ValueError("Key length must be 12 or 24")
    pools = ["ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz", "0123456789", _KEY_SPECIALS]
    chars = [secrets.choice(p) for p in pools]
    allchars = "".join(pools)
    chars += [secrets.choice(allchars) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _hash_secret(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _get_client_ip():
    """Best-effort client IP from Streamlit/reverse-proxy headers."""
    try:
        headers = st.context.headers
        for key in ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP", "True-Client-IP"):
            val = headers.get(key)
            if val:
                return str(val).split(",")[0].strip()
        val = headers.get("Remote-Addr") or headers.get("remote-addr")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return "UNKNOWN"


def _load_registry():
    reg = _get_doc("firm_registry")
    reg.setdefault("firms", {})
    reg.setdefault("registrations", {})
    return reg


def _save_registry(reg):
    _set_doc("firm_registry", reg)


def _firm_id_from_name(name):
    base = re.sub(r"[^A-Z0-9]+", "", str(name or "").upper())[:12] or "FIRM"
    reg = _load_registry()
    firms = reg.get("firms", {})
    if base not in firms:
        return base
    for i in range(1, 10000):
        candidate = f"{base[:8]}{i:04d}"
        if candidate not in firms:
            return candidate
    return f"FIRM{secrets.token_hex(4).upper()}"


def _all_firm_users(firm_id):
    doc = _get_doc(f"users_{_safe_doc_id(firm_id)}")
    raw = doc.get("users", {}) or {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, str):
            out[k] = {"password": v, "mobile": "", "email": "", "user_key": "", "role": "User", "rights": []}
        elif isinstance(v, dict):
            out[k] = dict(v)
            out[k].setdefault("password", "")
            out[k].setdefault("mobile", "")
            out[k].setdefault("email", "")
            out[k].setdefault("user_key", "")
            out[k].setdefault("role", "User")
            out[k].setdefault("rights", [])
            out[k].setdefault("active", True)
    return out


def _save_firm_users(firm_id, users):
    _set_doc(f"users_{_safe_doc_id(firm_id)}", {"users": users})
    return True


def _load_users():
    firm_id = st.session_state.get("_firm_id", "XYZ")
    users = _all_firm_users(firm_id)
    # One-time compatibility migration of old app_users into XYZ firm.
    if firm_id == "XYZ" and not users:
        old = _get_doc("app_users")
        raw = old.get("users", {}) if isinstance(old, dict) else {}
        if raw:
            users = {}
            for k, v in raw.items():
                if isinstance(v, str):
                    users[k] = {"password": v, "mobile": "", "email": "", "user_key": "", "role": "Firm Admin" if k == "admin" else "User", "rights": ["masters","upload","results","help","user_master"] if k == "admin" else ["upload","results"]}
                elif isinstance(v, dict):
                    users[k] = dict(v)
            _save_firm_users("XYZ", users)
    return users


def _save_users(users_dict):
    return _save_firm_users(st.session_state.get("_firm_id", "XYZ"), users_dict)


def _package_catalog():
    reg = _load_registry()
    prices = reg.get("pricing", {}) or {}
    return {
        "4_MONTHS": {"label": "4 Months", "months": 4, "price": float(prices.get("4_MONTHS", 0) or 0)},
        "6_MONTHS": {"label": "6 Months", "months": 6, "price": float(prices.get("6_MONTHS", 0) or 0)},
        "1_YEAR": {"label": "1 Year", "months": 12, "price": float(prices.get("1_YEAR", 0) or 0)},
    }


def _extra_user_price():
    reg = _load_registry()
    return float((reg.get("pricing", {}) or {}).get("EXTRA_USER", 0) or 0)


def _subscription_end(start_dt, months):
    # Calendar-month package, inclusive of payment day.
    end_exclusive = pd.Timestamp(start_dt) + pd.DateOffset(months=int(months))
    return (end_exclusive - pd.Timedelta(seconds=1)).to_pydatetime()


def _firm_access(firm):
    now = datetime.now()
    status = str(firm.get("subscription_status", "")).upper()
    if status == "ACTIVE" and firm.get("subscription_end"):
        try:
            return now <= datetime.fromisoformat(str(firm["subscription_end"]))
        except Exception:
            return False
    if status == "ACTIVE_TRIAL" and firm.get("trial_end"):
        try:
            return now < datetime.fromisoformat(str(firm["trial_end"]))
        except Exception:
            return False
    return False


def _refresh_subscription_status(firm_id):
    reg = _load_registry()
    firm = (reg.get("firms", {}) or {}).get(firm_id)
    if not firm:
        return None, reg
    now = datetime.now()
    changed = False
    if firm.get("registration_key_expires_at"):
        try:
            if now >= datetime.fromisoformat(str(firm["registration_key_expires_at"])) and firm.get("status") == "PENDING_REGISTRATION":
                firm["status"] = "REGISTRATION_EXPIRED"
                changed = True
        except Exception:
            pass
    if firm.get("subscription_status") == "ACTIVE_TRIAL" and firm.get("trial_end"):
        try:
            if now >= datetime.fromisoformat(str(firm["trial_end"])):
                firm["subscription_status"] = "EXPIRED"
                changed = True
        except Exception:
            pass
    if firm.get("subscription_status") == "ACTIVE" and firm.get("subscription_end"):
        try:
            if now > datetime.fromisoformat(str(firm["subscription_end"])):
                firm["subscription_status"] = "EXPIRED"
                changed = True
        except Exception:
            pass
    if changed:
        reg.setdefault("firms", {})[firm_id] = firm
        _save_registry(reg)
    return firm, reg


def _send_sms(phone, message):
    sid = _get_setting("TWILIO_ACCOUNT_SID", "")
    token = _get_setting("TWILIO_AUTH_TOKEN", "")
    from_no = _get_setting("TWILIO_FROM", "")
    if not (sid and token and from_no and phone):
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        r = requests.post(url, data={"From": from_no, "To": phone, "Body": message[:1500]}, auth=(sid, token), timeout=20)
        return r.ok
    except Exception:
        return False


def _notify_admin(subject, body, mobile=""):
    # Always log in Firestore; optional SMTP email/SMS are sent if configured.
    reg = _load_registry()
    notes = reg.get("notifications", []) or []
    notes.append({"time": _now_iso(), "subject": subject, "body": body})
    reg["notifications"] = notes[-500:]
    _save_registry(reg)
    host = _get_setting("SMTP_HOST", "")
    user = _get_setting("SMTP_USER", "")
    pwd = _get_setting("SMTP_PASSWORD", "")
    to = _get_setting("ADMIN_EMAIL", "")
    if host and user and pwd and to:
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = user
            msg["To"] = to
            msg.set_content(body)
            with smtplib.SMTP(host, int(_get_setting("SMTP_PORT", "587")), timeout=20) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        except Exception:
            pass
    _send_sms(mobile, subject + " - " + body)


def _notify_firm(firm, subject, body):
    email = str(firm.get("email", "") or "").strip()
    mobile = str(firm.get("mobile", "") or "").strip()
    host = _get_setting("SMTP_HOST", "")
    user = _get_setting("SMTP_USER", "")
    pwd = _get_setting("SMTP_PASSWORD", "")
    if host and user and pwd and email:
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = user
            msg["To"] = email
            msg.set_content(body)
            with smtplib.SMTP(host, int(_get_setting("SMTP_PORT", "587")), timeout=20) as s:
                s.starttls(); s.login(user, pwd); s.send_message(msg)
        except Exception:
            pass
    _send_sms(mobile, subject + " - " + body)


def _audit(action, firm_id="", username="", details=None):
    reg = _load_registry()
    logs = reg.get("audit_logs", []) or []
    logs.append({"time": _now_iso(), "action": action, "firm_id": firm_id, "username": username, "details": details or {}, "ip": _get_client_ip()})
    reg["audit_logs"] = logs[-2000:]
    _save_registry(reg)


def _find_firm_for_user(username):
    reg = _load_registry()
    for fid, firm in (reg.get("firms", {}) or {}).items():
        if str(firm.get("owner_username", "")).lower() == username.lower():
            return fid, firm
        users = _all_firm_users(fid)
        if username in users:
            return fid, firm
    return None, None


def _create_firm_registration(firm_name, owner_username, password, mobile, email, address):
    reg = _load_registry()
    firms = reg.setdefault("firms", {})
    registrations = reg.setdefault("registrations", {})
    mobile_norm = re.sub(r"\D", "", mobile or "")
    ip = _get_client_ip()
    if len(mobile_norm) < 10:
        return False, "Mobile number must be valid (10 digits).", None
    # One-time registration by mobile and, when available, by client IP.
    for fid, f in firms.items():
        if re.sub(r"\D", "", str(f.get("mobile", ""))) == mobile_norm and mobile_norm:
            return False, "This mobile number has already been registered.", None
        if ip != "UNKNOWN" and f.get("registration_ip") == ip:
            return False, "This IP address has already been used for registration.", None
    for rid, r in registrations.items():
        if re.sub(r"\D", "", str(r.get("mobile", ""))) == mobile_norm:
            return False, "This mobile number already has a registration request.", None
        if ip != "UNKNOWN" and r.get("registration_ip") == ip:
            return False, "This IP address already has a registration request.", None
        if str(r.get("owner_username", "")).lower() == owner_username.lower():
            return False, "Username already exists in a registration request.", None
    if not firm_name.strip() or not owner_username.strip() or not password:
        return False, "Firm Name, Owner Username and Password are required.", None
    # Username must be unique across all firms and pending registrations.
    owner_key = owner_username.strip().lower()
    if owner_key == _superuser_credentials()[0].lower():
        return False, "This username is reserved for the Super User.", None
    for _fid, _firm in firms.items():
        if str(_firm.get("owner_username", "")).lower() == owner_key or owner_key in _all_firm_users(_fid):
            return False, "This username is already in use.", None
    if len(password) < 6:
        return False, "Password must be at least 6 characters.", None
    fid = _firm_id_from_name(firm_name)
    reg_key = _generate_key(12)
    policy = _system_policy()
    expires = datetime.now() + timedelta(hours=policy["registration_key_hours"])
    request_id = f"REG-{secrets.token_hex(6).upper()}"
    registrations[request_id] = {
        "request_id": request_id, "firm_id": fid, "firm_name": firm_name.strip(),
        "owner_username": owner_username.strip().lower(), "password": password,
        "mobile": mobile_norm, "email": email.strip(), "address": address.strip(),
        "registration_key": reg_key, "registration_key_hash": _hash_secret(reg_key),
        "registration_ip": ip, "created_at": _now_iso(), "expires_at": expires.isoformat(timespec="seconds"),
        "status": "PENDING", "included_users": policy["included_users"],
    }
    _save_registry(reg)
    _notify_admin("New Firm Registration", f"Firm: {firm_name}\nOwner: {owner_username}\nMobile: {mobile_norm}\nRequest: {request_id}\nRegistration Key: {reg_key}\nIP: {ip}", mobile_norm)
    return True, "Registration submitted. Your 12-character key is valid for 2 hours and requires Super User authentication.", {"request_id": request_id, "key": reg_key, "firm_id": fid}


def _approve_registration(request_id):
    reg = _load_registry()
    req = (reg.get("registrations", {}) or {}).get(request_id)
    if not req or req.get("status") != "PENDING":
        return False, "Registration request not found or already processed."
    try:
        if datetime.now() >= datetime.fromisoformat(str(req["expires_at"])):
            req["status"] = "EXPIRED"
            _save_registry(reg)
            return False, "The 2-hour registration key has expired."
    except Exception:
        pass
    fid = req["firm_id"]
    trial_start = datetime.now()
    policy = _system_policy()
    trial_end = trial_start + timedelta(days=policy["trial_days"])
    activation_key = _generate_key(24)
    reg.setdefault("firms", {})[fid] = {
        "firm_id": fid, "firm_name": req["firm_name"], "status": "ACTIVE",
        "created_at": _now_iso(), "owner_username": req["owner_username"], "mobile": req["mobile"],
        "email": req.get("email", ""), "address": req.get("address", ""), "registration_ip": req.get("registration_ip", ""),
        "registration_key": req["registration_key"], "registration_key_expires_at": req["expires_at"],
        "activation_key": activation_key, "activation_key_hash": _hash_secret(activation_key),
        "subscription_status": "ACTIVE_TRIAL", "trial_start": trial_start.isoformat(timespec="seconds"),
        "trial_end": trial_end.isoformat(timespec="seconds"), "subscription_start": "", "subscription_end": "",
        "included_users": policy["included_users"], "extra_users": 0,
    }
    _set_doc(f"firm_data_{_safe_doc_id(fid)}", {"projects": [], "contracts": []})
    _save_firm_users(fid, {req["owner_username"]: {
        "password": req["password"], "mobile": req["mobile"], "email": req.get("email", ""),
        "user_key": "", "role": "Firm Admin", "rights": ["masters","upload","results","help","user_master"], "active": True
    }})
    req["status"] = "APPROVED_TRIAL"
    req["approved_at"] = _now_iso()
    req["activation_key"] = activation_key
    reg["registrations"][request_id] = req
    _save_registry(reg)
    _notify_admin("Firm Trial Activated", f"Firm: {req['firm_name']}\nFirm ID: {fid}\nTrial ends: {trial_end}\nActivation Key: {activation_key}", req.get("mobile", ""))
    _notify_firm(reg["firms"][fid], "CCI Trial Activated", f"Your 3-day trial is active until {trial_end.strftime('%d-%m-%Y %H:%M:%S')}.\nFirm ID: {fid}\n24-character activation key: {activation_key}")
    return True, f"Trial activated for {req['firm_name']} until {trial_end.strftime('%d-%m-%Y %H:%M')}."


def _has_right(right):
    if st.session_state.get("_auth_role") == "SUPERUSER":
        return True
    return right in (st.session_state.get("_user_rights") or [])


def _set_firm_session(fid, username, user_rec, role="User"):
    st.session_state._firm_id = fid
    st.session_state._logged_user = username
    st.session_state._auth_role = "FIRM_USER"
    st.session_state._user_role = role
    st.session_state._user_rights = user_rec.get("rights", []) or []


def _try_firm_login(username, password):
    fid, firm = _find_firm_for_user(username)
    if not fid:
        return False, "Invalid username or password."
    firm, _ = _refresh_subscription_status(fid)
    users = _all_firm_users(fid)
    rec = users.get(username)
    if not rec or str(rec.get("password", "")) != str(password):
        return False, "Invalid username or password."
    if not bool(rec.get("active", True)):
        return False, "This user account has been disabled by the firm administrator."
    if not _firm_access(firm):
        return False, "Your Subscription period is over, Please recharge again" if str(firm.get("subscription_status", "")).upper() == "EXPIRED" else "Firm is not activated yet. Please contact support."
    _set_firm_session(fid, username, rec, rec.get("role", "User"))
    _audit("LOGIN", fid, username, {"subscription_status": firm.get("subscription_status", "")})
    return True, ""
# ─── CLEAN CONTRACT CARD STYLES ──────────────────────────────────────────────
st.markdown("""
<style>
.clean-contract-card { background:#ffffff !important; color:#111827 !important; border:1px solid #dbe3ea !important; border-radius:14px !important; padding:16px !important; margin:10px 0 8px !important; box-shadow:0 3px 12px rgba(15,23,42,.06) !important; }
.clean-contract-card * { box-sizing:border-box; }
.contract-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; padding-bottom:10px; border-bottom:1px solid #e5e7eb; }
.contract-no { font-size:16px; font-weight:800; color:#111827; }
.contract-party { margin-top:4px; font-size:12px; color:#4b5563; font-weight:600; }
.contract-project { background:#ecfdf5; border:1px solid #fed7aa; color:#9a3412; padding:5px 10px; border-radius:18px; font-size:11px; font-weight:700; max-width:52%; text-align:center; }
.basic-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px 14px; padding:12px 0; }
.basic-grid div { display:flex; justify-content:space-between; gap:10px; padding:6px 8px; background:#f8fafc; border-radius:7px; font-size:11px; }
.basic-grid span { color:#64748b; }
.basic-grid b { color:#111827; text-align:right; }
.condition-grid { display:grid; grid-template-columns:1fr; gap:8px; }
.condition-box { border:1px solid #e5e7eb; border-radius:9px; padding:8px 10px; background:#fafafa; overflow:hidden; }
.condition-title { font-size:12px; font-weight:800; color:#9a3412; margin-bottom:3px; }
.condition-sub { font-size:10px; color:#6b7280; margin-bottom:7px; }
.slab-row { display:grid; grid-template-columns:70px minmax(0,1fr) minmax(0,1fr); gap:8px; padding:4px 7px; margin-top:3px; background:#ffffff; border:1px solid #eef2f7; border-radius:6px; font-size:11px; overflow:hidden; }
.slab-row span { color:#6b7280; }
.slab-row b { color:#374151; }
.empty-slab { font-size:11px; color:#9ca3af; font-style:italic; padding:4px 0; }
@media (min-width: 900px) { .condition-grid { grid-template-columns:1fr; } }
</style>
""", unsafe_allow_html=True)

# ─── LOGIN / REGISTRATION GATE ───────────────────────────────────────────────
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "_login_error" not in st.session_state:
    st.session_state._login_error = ""
if "_logged_user" not in st.session_state:
    st.session_state._logged_user = ""
if "_auth_role" not in st.session_state:
    st.session_state._auth_role = ""
if "_firm_id" not in st.session_state:
    st.session_state._firm_id = ""


def _do_login():
    u = st.session_state.get("_lu", "").strip()
    p = st.session_state.get("_lp", "")
    if _superuser_login_valid(u, p):
        sid = _load_system_settings().get("superuser_id", u)
        st.session_state.authenticated = True
        st.session_state._auth_role = "SUPERUSER"
        st.session_state._logged_user = sid
        st.session_state._firm_id = ""
        st.session_state._user_rights = ["*"]
        st.session_state._login_error = ""
        _audit("SUPERUSER_LOGIN", "", sid, {})
        return
    ok, msg = _try_firm_login(u, p)
    if ok:
        st.session_state.authenticated = True
        st.session_state._login_error = ""
    else:
        st.session_state._login_error = msg


def _do_register():
    ok, msg, info = _create_firm_registration(
        st.session_state.get("_rfname", ""), st.session_state.get("_rowner", ""),
        st.session_state.get("_rpass", ""), st.session_state.get("_rmobile", ""),
        st.session_state.get("_remail", ""), st.session_state.get("_raddress", "")
    )
    st.session_state._reg_result = info if ok else None
    st.session_state._reg_error = "" if ok else msg
    st.session_state._reg_success = msg if ok else ""


if not st.session_state.authenticated:
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"]{
        background:
        radial-gradient(circle at 82% 18%, rgba(24,183,215,.12), transparent 26%),
        radial-gradient(circle at 18% 82%, rgba(217,169,59,.10), transparent 25%),
        linear-gradient(135deg,#f8fbff 0%,#edf4fb 55%,#e7eef8 100%);
    }
    [data-testid="stHeader"]{background:transparent}
    .block-container{max-width:1320px!important;padding:.55rem .8rem .35rem!important}
    .sv-shell{
        position:relative;overflow:hidden;min-height:720px;
        border-radius:28px;
        background:linear-gradient(119deg,#071a3a 0%,#0b2d63 43%,#f8fbff 43.2%,#f8fbff 100%);
        border:1px solid rgba(8,32,67,.12);
        box-shadow:0 26px 70px rgba(7,26,58,.18);
    }
    .sv-shell:after{
        content:"";position:absolute;left:-250px;bottom:-330px;width:620px;height:620px;
        border:1px solid rgba(241,204,114,.55);border-radius:50%;
        box-shadow:0 0 0 22px rgba(18,103,199,.12),0 0 0 45px rgba(241,204,114,.07);
        pointer-events:none;
    }
    .sv-top{
        position:relative;z-index:3;height:84px;display:flex;align-items:center;
        justify-content:space-between;padding:10px 30px 9px 34px;color:#fff;
        border-bottom:1px solid rgba(241,204,114,.55);
    }
    .sv-brand{display:flex;align-items:center;gap:15px}
    .sv-brand-logo{
        width:61px;height:61px;object-fit:contain;border-radius:13px;background:#fff;
        padding:4px;box-shadow:0 8px 24px rgba(0,0,0,.24);
    }
    .sv-brand-name{font-size:25px;font-weight:950;letter-spacing:1px;line-height:1}
    .sv-brand-name span{color:#f1cc72}
    .sv-brand-tag{margin-top:6px;font-size:9px;letter-spacing:2.4px;text-transform:uppercase;color:#b9c9e0;font-weight:700}
    .sv-trust{
        padding:9px 17px;border:1px solid rgba(241,204,114,.42);border-radius:999px;
        background:rgba(3,17,42,.42);font-size:11px;font-weight:800;
    }
    .sv-trust b{color:#f1cc72;margin:0 5px}
    .sv-main{position:relative;z-index:2;display:grid;grid-template-columns:44% 56%;min-height:510px}
    .sv-left{color:#fff;padding:45px 38px 25px 54px}
    .sv-eyebrow{color:#f1cc72;font-size:10px;font-weight:900;letter-spacing:2.1px;text-transform:uppercase;margin-bottom:13px}
    .sv-left h1{font-size:39px;line-height:1.04;margin:0;color:#fff;font-weight:950;letter-spacing:-.6px}
    .sv-left h1 span{color:#f1cc72}
    .sv-divider{width:84px;height:4px;border-radius:5px;background:linear-gradient(90deg,#d9a93b,#f1cc72);margin:17px 0 19px}
    .sv-lead{max-width:470px;color:#d7e4f6;font-size:13px;line-height:1.6}
    .sv-features{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;max-width:475px;margin-top:23px}
    .sv-feature{
        min-height:78px;padding:10px 8px;border:1px solid rgba(255,255,255,.16);
        border-radius:14px;background:rgba(255,255,255,.065);backdrop-filter:blur(5px);text-align:center;
    }
    .sv-feature .ico{
        width:34px;height:34px;margin:0 auto 6px;border-radius:10px;
        display:flex;align-items:center;justify-content:center;
        border:1px solid rgba(241,204,114,.6);color:#f1cc72;font-size:16px;background:rgba(0,0,0,.14)
    }
    .sv-feature .txt{font-size:9px;line-height:1.22;font-weight:800;color:#eaf2fb}
    .sv-left-bottom{margin-top:20px;font-size:9px;color:#9fb5d1;letter-spacing:.55px}
    .sv-right{padding:26px 42px 25px 27px;display:flex;align-items:center;justify-content:center}
    .sv-auth-card{
        width:min(600px,100%);background:rgba(255,255,255,.96);border:1px solid rgba(19,71,130,.13);
        border-radius:23px;padding:22px 27px 22px;box-shadow:0 22px 55px rgba(12,44,84,.16);
    }
    .sv-card-title{text-align:center;color:#10254a;font-size:23px;font-weight:950;letter-spacing:.4px}
    .sv-title-line{display:flex;align-items:center;justify-content:center;gap:11px;margin:6px 0 9px}
    .sv-title-line i{width:45px;height:2px;background:#d9a93b;display:block;border-radius:2px}
    .sv-card-sub{text-align:center;color:#718096;font-size:9.5px;margin-bottom:12px}
    .sv-mini-lock{
        width:36px;height:36px;margin:0 auto 7px;display:flex;align-items:center;justify-content:center;
        border-radius:11px;color:#fff;background:linear-gradient(145deg,#0b2d63,#1267c7);
        box-shadow:0 7px 17px rgba(18,103,199,.22);font-size:17px;
    }
    [data-baseweb="tab-list"]{
        gap:5px!important;background:#edf3fa!important;padding:5px!important;
        border-radius:13px!important;border:1px solid #dce6f1!important;
    }
    [data-baseweb="tab"]{
        border-radius:10px!important;min-height:40px!important;padding:6px 13px!important;
        font-size:10.5px!important;font-weight:900!important;color:#36506f!important;
    }
    [data-baseweb="tab"][aria-selected="true"]{
        background:linear-gradient(135deg,#071a3a,#1267c7)!important;color:#fff!important;
        box-shadow:0 7px 17px rgba(11,45,99,.18)!important;
    }
    [data-baseweb="tab-highlight"]{background:#d9a93b!important;height:3px!important}
    div[data-testid="stTextInput"] label,div[data-testid="stNumberInput"] label,
    div[data-testid="stDateInput"] label,div[data-testid="stSelectbox"] label{
        font-size:10px!important;font-weight:800!important;color:#344b68!important;
    }
    div[data-testid="stTextInput"] input,div[data-testid="stNumberInput"] input{
        border-radius:10px!important;
    }
    .stButton>button[kind="primary"]{
        border-radius:11px!important;border:0!important;
        background:linear-gradient(100deg,#071a3a,#1267c7)!important;
        box-shadow:0 10px 22px rgba(18,103,199,.22)!important;font-weight:900!important;
    }
    .stButton>button[kind="primary"]:hover{box-shadow:0 13px 27px rgba(18,103,199,.30)!important;transform:translateY(-1px)}
    .sv-about{
        position:relative;z-index:3;margin:0 35px 21px;display:grid;grid-template-columns:1.4fr .9fr;gap:13px;
    }
    .sv-about-card{
        background:rgba(255,255,255,.96);border:1px solid #dbe5f0;border-radius:15px;
        padding:12px 16px;box-shadow:0 9px 25px rgba(8,33,67,.08)
    }
    .sv-about-card.gold{border-left:4px solid #d9a93b;background:linear-gradient(135deg,#fffdf7,#fff)}
    .sv-about-title{color:#10254a;font-size:11.5px;font-weight:950;margin-bottom:3px}
    .sv-about-text{color:#64748b;font-size:9px;line-height:1.5}
    .sv-footer{
        position:relative;z-index:3;background:#061a3c;color:#d5e0ef;border-top:2px solid #d9a93b;
        min-height:43px;padding:10px 29px;display:flex;align-items:center;justify-content:space-between;font-size:8.5px;
    }
    .sv-footer strong{color:#fff;letter-spacing:1px}.sv-footer .gold{color:#f1cc72}
    @media(max-width:900px){
        .sv-shell{background:linear-gradient(160deg,#071a3a 0%,#0b2d63 29%,#f8fbff 29.2%,#f8fbff 100%)}
        .sv-main{grid-template-columns:1fr}.sv-left{padding:30px 30px 10px}.sv-left h1{font-size:31px}
        .sv-right{padding:15px 25px 28px}.sv-about{grid-template-columns:1fr}
    }
    @media(max-width:620px){
        .block-container{padding-left:.35rem!important;padding-right:.35rem!important}
        .sv-top{height:auto;padding:11px 15px}.sv-brand-name{font-size:18px}.sv-brand-logo{width:49px;height:49px}
        .sv-trust{display:none}.sv-left{padding:23px 19px 10px}.sv-features{grid-template-columns:repeat(2,1fr)}
        .sv-right{padding:8px 10px 20px}.sv-auth-card{padding:17px 13px}
        .sv-about{margin:0 12px 14px}.sv-footer{display:block;text-align:center;line-height:1.8}
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="sv-shell">
      <div class="sv-top">
        <div class="sv-brand">
          <img class="sv-brand-logo" src="__SV_LOGO__" alt="Softview Technologies">
          <div>
            <div class="sv-brand-name">SOFTVIEW <span>TECHNOLOGIES</span></div>
            <div class="sv-brand-tag">Enterprise Business Solutions</div>
          </div>
        </div>
        <div class="sv-trust">🛡️ Secure <b>•</b> Reliable <b>•</b> Trusted</div>
      </div>

      <div class="sv-main">
        <div class="sv-left">
          <div class="sv-eyebrow">Smart Business • Accurate Decisions</div>
          <h1>CCI WORKING<br><span>Calculation Utility</span></h1>
          <div class="sv-divider"></div>
          <div class="sv-lead">
            A professional workspace for CCI contracts, lifting, EMD, CD/CC,
            FIFO allocation and structured business reporting — designed for
            accuracy, control and efficient operations.
          </div>
          <div class="sv-features">
            <div class="sv-feature"><div class="ico">📑</div><div class="txt">Contracts<br>Management</div></div>
            <div class="sv-feature"><div class="ico">🚚</div><div class="txt">GRN &amp; Lifting<br>Tracking</div></div>
            <div class="sv-feature"><div class="ico">💰</div><div class="txt">EMD &amp;<br>Payments</div></div>
            <div class="sv-feature"><div class="ico">📊</div><div class="txt">CD/CC<br>Charges</div></div>
            <div class="sv-feature"><div class="ico">▱</div><div class="txt">FIFO<br>Allocation</div></div>
            <div class="sv-feature"><div class="ico">📋</div><div class="txt">Reports &amp;<br>Export</div></div>
          </div>
          <div class="sv-left-bottom">Precision • Security • Control • Professional Reporting</div>
        </div>

        <div class="sv-right">
          <div class="sv-auth-card">
            <div class="sv-mini-lock">🔐</div>
            <div class="sv-card-title">Secure Access</div>
            <div class="sv-title-line"><i></i><span style="color:#d9a93b">●</span><i></i></div>
            <div class="sv-card-sub">Sign in to continue to your business workspace</div>
          </div>
        </div>
      </div>

      <div class="sv-about">
        <div class="sv-about-card">
          <div class="sv-about-title">About the Utility</div>
          <div class="sv-about-text">
            CCI Working Calculation Utility brings contract conditions, GRN/lifting,
            EMD and payment allocation, cash discount, carrying cost and reporting
            into one controlled business workspace.
          </div>
        </div>
        <div class="sv-about-card gold">
          <div class="sv-about-title">SOFTVIEW TECHNOLOGIES</div>
          <div class="sv-about-text">
            Enterprise software solutions focused on reliable calculations,
            structured data, controlled access and professional business reporting.
          </div>
        </div>
      </div>

      <div class="sv-footer">
        <div>© 2026 <strong>SOFTVIEW TECHNOLOGIES</strong>. All rights reserved.</div>
        <div><span class="gold">Innovation</span> &nbsp;|&nbsp; Technology &nbsp;|&nbsp; Excellence</div>
      </div>
    </div>
    """.replace("__SV_LOGO__", "__SV_LOGO_DATA__".replace("__SV_LOGO_DATA__", "data:image/png;base64,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u/XWlQfbanzMVc6lEyU0tm0kZG/vsoq8+/HHX/d/ve5Nb3zj+x5//PqN69euXVeEW265dPXq1Qfue+gFL3z+B3/wi6/s7S+tje2BqgY0ud21sgeUjNWlyHsfe9/1gyNVZdteunyJZGx55+1XLmezwfFST4wvEoLrN6697/HHF83i4PAwNIEmMba3X71y9epVD+yR1FLWSO1PXnvivY89qdrE1qK1FF00ePDeey9dumzCsJJiL1We8tH1xPVrBzdvHiyX+80lQNrl0b333L23WLhqELnBlCTB743aqEJF5Lfe/rY3vunNv/OO33nnO99184YLfuP2K1cffvihZz7zmS98wfPvufNuT1v9ubgu7FSe57+MyzYyasCNg8Mnr10X1biMSjXQiVsmAlVQXSEOKoFZEkGy5zEEkbfcsn/n7bcne3OEte/qbesutklho0FEnrxx/U1vevPb3vb2d73nXY8/8cRyuWya5tLlW++5684H73/gRR/84ofvuVeSOGPbUUaL1emOqwZPcUTEEs4gyea6cJmyWeypoBX+9m+/9U1vfuM73/GuR9/3+I2bN4y2v3fpypWr995917Oe/aznPf959169KiKHMXIZQ6PokfsTXY4Z72gmYNsePfbYk61RFZEWQuOth3vuuhOiUxtatPa9jz0RY4Tq9Rs327a9/Y47SMa2zQJvaC1aosCIWUzCXGQUisj+/r6KxGj33HXn5b19Ea8YJdiOKhzOKZAmi5Q8+vjjb3nLW976O2991zvfdf3GjcPDQ9Wwv7d39erVu++++4EHHnz+857/wN13SYJAxRAELpwD9oL2MvJpXOHfeRl4fOIUMe8d/3MMVite9piBP2XjXPz2Kdvn0bUboS0lgkvR//7Wd/7u49cNvHp57yXPe+QOSOMAU0YzWx4eaIA/THUWtwYJQWgCadA4ViMgSO6ldnafa/mtA5izxZrE6KeAiRdaaWjEFdrEApTG1oyO+oZIS1za218sQitJUvqG4A2/99hvvePd2oQre82HfMAz79oPCxExhkTudF81c+lKSBggwE+j6FT1CuMUuXYqkJwzL6e4FRvEGOq+Z8OmDrM2qFCj0BDc7kG9kKRBGVsIVZUSXMLXgwZNmNqu1BAFrvcovXJ3bwtdxbl1S/uJf81bI9FVfTtuVdImIhnZmjHGGBaLvWavMhSHy0NGahDVZMean5cOSKnZyGhNPzGfPjbwKCk8R6/eqlAoJrGNCKCwCYu2bckWCH4IUQROJRATIkbTIJAmRWOZnWTZ5Gmw+xTZvGbmhBgnW7qKQL5Ty5xZFKdcV+RMFRQyxkjIfi6xTEzCdtm2QoRmxdQfl80Gyn1mFkJo26VCEdSENAkIqqnuCPUuvahAN8+h2DusV8Bh691tV/lwqa79/b2BEdfgp7UolNDTHJw6JIqrNenNJGGJpMNh1rbWKrC/d7ny1UfxKLZRRBbNIjn/ZZ7tmsGvpA1eGFVUk9nbeiHW7WXXjqgIqA9PueA0je1aIVcpZm0mWCVBLekALJKqzklAz+jjaYxBtamuU/+2ZXsklCbsiRi0r8LXf6QbDuzT6x3PKR7Uz++pcKGeCdcjlcLWj6mgARF2JPrff/sd73jyRqTdcXn/Q5//yB2Qhbj5G2PbLg9vNqFxjG6jITICDUJwL1ZVVQpjFEFA4yrkQNM5IcMzT5EmL0GHmTfeqg5Qd/Azl6pP9sVGCqhAJIjY2foh9mDiJtIyEnQPkq4CnjtY6MPJex7qkE3G4adXaSjWDwaPGWsqir1MNHMlilSZMQSmGEXONVYf/4LBtQspdnBwgIxwUSwo0jRuZKDuduZxRlhj+cqqZ9YbmNRgosU2qjaJX0d1YfDufd41cNCYq244xqrRRXr2aSquGsea+J0dU0OSCYJ7GKTjXKxtrx0eWDRF0KZR1WgGmB8j3pNToNGFhibZZScJ88F9qatexzaaUCht25q1IahQ9vb3VcMgYug3m/JjRmzj0eGBqLbLlmwXi70lWte9TNKqLpms6kYeZlDVtm2XRzdCaJANY/202Nvb67S5NoKk2nZp1voot8tlE5r9/X2S2dZjbY713W5k3WheRbQJRlsul227zDxuwCJFNDRCCZ5fZOHX4iV17bligLJsW8QUFRjbFNsmMQAIpHVCpmacf57XJoas8KOKpmk8YJL+3r3OR/ELaJrGiV1Hy9Y7vABVFy6L4dSYaEuFAA0kIDnqlKFqk93Jjv+SyTZBBQhQpcXDgxvL5VKgITQKFaiRCppZjK2Iw4cVRsdccB3j3hXGk5IHGot2cHAjhKZtW1UsFnvOgR9jxjsjzY4rnYWYzMhFE6wjqaRF59BnByODMFd3atulJCFnFQuqjayczPPY0zw+oTCEoETbLtv2wMwS10+JJDSPuGzNWtXQNKFpFqFpkrnh2hGLKbDXVoq6O8EiTB1DXDdIKrYhihlvsbo8Dkyn+RGbgX+5s9nNdDgxMiamZZpnzMRw71pAvMMA+rITEVK9NJCx6sxqowICKmZIzF82iRacEBQZ0k2DqpEwTSGnIsZW4XxijbEFVTVYjFGiJiUwiDAIAmA0XYdv9HywO4ZmZ6TB/l5/ehiWqVBARlpjRXuIcUOhz5UoMioHH1v8ltmh9ASEmG4/ES5dugRTqCdkXoDtaaaMvmP9MtDniyZIuYjb5PREubQPEhZBSPbFfr8SAswkIDiQIsMX+kea9Cxus28GTRVuwBhC4zzzPdkTgSXrbUduWRPU7RAFUApUo1HBKVvU7r41BMbkakg2HQu8y+QGh3f+qKQTFULY378EVdszMmr6cu0TrnzTVw0iFgJILrBYNHsO9RAxZNvPolyYjJqpLu3sokPQQKBpFi7dHGM0Yz7Zywozjt0bbE9JsTk07h3V0sCggEgwWhCFOmzJ+misIq+4+2pVjdFEYtMEkXTeqyajPI8aHdukWTS20xlMDlmqRhNxbeuVqYcJtTf9KpawGpq9VExiholRxFQbUrQVP+eylc+okz1KJPqroydK2Onbwk9Eh2Owafb290XEDKrerXfxHJILsyiCEBTURExXgIAOu5NIn2xuPxZCWCwaWWm2oj83RjCn1Pv3jlV69EAYMgB8JwldX9LMIJobXl3jZqBM00sA4bxiIWWxaHJ50tZydJX9xSJGC9lrfGqhFdP3cxGKruj8Tm3XxaU35sRNqfWMAXBzql+bzwnnmWtKy5NnMR0vleqsIYRoWY6RMTUMIUbPCiTJqrpwGy2ouqc7JMFiPXwOArRGaOpDOl4JGqxN4sReKWBLuMCpQNV3b+nuMYoIESQ4/baThOyUi9MpJivl3tOOGLbCsk49nkGxt177Gnie1t+ClZpWvbSomdPasQHTWa9pu2xckrcBhWnKpLMeKwJTpGneAMZOm47BkORNouNod9RIyvLhqXybZKMHdqZjvGvfEl5VnYIl0iS99LQraQDzhSFIcHCGotNf0KCTlmD9S/WUNG+coc9ZHevU9qZB71KDe8mr78OjvTthk2np6PO/eEpNQiR4EFAE2Bbrmb1qhCeS6DS5/Uio97lKEXB3VCfMxwKhS/4aDcyHKnIJfao4N7j+ENJsaZrVG7s81UW4k4yVDENVH3tFFroTdN/O/kPBZJjlXxfCePmkUQqLPdnE2Js6zFYLEyuavog4pwAQQcBqHLr/DXmgUvcqayPn1YEhdz+XAtwUrW9n2iE0pZgHr3uCl1+QXNiS1l7oBxGqgXRxnVDB8eXqoHVpiA94NroMnR5gQjsBTeOEIGazArrxG0qztFjsOQbea363uoLUKeUPMvXizm1rqwpHUSt6KnSYOEd6fWSsAujAxP/pOU7R8wv17qnRYAJ2jlEiIlQxRkY0CkZloxpaI0WTKCO8octGFSaR1qlHaYxkklJ17dLEtwQSPT95VnGQsq/62TRLACd/vYSsH9nrR6z3ks/rp3tIg0JxEdfaFzRdh7ZOnlXFrbZS9T3W2kAP7aBw0EIOzgoR8Tr4aCAqkrurHNx7zcaeSRK031TuwqCpzn1voFZS8710RLI8Y7FvpX29jSIoqRjs57+u6qL9jWmstdWJ7CYXeKMqLIVha4X6AQpidE5PJhn9vakzF+hvQP3ifz/02ig6PgWxnAiPMMUkqmO+ijjiccuvAgQrfHsGa8zBwdUbw/MRcEUnsDmexVPQtsG3m2RtFEik6frEdn/RDoPZLejC9Q9vcq5CXWGnyj4GMm2LM3jKpOa4YcW1nzIdnFiY/bXMcayzc2bEnIZ1P4KpgBmnUI3FyV9ZVoOUcrzTbn3vzMK2eQ9xxJYlZxgkbVqoN3lT0ZiERCNBoRoVgWomRBOhFCxj63awjNwLwRILTkyRod8uEKVJhDok3zEzxEhbWstk9EeTNrGvY+e6zATIS6mDO09olpvN95SCbS2iFHeLeSzGg4MGlawLHgxr2qrF5kL3r/29fkCknLN5VYVCi9glUDrMuSabu5TRpaSN7HzWxUUigdTPTMRHp8ZPgjyky4mTMF52ypkqnACaGpnrmdPwYBBkgxL/UvZ0e3TUTRzfva3FSdDiChyLusvYlZSFXGcNkrba70JnEoTs6eVwnf70gWsuIIn49jfHYnCGlWTq2oUN5lu+Ki+zS0/ketC0Klux173T1nQ+Jnb5qXRncNn1MGJ9laXx4rgs16d0rv/y2BFSnjlx7YibGBD2Kk/FWKESvgwijOIfkuWnF516QoerKmMS0CU8IUdBJXq8TfQRvvXMItNHeztVv92YuxUiVkqBXOXMq8K+8wQkD7daS7ewaawehHWStZU9/HgGExsn/2YA2Sbm5DiPqofLY7jD4L3bejunJdKDjHVTGOKJfzobSE36Ye72oIp8bitEiUYXDvYWUVKFGg2tSbafVUAjJVIi0QgBqLlNvRkhrjZpsU0ICjGFQjVSYtsmNTuDdm5JOWLUpFbstr8rSwtk1WgkwL5l+Hoqas+ZE/Oz8Eq9q7h3j3eEIhinKMAgUtITnR34b7UYhgTizjYBg56I9iwJUlNi3MfN+esKTdIrhvde6f9qqypEOd/quWZO1RgBN3bMk0PGeqjSyzw2uIthIsustxvrhZ9i0bIrn7CLebFSo+nemgYg8aQ5yKKm6k8OoV+rKxTutt7axEj4ZRLQN1X1GWfY9XS8Tz+ro8RHZd6M8unNlv4wnTy5HCX6SZcZsgG+PtWf7h7QqC/jtxw72MDGok5vxrPYIO8NBev5w/wi7VT5Z6L4NzRPkk4aTrVLSLoZ3oEZi821Ip6xX7rrtA53XmOodBzG8PMp8MEU+GwAR6h3BqdGoLhktsqfcwjuba3uRAAN2RxCMobMgVUmIfQsrtx2Rgk6tRgKi2xCY0aVJLzfWR1a3u7UWutKwQ6aiBIjWw0qjt4S0jpYmwvXBVel9AiUsMH2BpFVg9KSvC07VcKVFsUsMb6Z06jY2K5UlgaPf7xj9l3Sx7W7fnp69sqVSMDmqYJKtzwwOFQGM1gVpYNhtD2pSo9WPhifVEWYNptfWzCCiXSwV0+hlCCfG6BSM59CxV13nP2vpYyrsD15wKx5SXQY+9WVcJzZj0dmhoY6+klm7k/XqtODRLlYJ5hCZk3V5McZmPboi5Xtr2DoJ5h6/FvVYze2ile5ONHHUY47L8w/44bdWNunC4BytYyDoevPGXZjOxnWrB4ZTmevmPJOXNsG06Vqv+mwumqoEKV90qTq9DvYIUfzavebZ715VLz3CsKxco700e6D5z5Gts2JyLdOI6Fdc9gr+YnQkI9YON3AH1PmVlAhGroLbimRbGldRgQyEEFFIYugikxzSLaZbLRpUjAiGoKXoUCNjolzWr1TeYUIITCKmStUuyGKKEVFYs++Epo9MCyV43qcqVWG1jGt5wQEc6pJlbPq2LHIRjSKbKMVuhPpquLZMzEIw1J2BpbPuX7r9wowo4Oadx5BUm5hD4PNlTx6+VCU2V3arfPOjfOkn/oPxmS1v3AQE1gPYFFsHJRpNfVZPa+YdqIIe6K6bl35Z2N3uThopbnXSVyzt8cRm572VoOQ025LpczuMOhvL6OC3JzSVCXp7P0h9J/+8KunF+nwMpwwie6ad2nHU+EW1feQ1Rt79m3r4N+mOp+z7fdo59m2QLtxP5zpEDRuM9UP7CIisrLlVmSapIqa37h413WApZe/GT05VzFLHa4sh9urSYiox7iksHVLB1U1mHhzNZsWeuzTiLqgGTttEEuFZ0VKFyAiFs2BEySiO+uJJP+1VMtwDZ6uS7bqEHclO7rVokVXdMmTjSN9cWDGBneMMkOHJquftWPy21SPcz7wYiq7OkZ4sanMgGIIfxJ0yPp7bdUGGSWvU63HFDVix5XGjU2oShd/pqTrnKEDBq8sDkl5Fk21DE5jZDbeSP2ZusVX8XO2Esftx4L975kZW5/k/vs7TbEzJbN1dSr95jnXPHPEcjI/RY4+ztOf/9Xjuyu/FzJnxRUXzhi9W0eFn7AZsXH8x+XhYz++qQCoWL8cV+a26UQMi98pZO/BShJCf/2yNftOJy2oZEmbriUIggO6neyY+C9oY3RHKqE7Z4tbS8B1p7mKlyGZH5FKyaJtNKVL8hgtOlLSolPSE1OidbFeGpHU/pMwBJgyt07bRfoN4i7u2C5lrPfn+qy2cTzRvX5AhRhsecWqVAdZmgLKTqcjp/hTQa4lXLSspFTyuY7KqZD7GtLf9IvLY4SszhbnkBO70rm8tFbzTnaUjSLkamprq6P6+7/st+2796r6J4SkUT0NO+9VI7Yrm20oy2NuObrSKB0Zo6xaKmOX3nGLYTVKgKspTGC+yho+J6//jc6yFWOz+326tSiJsz4kCq0PiyvbCGcew6PuOGnRda+TJv807V4mOBCY17DF9KSY4nNJlQuwGpMsXZUoQoNWBWuZVUlTH/1q3OiurbMgx04jhinoroyQBHVHiXm5BIqxeCU9OEbqOH4teqq9IoSuIjEv7DIpM6adLEYjNfka+7qMQiNCJ/AkIowxGkJ20jNxNRGYEuKmdgKNFjsUK+EUaRcKVTNQNGjTKbFo5sOparLJEu2lmMj4cadZg51dVbo9jqb+bjANUzrNg9nTR6dX8CkDm/NiRHJBfoq4G6zkirmiCowbqECFK1VOhtaZI+NwLZmjlhfbVjvDzJijgG+tl2T6D7o/H8YCCWO6dp4AhfJmUZF+zi2c6nSaQjj2eUDFLXIM6BlwTNbWRVUu6VxAP2sPWst15mKvvTjhB1NomCUnQnna9xLqZLoXcAZ7Qh+sWgELj2fCmubajN7KeBbVBevWxwFnMCRjgE4R4TEFXay0pMeJ6wrxXRI4mZENFP+8liNNvoz9qBEGap8Yw0xPk4RiU9ffd7SaW8Jatt+GiZiTHeiAxiheahCKURqB0R+eBhM1aQVi0XGRTtSzhKMPcDkHr14qAsnOCn3tVsyBEARC18IgLKEtV5WG4wB/pjgRxS1yXL0v6jaWYtVhWWlcXz3LcsLGtLIoZyQrg4eaUoKsCxxNnTGVjWNVClv/kONGDCKz8o9y/D4Fy6pAICtxWOlE0UrsP6I2FJ7ZSfgCffH1SuFNtpCIqeKwOi3mwRNPOkTHpAKdJEReL5CMMgSMnlrecipWQ+Jt2KGwPevBkPSood2MnYLOzReQ2CoWHM+zYrhWRGn0BeX6rBY36+qHPhWV/XqdtVKO3S0KkhPCHlMU0CkpnUqMNdZ0KaKUKliZGUFDH/41jhgGT9v7DckigKmanh6iWVJdztRL9dajy+FZjEaoOh0TAY2sZBzcAcLVRBoFTBjNFDBxGo0z0SEdOQmC0KhkEmd0VzRNMGAFQmhIMYuAQANcJjYdsElWaIXJzZLY8P/pYsxcCdtV+6rYouvP1KnDfv0Du2dDkcFhfIEKDPUBWRuHCUhwPfTug4GPvcHxvMdkcC+dRUhxe5WaEAWmWuPHubZdF2PH1zPVpa4XXQcPvfeVPNbD52k81qK0SZGfMgTxjU6OAV4Ba81SJqZhuSUxeXedjGh5zuxoUCo1kqJ4wPhfp5CeKzLherFkirK+EZ1zZgWnKXJQn9Qw2BDGLA+pCjn3A4KiAMwx+g6cu2miz6Jau+yswZP0OrBCGmeHMSC5T8HA1mJrXLWQEy+fGhYCuM5jcjmJNGuFUdiS0RgFRloDaFAlLdJEGzOGJMck6jZEqi7hlGz+yJi85zIo0lYhBVa1hmyqmBRNiDUdkV5Ie2pT51gSYysjpd6fp6K/ixhA1E/6qfrKecmxnWqzpn5fdU72zJl2eh7uczKqeulrJtRmg18a5HgFwVNaLBWIez12nLrfidnSuS8Wh86OVTw7i5xhJgdk6nCdrhSW95mLs0tsVNU73gLfOJfO8lGXU4V+TZZUAmJAiX+jVCRH7GiiCjc6ETfbg0Sm4CPSgiT7ElVRejCR0A+a5dN9hVhAYxHRxIAoZCp20MjoFQPXhEQUmDeEqZ0GTtpgVJMroocxIiJI7uk9lgiPl47O5DtM9Jhrb/Fx7YlN6PoD2xkJiqeZhW9Mi+eAlqehxexJENq6SuOMOX6GMdPGYsB2CcE0aHT2M7fTOzWnwqM5SCCZlnqsPUic4j0e78yoCK9tv8MEkQps0CURB1qcrmpTm/a4SGnH+PzrOvHT22MNY9gfh4tQcN12dZ8cdFL6hG40yjsnBkcPtjhrmOJ5HQbpRkTRfN46CxOqEBV3ssqaflBVdZMUmrVKUypNhBLN3GXMyJbRtUyVGhCUln0hADJ2ztnubKLuTKXaNE1IClHpyDMzZpvVXmUFFIlmCFAFjRZXC4nSg2Qf13uiEgdsrCFve3Y+HX/GXYapQ2Kr7sOFgn9unB71k2Ms4LrxNi9sJaZejt5qn62I3D0tnnudnlBs0BT7NU/HnWEjQUaqrOmTqCac+0DV08KpMGjQtuhHnB2aW2Z09C7UUKzarLmn2InPIWlGYhUdQoxmAl0sWouWTS9TXENr2ygUc5drbXJHA6rBtdlJRjJSTGAiURkVJBljS9JiTKrQlvoNyZtCCEsy172LTCwK9iSp1m8MJ0m4Kyp1UzXkAaJ46oAZK/lP//L4BYYdJhzzGUEyohIdS0IRvWLMdjUVntX2UWGdFX9Tl2qREmS5r+43Zxqvm4acVnGl735UCYmGqoUlrfRtY8qdV+NOHj9Vyst1KI+sOpJWvffBQlDZ9IhPr8RYzPvnEG6LioSVXSV3Z1gdljOdBnMoMMVHP/jXgeXQFM14y59uNPo7526GKOEVRIIk74CVLm3oMIMwi92eYO4lqYR2fg/++kYUEpQwSvR2QVCVoN5fAKHMPjjMdpQq6NiVFEJDdikKqo0khroK4L9Xr0VQonViLuzEncTRmxSUd1VIQe5pB/tFEbQ8IAX1hXurJdwBWvVC51tFwZC69+vMxHR6fM6z2lqvH0z1bgebRRHNVERKXuQ0a6rOPFUwqHTuKo7As6NEOV4FcVfBYhEzPyXUMV2uGNg78AIu/7oi05Tm/dRJuc3ExuxM4Rzy7MFzn0om61qxBRradFJ67j8m7FogDn6nsQtVggYVDUyP3GiWzmwIIKrmEIWg0e0qkeQdoEHMggLqwNiEbNTsQiuECBOjmQjRoKpGQJvVySuaFDu0IR3xYNDkL+EdBxUJkNTmGDgNjDns83TWKsdbX3Jgqqm58UNK8527Cho4OmN3uKqcAVs09e4vm36tpWg+dKwq9OagYab+9DGCwiLZaYpEXhHskiqDtHif9b9WxgEnHoQ5kpfdsx5w9Itnxk4x7SxN9jMKm8YWAFOZ6JiYWhrYYwYNZ4ZpqNMFp1ozM30UN2EG6/dxwu2t+Pa5nzlltLuxCC0lnkWRXrHj2bvh75uDRev7p8H9ULI9ZIIEgATdPUQoEIWKQQADYmxJizEaTUgzmkgkSRhFIcZoyd4yaUE0oJqQlkRMFcHMQLUYQwDQqSiq0P+sCC6vFow5YFlVDrKrRfIwgmXMRL6tZHHJefJnlYh4Cio/9Zt5jxy9YGbtk8iiv8BmxzmMXol5b9y+dzB5yxvd/OqmGGOy9SzHkN3d6ZSRTD11OHkCfaz0YvJ2TzgOG6W3Ks/3VE/t8y07zXmslQkwEStjfQpjm0dMDDOjlcbLroowdWz/lM3EjuAa9Yl8StMAx5sM8xHfFXmJY+wb/ZJVJ5XQVdkn7wjbPPrkcRKBgKyb0hmgs1MbEhIGWNZ5lhgNqi57huC/bqylNmqMEGlC42jFSNPgnhaefYEiGsUEqhqcBWnR3BQOLhgFAuJNEZfsdQP1JBEawjhSslSrEDN20tF9mrQLJWWcKGQbJenjVeqOMTWn1BLPvRpZlKCY+ZZj77zjbPUCwsTGvYY5lgEbP3P8c4z84fRqsFPViF0Bwre82Qu0RmbOgYnHiokq4czEd03L4by8cIsbxS4ugycsQG7/dhz7xisHxCboxvGfXe/1nd6PDL18t68yFnuyqur1g54jTmokIMuiI6E7TaHekvAX5RaGCt2fSkLypha4iFMIQnGGhVtGcCUJDjfI0iYEkIAlEwqB8ydBAVSDivqXQdQgJpqdDDr33q5skjiXfdcJQAJlIKe3gaIzlvKeKswO/mnLM8Pyf7OAkNx+hR2jrF2pxI59nOV0/LFOnhpgFwUVmbCAkhEiuiJxeCaH6CQecFfjMDUmg4feb8eeyMlswy/1XBAMFeJDsUJ2rO6bTmwU9UeMjHTT+g6w2wFZXV+GwY4ljHb0U3/iZ025rLAnxkJ/WyGWdjRWNSDkXIzxqPvMniQrVyZBQkKgpESI9RwHFUrRmI44cwYEKRLF6OBHmrUklUZraXFFJ2GLIAIJQOjMfkTESwhGQlVAqAFUhZl1DhLdQ3AQZv4H9sKnZLBJittiZXh5wl1wHf84Bwt5XK/xDToNZ1lZ21WKOQA0DTTwK9bvT3dmab2/0L/9PvL5/UOxqlJN7fy0Ntr7vj9Ri4ul+IG471TQ/HRhDh9vVvQFms63vHFei6L/iAfOMsUT4Wk0H0a2KV2M1jPzAEQCHKq4Pijmag2AQFTVxEiaRYr/QpsmhAShFg3+B1NVd9Tu/DOT3ZRmpeco0XoO2ABaiwaTAFGYmZc4KILQCARQM3HohKzpS9Cfhr+46+qQEbTstMUsB6nzp8WUwdKczWVTvNolB9gyst6Qk53qqi1yh2ZG4sdNO+eKOw3JltzBzR5jHym6P89IhlavKVIu1/TGtxxBnsI+MrMwe1Y/tvMezRxQc1EXWY5ZnJ+aJLXdgFvWILm75VCT9TzTHz1f5u34UKjMk/NvOu9kwFfVFFUFIZbz9OyNAiEFZohGGkRVmeSYlEnFIQrSSW5CCFRgqajhZ7UaGcnM1swEzwAVElCBRhMoHN3ggZsZjSqAiZmZ0ztC0AFYCDnbYwd2XHl4d0u3E6AgKVI1Mq47vv8PElDPLyFsIznC7Sf2DtsOc298jrfvfKnE2bEcLuZzHwzIxoLE7Ce+y3jmDKpNddH0Xegc48JuBWN+xJQgh/wP81PUYtkIHK5nOjPWDkd5xCk+9CJFiCspJ0c0dFJO0jlX5jzeVBVMpmTezzBapAMWYEaERojoxzcpQGCAus+lQqSxZOkKIwmJdIaGtyoMhJnpSlYqtVKixUbUaB4bwHWh+1gMJGmpxKlwjmfyQjWAIsqcjnSWspVywswTYtsQb+wThxNsJ6e6x2y0Z9x+cFaGGkU4L5P2Btk3ZezNVBmhzKeCi5OMzikgUjG+z9FdYOqxYkQMOaWD6BSaLBstVNJE6M+HOVyR0ctw8i1yph/psZQG5mwL6E+MjXfF7Z84ZDcrQjoRnU0lqJ3kwZiznZ55uDC2cd9+BPr7YddP799X4S47+6T8b9hqWHCCh55Rjy6YkPgThBgQNCg6CWZzEWikY9lIEAQFKomM6W6ZFAEsmclLNAtQJI0zl2ZQo6lHLG5H4cTKoAygusWbnxvq3E8ooIEiFjQ4D9MMmZ6YA4ZcSPCyhrt6r9wuE3Cjk66qZasV3dONKficvRsn2HdP/jm7CiB2lRiWTn2vVulOZAZ2VVw5hdGcXMBTipaDYgtO7SnPacFusyLqoo2sz4eiO++G2z9u9jWH8HYaOF9MJBKV2+yOGpwf8fS4eoWzRgMTd3camXWd1zAljL2jOYAePlE7HwhUZ3VvIbi5w+R02qWocBeOABC6HFMqHwDuK7neQs3+UqoGkgYBFVQJgWQUUBih1ERu1AwPUxrUCZUIEEn8SawpHFMFYgbQ7SMAmpn7TiGoiKgg5CNfV0FAj2Bpbn2JEFSKPjrioQssFTY4Z+OQiy3L9f5ziF6Yn4v4ZHmeT7+I1Xha4z03dh5lEzxe/kf92ahh9fRd8nO43+MZUkGCn0owdQ6jUw5hLCkjZcWDZCvdrw+jY9tpCE2zYHqJkAwahAJVM7oCNJLYg3cGGNtoSXIaAdnWzI0iaCqibYyA0izaUswgUNW2jV5ToKgQjCYiFINSnfuJle46mVSfOLSx7/6WIjLdlCuMlYvO3xTkdMLt+WoKMsOEaZvIeg3PSCFXf/XHZMWEe1dqj+d4+h+nDzkfIbDryGmqTbOr2tsUEHgOYxBTGdGm7bUiiDJHV2fsEXAak+QYj/pU+9vj9LoO7zj2BXNT2+U08E0bRHtLDNsTy5+Pf6wHdO7vh5hYNaeO/RwOCzvrx7B6BMlTGoArN0B6xEcANErk6iVUEZgZXCJB1cSM/W80QKiaGhSSugdJxclHo21bERjNyEZD0KAESdX8ePIFeo0jsrOeEGT6ByU12Mxi14fgrrfY97Mk4xiE0jlHy7HC1/pf//+VlfMcgQH1vNjRfzrW3sYFxZnGHxcHCb/bisu2e8Ug2Hr6unTO13A8zf0BlQ3wvNw12LktUkR6vLnuqtiBsyC9QIZkyICPGC3a0hsZFs11pSlUQNETkFiXnXSRaW96KERBMILmUlEaQlBVCMzEIp1PaS4gRQZGESMoLmktyvQSdlBHVZXQ03XPFzGsQ66gkbPm04BtNUfG+DRywYGtw05C7Jmzv17LPW5usdlaoVhUOIOVUwR87aoofewmNAfN/1MegYqZgpxaM/ss4OATc3hbPaKxgM8OiwHlctr6O5Gxsf2axMZ5dXq02I1OlRvv9PjeDyeb58cYkJkt7O3nfzJa6P5QKcbMH+GTDCOSSxQSA9L/wA5KSMmV+wTGWL2MjmbIIo0OTxRxbwmAoGoAE+kSkhWfE5JAIGhCEKHzLlQYBVQxdWCEqpnFGCnpmphcrh0YKfmUBAQKJb1MwQ5pDwijMK6EIpEf5bAOKepCETNPjuPUoFhbqNseP2MFxlNaM/MD7bNBRK6rcbHrQmE2WPUkg7YrEbcyd7e6ERTPjOEQzbZrPUa4M+UtUky1tz0tObphrt1UWu0rFrjkLWHTZrlVC29cMDjWVGE/99pt8DQIpPq3N5Sn7DNrNslX9/88dlOcv7JOXrmcyrLRY9BMAWOndsWto5aqYUTdhPZs/FaKo4EZQfCuoupumqypXCe3Sl9FltauIyRT0cHRDpbpjewOchUxRiQDSxGxmAUbzWiECVzusWU0iNcQGpAKtYA16h3QhMbMKEYzMQYvKwi9bQKICoyyQOjWCUWTWaYApHvsqWgXO3Q+GsViWpGLf9IZsG5tccK5tcPpOIdRtvECzqUC2dmMVU6sMRFuym9sXJHuo5kqPn47qVvs6FliZr1hjqiG7KI/vdsaT+/PEJGdXFGxhF68641PPP+rnWrZh5siHp7CfBvoU8kF9nw5ye40PgIGKrfZNFGK/3rB2zHDSZ5dITYeCoOcYWxh6sV7/1+ucJB07u1KJwEwMvq1qA8gVvGOj54yaDCRlowxKiAKE3M4w5JL8XF2iiXTv8FEorjthIhIZKQYoUQAgiBgPWECgl9pgDa92AsiqhQRFRWhmbkmQ3cj2ROz6zIUAvaBBurxKtInlAc4dn118Pupi+//fqZZxm4xXziGKAWOuVQqffdBsDiQfe3HCmdWWdnuOyYUAueXjoqY8FPcBNEJEQxvGFt9yIxi69TMHyfW9XfN2AG2tsCY72k+Ewo6s9tV2RCmDtGx5f2FOA5359hZNBgaCEKPT9bT40rUSyz1olQ5IBBJHAfWuCHT5jKxOyIGSQVFXGmhh19MJf/BiLltlRnNTKAIQYAQGhOaJMVnJYIGUUiKCCJpKtpICAhgEKp6pcKELgbdmZ0k0WijiCs+MUEw8m20bauA0cSsv3DXt144rQK1muQqPtDM5tjKy/GixZIbM0sPnAdR0Ryt+IuP+ZoT9AzqmeNcYaqL//TCds2/2m4+bCPleVGe98Zy2vxSyvgwGASR53tY7spsrLLMp7SJxkbw8n7048pU462+OFDjZONing7DtSwJK1AXfR8LWeb/ndI/VG8kdrbRHj0r+2X9/D35sA1BLVoXQCRphpXUumTZJg/XUrThCgtIhlXSVdjyJq5wXwpjpESBRTHR1CcBhdEgRkZCTLTn351wnQLNotHuhinZITSVtvOeYIPdoa/tddGWx3wYS13lutNu6+KwQSax8TN3vnvOx/jUc76i9+b4tBi3IfrvGmwcp3RUzHmaO8d/FUevmw/ntP0VPEQ2kmxzURH1cUvx0JiQM+2sVoEulSSxrZzU7WJubOdOyZ5o3aZFuq3eTDGMONWNbidLaI5dSDcZurhhzrZ/Ou6dJ9okN0NfZQs8Sm/+r41ZKi3kiN1L9cCaEiVWnuwU5IHqKJouthQESZpZJHM1VRAEdEsKijJrPyboAbuoREQkOLqCcFkHIRdNYCJX6AoPlTFf3hoBEKCdMydyuxOpzbJCbLBwlPr/6hRv6ukbTVe82wd7ynid1P2+L0KqUc+TpOS9WbTwnrIPeD8gEBZJLl32UDFrPtviM0/7fW46Wo99j6dx2ftrEfJ/YjDB7GW+/sgm6wH9Q65yd2OBpgvIOu5ZFW090pVe/lb4wYu/J4ygDEmUcTx/xl2J7q9mNaROd76uI/ei0D0hOiFmESGUgKqqkQIYTZ074fFEAhFQlEKDiEXrgRw1+1aJkOqBixlBKulBg4rQYoJLRwsIoFdXAAkKdUEnyZLWqb4gpIiKkPCRAMDcGnKexSpe8IJEV5uobhNFl44zKxvIRCw57r2xdKyOLE3Rb02NT9bKetg5LleqYTLmXEOP2pr0QEsd2f7NDkrN/ehhTsVlV49+Xvq4xhA5Bkyh/5spmbJ62HRsSuGmqV9gfUzRRjiQaeOGmhOmGQSDgmIFSFthzeQXSIkCvItWe59pV6KZjNd15iaXF+mg4TIT8VNHfpzigbdpfuezpleO6u2BNjVE1fJkkT8yjqQrQecOD4iih+2EHiPrn2MrJB+Kdz24hf70UNXehaxtSv3rtK66ALeuRNf478EdkUQXAUu2lgwC0FbATKBN574pIBRFEAMjFQGigRKgaiZCMaF5tSIzQc385IcGST7a7kHFyGRulZ9RJmZ04QCU3g/BSgibOsSQmmNCimM+7uHtvHe17dYynp0FUGspJB//jO3p+idrpR9xjuyJqZhmDQGLWqZYpFSMD8UzgC9g5a22IYwc7N31EsucgkElgyxy7U5mwFOf+lnQbf0ZVrb13mosx0bF2VussQ1i5eLiquDCSqOxU2nfUX4ws8I8PgameGGDG5/6cz2qOJ8ceh0kOLw7EqMb2dYKYIB2lGl25SnpBc8HQlaWwCBWGD16pz/EAYVyMG7+so7byP5r8i974gxA7gKYrGeyrtlgUAoM7nEpQYjGVsxZ80aH6zB5mRAijdcLlYakBqHMu4eJGc0tMAmlgInrklmfqaFBilM4SHrUIZkEkZigiQUVoxkTuIEJa5kGBByH7efONKscBlOzs5gXDkA9/bNnHGD286oxyep8sR2V9Khelu/ucZxpTWUYZ3uPuxSw6e7UQSqD8GiqPuSvH0+D4lCfQq65nQTw6pKw2Z18KmyaKpkM9soipGlT/+40o8xNQVsxJJpaFOOzsEgg2hh6Xsya/ID31F/4o8L7ZDlBnubeGWt3MbHAB9tDR0EYf042nyrP+bW2HJRU42CaghQakgFVC1pCMbQxWvJHRTTvE7iOtNthdlcC5ucVhepICzGqiy+kyMCvOWpoSIQQVhstRKhimsKXIICF1J7olSVFGE2hXkpwYyqF9pZHkCzdUE5oLt4MkKo81CA+GCtXFhOIQS5bDFDON1TaiDka02XH7RjZhIU+25stmj2eSEB+sF1uFKTC+s/G6PBCNXHnXMygATGIBopBRr2aOOgBX5y8YhAKjNf74F+nMoSnEWNo4/Se2hPWC+/DMHEQSD1dwEyV7uFULSTfOBKesIeG7mZ4p04BuHQyhh5U6+2RrMNm61LFSFAbgAaQojRpRYwCEkFEgBgpFNXAoJ7lq6pvh2bRrO0CIBVRJ0WoKmmAGCTSlkdHbdtmkKK0beuq1x3boete54ARZDL0Tt4TlNRZgetZA+hZYSSFyBR4dINoZhdnHhS7ZXOOz3H0UDxFBunphT0Suhk8lRYMQAmDTxu8cUo6fneuS6e+S45nRd1vsBhGDI6NMUn9opXc6nia8Z0OIJCO+Z2KJIoyX8Wy/7kfpeulYxbDxME+ULxaZ7wV65EbATTnb9q3XgupdBw6daZiZjggW/ZHbwyhvWiBQmUJVJZGsZpYjC99ZLpRKece4nZVjOIlfzpzkToFOFURNqGBiEIDVCAKdfuIGKMYSDUTo1EMgGpQTWRIrxuo0TqTC6Opqom4O6YjG2iMkJYGXTRhEdJbKWiAoCKKECU7bSUellqKiwhCvRfBBHLwJZOmlJQlffr1q3M/O4vPuFiMLVYmktb3uj7J4J8ufjSd+ltVkHOfOjhVeFiPNcuHRLEHfAFrTv7LgVzdxvSrmFNefGE7GZFfxmIbg1bL4FwZF6u3jRHPhnC4MSWYeoJds6k+Dbq1MFj4ZZnzC1mlH+vozF8+g2kzVYR7urQnBteZZQ/KWtdTmeRgB1jfPLv6QuyBztlDm3aFUiGz6ALXFM3T64AoZo5NiJJUGsg2tiKIFkWERhUB/PwlXeORJoCREIWgUddsT62GJpKqQSlLO/S+RzQD2UDhrlUU97lKFE+qGUOGNXS9l6aBqNclKASUFJgLT6wGwpkXHali7ZA+36O03iCoKNLU6+2DPOnpUoKbut9i0jwFXJoqQc8c3nPfGqYeZUWrbiuF3XpVdgy8Pd/ik2wymSxmz8Wq+0aw58Zldb57wqDkPvOjKooFG+f/ue8bUzSf+uZQTMamPmfONLgIW2LdGqmyb1Se/kgqe8XkcFwkmJk6no/nY3bF+Ml/soQDSAgFiLMag9FIw4powaChXbaqTQjedoCZUdoQglMtNGlga6NwliZFEGnpUoEGDWEGIURTPcCJmUI3vM5kUDN/t1guprjUlCpjbEkjQrKkIC11aATQ3FXJ2g/rsLjzOhLGKMVKyD/Gb/YaUQV19CLnorJlnGPIPAUCL56IY33TyuqqSM3LGTati7HOnOSymBPIhLTl1AkxnmNj2auLEzxV7n0qQ5rKmCuwhnox73xbEhvXfnHJD9bLHFbFnNDwzIaispzrN1i5u6m5ND4yzz1KqLPA6uWQQRdmTsg4jhvybyR7W6bygSMEkn6amHUEDSNTZg/N4cXKGdK1GQEgqEsjZE0EMpqJqjC5ZVNVoQ1o9AqDZP65mEIoRog0aBpBkISkVDKYQgBV0kQshzEUaKdszX4TAd6FECMtRgiwUo+wNUVIEbMoUGFgtHPZGQeTPiE2eg34/sUMeiX9puyg0j6GQc3s1p9vs3acJYw70IMiQZIhLUGZBp/cjeqAXyDTHdyziRhkBGTrP7hxD6VSbtlqZx8T00tuNGeNiOyq63X6X71sPlWUKpKKi0dOf02dkop8nckpI7K0lBBtg38aHBJTyk7jpucU4/QMlvzGJzse+WLXeNxxmBJrkQnEz6BuP6dqe5YRksxDLtehOeNkskhaLpVnSFrvQ9SlnDN7YuV72XuLOSdS+2qQWRQg8TwF2olbi1KgEoDgYtE0Kj3egJgwHeEClYYGVRWDmakoFBZNoUYDRI0QieIBSY4QBZIZnJ3Qh6yxRUVCEKz542UDbTGzEIKXJGgJLLnzMlRZR2H6AWfUyXAf71A5U3TQ8VYi0+ypqWrt8SrkO0myN/5rcQDHZfm69dzAbqNyLyfJMCqb9eBZ1PGYRV3bPol0LHRdJwRWEBvFt58LvqEPSdl4BkyFUOMdf6pTM1XNPtVa45TVeBH8O44Dxg+rspanykV1evmxiyszWU71NtBUXWQqIixSizfeQsXFtKgffwbhwtQZNNVcGO8nU5XCqQNoyopvVLvyBF1FLB+1a1iFIGpi0uNeWq5PdK8yAUUstghBNXjFXyQCC5KqYXWspy4BelAJirRGU4TICFBFg0iABiCpK5giugBlFlxT7AELJo+J3GDoxqsXLwTpYhmaiEWSqZZipCEfz53nQiDA5Nu5071ynDcXz4liXb2ycxUbcmPRw+I+VQwvjtdSPfngjKOZMRx1XGGraE8NTpSB9mWHA61Q706+R9TjuakPr79sfLrP6VhVntp4GnT8lCIieCcBYhHeX6+BTYF/xzCuIpd4nTOGSky/kb+3w58pUalK/WPcnyoyCQf+KZWFU6zz10/obW+tsuqn/CDGiinjKuMY3Col5H9Rxa5Y2epvBcUaxtl0qytKrBWG19SNF2tOg7Cgj4uXaRGgTMtEPr+FZqR16gf+v97hx2pU/arWVCRFRBUapBNmouMRFaBRosFMJCZao0KVHhKo0LUZVAOCMrilJkgxitFcRTKSCIGprYAEs1B3ilhNTVV1EwtNdyiW0Qn+CiigQhGDXyjgUg1AtEiJlIIe1g6Lb1Pb4pg5XSTZTwUxMzuOG0+XHcsDb1+KLCa4RRfKschr8fCrZ3XFjPNU88ttebPFgsQ4D5hqUhbJqBvbUl0168yK0pXnNTgMpsA948bK+Cv6pZqpR3BeFoUzu4FTOffAUWVQhO8jIsdZ6cYq1E7ygY03vlVDcKPUypQCTXG4ZOS8k06TElTiVBsQU9FAEa67cU2Nt446FHSKSDlKruCncH6/kbFzSPPvNzOIGCTDH8SyiPXQ+Gk18hRBjCYiKlDAYuxuIcaY2soiUCVgjH6EN7SoueihCn+ZAmbRZR6iWTKogEHgArxIhpkCbfqaVJ0Hl3YSlOhiILgDlggE6UspmY8hnOm5MF9YZubvN5ZJi099XK6UGSCmOeC702hPbMuJqvTsi7vGVEVuZu/j9AiHlSuv1CRlApxYr4gcW0Wg+F07Px0H3MiK38F4Hy/ChB0JNBClKRKVK0fOuTRiNtY/ZKLjLhO43WKfbk5oMm52yCmjPucv1amWn0zLZky14aaqjHOuZIejMfVF41m6cZRmwlTrJw5JwHp6BYVcIl+GCMwsiTG63ELiOIiIQFVaP2wFpIm5ywQkcRCSJSaNkhvx2d42wCuCEDivEo4v8C6GCRlF/HtICKRRVQ9WNARKTIhKMZhJUJqpMwI0MIlFI1+A33NYdSvg/wOBmMXAhWRrTI8YADHGJIMtHawybS9TjbFj78LFRl09rRz3mabK6fU6wTheHocL24rVbJyL2wbalTi6goevAKErZ2p3y30e80ZLwGPcZuW9vQeaoEOjXR5T8sZd+XRQD+iwGmOZmqLUXR0Wfnpg2CnUdwX8r6vNRSq7fIUWsRFgf6onxJzp0VVMi/n31FYwJXI6LjhNteGmWuBFwM3xVvqU1UtxEIq99qlMeqvcvShFUMm7puqRp9GiGscBXcekeHIfOyybKC17/WBYbyhOMxFClPAoARBjlirIdmnuPEnQqRGdLUiiWYSAllkiIShJoSbja1XXjFRkz6hO/5qWxTsTLhGUxrsikGBmAgTQQCgYxaJBSWNH1UidBkleVhSxGLVpuuZKUq0mAaXQLKZ/oJdNzEMkSdIN/lm75+VPYffGDbxilDAWgS6enfV9cGoXmOpQbHXjx95Hil3tca7fv/EBPm7qpJmTgssITlHnKx9j05yz3WRI7hjSKDk8rzFvi9lz8ZqLULg56LDTOyanekNTG31lfhYXjkyzB4s5+sZSxClVngYnRFHDuKgEP3U8TJ0NRTTcFFlmV8ZUFRDfnJdNrb6+jtnUlJ7aUetndr1ldnozYRC6jbHAYz75VORXfOOmBAn9DWcqDEW3Ma2YlQJEMgCAqH8JUqWffsWgqEpIjQC6vCOFIkojhfB+AMxS8yC1AxKgkQQ0O2MaBUGCgiJUEUVPct9cwkkk+VqhQRpHS4AMjza8NEJN583Q7UcFQiWSu5VEevChTguliDG6M1b+dps2P43ze34bBX0H+oxjAcqpNTxu3Y2xkFN1uXrN/Az2yikXnOI1y7peTRHeJRPKbmOgXAUdPWfX2yEecH0XW0nyDYyjxl3/OidiSiiwqFVwSpY8dUBfZY2MH+JGHEZRz27jsI+lsiv+hPVx23ZYpgZHVccBsVQx/2MSaUWfoKhYVVfE2mG3vlIDmAJpVlQWihvduPtQLJ1W+DJTe8LZ8CYqZqqVQtF4gxpf7dRTnmhxmkskr9+s5d+n1w1L3RKSQ71E6/gHKgpNaAUVP3ktdf8dLUmiTZSIJNcYHKvovtOGVGBIvQkRBYJoABTuA9EBdkBXjjRSoTBqCKrqJpWiMDeptAi13PNQJ+krezDObCfRg3oIHLep6MzWV2PtalFpHlvp1F9JXssIpzp+0kXA/4D23f+EcXw3hgQWG3gVq/uLLPJY1+QZjEwfz19BuVeMuM7lp47xLv4MkGvFLFPWZbCnqOTF0oLsWtawrhtRfOUU8biObutH1eMOXTHCGB+WAyWDrRA2u5pOY1j7oNIgVeDzuMNYjLTOS/x4SmlKqg6cdRLTHNhgpWk71ROZ+cRPY0+Qka558byfQjJVML9d73UqMauC67U0nkkWel2hQXod/9RFSKrL+YwkSRMKWj97EyfBKIIAwpGLDmKAqhrJNokqeUcj2lLSBye3KcnVDBeDUjK6GpOQAWgQ1GsZlpUgjKpBtQHcWzNdXkyOU2bSgsPyr4hCoBqg/blrhEEMyRWTxPBY8vZxb+zckBC94GvuPjJ2VCtKskxhGgZSHrutH57GSbnR4WbKCkhKyMRBPaZYnply6dxYazmzDbS7ML/4jQi4qeLB4Jb7/f456YiUmKg7Cf42ToPKLdf/OniIfTDHzGc6Dte2Rf6eMJyqN4Yq4itTJbRKZVFKXOVtb2HKJ3r+TzFBqkyGOvt6quxXJ1NMOS9chE1yqhgwJf5YCYnqEtFTnzwch/SPuirVmyQWYj74SK8XCBhIkHBfCP8k9SM7f4p/t7mApNDtrhVKgcEUsGg0IwQwiAlMhJSYz98IpN1SAVX1A9w9tP3imu4+PGRqzbQHB/Pt1owpvIHENpKiqgoQJgoRdUzkyvgqZajpWv3yu2LEYEGqSoqDNAihiZJZnGGzAtW61nexDjkf7XwBf+ajKesy6dvee8WlaSuGyylJxm5lBDBnOu02GdpKh2erT5j5rkqNZKY25cxhPDb+5nicmsrTnKPptNVpN9Y2qPQ3j3fxdYHz8VsGInUyG3BaDHCnLvJ4c2xO61CqQMITLreZa7nSYSnimutLb8qNYoiVAXpHXD6CszO2lxIsD4ZCXI1ZNSXskLU3pi91oIDk31BoTP7ZZm7kIIAEGEVFRKVR9eYEAKMEwIxUCSFAlEIVB0IaTSCQVqIgOMdSjGJGDUIVA9mSZtG8jBFjRKZUOoECq15E9jYlYozmDt7JYAIQ5criUhIWAm7+rQJMRwyFoKEiv1jRKOxPyjG0Z47dYpFbcV6hRhHCWd+PNiqfyybfhEr9bdszZreBWkWOTaa1Dqd6N/1/cnphR5eQGRJ4FTjYSUKK8XcVKyXjCysKO04JYsqEROBUI3+HEn47SU83emTU7U7mkK2K0+nkoiMzT+6prtCUBUalGlc8p+uIRama6RTxgFOroBJ1ndK+Wunj1M1QZLpFXh/bic3WwdfsteCdZpjkl5LRRPpno6iodGGCIpDJBiLZR2ZNBGbmBCXn8pQgAUINIbYuHR3MwQ5ko+5BmYwggNAjmqnA/JZAmkiTXiRipDMuzEMPawEopLWlAx5JZ2OAllodUPU6SBfsWFowCqBlEpCACPojIuohiDMsFCqike6NFfMom9nmZHEjmX7CNKwsxjBGbFUoGGNd4YtQaegioeLQFXf5KUqFTOOipzKVKf76WY7D+OEW9QO6Wm6XmXmTu68wM3X8T2ke1Gn6O6cITSlLTlXON2o5yzwTgXEFYuahe47xdHG76Eubj0mSU5pOxaO6iHg4M5/eqfrEVNzTgTmKW2Ll0XfrRSZc6wafWYSC7bAgNz9/qAe147uuoyDrZJ9xY2vqBBnYO3fGDFkomgJQYq4f0KwNGoxdL8ItHylQrkSclTCaKPqXBCFijCEEix283SEICQ8ODYxRPHQwQr3VBVXN5EwRwMwaURGzhKrwoq6Z6z+qQUSCqiUHS6UREqASVpwldpKWzEBIvx5dqMc9XdllldxrIl1A1Cx5c6ZoygxeyQAcVbHxYUtJd6W4jdbtJ8Zvn+rN15fBWW6XRZBmZbeqh/N1PtWUxM1YFEXOFcqwkbHZXfAAmiDTCuIVt4WL0LUtHuRTwcHU/CzGmhspdjLNpTxfr8L6gMhIDbYuVVQfuvOd+VM7Wz9/WO8Ia/2NU/WedLrM0Du6UFCGYqFo+iDfLKkyZU42c/eYGJb+bzxoUDKm7oQrKbrBFBRGI703kbiWGSYoFFAUui7KDVIUMHPimKFDQmrjSs5m0fWbAYLO6nRLbW9iwMw0qKioUoM2yOd2NItmHmIQNJoIYVEZHQJp7TJBFmSNB+KvzN4UKkoRE81u3avtFQ76dH0GClUpiADNElrCd/MBcm3qYRQpQBtbU4P6UpcWFOFO4zJUHaN0luukvin3fQs3Fh7m9DUu5sE5pncWS6bjBGucf5xcufYs/Tnrl4HsUy9VaF4HnatnxvWnXDkwztfqfXDX/XZkse1S96SoCzbM7PqfBsNiirYwFrSWGT4y4zEcv+V8H+v8349RnFMV1iIpfU6KsvMxWU2/rkSflZ7h/AGFQnrXaPllftyz88FcTciEnCBCnu0aIBCLxkgzE0aJyQATsEggkMlTMulYQJOQgkco5kEBqUEUUIXCoZNUv9poMFMgaMJNynqw1rFFgcllk8SnJSjU/buSnZUZMjy7KCEytSNUjvNjPPjzLRscu2tbPEGlJ19Yz7rGA34S/Pa5JBB1jsA4wSp69V603GhOraj/586pfCzpLxNs+IFRwtP3Zyw5MK5Qdjdb0R8s6ivskFZdhxqcxmlaqQ+Nd8KpCu7FQYjXc5h6WDO4r4E2j5S6kMcDI+9sjWdxpEyrFNd+NG9RwJPyTvFAKIRKdwr3xZNSH0L9xDU/vo1mbEMIqipifcWl0ATHGQRop5qaVHET3UIynxPmplKg0SiuDAF1OIOqiJoxOjrDRbNVxGMXlU6F3oQJnZDZkzB2NEsPoFJrxoejyV4VLlztxJJsujEr+5l6/APztKKiwEay4tPoXNkKhDVORy5CjrjV/lghHxYtGY+97e62LjL42WHwNFUSqEt3n/ahdZYVpvFamKPXNK7PVSqUxz48Tq9fU2w6bGQ/1qmVF3bhzy+LFhX9u31vip88KD6d0vTOn7iSEuhPQ/d9yNDEQFGKJhgCKOJukVRB/oMgi+tQxCir8oNE0i2nFVQRIaIKvVjgqUb6AnUQZb5lRhFbleFVCVFjhMA1IIKDJqMZo+TFSMK8YQEAYT1Bp4iYJBkpisSsUe2wDQhCQoFS0W/auBNFJ46p7rvt1MsOGzGTYykTGj5Tx8bGefB0oVyO2wqDexw7y8k0cvhpcdfzYadT1IBKCXqjufAOo7qxEMixU+px+Fu8waJ3c/1mtxqKc0GxTHHipwD/U4++PysGNu7jJXaqbNvjhU1TO8O4LTUlcDdfU/LilNnmzM+6c14lmRw/9FMbEPRPuuw/mf7BLRu6ygAQuonPRKukeslBRIN62WBF2PQXu7+020CoQJRGowGSCvxgdJcI48qyghQkCccUyLiKg1HaGJGCE1MxiolITFoLqhqc/2EGI811q9MHuW4gjZaAkNkaGxQlAjWpUon7dSYJaudeAiHTPPxbkTSsILlbgkrcIFW+XFGTrmjWXMQYP12qskV9qsrBOY4Vzsye+5SS2sp6npJzH+Pd6l3eC7hv1i9eqnRKKWEAizWPi5x01k+IPoplfN5UdMGLMmjnLvh4jKUxVpqSCbHUi1A02m1Bq3hrRWLqeB0NQsNTBWyhFDQkomI6DU2ynDONAgWV6T9P7CUn436sKjuRfBGQKn5kq6fnyeFaBBIIcx3FZbukuCoCASb/CbPkaAEVYYzRywSkNQoVUqABbGOkUTVIjE6bNIoII4Fkmw3VYDSHLXjckK8woRXg9T0vXqipKjOgA+nG3JRL0+dIiD46rkTh0tkw9KKtjdlkcQZMUScq6hxPi0pDhaAvI95U5ZVjxtTFrzfUrb8q1fj+ox8wLS8C1H9XZacpzv3U4Jwv5v8sA6xiAXIrIsAFnBuVvWujU1qFKnKRp8Sc/tqcqsAcv9nzOhEAmC1dTNkFEjVZRzBKTDYREqBJkjErQwmNEBXL1tKpHEHArShURITqmo8UA5rk8UAJUBqZ3B1ExRzLSIrFBC1QUWN0sefGTMwcNhXMURYm5md6rhO4ZjUpmhiTLkxFAD0hKxHH3/mVilge7U7qEoLOD1sk2YEzazikj6k+80pGWEyYprRKnqYnxBQvfEpBYeP+8jRNK+sVy8ogdBpN42jp6TICRcbXRluBqUE7zdLrOYxMXeqqKA0u8/rlF2qU6hdcmQz1s/DpMhOK/abiJllxKZojzHrCsscxHyskxujOD0l7MVEg816t8BydiRbRTf4s7tQ1OaiS6I0uJ6VkN3pCERVVC9Za0m4gVBdCCOma00DQECDOdlQHT6ShC2EBoGkabYKlMTdVcVyiWXSypFl0FUthZA+A0LvvJM2QFnCn4uD/6DhPkJKom0FcVBKki1TEhG6VKDTXxbJe27GSQtUNI3bEkBnAMwcwFpZQLTuuwhXrqBX1iCLgo3pT6xdTx6NW7qL337E/Zz4QaaqqPP4ZMynObrM77mBWTv3i2VbHtU2EEXGFXR5d606u/MziyHGsIPO8Z9dKMuu3fMJ5svMBnCOzva3t+Bb3mO7H1n6TvRN4yrdfZ+NLVdh7K+vwiTu3neQYg23NJCZHBwlN2BMGIIho8pw0ZjlImkTXLVAhhKCoSJZp7geUAoiKizomzah0vEbx85cQwtyxUiiqEEagN7hqjrAUEzj8MMaWpEKF+Z1IJ3WkxRgBE7Gs8exCVVCoiroc02A4I6NRzCzSWzIeRyCfHMwmWgK4TIUXOladCA+FkKyr3E570kDv5MXtp1Edfv7hUSzEzcyWVkHGBSsyb6w3HtsraLQpPO3L8se66xGKCE/Xez+DxT7lMX02efbMOOBsNj2SYheuSnFm934COBRlxVaILriwnvQytfO95I/h0nTWpWUKgscEqppuXddmS2o3mGW5BA9QEk9SPegAAfPjWlUFEhlFmHyq/cObRr3Fq6kVYiAggoBG/V3JfUpVSYOqCGnRcxIVcbBml6uqqmoDBqWqePfCAyIvUJgxJr6mIAgVFFXJ6k8iKtFUoOwkuSNtOX4wcxQdihHiyX50fSvVvrdptuXc/cZ0vH+dbcqlAzCOyBoFeCeLD6d5Bg1QCyc2kzyt7e/kgzA6ooazeopEk0tKlUvTtaMIawPRv/ILHlUVeUM7f1hAJ74zzzFu+tO2zcIrnYU5vJjZs7xaVU33k6UvVJD8C7WrLo9vn6f/6I9RpNx044WVMlWq3OZIoltMA049lA7CmImT+RDPBVITtdSOUC/XR5plu2yjCUxhAC0pGqRn5iVEQP3gze4WJMwUhARVgWgQxghKUBGhmdEsONbAxKBGNDG2GcboXxVNBDAoHBbh45f4kS4tSVKoCBRqUMLtLByn4MoPJE0g2jRwIGSmWMJLDgnD4GFRSEpVyWaDABDceMOSb4d/kXVJ85pcz/x23S5Ky72tdCUSXnrB7pKJSu+t6IUhx4QyjS+eW9xL6daxuwNmg8Ns7/fOmtsesMK18P1inHzjSntFJ3uOEHJv38b0CYLiWFycAZrSIRiLnO5Yi2J879jN9MbsWx6siIrQ+9QUmvje4qwYBIqYOUSVm8IpTIbxDnDGwDX2/JW2fOzBlQu8iu94Qu2Mp5NAQmIVdkBAQIwGyRoNqQEgUUxElMjOFaIQE6GJuBcEIwCBxk6pwtGEkpL2aG3TBIPQRBMDA0Tw09khl42IiIcGKoRE962CKzu55JTAgWPiElRCa/1agUBNGhLuzZUhDC5fzRhj1tySSAbpfLec3kGSMamjJzxHUqWEZlFqU6pDOntFm1OPD9ae/+ovvV+jSw/WmiqZRLLL2uPGnmVxs5Bp5bvah61d/zFjIJ7C1rCtReT2s+I4N8vTrOLPQbb3wsSZxoDIySIqj+6CNyW2Mvg+9hZxjGnM058MlUGo0EDmrP2JoKFfCtGLPBnqd33y6GHTw107JzBvl+j5W66Of/PEGhQikkHzh/S0joCkfgi4ZUMyr+oQjsiXoiJUWGtCxmhBxQuKnswrVAQKh01QNWQxBTGh0uDyCAQRFUKaEsoUy9DhB40wEEpRqGZUhaoaoknMmo7qFAtQQnrZ2glrXiToI6q61oMXwyHCruXh9QwQKlCD4y1cXpqEQBSiWxlAzCgrWQ+fZ4Mi4TpztvuLimgP3AeuNyYgCtlBCLOVpONGqbupfTPfrPVOEYUQqwqkMiedg3dxm7Rp512JE7dOme/a+l21Y+yJ2P7uOMKHnl7dZeLGp+4Uo9bbcMvDSUrqO336FSL+nBdvlf1zU6kax5rqOxm/mVoLM8ahPv9XO+cxLvtUcZF1Mt02geOG5jW8BMe1RVG8C0zsEqOFzxiX1nZFoCz/TzeMCeYO0UynoToHMekvqWUdqFWMQgDS5kKApVzC4w01P0hFNBAqAiHcMsoUVMAjBjPQAKzMmIQOUKQRRtEQAiHZF1iCem1BNASz1iy6yGSM0TWYGC1xMt0IW2AG6zYXgEmHtTPGNFLMiBW8sVdF7B9vTLGP315Cg6r2+xGVOtv75c/8zW6jEvbTehDmn5E7gqTZBe/aV3qou3ncfPqBQYvhQt/37v1+ozj1WTE7jL5Qa+Eiq2IDoiFLN+dSA6WXCmbTKkouxGPlOtHb6LxfH5IlZGlFB2/aKmKM6bhWdT4kY0zOdhpUNYTg5QhHGliMjjfwA7sBRZ1/KSIWXW8yxihiCZeJEBxUYaKhce4FFIJAkrGVLDvJdIuAQBFCXq4KgbrSA1N9xUmmK7wVKIikAp2RNt3fgowW1W9Bgwl1NVJOeglnNs9Pr/Y4VULYWHA+CVY8R2G6fleYyjWx/sbul/OhEMcbwGMQZDa9ZeMT11PdVbeaA2MLvm1lp3uvr984VwQKzLhgnNEqmHrE9YLzDo4KzitGl37J9ZWyk+5+MV/a6lzcyfGJQoStvfu1bgWxioDBjubDKQc8a32Z7jh3uqNs3CvXF/KguQ4ooLRIV2AMjSiYMH8ihCK5QLmvk7qsEZK4keOWrTdZs6xioioi106YLpZZsDHQIBDtOhoAJKRDR8Aofm5TYuZwmmrykUhjkRN6M7PoBhUrf2pnQ6iGBMrwyCibT0GTBpSs5K5TBEOaSZaDVmTJCWeTGlI7RMRcIVMxGnwmsene44zRP9hObBFQ7Dpd6FTyNJld3PlecjZFl5PxIy50Ql0Ust1W0ni0yfI8n+JOT4gplfT3158zzJi3WhqDs3NntYaOzD92hzm1bYG19KqPPq7cI4YXsApz168wYXW1+1dH9XXZt6svuNk08j/kISYpZh4T9KyxxXUKaEghg1oK4EhKNoXIzAoRDapNkCYY6CrUDnFYDwzEhaH8LU0Xsys0ShBQVCmwaAKl4x9FaUaJLiZlJALgOtUSIy1YqjBQSHGPK3r06cPC3LyQFJSG7LfBbjSZ2BSCVXPVFaWQJSlcz9IdMfTE+wVGCVYZKL7FhrojBGBRD3jn1bYeWWx0x/05ODEI4z8IRr/hxO+PVXSpZFonDpXO9rBMom8bykgDELiUKvBbSl+zOkcvukbyVhPjNEJoFP88gEdXXvn0iBu43brIxWFM7gfH/Ok4UFN2AbJjnd/i6sBWZd2uUj4FNVtpbXkpwEWTvAjQew9h3bW4YpELMorA+RBwSwihQxQsl3Ry4UBl5axNpLM5xshFEFGB0osNrcsWWAwSYoJLOE/Sr9YgEkKTBpYQYZNekvoCUIhn8v0T0BJlUr1wAEDdgIpikIVmzqeItxhUaWLQtWMJ3ZCmgXAHDnEPTyC3LsjEMFkVcDzIcfJ9BzdFHp0tzseJf0M9PtiiqI6dbQ1FqwjZAYuyf7FjDl1mT+xqa8IuN8opbvpx9whMXOXpRhLJuC3HmFsVlotMy25FzLzl6RHb/Y3vsKM3jgzmsUV2MG+x/SQf3HgK79ZWGosh4NmUEaZvivMf2nrWcSrTYBwKFCPpXRhncEQXxdS1T/SeNtCOhhs4kNRXkU4yzyTcTQKrWkbiI67+LuplBoGqiHknn1DHJkK6w9FS8x5JmgkSNCk00uAFe2dymllASALUMCrEhEY6jsAbH57+I1UhKKLmNQ40Pg4aAgDRQEg0kxCSRzb9CHcSp/O3sjVlV4GBED0NKmeCcli3yp6dEtzIwq8DHUaVMTqWwvIocN2xbb4aPM+mKjtHq7yTpapbEhdn29iw7sQ3PjgnTqvSO3Zk3uq9Fd/ekzkCYBp9sRs58G2Clcm8pLb1zHrKg0/F1in2+f30xeOLkMaB7bVcvJ+iEevAXPTC+OBw09I4/5E8ndILN6WNx95MylVDPzfdpymZNeUE2hJFEXGFfcw22EZmamH/wtilIEASRVovhSkcJuk4QQrF0Y5e0XHyJEBVDU3jB7ORIDqjioxPlMYPMNXg12UxCoJIZDKHMGTxqAgVhQqjgzMcpUBHJBCWIxDxOCBoJoREkYCMsFq5Uq1v/uv9HjGK60BBVZCoGOkLIKJm0vPKspMg12YSamUUiI5lTDZLwuWdomuv1KtYg4xK5vms9Ct865NKS7O8Ay5huhhTjKzXXoAZGcPxYJvFJHt3HYrtFr/U2567Lr4UfVlHe5Cs1t7mrTYtFm8Y9z04ZEScrtdhijfFrcKiGRc8Xyl554cuN91d5ZfYdBBJyeTibE5pzNoxij829Zp6LQEnLjvNdIs41k93U/3g28qQ8OTTzG4f2rhFUKhSaK8gAQUsintACkQDaD3sQgDIqL1Ho6omlovxsXsWSTMaVMcHOExypQBBI4QUp0tkVECMhqBJmDLPQvo3U5jFlYwxOV0Z4cCHJA6dP0hdPTJGtgxO5Mjyizn5ortdsVca0lyzyB6dlkAZZt0guujCan+CGOnOV8OZqx7WaAKHCNUvPv0k6qeIi0tclIh420RnK+x3X5D86evgfPIM4/3JknHb0KG4e870KqTrzOYfBzdf5Ps9jRV3jrfzP9SCvVA77a4miTMkAUlekJs2ou70z0fVqhJmZm3biohAQyIAqrk+gaP9UsFdnJLQBTdmliFRnT0Qnd3oKAaSKq6T0HNzcrlGS/xE/0Az0xz2AKBZcJgk0okfsh6lrPeAFEEd0uD6j6oKcBFUrQ1KBZoQ0inFGDSRRlUCSUW2jBBk4qdQxGER0aIQyi6cEFGNbk5FoyTxBr+ehAYRWX8KylyuMC/fKIH0XwgQ8frGxtU4V7enqNWDXtsf03+VbezmOgLIYFofFxI/587GqlaFwSlm2diUO6J6y5XmwsAlYepl52tvPzVJUJowlbvYODemnG/GvbmtZkjv0FIgzNEtwLRuVf325xS1hxZ3M/x+pubJqc6EmXeHaoaNmaeRJCuAjYvo5EfgCShDk3vFnDsd3GNltssmH40ujJ4zGpsmSXdT/U1Se9u89j7Hl5KbT062+jKW39N9qGr/ms1MFSGAjF7SUAm5ws6Mdkx/N2Myd2KSpKNbPPrxjaSu7I5TNJRKwI6gFKjjAtV9pf0gUEKNSqo2RpFkqkkjjeh8sH1A/ORV7y10JAcVetvEva/MjO4dJQiiDo5QuBykMAMLhuU1QDUgK9taRxFxJYYEq3Q8JdJHaSqlDLFBXvxAAyZtKwqNltzERxvQRY6LBwaMmc3CjbJl47Uxu307R6/sHBLHgWVG0UpgPGJyHnaCx7jZwVY4hwUwFRv12/lPi6x6M4gm9+bGJbSNUZSsm8K/HxScxiIcU5HB7lCf58k05vqN9B/lmCN28Rf7/PIG1n6kY0uYWc6IkSd0r9eooqpGwsV5cwaddgY/H1Ps0H+oSCoZudrhXAKaACoUP0zTmEMQApPpZDpSoUkULZ3UWXIpWWMkwkInCcEUt/gLVQDCokEkWhQmP63OeztRGNZhY2YQQj0mQQov3OoKa3SUTiEjBTPoOzxkOX0ISNOM8lR14gWNNEOmll7ceVMsra9xb0ab5tRGWd9ntwkazkoebl37fXxG9veOPlBocI6eEum0MmrHe9yDfX9qix9HiuOJMX77yZUNuS5/u/PZnvaxiZx4Us58IlLsj0ylW/d+UGAfswD66cTJTTQGUwCdyuDZ3m8+5ia5xF0Zv3TQ4gyNCXe59Y1KJpbcI3utCq8c+Nmc+dhJfMD3R0L6zMzu86Mw+BGZYROeh7scggskMqEGMrZRIAYa1Jv/CgEIhSqzS3UUd4VqQTEzgvk/ULK+dFax7KMwmOeXcyu7ac0UbaAjUSbPyrAq5VikxTT1VwYPuSahHfUSK/GrZFXBXiZs2XtDmO9KROgJuscg0SSu2yOcp1xPZfvr551jQ51K8W2qYjkvVKponJ3zKipGFYMwYpen/jZ3jBPcY9EwqV5oKRZdBhCWHdbbT6/cNIiAp5pKxTnfD6T6Oej7QUywcVGMbdx3h/YtMAmZz5gz2BDG0X83w/3gHIdKfV7JnILlBf/p1q/DEcT1DZIKgoszrwYrZAShiFGiCGmRjKCpgtk4O7EzBUEQXTOhO0+TCnWyhWQ+RpPuAUEjYAAZXU/ZSHOupAsyJoMlr3yoG2VCZPXgslZUkmyUaCbQLoZw7YkuLDKaBlWFWTTrbqHXVmAmfqhmn+v+PgV33UCiqFp2smQKctALWBJDwe2xYJRopiGIAAgheWkJJRU8JLWLosX2vPaaKSpEJfOuoMTXilmbag9bBg1nsSHOHKUO3rGt1uHFzC3GifLGbVRKrpXFIOPi3349ma5Xj4oD+P6K+S3WDit5wi6qS/09AbvdE2QCzVPE8Bb1FcYlt2LS9fTtTXTIBoq3GgzQpJvUQ03md3oFwBUQE2Kh/7RWJMze8SspKc+S1bl5IZK1IFVEHUFoAjUzs9jGNvlJ5QaEl/hD0oBOYIPuG9SEojCXooSlKMAZIK6p5MipjLP0/T3GSGldtcoduayrfDmX1CKsI37kCCB5bqzmhCXopMo6BLIbAz9NEoGir2JDc4nNBsG7KGlSKiToOTaAjwFVm7NN7HTf1BPmmSe5mOIZ0BFQd1SDLd0etttodzU+UxFA8TX1M/Lks/qMS0zjKHAmSnR3Io/nbzw2HwY7CDR31NHXvjvlRCaBXZmtjIslxRU9tQSKbZqnXWmhNBrJUNcsZowhAVIMFl2CmWQkjZocKpwI4We+QlQpBlmRHP0YtfwA1+msSfAphRRM6bSPoNEojhuIQlPTBg1MLBqAEELyfxCJ0RJkEGaMfk/IB3EKayApuPAghlmtWZvg4EjXb8wYCaB3wA9Um1RdqTKHRey8Qc1o0SzrXDkmE3270f4yyZgprGuhpDtJ+hNZWDtTN88zYhjoL42hjsVT5GlUjJ2zl41TzEERvnKWPH3r0hvPv3EpvnhGTo3exdw0t4LoFjGMRZTr/yAExf7mMC33+bRZDhWM6oA0UQwvpsKIp12lobuR3JJIlw9dtSOj0Xv/GZ3gJzCRvLD7uXTIxIPE9uzoEjkOSOQKrhE9coKIVPt3SGJw6QSzkHoSa3iL7KrV3UgqA2e3WKF7RjqwAAKBmkURg9E1pTwgMjNzESADEEjTIIAaxfxme/GqS0V4iJ/tszK8EUkOIyT8aFeUMQUaxyxkjV1m6qkqO6qoCOFlkRXQouOUOEtDVXQV0ZxsJW8stG5MImUau3AmzDHbVtbwGDWP8a2Nd7qiMHbxhLg4Z8Z8L5x6XDjANm4koI7z7F2NBkY6RScfn6nZPo4ABtqOUjKPmDpXzquoduxJMpPsMIDUT334sScAz8NeZXBHUyzZbDo4TCQGyJ4zY9juJPhb7byJ1meuomA0c6cIEaUGNEEXSROZjvenAgoib9qEgKqiTNDBJgmyAUITMc2y8P3HC7qdVpeHW9JuYhQRmrUWJfl6JO1EJ0C4x2QeZahbRSIpMJkZYvabdDKEQk0YLaqGpArdgS9dryqXU8xcyDJpUobQuJvW6qmvagRK0mLMCpfpxhQCSVGWINUbupaJZolC74/4vedplOzAmE0siNCBGXzvEffpPHF2OEV+62cDg5NgnDAl/Yl1aNsouEuoEZnn0DMnn5s4JuZW36cim6Lk7QDNN66yDHgBgxeM0V4Xh1s4B7A9oMD0Y6OxZnb/fse93vFYycnktzfOjJMvk3HgOIC2TYlAT7HsNp6j2x+au6wEFNd45V0VK7KpULseTJ8kTNxVJWnwuIsR3rjhMsb6dH5U/dhxgIWUiyTmNt4Mp9KkFWbQcQz9+Q9XN1KK5+6pN9EgKF3PscNCSuotOCKSceXKkCr9kJUsZT4I/Uu7KkSaBgmwmPiITsIgMnaSboThulXZSUsUAXSFaQKwxAlFA6hFJyDQpa2FoLUJgKjBqwwqMFp3xWaREiEOTnTIgdcuuvaEt23yXWbiZYqMIJBIgZHqgUtn5kIJkNQ/cT9QOMJUmSQsY15L6pULt/oWWmdVOR88VekiD2Sbp7prg3OiuANKiSZwkqrX7LdX5LBRz5DGX9SBWny1T6XCU8DvTohwqgxr7rd2AfIJTOzjg9N9kDEP8ubBzco0wXLwm+I5esaZ1thntVg9lhIqfqx3Pq4rTMVMsmPfphMZZlXUrCsTdfAci+rvUwdhJdQ4OSaUJ1D1lnVqw/hifFsYV4+KMPD+mTqYDJ2Mx2nqxJ8oixjce7El3UsycwacBLy6Xr0IxBWSkX5JN6T2F67WThf5Imk5uOqCkx1UYJLiDBVRUPwkTDQHkKJZ7ghBCVEmfQhVFZoFKBo3zAKc8hohSZkSyMe1hx5QcWtsv+oEJkw3qUFBV6MS9fwdqmR0+iMQSKoGMDpvs6/XQrJNutTodLolBRUpuDBSXXGbXEVKWPNIcJQpehdt/UBPkNwvRCITzBLBuyF6jJP42Cd0ZX4XDVvnoHs2bqzTF1a0mRjUbI8fS42X9PzR6L/3IhOoitvrxqc2OBLqe9/ZxwH1I5SlaKkYuMy5fal6CxVfuSu27bZTvRIuHDuyHxTY54Qd4wrNzj0wcaxIaqpZVlza41UwpbGxcSGcowvoVodFcTR6l5qK40xl9CScICJCw7rjkmXZIocwdnGYGgkLUEtvgXQW0ul9GZ9IB0wma4okzdRpUpNmDCGdHSaxaYKbTCkAaZgZj9mVkkCAMlqEAFSKKdwv030gCNJijKoQQYxRpKNeZpppN4FMAESLIsIYBxPQhKviw0rNCdmE2h29EGNkTi479KODJem6V0LtkUlcCgpIzRhSWleYkszugNdK7OQmijOj7wqea6tK47bq0WegbDNVYx+3SDZq3w46l0URpPkAgnMvTo5bsGOawBjPuPGh7xQzv+Ni7Jz5L5ukjqcAnuOmzMX/qV/nxuV5DE3ocxmccW1s0K+p75/Fnk5xRfQ48zbVrDnLdTEFsilO1MJayJZUzhaM1qZagfTUk9PhCFp39sFyXtyVXb2AjpUOgpgJBE6pzIQCzZ/ZXQkDHNHTW2iUoBSIhgAFVaJDA82kv4m5ljWzjhShgMVstyFsUizkAANvppBeUaAFZs8H/0i/E++GuIrU8kigCeSYgxSRSFBC7uinKoD7dYqa9xcoIaxbRRrhcRKjsyqYizPZIwRRRGBtZFCnvRpp4goV7jQqgWLJh3vL07GyRCsY+GL8u7FEOUek4biZ6In6mPUIoIg5mBqrAZppcKBOHZkXgmS1fnFFTFb9z8VJNcZ5FP/p9O66fo7Juspv/V6Kc6NSDCv2O+ofdV7RwJS6xiB17rb1ymYyRqtMzZNijWEsCXrq02BT3FAhiBV3hqmDv6hnNWgBnGV7rtJ9m9q4xq/p347lDoLQwECCsFQOkEhDFG0UXlaAOAESmpQg2WjIB7ix03ykAP4ugcCSaqRS6dkzFZYMCjQZVvlRCDO2EBNpUs3BeQRqkEY1O0q5KTVExBSiYuIHvqFRBUgTaK54COjATQ0aaU6vML+JzglLzRkfRkuXlJAUa48UIq7swGT8nTSdKL0smV5foQgSBtB7ExZFCGg25+itN2h+qgghkT9UVIhOXhtIJtorqakZSvj95GCM6x5MqanmfRHTN/jkoTXqiVFOp1pmmAPnrvDpizKxMlH9Ppda/bZJ5JQ8UfGa+yfKYGpV6vDFcOp8K7FjvfMBmHdwayuu17q1ylj7csJS67zdyCZIg1O9lbp4V8U7o963uijgntEs9cfav9SB+8+U4H1RKLqIfxrjIc5gUUw1Uqe2r95l91tR62GimcCg2Q0hf0wQKBA0KUb7saWi0pMf6L5JNSDjAPuWV518U796IasLMMunqng5wJ+diIjEaI4XAJQqkRRITEIO+e5UY76vkK/Wr7/xaAZK9602UEOwuERw44mOGeH+1EohXTYq0iSpQgJikvUSujEVgpq0tiFgMstOrFIxQEmoQsholvw2pdNvkGRiSQLBu/SW1bQCTNCICLQRISUyaU3TtahIGrIyN4uTo8NlskKSrPDfioiYSmWiiA081d2heKJXUpnxVl6E/AwCoLFI/njjGyegZ7AtjqGLU7t8EcE3ttFaAYhKm/uUdfWYFlFp1s4ckJNi4kqp8FRhtni19fLSuNo0NbVOI5kufvL8V240FK0v3g4AW4HLVW62OOvOsspSKRcVxTqL6uBT4zlgTEzNsXExQ3Yt41EkLo0zxuKz7q7M6+8icM8HSS6Rnc5Rov8lBWTRkNEFAblhIAk+YPAg27F/lmUKVL3gvj7sCYpOSZDBVXkjj5IYRWLmHEMABItHABAd4ph1CyAalKR2nplkzH5akZb+NyZRBnEAhIklbmbqQjCLJVEyNIECUum+l0YxQkOSqsBKDaKzmUiilCsghnVnt8tkQxSSOJkiogrSNHtrsKNOmFGEFimGbB8uCJZMVkFLoNSAoMioE5f0JlZElJEZZkdJHdPDpsqSYy5+sTA1xR8rcvBOL4Ke2jQruF8paTFJVVLCX9OXdCxaUxZji42QiGPUYCrkwMFhtjHCGCO5Vst1NKmKzc5BEWXjTJPZ7MqTYuJGRbU54aZMUCuLVluDGT41kXZ+Ih47nNqorzU2Sih+TncuTuUbxVNQqrJvOx+iIkRpyjZvqokw/sBBIjH2UhmQbyusnGKNZ4fYl/qjHLBJhw3W7rzLaD/SaEkd2g2jU9wgQvO0PiR2I1zpAAk7IExeFGIUmgswobuGbDoFd9gOq0FbXQw1VyjAIMCq4CAEQUJIpTTqJfx0jSqBFFh6uytbJ0kF0XQQd7cvYu5FDWsiY+JkeNwTzcTMKRLJmjpXWjRhGVIrQZ3YKT0cxwqsnKosgCLrT3RwUiizKlPaggGIBebrNBFHaK7sOQwUggHadtw9cX5pCtcgYuaoTyhgFr1aNLX8tvKJGMQBU8aVMxvYc3r5u+rLVpqmReR2EX4xlRlPWUzNwdsXN9Di8G67F9TrN5UGk0yLycy5uw7xs7GlNVVd2Eg7nMqhj8FXrF9eheFSzwsrygQVJ46dEJKnMuA5LRg5Fs9lKt4ahMV1FslU5fL0CgyV0tHGa+izJMaBlJSQWx3XujjV623QSgBx7KkyVauoj/x0NbEP94cZNUkbWG4MBAJOh+ycIvzgTNQKUQf+e4IuWVZBoTFBI+B4BlqL3ALJ+TmNSgjFNRhiEGn9arSTNvPE3iscKoy97R6JU+E1f01KElANQjMDdDRVRI3mUMw2snW/TCgQSMRo3iLwYz0ooa68SCpAocWVplUvfZdkzp1ADcg81d5mbJCYh5XGVlzWAjExK5j4qpYKM8pMywRVEQCJYqLOhE3/+dWqqNFDM1EStHpwOhVOjmfYoCFXaeGPu9TjdbXDxHF+p1ZGSLcpGatK2lFpvVeUoYvtyQry44THRnEDKiI0izlftrqXcde22KfvUsy+zp1MUCSmdrGThEpTlaFjfE5/nk+xYIr6x3NqFZWEbyet68rU3bagVbfMqKuhdxHklDTyORo914WJNm6V4zf2jZ7HT7+fkHRydvNnxcyNpb63V/bDbeuX+UuTnQS5BjzPjSkyH4rmFQcKIKmQkAr4KqKWGZgeHIREUIwCGDICUowWYVGQoI8EofmSsoZC73rzxiUkUoVeQ5KppsJtrk0iEtpSVJskuaA51YbC/zlvCKlnQFUyMoseQIMZwaxj6DBDL8Oi867wmIcWDSpmkY5YXBvjBGPosyfWzSnSvIgxmplCu4pKh8MQSViPHCTl8ekfM7Ky0Yrdzr7ahFwfMsYYx4clWWD4FPevjUybChi+HiCfUm+i4gRTqZ1UNO+mfllPK2eqbW68+OMt7HHbvqI3N44h+p2IKcnL4h7UIcWKbMxKUXf+kz2hskilA1K8nnFrr/LiYjA608J0J0KQU9WR4pld13MsuiQMXuzn3zik7jfvKxN1Pkl1t/vDIDeo4FJlRJ4a75AD0OtUZ6F7vl03c87jK9Kb53PUx3HbuDfURcnj5KGLh9Yvw8Zzw9IP06mb+uwrtzDKuhycuIJxqhpktESydOxML110QN0tOrctZBUwpDa8f1MQcW9rcKVVQAhhKVrz5BpCiGogzZWhTOhmWkEEjtBkAdLrJ6mGsACgYqoqRiAAIUAhokEEHnkYaVB0kgik/7MgJPJCN3pMC0nYmXKKZBWFdNOpBECG4EUUpbQgvTujAmFc22hyJEXXvDCqqCbnChMxVQQBEKBN5pfCHy6gTaOp+CP04VtJaU4fUVNgxoEOWgW5M7UIK5n3sRuxU38dtEimNqwpj9pBsbd/uPbRXt3SKhb5i9c8p3k///yoi/LKCHo5Tuv77x3UYGVCIWNsSFaxVKh0Ac74Zwxe29ieq0M0pgKvegB9qpn0xjbE1NOZQrPKhBdlBd08Fcv2kflzSuJn06QY/KYrmNXHdhB/FGEHxch7Kteq1AbqKJx6ZDZFodw22k6CA35O55ONpEgMIf1Tl0LT0+9VbttV4w0wSitijVJljTGhCEDIRgnmh5ifXGJM9k0myTghfZ3lyEOhgQaSahIEpHp/guhklwUaSKUoBQYnJlJFwSCRFh3a6XWUCJAwKOidF4cietFBaJojwHRvpPbJVKIiNEZzFKULPPVUAVyPKZMnE6VDeobfFBOJrkzpKlmW8J5JApoCoxlpjr7MDpYp8klFHACwZMdBEYUoEKBKJBSJt16g0EZFQcQUK3jgljichTRxvMGN/QL6C34cMhc307pk7Mk7uPUO61QGPyBYj9GCdUR9/+uK41C55dPRDN5OUmmqaLStR85gEMa/2Yqreexz8XhnxjhgWqnK5LnRZ9nVR2ZKOnpjTn96zbhKfl+J3afsQgatmeLOMFg+qjozzD1j5aJKyNJzNSwTXqZC6jHEoegwMgaEjWm9M4ObOSHRgBparKnUmVY9eUcmiD9WBUVVFQkun+yxFkTprQd1cQJjylqdIuA5c+j68Pn8TJWDJHiU+hip1p5DD5cmWo9+MojQSEldAhDi+s2Wihx55ClmscMX+kGcCA2gZefrVN5wkKBXO1QJkGzEhZ0AvxK3kexkpHrkCBWqiLtUuJQSA0ImXcpKM9NvACBo/RJHopIyIx9DMraGBy7atobgm6z1iCY+FJp1I6mqPbLKusaWmGtuBpfZgjABTdN2SE0RiwmFyQ98SoWm3o2uUDGnnHhOmEbUa4nFbKlOiK94BIy3zjoGQmY4CIyLnMdGuU8xJGWGZH0dfliMwCoOEVNd4cEeunPBojmftjGIrLDmBnyfSoliLD4xPyI8JWrAOGqpUCvHE2AKFVhRbKtPvJnU312NxlQzaCMSvDJ0UtIfm1rRU2lMMUzZais4Ht+yMsmn9rH1gKB7cUqxzUw12UlqkhrKLkhBnQghApMk54wE7/PyREhaRAZLDIeujSCgmDjHws9jJDno3LLQ9PFwfwnPfikraGSGQcCcxODyDyIOXgRAl4MWUwlMZ2gyr/RIIX1TZjKYGWCCQGFgANAEhOiHu8cFQOwpUxFmtKZpohgAtipeLIE0GizSTbAiey0UEaF5bMRkduU3Du2CCiHZGiNUO+SoaiCgIpEkRaGSQJqa6hz0sL2LyDy8IhMcJcVjkry1kuWnQM2YmipmEoIw+3VpT3FjU9t4DrR4I2x7I2V/zg678fPnb9lz0N0b4/1iOjJH4+F4++DUBVRo38cY2DljMpVnj6OomdXU4u6828BiI6VljmrClM9nZazOTOexskg3mr9UHDdOotl6vANyJ899JoGoMiyVi6/rTGwVI+48rpoids2c1RM75Gowcp7eKR17Kq+ZcZAq8+nAzlWKNWUgcHBdScBJaGQkg6onvrlY750BEVEmdL+C2bJB3LAqFwaQSRdr7RLzOnxMQk9JC9pvhokMmoyxKSY0engkQHDnbEvsR4j6xxusja03P1zHigAUrh9lbl8hwVUiLdIpGs58TLVNykojMpeDctXCJSWCVzwoGUALikjQBnB5Ry+Hat89p0NCOmtFBe4CvgKDII1jp7shneiFUUXRK1LRokKCbsZASQlvP6cDt7EmvNvtY6qyWgEWVegPFTPcKUTCoGZbCSaOd9cVfYVKdboihDAlHd9v5c55uB2Wto8WHBdgN37O+NDdapQ2DksR4locgfG9D5BogwlQQUicWXt+6qdCA6l3JQafUFzsU3vFOf4Ur6QOe5r6kAoyemopnUTGsdLCOGH3rT7z67toMeZYobjM3CAp0SjMSHZJbXa19M9UEVGBCjW18VPu3PvoHoBv1ffP0FGgk4xM4UJ+u6aeQ3fMZrGH/gWLQINoFnoSh3hb8m7qMAlGKFZZt2oIwWUpo7VmLYWttU0kXQDbERvRT/WsLUWaqICRZpCGFr3nAhFo4+0bdkV/SIB0ig8M2kUGdB/rNC7mAo4BIYUCKnTyaB64VPPRoR8bAIr6Xk2k+xeRRsRUXTUyzxL2YogUFAkMgCKISLQIaNf4kGnr3sqRPL/GcAbZ1VS+O3BqHoThA8HjYkt4rGc1BgBulBDY1S4gm/jxUiKXjjsacwioMo2HHdRp+63rwT+dwayo1MM3drWnRL02mlHVFTPP0UKiWPPomH7FapBMIHgq9If5mt9nWWWRCR+TwbMeLHyZ4U82VTWs7JAXQRC9sjcWb7yP6anME3c+EooxIpEgDRKSioDQkFWiRdTFhzwbNxOSMCQJSGT5xL5lowCBFMKykIOsHJ6ShCEo1rsFTcccYbS4WqSp2GBmoKZAxmJASHm1KVU1BG9y0AwQiZYsJPx7jUGDmXtOqIrrOYmEkHCQXfk/6TkrAFGobwfe/fBQSgWa1JtS4t+RQdL4ZtWrHnQ0OlQ0yVEJjCbZGNNJHX0xkCzv0F/VToNxnidX/yVup0O38pYN71Pk5d2DdAmsD1qd8gKQqiDJVCX2DFKKjStkcGvjc26jde9YRKF/NhRl3YqN8NO46ym146IqRqWTOlXAnMKCFEW9NpZwz3j3nHJDTVTqEdxs6oLHDJqizcTAcqLH6j6fbLvYT+wQndMwt0mLgQp0bo6+yHzdiB0+/SLOpihWO3iIlRbVOK0qjs9G55rzjRsq1d8+AnSwlruIkzSzSMakwaxAtmhKGTBgkpQdVzQB+JkvAEIIqj3OBFYEyy5JRp9GkPiZA0nKLJKxMqHIMtJIfQrp2WGoIx0FIhJCQ+li6O6YTTSC6GUGyZ4ZXcuE6aMaDV1lgiJsssy1CkTNaEw2FiEEcfCgl2Ng9MBEEvTSKZ+iIlCqMpiErrYACSKpjJNmkELd2ksF8MDLzbVEmGSkXBgrQUFMEmTSkN0lkkOo0e3FmSQjGBDcGHPV1kHqQCV1a2PIEIuCiML0CpnTqT2zzLICLJKRTFMRS9H9pouv63fXPxsqneMz2BbrXMG6mHSdJTEYtGP0jC9IcX5m973oJ1If6joadIovcMYnRF3vskgInMLiFOPsrexYz0bqceZ3VTgOgzB6LHtfP3ovQrmlMiuKejkVbdb+PtnNajKqKsUiDfQ4IxjEhR2Tv4E4dwCaqX+aZJ6RbayZpIySp6MbXXaVdU1npARvtTt0L3tHGqiUlfgQzC0bVnQEAJr/qiJGZLU6hKCe9nsPQjTpR6mqOXCTELjWU2YjmghNEaC0DKkgGUEGF1v0QgTJGMWogEJTlKPZzyJl+Ua4R4WYUZwT6uIOohkSoZ3atZFd6AJ1yEYQEVK7z3EhbtKYNLu9EMM0pgmRakzgTRO2AhOaK1kqVKG5LOOWGeKKFmY0YybNJJpJhj50/xklJs4FNh9Cm5YBJ/+SfsGdHAZT7bqiqrGUmA5FuZWEM6UNb2Say76TbXGjiGy/7DS1OxQdLqbGp97sLGrdyMXw5JTypKq3tNnz3Fm1Pjt/vAQPmmYF1/m0p3x7nDmFxniFsdPm1NlW1N3Kb+EJlurxlzxTRXXrkHrceBqLuc3Tqx1Pqg3ggNPw9d5+OcjG+EnWFZPG0WE3AuiSzwS9z+d0Ui7QxOOjuNOzMZo4boCaBQVdVFnETFZl+Ky1wJ62cxstiv/eUQTd3M4MxyRInTr1SRuJ2Tt61aFHQueE3EwANfU53IpSV7pRIQShxbhkdsdwjKXLFojFxjshhhAIN92w2EG6gqoBoIkCUczlFZKihRqgsOjkRtefTIORp3cyy/JoKx37K9aluBID1O84JbuA0bR7cp06loox5vERH9lcmTFBSMc8s6gmlLRoDCHQCxXR6yQEKapJMIr+9q6+4LJUrWaX7xPM+FSRkZUo2Ir30ZvQJ6IdTh1sc3KOOumgazDl+tEOYoKZNzz789n1+dbzywJtoZIjdi3elerO+qUOLntn8NXSaGwxJ8ovZe//IKOkGQAZ8yh1fOYM8c5c8Nqdsnx9a5Nn/dIGtdc59zg1OIldVk1bK7bjG/kRmzQ3vVyqWy3b1ck03AG2DDtssBBnraCCHWuW6p8vLZrft5519E7NLYoux9vyjp+AGNLpgIl6rXQqDOnPXRiBte2aq2Y7FMHtHgiSLbBwz4YmBRfC2AYN1ODFeGRFSBcoVA8zEIUmEpyoKKkOT28vCAmYZcdL/6bsfumqzJoaIqsCg7msUbeJUSASkyFmOomJ1MQPSTlalW5TRWhQkEJTZIUnASVCNX0GVcEAKgzRxACTRER0skXigwYlKGq0KOz5TqWvggod7eDBElQ0BG/3ZFdLgcCxkEgqWgYhYCpJk0lBTTrbxmghSXECoqJKiZkWS6V6cKCAUCEhP164u3fu92hAo0RA8A1TgWiOZUhPUSCRMdIE/l+KizJKgic+FFaHW8f4YFe3ET3JathF99d6+5p0f13tvDjORU5dGXa8X6SAbH2Po8/i7OpSxMlzcKfDPYXsA5vnXbZN/JKyzWgMfrnSfuHM0dT0X+YxlWrL3TMFul5sYnMNbr00zTDrwWD0V0hOj2qfkbXtJgZHNx1LHQZqZRNc0s9YX5szqxd+Cc7/4iA9qNQQekQ9xQmWvGhtcZWlzNa3oZwJSCdSOJtKkMZ+7fVr4P9uqtvEWpg/fYprivXVNDFdnYhXwFr1ZkW3CRgQpTN5SmxEVyGOqcygVGVW++kaCr0+vFsoMIkXZasHPwUdF0DXWJTcuYCYIMJNnlKwbJReLLOOFFGoGLJLhAso03pmDg7fcVVpF4yGuPF1AsILqIEdZMIixUjGoCKMHs6k60JSfFBo0EaMNFOjG1g6+kMFkCAIKqrUROE0S5rVjt+wjFj0dkBhFpAWI2BB3a96zYVcRMwEgHFlc5KqI0YVNCGxLhVrW2ZAgEBFKa5Z0d/Vk/zF2meqAkqCQg1Bm4VAvT4DkciVO0Y6NjIXA7Kr7iNOKaqeQ3LbSK0c4ECL7z12LeT0EgeZVK0Xsj/N2JXZi9nnwFJhqxb1Gbdj6weDVGGhpXdhPToa+hr3re0uZAumNjf66WNRnWlUFNmQ+vYK+5N1vgsyTwr9Slk3KujpDEy1MGSeV8hG/96zf/QT07VgQ1PsZPWLlLlbl5qzyCwCoTDbOkOTRgBTDcCjE0kqDXCHymyXlIoDzKjGFBIodKUOAIQMRcinu/SElTtSgHeTs+ChsBNdSJbPSZkpZaopLGbfRE36jTaKUehX4udyNC+B5DRXNXYsTopCYSJRogtRuxa1wzU0y1gmG291nYPo2MIAdVCh201R+rIXopq6JcnlShyq6OGqBm2yP1W6bqNTUAJENQEdKKu2DbCa2fDAlyaUrOVpqS3RQJt0odJVg0xcxkvI5G4eCRE1IdSgIKlQZudPmdwRbDqhHMTC1g8adhg7zJdhkAm/lp50dJruveBARx/OY8T7KKaPrOVJW52gE1yVRF3mqo6nUyiFnmj8VK+HnZHdFmlg4ZcY1zbm1ySw/qeVuv10JaAvOJEfP8bUyvEMmTwAOKNHVG3X53+aU9XX1aYwlU+XvmvwEBU9O5ypEfchojeQbSOiZfpkcjiZ9Etb61dom8do01CvHtlkJ7TkcyGrQ4nlSKjgx+Fpt39mnh7s/sqsJbA+FJor58qJaso8QEd/xfXdnlCZP3OER4vMqfUmJta/MZ29ZKTE3miotxBijF3VxhK8UZNkZC7l98ZTxKBe5CMy2TKrLbsWkVmWipKAJgcKZhZTBKDJu9oYBbGLXphtr9L5FVdDaNLJWudtDXkfSKBEBFUNgQlySQAaGhdCWPEWvJihEIXSCy/rS4xkNBGItz1iNNd9hKoEFSTwPI0WTdgZca7RS/yZJ2FoZt0Hrk3uLqpSqNFIidFWdD4GRRhhmiBQB1V24hkZeurS2+sWZJpqGAShaoBjLPOukQK2ZBG+tnHoieNonLhtv10/YiASXEwjxkTKinTSRcgkKhYSUzq1UxIxdZ+OQf59ATPpXA+TwdnQBdndJqiqWO9zTDmMbPA8M8qFG4lhQ6HSsS5i8QZ+pB1Je37JZyddyxPW7sefXJG6n3kvPYj0rre8M6o3sFRdY11rZwr9SiZCniL4qeG+SNF1nDT0DiT4mQLAeg6O3SNQ1a7mEZmA+0KFIJoXMATqbhQgxaKpBDdQ0mRRnXsl7AQ2VkHeKjshpVE4O0GyVmWuLaUmikXvS6oGh3OZU0KYIImCYHT5hiAiQdCnRDXZwyIl5SISLTK3u90hSxVukZHtK7k0806Be2Cm0HPQ/4sUJdih6VRz0waQKCQldPuXRM0wi9x7S5pRbrCZIJXiUAd2N0FN9FHfT43RNadDQEatUAWiSM0L6fw4DB3KAhTGVCBJYXQxqtVqltkvKGh19xGZD2oareoiJWygZrFRSbeoJF/aeXWq4VJtXdj6K3XVO1yVYXRqK0W18C4l57CRAL5lRPOaV6qUNK9KIyY9xF551+8bsM7IxNG7325wrD61ONHXL06JRJpePS8P0LUnmVamI9blrbDxNEX//8uzGrPPUky/fi0pXJet7574Kl9BrZw+Wj6kmAvVT1xrXH+Cm49MFPNqRMk+gKM6pVZGBhtClkIps7NkdGAdc8um60uOlvZgPDH1FVgL3bhCaeTRw8QERvXp90YMlbWWPYZWV8XVkSPiz3H4iK03Q9KNl7a7/lsSrzDV0SmKJsKMBCUkayTXd/KLcbll46rBTVIplk8qON7Qa/PM89FMICE9CyaAqsP9SaZwATRGgRhjBjiLIphYp5LkDoxkpFIolsQf3QgSFEPw2oEzCdRak6CNO2gIRdU5dC2NEIppUIIWo3+be0PATEQaWXlnuY1Gr65FkWSolYcy8x09SmGM1OSRlTAaHtqYAckRK63Ebjcmkq04kwZ1tg1zsQvmPEhTuCAJzLp28gl65YzUMFIIeiqTkgCNeXLnqEKVPVHwVY0FCG5g4TxarM/5ksRerwcmqTDUn73rwLq1tTDAE5SOifyRKIQLE14Pa9+xMXSYsg+eao0X4onNmz9z/W3lfDZanGtN1o3876n7Gq9/PwRXU3c9IliXC5NS/bn3Rf+/1t6lyZZ0VxKSu2Kf7hEGZjDl//8qhlgPMAxGcCpDcgYufRErM6v60nDbrO1U1d6Za0V8D8nlj5kT/uZ3+aij/0kM8S2b5fM5/BNT8uMq/UZh//6aeHbo8sB/uUt+PsD/kJnpZNg8bPnX7fu4vnyyJd4zb3y2ws85vz9haOv/wUb/n3JGxqCF3/bUP5orzFBSJ3koluU3j8tZPL/Hguj3BfwN4lpJ3C+1KP55Nv/f1rt/eyN4Tbj+G7CRz7/TayD0+RCG8P9ZXrwO052y/9IA4FvF9dqzkipeecsv1cbHollBx28nId/H6c+ErYd5cEyc4zU+N33x3fqsK/E5YbaEav+MRnMb2ZWqRKhXrjl0Aa+mrRh6Ht1xIQI0udFOYIhegrP5AI91RI+f9GZAO487zJEk7OMABoNQgII46LsC4IWlbCQzQXMDY3QBqBOFHZGZBkk2L0K675uIJP9kmrXwWLxFhtAPshe8nOnA00QiNGUHxD9XkKFN9Zb2iZgCmiF2a7flXOquFUIB9NBOn6OnpRGPtk2l2pW7oMKMf4RGdTttY1Iwz27BJoW+tyiErXKBPKOmbxrfH2G4OkOj+JQy/1fd+H+e5tK3qbYi/pZZ8Tdm70cRFH9nGv93Gc3/v+Kx+lb67G3xNsPA+xz/Z5pS/JYU8NOcZ+PV358dGM7uz1pEP7Hc3yggL8Dpb5EY/OPE9p+ZpPhvFty+/++VpsNvkp2/C1n4f+FZqRUK/uYM+Hq5H8f9D8fAx5nu9azef/E/gNj8FuHxSj0+bId+2678ZIv8Oto3thzvtz+a8PXV+3D+xz+ggN/erx45fQRyyTRvasV/tDD4j0wPf+yLAbHfnnXfFt6vP+Qfx3Nvx3/8gBj/aSDzpkT9Nj2Mnyp34BzP/dpQvgg/18YzV+yfLcSbRhAv6s/fkS4mZdp3vi2fMXGN+wCXajoywh30T4risgdmxtcPiULxxC07xkJ17CZ9jL+zGCUBCSSRyStixgqHFznf0Xdx7MeNUETO/5AaipV/ggCSto4WIE8XHIa5jEcQCY7ZZQIZ5mTEJRXHTao7lAH4gu+WmgxJF6/7riCkDqIFqI+5FQIZvF+9FDyI6Bv71JwZ0QezCFGb4Hlqp6DCxKSepn/+0QML9CyFtmbUfIRu+3NxkjiiO8oi2qXPLK0+/Ks4ipC5qcED8cBWEPb6xpAtzXY9cqM1p1Lk1pzPHaMPYE3cALQFjX/FIftFI3oP4Q4dSd9q4eNa9MbZZJ6s4uUPgZemvCePdWovl2Ix8NDTQsWnScGBMQ7yrO2tsT8nP//lrzci5w57jhktBqi3tEbR+KH9/tWT6vuEIkKcmcTrTzL+vwVA6I1N/Fd+wj7bxcY++QQh9X5ffkMI/v6SqPeY7Bwl24HVsFlnseFzWMAHw5e+h+P8AgZ8qg9O94zvF/p+jFMY8ukpY/oC/agCPl/Bezu4uc/4tvT/Azfop+cpVpTv318DvSCgUcAPoXt3xynMN3Xv26hFry0zIgt8Xxv6brrwejLP+vwooscIQG/O+PP8+/0ovtVTf1ezHhLY34zqfiX9nfWzx8gPTOvzX+oFE+RvpaVeQIp+nnJYP2F8ABDnMWoPojjcfmB0dqMAXD7matz6dXnbjKF373HgEPB0CGeEN38SH3lmB6CIbfEDslIv7CI05ZJ7/37pTX1Nz2Cud6Xcr7XxvGL0AcN0ukdu8nUoOpiU++bzww1T9LBtN+pZu9iA+QkuDaaA+ny8UKhapNZNSbhyb2dK1VogQgKj5hltkesATyIirvG06Q4oESWpbInYB7n/6poxQjLUKKwf8wxp3rfOgx/5VXUop1KYfO0FlXoctRGO+ui23mPEEnuDdt+JRETP4mYMe3F8r3pZDdhrezybbEwdx/16vpQxrN5St7xkn5nCzjue7UKXbBWhUDqPZIw7P6xaetzAtD/kWKA8n+2gVWP3YQUpona8ekoRz4vGQuTnmflqgns8qlwQQZi5moKKtmnoywcdZyr4XCvzwubzP+ju7CXEseYC2w9nPqS/+GSPzNf5Dt2fxHfNp5l9NkeHtTPa4uLv+G44Tf/3xuc18ng9xOO4gc+/svPIX04+fJZ9eG5D9QsLnQLq550xLKBB1Y4KS2veckaw51P9OrmYJYTseK564Rt6w61ox0iNz9OewqJ6fFG2HMeP5/b8AjxV5/jOHFOEXleiDqOdz7Hv/814HNv1fKeznBC/DOT4NjtqtfuYHSt+HwPt+SlE9P7/rtB0zr5pRmcRCmdw6DXvpzU0jRpM3cqp8wSeyTL2bcazYc9/nSr5hZ6t2E3NeN/NvmbEIX8pA7Hv613oaSICvi3C6OdPGvf5NvmULxOdrYHnW3xWM/oGFygE5PnDvUvoc+cOBICXSR2+v53X7HX/adewzkbQa/3Hc1SOt8+Zcp/WrLci1a69z87kHDXwqaJnAuQ33tqLag2FuWnL7OhJln59qT42uG71xqN5HkudE+iZvQiRofLSraneO5THmDDQNWPD9Tg8rtFn+omdizCqfTHyNVdydx3HbumIyXd+P1spn5zefo99Z3K0Mk1MSERZqykEkGxFBM1cjAhmh0x6OBOQ7kLmlcGp9RndfV3objGloAxLsLtqqR1RQX9V2lpuJAbXXMNzANhxodQN3cYBxr7Rls573egAOuFPMGqpFsldJetWbVqWzFa1Uwy8CXt1ssP76K0EF7CgCa7geXPtsmUmoHFh38xyhWqaRnqEcm8XX6tXdd9NLA7oYhZzHuG5V17Y2zONGTmg4ixUKz9xICc8B+X7nH/pcUP6FHq1/c2nr/dLY8ve3EeoMhVAt5I4QORZHr52q7X3zUPU8GGNsdE0+2YIGqtu7Ag0GjLLhI95jjTVTDf4aNC4r6qjVxj05j3vFz5I+Zthg4fL1gqFcuZs0W1blY2u/9HcnL4PC5Wco/t5rOusGJ+//XVpCq8W8vmLdoLFwjXhDanqR/+Anapk8vx0Ty9tBLtiN71P5Flosb4xu4QeKsx0gup4qp79NnJ2jD8R+PGoMSZ4aviiitLHKKfVY/y+tcI0r4p7Gd2Nw4g0DqnvEDieS2dfrt8OvOQeRLmf41mPK55PVSwEGa/3NX2LnXTj6XzRYwY7P5/Lpitbrr1g8afTxSI0+2DrQPyLi9EI3tkB26feo8SfP9gGqsf1ZoqVbwoF7lucQB88BJIaId38Jc0FgPhcGqaaxcdC1/OCER2RoRe2iTkZ4jmydplMyyS8jSx02AJ79WIzAkcB5yPCb0brfPy2UdILJeo2eXB+55YHU030A1kIW635VR3/LixcAIK96ISC251aSZc5x81h6Nlx4cyJpreEFJHeOFv8uYCptonyIBd6xpUffC3M/XJevhfieAW1bO0QGicwYtIt4f+Ip3AN9GwlzwxoEqPatkfzj+ZaYrzf5xgfCACv7wWGElF8QrBAQARYkmHqJkG2SooLCfMKYETqWgt1QLzuvknCoA02Qloi0/wFf5aLrC6OuuQKGF7RDFeWNW3UfpY+6EyIjrgjIqFXWYpPXvLsj8z+xLVtbfn2bSceHN8ox9cWsDy9AhLv+hdvSnw8VNphV1xGMw/p2RSSAxPWD0rwue3e42+9MK6KF5XiTWp7iew3UAQPXfATHv056WQgftgmnDmETg81f3sfwmfD8dwRGV8vpFTfwNbEB2dr/bofTHeAwp1njNwY88cemssai2EeMhMv3ODdpmTgO1EM+Jt/PKjvoUdhQBXfvUggor49/9fw5oECPt/LizL2/Mt+rdsPuXA80wg97nLzh/vF3ZjfnniDqgGQcb/AksPtQh5e1of0/+m2jgpoat3n//LlC/HtJc4Y7E1pjB816blXPkdFvhvzvW1n2T9Duv5846/7/jVg/kEW1Lkvbby2UXX8LPXmz+R38PxAJowFERffmnc3Rjofg6Y6u/gNcb3fHb7/6v6Vvvga6b2EKs96OBufn43+x9Z+b/38btr8wHWYQucNvukpxxbVfD9zPMuer8fLiHjRxvVDNDU75fPwwPMv8aFdeZ4VHp+jz52ibyObfMc8vjaFpwuvU3Qv0n1Nr+kJdkNtkqIthPH+Y/dv72zp9NDn4cDnUMo4FhEZEfl+vIrI42/6cnc459hLRbMMTsQ9zkb7+PfoxmviM4V0vkgW4Fm9b0aY9kF5PFMRxbxjrTl+4UzhTFOq2oxJAuMWyRlAtCqYEXH3HeTg9QICJbXqyosthfTVUjBawCUpogiqWxAAVQPdCmzjhGgya1D48/qfArsj/hL/l//yf/x3/1o/hm6N0hFYfyx/Puj1hHcM1x2BqB768l6FEYE+dXbPb+TL9eHgVLPKn0Zw6k09zCxtSdjPFYsjYYlH0DnH0eo1NMdQHFbT2G45MixIqBRQblHarUwMeO2fyIN48sRmKL4LMQ7JnBKEcRyPB9gA4i4hYWmsMGkd4+pjj4oVqsTn+BlPUgl25GjN8YkvePOJ5CgSJ5jUTibO4eGh8g8TGpHjynXAjEO8f1rF9/m386+Z5gyYczqnhw1gGfCKoyYSLkLkBM6DmGHSSZk5kp3h/gR43EY+cPQzXh9eeItACz5AbPZ6nusZIh/eqa+qqAj2GQ+5u9pR2V6h0MCplEG+Q74MvNggJ2A3RlIKzQQU+uDwvQmSUrdR3xhI55D2jjDJWW6vYmTyY49mQpgZ83JkNMAfBvx9hvYcunV8foZvQpLz0LYvildbvI52VjxprqVpF1tipNYtfMDEPSKJVqD7Cbg52IL3/cA0dvPzUaJT3wzSsJS2I46aJ94aSnsELisIhoGJVa5od9uZJS8WIR7knt7mbKPNbcdiZD6bfqrsdRMkpjveYT2N8emgXQ9lSKdq4QpUNfW+iEF03kzHPRu5vRg+Fv9AKUYTdgc+IxAvc0c+fr7HcwR7S44p8Zk794blPsvghZ0M7DtCFmMbxPaxQ9Mh4mVbNL0sdnyngvDm4SyjTe/BrMWQCghB6wjj+TxjO+ih7egS4DVI1cyGZpCfXEbtEBwqRM6y0Vohh0S+OLYSCLVIBzA/GuLoFxMXkRHS2CoatjibrkMF/J//919i1t3VpZPu7OXbOhKobp8bARJBtZk07c+UeU7mkTS2OpExp0xc3MPjgnFsVoiMuhuISCKkauOoJKPR6o3Sklr4kxEfhpBkBhK8pPhf/7f//b+ESFbVrAISwwiZ9fJempwXQE9s2xMAAD0zMEdLKEaO0l5QxwR68CrBbBHNhTA/kepuOJVbgyhCltJq7cdHS6LuoB/okG9hF7CHBonuIbk8rp8NOVmGRFVFB9c00zwd9wmbzu5d9QyGAw4R9ZLuOV5JQt1zTRhM6SQj6FFM1EhzSbA8XIM5t7CzVmISUqccEPaeGeoMDelskzOKmz15babVHSRe0W6FT9HTS+iIha6FWfu0nsHZ5nHmMG0Hj+HHrCK3OgLKeALc1CdIaSxGO9bJw5dxR0f7CPCZd+BUXxaFQCAVZbAWDo/f0dZojUPqAk9PvqlxnK9khXSIK4PeAxdjIhaPp94K+A7g+6IO4Jk9MaK0oOEL9/AhqAEjZ6d4IVk0dVDWBtIPw8ui+55xBCndeuzwj0q5uB/AB3cvawrBDaEduqsCTLr22ApTcKj94Xx0g8GBi8aaovsmAVx2tYsAIodnjmijsmpyjuO5uMjjBw+Bnom5g4T9G4vKsDuOfP3Ezt1MamB3R2InMfFCPTqGxD2Vgw/nmb7Ppch4YZV+H2T6axoRbkRmdnd3IQEl6XjeAZyf5eRK+kwlYixp9MaVmySfKT88Jn9MKTSb90wjceh5z73yUjccryEdVD0AdTJ7ZAHv2/2b66UMcme6DqDGWjDSIUOhYRGCHIuhOkEPh+h2jpeNWJxJ//yO7tX4P0UPGKoKn5RJD21BqUVFEH2Cg6zDbBQ8Jfd93A3b/M58pqFNDtjq8UyZu9egSZtDQHRlZh9hBdx8+KjM7VvuFz09vdO7xfzFidQngqsEb8KZmCq6O6iH43AsfISILJVvs+E+7p3gctY0l1IlLpAd1U5/zD+67zafiQ+Huj0r8aEBCHFlyg3IlOWL1mFmViTvHlckD+8pEdZvuGnzZRS6XGzZekrF5KZgAUFhOATe7vuE+E2sAnV9fVW3OQHRE7Jtg+vuznHnDvqtTGd5cRwkiEDDg3MQElvNQJSm0vbJadRGCrRVDxVPundFIJluomh1E5jMEBoVADqDXduVSErkluwXetlV9Ofq4B+OFdT0gTK/d00ywPTq/7qLQOBPhG6tPScSQnfP8F2SoroxaSJ+bbnD+uWRgdra0OcMIi9viQ6Kqg6yEBA7TjzZtneimz8yq21wRh1O7hS/PsegLiQpumBDXqpu7lQxiGOJi7ab+F1zuXIGVccsTNGA0avpl01ivSR13D4HgexSqKUY27S2kznehP+OsRKbe62m9hrue/BL6gBFiKGqni7IeS1qWx3Zgk1PVxpCMMd4tEzIwqSkYquaGXhdiapaUEKIXCCgFWkj1bGB7w7Lo03oGerP9J3PaHPue48qNwiGoKJcJXeT7gHCLYvooYtsxqKeYSmGUYy4K7oJ64RS5aPCVw+1RQ3xGUCwY9exrmvXkQy4NoLuxstVIfyrJSa7m4Fgzhi4FOjk1QqJUbGDoxqX6BknVCAi5cA8TY5fA+h7eyRNBaKhKshPlR69AeohAMyZaRkNL5I3pL5DTLCm8tml+TihuSHssJ7rdO3l4rgHBSGjxeMk6WMv+ddXl8SkqqM7OZTegRwDCOwFaE9egXPvdfR0TEOFploPFVQ8hEqpfGUHko8KYBh+04UP5tBzVlBzQo6AfYqr6ECp0RyM9CXZlTcwgui6QyD5Vb7XeVCb29Y/HcyrH/gPdtT1D/C/Gc7moM4M6WulUTrsSJeF/ZJx3Te22XCti4Dn0AneR6PvM/duWwZ3iMEORSh5lb/rIOOAlOlGohC5w9xmXqWBrjwG7yjaygcRvBhZtm+iOVK7iT7ueAQuCwL7IWweGeQsILUSND5k0wcFggyf3SAM8Cy4qA7FpShmqk0CNMeyavId/cYTgSj7OYaqwZvqum91i2x1gjt9nnBrr6uOTl4+J5dvR3Uncr7nzEhzWia47dYVymloJK8tBqRIQFEJRpdMRtm5i00XEJXMW/c6oi+OFnFJ/znU/dddc/C65BkhSccfHvUCHXG5P6RcwrgpN6QxJB/JkEGrhGBkr7+CAzcShNrx2Rlxq2dbrhEuaoBdbCsyBBD1UPBsylHeb/cYTk3lpWiV7i2lZ5gO2VVyCWNO7Awo6l98wAIFNTxRTWx3tI+47v7jAgnXMcc8wwMXsEkOd9TYl1NKNYRkwisHA1Z7GvMIKBSQs7t23Dv/r0e1S6F8OJERYuhww9wl/rn1JXjE7Ctj+PCIC6ByeqYECwrtn9xoEk8+jAANTxU+Y8igUr3qTQCIlBRWAsfJjMERWkOUKthg9mDLpCCoDFZNDwTbkliZiyf1uopSCF1Etkjk5M4IdGtPpKVGMURkUJLYAbCPPMG4SCHAjorwDTX9MQCDWmwEVCt7u6BglEyaisClUMCnsftlV67duAWu4wv9aDs6k7GmreR191dDNGwJz8ZGxJC5vSNTWsrkoQh7WMA4hnTWZA/CB2neUm6xo3SEphG6HJreaKwBzxrFgJJgR1QwonMGVyetV4/2ZsqUwY8Y/+pWoZcULDJXjtIQx0aXeUBdUb5WhMEkpkdN3A0GqYkXHBRjDHZ8bfiQTgVdNLQvT18xpN9jRQ2Ow6DgbaGuvnaeF1S4SUhXZG26mlAVFRURCRZCISZ2TMMORbci/gRENJXKcFpiR04QYvibLUuajWg0dfBJwlgjSSHStRcbjhmMUCDB9ikkt+gEPbeaJOcIRlqS12mQpL0esDZGBhVqaP4QQrijRLCDyRxq+/4PVxJ40Zh3QjAa8X5KWatbQmTuegsOcFehK4gwSa5hAgWlDlJAuh8fAwPTGVNV61wQbT/DFWeIGDa/iVAS16uTbE4vi4C6ojjxTI2oneBrbp04bR6kSDerUxEiQjnwUhv59rWi2tUarehpINZZcpGHqy2CnILBKZSXQkOMnbSKnkomGnTl+dXx9a+4eBTULpc9mE2iwx/VINEs7FA0+OeaK1zGtjyQcD9kZRw7dAUiCgzcfWdOooQP/XqJ6V9MmB1Iz9yUWMGiuaYZ8T//j//D//Tf/3emQI7fgBkqM91LN0nxDP96Z4LPiOAZFh7U+3hZ4PTlvTmcoic+6Kq+QIE9KJkyU1KUq+LD+NY4bYGlm4a76ZMbMjsgUpakTlVuwDklX/qRiN5huB4umT2qR1RzhuQ7FZzppw+pOgS7E9i9sv5vtLyyBn1bxeH4HboFXk6URj+GTItT8J5pw+C+70c6b/BF7HuFyjtLZRjcS484zqGClXhSC1d2NUcZe6yuDMM62vSh2r0oS42VlUlPLt+B6rdtmUZZ3/X8YkDRDWVA/agaHJvO2d96i8p30vpY0plTvfwAOFm+d8pqis+yCfTEN+x8VI/cYqiITKgFO7sy7u6X3nyFgKBUHpdPJs0SSvro1+YH4MgPQqP6HDI943R1Vm179n1AWj00Q/u/9gzFj8hwtW5Dd1zH9yGbyE3kEcFNSGyMpZqSw0HDUUTOt2S7WkAPhhxsNfmoFeKAtzDzXCNHNJuqTw2rI0ZSKPd5HNB/6Ty7vjnYjsdKjWdzxXtuLu40zcR/6FGG8xheruJY62wEa0nm+Ju2fTbXW5WKmeKPFG91Q48AEmD3DfknmcQ2PgH+Mn5T2t+s9unkV9/LxIMxahmhmk8eA2SsABGP+8uL+rpUf4UupqBSUy8nq9ZtRxYfgIT3gb/l3LCAT0I8vJgud33e4bvYnPRhwprRY0ve5wGDL/akn5yPZZxBEcd8USNMNy4NryuodKZUe5DpbWxzbrRVesJz0Hn9Laz5sxeUL8wNtnhZX1SPolTxSkFiRBTK2yx5wvP2KlzSGIJvy1orm9qZCSEGJVqFAh2umJzAgCPcQLSBRkCuzkOh+hfwn6/lJoDTDZAEiq/2T12Ky/IS3/6aq3xWMnl3dYjI1gz2UFXqDtdbvraWQu0gjbzo86PUIPuu6NvDvEB015X/+vPnP6+cj7eGvqMXU13fPEI/jQN/mufhB6cXPyy73ia7/xypgx/Oxvikl//6iz5sVT7/dxyk9fVvTPe9XhzyN8/828fGy+AGv32vt1UKPw1T9PLHUXzS3X/7gt/+en8aS/GHGdO3V6YfSo33B3gnKLwCAH53eIlP2yn8eIn6sVr0N37L+G3B1D7zbw8zXzmk+Mwh5edvP0KJ6zejJ346Z+HzWf30mfhpHPxTHMTf3v7fPQR++ht++znx40Xzc62+o1d/rpxvHw8/LDzf37E+/+R5bvjc6f1p7/W3Tsq/PTR+k678WIffXnT/lpcQPx7aP5iY4seP+mmKrs9twg/fju9fHH/z7eLvzy78zdOIfzzuHv3U57r69h35X3vm/UOT9XON/cNP+CZ1+dWFE39znryPd/x4XD+3fH8egz/3oH47+vCb07t+HIPfnKrw4wT7+bK+fTb8+PD4vAt+nhXv0/j9TPLzyH2/o/58cXyteUbkcG8Vofu+/6p/D+tiRreOfiJI+wtcJAG1iA5kxZA5buliHq74ZY8dE426Fa280EPklVpVrr4gtcocCnSVC8+Zyr2EMAlRp/91Q2SusucJU27+6h/7qXL/5q42JV0PvwufDiH46DqGSG9w7wmbMC8DT5n/zVbnUYe97d52lYyWFmsOwccLZehVHq0dXgcVwLl+dHShp01+9DsahynbX6wIBeVq+LFnOH5HMlj3zH7jcVQS3junEdTmeD3c7IGwBydd/4Z3yMC6jh28QR/pB+OQ8Xi5HOXy8wDfyq+jpuMvl4c6Tq95xKjPpfXO4eDnsbuCdx2vq/O+sHT9Tw+6eYb9XSk5DbKel3Og1cf+RQ+WoLcLx48T/tuK0uj1gV+NJD3hx986Uj4vWi/W8euMO0TcdSbZZfDTfS9e2p+XO8BnUTVM0kfBiMcyIxS6xtzsvQ3f1bZtZU9g9A87oGeNCZ/iS7wyche2Bj+OUW1/+R+IpnyO1O8L4NOWLfCI6fD39qCP5mm9WBCf9U38KJo/b98fy/H1X/E66H4+rlH6QC+no48q5KPK1C+n2zPQ+K2O+bube01j9GmAG7/9wVd6FF5HR+CzAft4F/xwzH17T+zkHh+fXK+liNfv1UvgqY9eQq+N8Kyld//Ac47Ek1Ojc/LtYqvXe/wU5H5YULzJzI+fC05LubjYp1MtHl3Sd5W8+UbmkfZakvQHenp0WCEVYRuFl2wzgz1zASa6xipjRVmdtAihq0oEOdHbYLJbpRh1R19Cj95sJ/QRCPatO0KRjGCrR0RQfSFv9ByBMe5TePlqrfkYsK47Nu008hafh1d8hvk8sK3e7+aZUkTE5ZnS87jFgQZFwBx0PI6fryXocb0ZAXFifXynntutdyB9Sry3cen3ECr8TAVcVyY+m1MfMkKtbe2gmfWqHuzIdLSjCw3Hm7O2xnm/4R96HciYelIRdX7I+5AZy5nXkbWBYcd9wZZaY5n32gbnwFIGzj/rVec+R+FMFNojmIwx85o/OePBme8RcxAfy6Z6DPges4q1EdU6Ic6rsf95bs2Nj8vechvj4NGB61iL7ffKz6768XQ6v/f1MaAjTTff7Ry2w4593QE6EvfH1emJ5Jmi+xp5CI/Cb63W/W/xifHoHUT/3aX47zvLz/wJ6VVd/dp8xwOe4W1F8Q2keeSFU1Jr//8zTv3YDHwFvcUTGrcQ9eMLaH7VzvXOdOEpOIDfGtlPSOk4PL4tNvQ3MY742dO/bvfH9xufJez+o87N9DZH59D/3h/4aTn4/cZ9spXGxWqJyHzcHh8fZbzisvQ2T/7FsVTfkKT4AT59Fg1Pgtf3nvttAP/pLP7Y1Op45o73lD7Zb2cxu3E5+pcD2XM5aH3s8l5Knzkr4pGgr1wL9fizfsSk7ZpxGASPS++zp/Dd1iB+OKC8IA39OHf1q51+w7EH8A/ZM03fQW5FntHwkdLiY69BjaOl/fyoJnMz+eyzzWqYVG/7W3G8oTMzQhdB0wI03EHb/mbGSOxHgMzJmaxlMkZE1yRn3Dy6w5lHB6IiVG04wpKeK3lXZV7/6V//+a25fIykoz80ZvFSTO/R1PtUuf/1tWM7XoNV8696tIUbDR7Hzmd2SO14Xt/PwIwBA1y+9A94FT0iOkY8pv/9oy85jtF4IWD6MB7eydBu1No1xf1peu3N/ccdgkZhYQk9hqkucSbo9nGH+myeXqly6vGgZXzkgm4/bcPLWe+P/l+f1xJ+QOIfXluvj63fRieK46+DPjkFH+jRsSh+mu9lIH/4cH9CtUdViQPivX9yP6352xfxe8MXn1fLzyFav90mInwe4fmOfYx59IKChiGN7wMI7Y7gnmvxGqm8HrXw0V4fY7PnD/D7A/kGTsTPtfHDM/sDq3uZSX87KvW2D2K83Ov2J7RpSbt2EJPhy88Po1cjiA+bcODl1nxuDWFtmX6DvvWJ7R2bk2+WqZ/Ncb/8oTs+8xLj2Qv8DX/+Zut1XsyTEK2PPv7B5JZV0Hyicd9eQT9HS4+51GfMl/BjGf9dKto7L2Ptt3HQx39IS3s94f52bv89BvPLf639FvqswPAd3pjKsd3Cvdym+Rk4rO/l8vuf9b6937Dut0rosOdtt9K/TXvx29OIX9zNf5liHwfO7VUORj57tj/GF4hvj2LANh9cuU11fe4dXxPb9FqAGvSfs63dGrlFdwWj6uvr3/9mcl0n2IHM69DguuPKrKjoAHJowmFip0iejXb5YGyrpxwE1U386aqVfk3z7T9oPnytAyiA7vqqLyLXMFdjOxIKdXdZQzjWzQMSZMfwRdvJmezoSGS53xyLjHoDONSYQtnhozccKEZpFbk5nuPb4LaZq5NujN3VFFve59yMtBICmeO8rcezoJdWhJHaLvgHSkiqe2y7oyddfKQ7DhI/ci+Pi2Lo4huwluYqdt+nfu2uUSc8wabrNf7YEDFGVD7zMoUQ14aaVUwhBgMlGrnkuj5Em4xDkMMuZ/eT5bxKfT/SVTYiqouC3fE01kVBUGIINjPBML8G6et2NI7VwFOJHyLhyO6RYaBo6nErxScmI/HoykaU0hXqQHbYvHOYROsLdruM9EyPD+4L/0JSxJ8etZXJXirpz3H2bBGopW7ZDM0eBlb7/LE/GR9h6EfIoUaevyRN08faKgwEKdwR5lqG/KIgG6IrLj4/8CB5ZPaAfoSaoSROXtQWZ5b1ZbwduqIzr+5YG5J4jIMRAKot10WEsArBxxtACvT5mYRdDabv0lhJnE9rLmd3qCKujQ2YFpA+Jcx8nKrMYuTQHeZ/jxX8cMK7e+xGQmhL22FOlaCOjia8zVuJ7GH6W/ncPaM+N1cnFA/WTHQ3x6UD4yVj9qVenOOZ1bwrZ24qnyWgbSnGqaSsVyYi2gFB9ruJUPVqqjdEwCxCv4VuIBUACmVC+oeHwaTeBNcDr5/h1KQr7SLE+MXomZAImwOElpRj3vvNEX2RbhwbJG3QCWZDxtSzL3bxk8Iz3ji292EgqOaL/DNEUR4NGo90rpVcrZw5tpGjjy2vnOpK5sPXI5/sUxVsFZWWTwHIIX/GEpg9hx39B0K4R7S1uKyFrt/S9siyEoNN2TrCDxNhG+ZAwhXhqHGrm1A6tkpkpD0iRi8bZUdeHPs2u2DbQ4AtqKsz03odY7MntWTZoGg7DigVLYgdtp6r6OSxj0Im7q6ugcm6O4JMhtp3vZdN0u6Nyry6mmY+4+xwBcbm/1o8+PbVefFfd5Sq0ZI6mCuQUCLunvliHCF6RER8/fXvkAJZZfcoBaKqA6q+o45V49yUQEZbOa8hYXY5sLzHgWuCN6xLn7tOlkzD+jwhpnapyaKitQKJ/ZrDCQcU1ZGXc0gno0tKJsWKOoz9sYgDQiRR9x0RwJ+Eqy3PQRgSMxCsO4LdG/Z4uCyAk1I/MrXHNWj9MMdl0MR4RR/5GE6YU3xEbNuiiuur6HCPIbReLtIoagE6s7vXpO9ReQAIpPXw47sqtX1O9OEyafO7wQAWW6wuP509y+bbZFzRvHX7I0INiEwhzOi21c5Li6MAVVpQcXJXj6L3meDYvqFVeL7CnhtzdsSkXq0O31X4jKB8iyBt2KDuMcofJRPHNIxrnGZlhHWZtJRU0RU1WgynrviTDLP9OCcRUSpbObmk3gRCh7WG095EwhO/WMaJ1STdvWLuCDVd6NC5N6a3tF0NBiy3MUCErK1ThQJ5dVhF17YxAjZmbuASte7xwnOC3h6QE9X8kNdPDfHYrPpL7QKYHT0j0k9zdL+kfOy/7JNxa1x9Yg5fMVAku2zHUAx2QHFHBCNbZZeY7kaaulVv0tPqvOzKVq6etY4WflOnCKCtBahh7/dY1b/KvvRRsUj/8a+kLMfoDus3pB7GCr1bqGCrDCKb1w9FOA9I1c2x5i0dm3G+Z7Avg5ZzALS3+fiiWdfnW98a/mgiYwwLzmkTN0Bfywc3nIru4HyvYPqRoY7qPSdk0O3EiCX3fGgAaSOc+et79x91yhQXsmNxrFHNSHOwrtgAuu62azi5jl5pmybgaNZo5wsypfLVcGWi1eiRRneTfM6xIN200dy7bH35tpb6KLG4ecUVrbDoGXZJHm06EBH3fTM58B5U3bHHoNHOGeR0rFx55y/zecZO12Z+LSHyaHf3lPZChc8hupHaYzDJUHHlrEeoTF5z43Qz/Zc8wnY1SiLE6GqOStz7sjTRWSfeuDcAR66LIn3LT80nZUVBwtfXv43s2zeQSofa7VUS5BW6H/hUsrOe1rvwKEb8jeu+gSDRLUV39brk+fTWSTaTXDfg1hp1uU7gY6fXW9tWVwa6OknFZKAOo2LMtgARW5qOF5UdmXbSNx6sPo9bVyYb5UZhUR9tl09e6u7uRObGiNkFZCZDYFeQqpeb6mp7PKSrKYjhQkkbIGZPuewWOQZyevIsNuSpVZDVz90d2MkMossn0ljz7XkMFASFb7JW2F0gCB5DUrd17KazQU6yC5nGuvrFq1jzaW4IZZ9Z2coj5/RJXvYGqioSjla/Lrp96im2OhipuM8cXQsjj3yXeNRv0wiP2lC44x61L3KilbqEuHyNjwmEfZij+778DVXJq7sJEESj1MrJXafGjOFl7fn4X3WEWpftfNCWyCdObJReZqZjGBHqOJfqUhg3svmSWtHe3ntN+NIan1zNpYQIcUkUMkOjiZjD7pFTej/3ap7tepk5MkybYthLCpMsRF7drajjkTA2526V1mF7J2e0O9BhNOgFfK2D7QJsRiIJr7xjRHwxx0DQ00TLxuyoT8lcI/cCwbW5Zj8u8Rtw7AQT5+Ttjmtf+RLIxRSng1/7jSLyXJC+kJNXRdlIoHtYYCeJyJ9O3Udgp/EPxrxqlY5viPmkSmaom4NEd9lkopX29tiVtoDHi7uAMYwZ6w/pPWeyi9EiZTymVugEXEcJIJPqCrHHLtqg4qYBY/yXQE6R9GAnp+YmqLG6C/qG7B6r5KWY9AFax8J1EsJ6XtnLVR7N4JSkHMACQKr7pCupWzEX2BFIPsNaTvhA2mjksajdYNORnxcCVcVkxvWwL0sdmrzMCUFdtTnaljEZdk1dl26nniK6mjGup4wTjOU6XdWF4FrCByaeS+zoiH7cw0Gya+2+IbiScZ85pVMvmV/rnzHd5BXBTHvLS0UgcxNZN26OTBdj4zpLmyhGd2dmZnYXM3sTxfxqKpr809XARUarrzkAy3Ze7VBPZFWNMUwjPFCo+io7gPYJ4jZeMjwPW9VKCjaCXev9nQ3bTt83SO8rRTsJQ91JVne1z17TKSb82jiML/O679D4Y9zdRJ9Ubj1pMn1uqPNBXUBKwaTrEzd7HLu9RpI9HLwaH9OhsHR3oy6k7U56WEWTAqg4KK7X+LBs+vgr73iUySX2+GKuocgvk6X3YE88ArQFwMc0mhljKf/hzwcgCz1uvYGasIMd/cSjGJ73CqE+Mq8ea/odkrmWZ9O/Q7IdL9pN80A/FUtEe8hyLSa9cHYqNFIa87+6g0yplwU4JSowDp+bSHGsewcoskiEUubVrSUuuWi4ZtYJQqy5V6wzZkQB8aVbUn4O+J9HbXv6SPlS6eYr/nj7rCe8b32R62G84dFoY5uL19TM0Rdg3wp8Ak5oNAOMrHYoFcfMDPIx54Z3YGytUamzcezn5N9Muy4mrNHZpCoMT7Hn+754v4d01n5Na2dnZrWTaV7kgUOkcjGcHRVqxrSY8QAewU7ALmr8CDayHYzwIvGNbSUiNyJGDxX/CYc5eS+HA7eFyHEKhdZQy3mD0yc7Qqsch2IMbMRGWBBbn5wFSbnxCmMupK5EgOxybcfJ0fSNQ4fgjtVlUFOaw7iOwJylgVS0wg5na0U/Cpzprl2eYjMkJFPk+rLVwvi1zxDNL8P/8iSmuSvqbht9ekJEXusHYH7JjPn8GTzcaXsTuTf1NBFjUN1g4SaoZqjCgLKestgVRH21Oxr/u5ZJ4pGBCj+HJxhMQnVkBtdav6MOFhha8pkLRbRtTz3Ep90MoQxCJ3nYhbVruNs3vX3dvOPWY4AEG21KX/lDrqk2IoovkcU7jHQqij4FCV6pxNixKOSQBp3gEygyVEAEshWJr64MrimWj2ibgtkXnVWNRSMSSMendSdOW8QI+6uL61PHI6UTPKaZqRtO+RQtRsSV9lZHZpoDMB6gsBuuOdY2TLqSkMYJi9XhfGmeEKLIcDjFlOZo3dUeFNrEszINBsJASXtwMF0P10hcEzmqvquZjIivuqPdu6js77ru6oM7SR46MBk9buZ1F3Ogp1ar7RPtvmEpoXZgAboL0eNfAhcNfQLyGFR2KFRjCx+thC37clNG92BjMNDlpkezhRV//vyRv5TxarqypON1SahCm6AEQbYSdiNCErxdWJl3RHR7prdm6TEePqGxTJKaZD32HQuJyAnTnsqhY/MD1Mw0lmRN69ihW3TrltGGwTCQSXceM+AjHoumEMRQNJWZXT3MAKwrPgDZBs9FoUq4eLnyfa4HF1cyrtXRYJ60+yFMEMm5NDJc5anBxLi1Tf07yShrsMM1ghksKaQObt7n5E3YjNgM7XXe3Q5fkaCbj3mScuGJDoPzBLpsrWTsPUiBc18qBvzCQYannILXuYN5h7ZpPyZ4wUcfk7CRSl0mZhpanxugEWoyxejulm2kccbJmFCYcejV4LuKrmQCrK5XVlaHgkxvthysxSPqA7rO5jqckrcAlEg/Ac303EMXVnckuhqBtayhr/R39MxgFbbrJyc+tknQOQzYwshsnq7aqzC6e9yHQl0DhU0PsE2zL6NQxMzTwotjBgmqTXLKzRY6/3HGMIgkcK+ferNDRZhBk+Ooq/08TJdaq3ZtTGBw9OQ3C0763ft7ucMuiDkIezcjWzfyKSKv/FNVTbsKN2k4pHtTSzhf9YyIXQU6uYNr/9ZETgiTsRyyowmqhbQhZgHouAOJSKNuEyuNSv7pCdoLlLnNeuVvzYhlehm1opJZfSSKVlfVtFfz7JQca9aIUNdewuu0bDSHUMsufA9i6saxFTnMFI8aTHTReBi2opPXHT1SvQ6PHB061ZMo5KJZ/jzaNITHda4Vb2rIIMOzeLjADxdA9d1hU0AX30GglDwTHIeD677rylyBREeAziPaaWm0hXxjcCl5MLQ1NLkjy83Y9rNNElHd3vsgiewujALCmFO6lzEBL/Ma/gXoQC6SGmIjllvTeV0MqJblldl9c3wD10nY3/5LX6HoGmPIKAEhBztFniClo6ipqsnQtH4jVdXW33f0FVdLHSWpqhJU2cBq8NHuVgdIVYvqrrEIFofSb64D0SHKkNqiYZNVc7ua9JVWXR7pIkikIhRlBprXBIbUGIwUolWJlZYY0ZdaFQMgG9RyHaEEzbcyDnim3X24Ok/MeyUvjaav24dLE6HxI2S07/WRnx4a4LA26yRcZmw2X9Q02TPB4C51RHw1iAlPmDnZO/S5hTf3fWj86Bpg/MHVV/2rp8UxsbHJjaHkDGbb7MqRANWexToi0ibUncqOFprCk8a6Z4YAVc+F8wrm7mGF2Ti1D/7fEwoHgziPSTQ3smSle8dz4hUy69MIq98y4DKE8ANNDPL1os1rZGO1QI35wypTvIem0IrOyAXu5zo37m69aPY4TerytQn7P0K8VSDh21Alm4Xb1c22yMJHWuCjS4zuVndmTjH3SsFUBJgW2syYaBDWabxz6WPP1/dRhwCjjznuaoNOLWun3dzySoq02hkdyAGrW7BfpOMCYOtkkDNuiDHWhJtjEwjPLhiDyaOydeH4BBtOKazQhJnlwP5jBuMb4sPXY8kl1cPtHYyBrb4yq7+AkOqadN/YCsAiEBAMySwxwhl2Ati1BieTCqaXkeumMjAR5pnGiZqzyewUgkS7M79vjf3HpHlNvT71x/Wy4W95/N9acNwDUz4ayTzeFOyVOG1eY8oph2SfcjMsVh8Zkrl8ACIHnIsXK1Dt8FPWSNzci0yh7PvVRBYGwpcFFBGZl++FDQB5XlJXr75r4xtHBGinZ2f6GHFhGqDliKeE7rrJP6bEXUnVMDA6YooGyXCItj/RhleFpMzYlFnVGkcGKPpfrs/KDWSMCdDi/BJ3+DI8LUXFreM/QDhrKTFZmu5QGRlDZXfa56BfppItWwJgRqujkiaJeaA64+iIIC4nAXEwIeCIlman5lGbJNP72tvZrbUflGsjugaz45OK8Uzhoofnga/6ayDZ1nb102hSiT/5VRVVIDsaYN19EVJVdYIGMOoc1poWTX2re2aZtfazbZhiBQjOJJsGYGrnqnKaQ6l9ySEvzviN4+wtMbM3uapVjixKZ/IiILdfooODTaDr40wvkNF40udIDcN/YAMByCv8RSKuaXcC4F1fh7WL/T8nlu6LaqNxx8fJ00oQAfRdLhEDTtXSBnN4Kp7VXwFcZHfPyeKerG+bzNscu8vxZ6MYUjcvg4YhoLqtkzF8VipkGn7o6gHqSYTzjFz0mPyKyx3PHNqjt+510HU9zkhzpsx9ewcxn9DP8fbF8q8iWsrVEwyuNE7KQEd1raM6wKEiu4XiYabLsXL1CJOhCQnZORrHUDatXRkOxIKBM/k7D/xwVKZkzAizOixr8rFRZ4rYag64NPtlU6B8L/Y+Ck5a6aaA9Nh9GXspDrb+/KTwQLADgVJlkmE+P/TDFm/wqrd7uB/UKuy6a9Lh5kdio8kC472vhwKZ5DuKfQYcel+i5nokN/Vv3XtHjD9sgF6nC9gH/0O9P/laRidwOFLH0ZmceaI815rf+VAyzQyLTT8oL4MGAtXNHAomh/rHXneHtwPESB724txMsiYYQ1ptBKpaJFZdxRmTWZHjMiaHKrWhfdU3X5m9R+1Y0XnA8B2vIiIzrf6QQzWmTilYrmfdx4SIXeOXJWXEHR280CEVY+ezxq168dQhL2Qwu4qPydkZkfjkjMzsBZwkZV5u7tw9E7HzESxXzCk4I11XW9Jl56BZlt0NYUhFMw8fEuWR6W2wiHOoFV3MfMjkGhN4DcS2xEQekXhvbtvwurwaiaX44MRw+w1ygG/M9NBMBYBznnvQ1iOiAMnIiG6nsa4xIIm747H9II5CdOa5xtgWLiHgUGtOE6CWwOvpASZcUCQ4aekzWwo5GLQVIsFRk4BHjUu4fPRcftAGz8QSz/cIdlcyvUc69iPRZGB3pO2QZuLSLS+qtVhfjz/3qF/112yU9iUlT0E892jbHnR3iBfqtktJL3+qEbqrI3NyObqnYOubo7Vj9wSVRBcZd7f4BKyh5VK0qjdsE+2/JJGpWsrjreu6Ko7IJvZnDEQcq+GBslUOSk5mxe20CKbR+9MnNsJ9jQVXXy6TM4C8+jUHDXmURZKqjj1dhhBnjgyz1ei4Ere61CY7n4GAD4sEppKOyWfrFoR97DE57EFVgwebo+ILyggUFIGL2X2f+cCie5ER91YbA3JG3H2DSQfWRdd9kzxbV3MajH6QSDNpndsDseLegOSYKEg54IongsDU5Xkfq3ma0G0+yjE65U9S+v4YM5AYAmN3KXmNL4d5y1VSJLGE4eUh9kYF7TUVEVV1WZQVgbx031jNxibkTtMZo7CZjzpIeJvNN1Bjzvzj9pFwVyVpjVb36IEZuZWDokWmJqwZUpPZCqiTCPHWbZEuFJE82opTaZm624cdPRjd8HjSnCVF5p8YeY6TeZX5xzT17vHykuri1Y/XbIax3Me3btCyUTBu7OmC4RYXWAg980urvLtv2L0s0FYZbILA8PvpGfNoGRKXBp0x4CzLNHjlXdYWoqFkZGR1dXQYCBy6BtcIiy1ZPeKI+Vu30195OVWn9rn5jfMReryKhrlCYCP8Kegzciifhu0cHGydKqOrL8I9u6kd90m3H6JERqigK9PjcU3u5OYRvygkIJdYd9I8GkBVRwvM5W04M3NeR55EOw82Fdc12U4tffMT2BNgUwbQQ7kdstHyFaYpkKlR13VJE7iskPr+8+dfFutGqGr2sh/aUXdIInXfzbw4Z+tzp5j46dnTsCsfzaTTxp1CaRaw6i7klURU9RPyMnM6j8L9UuqrL16mfk4MWo8ZYnX5A3Cm/pSi+ya5zmJHGlaY3qg19EB1VOYFUVFBVBUxAc3A8AYimrm2Qz19R/QqoTCqZ88dJmupyy/hz5//1B3q2zIBB8stOOd6jKvyi+skHDWu63ILRuDuyusaHSlnwatWyps4R2ggwz2k7QoMcpF13wEg2QraoimakRODOblLvhzaOemS8G/9Bfno7xNm5sqiW8O6P5ut+5oomwJ53/djio/yPeJz57EnM2eyS910Z49GRFkVN39BEV1VSURktbNAj8irpRmLbCzKSZDSMoQbSjWR2PZ9HBakUXlMUEoFcWmk1GKE0vAmI1DRqkaAF6eEDc1YwRF55KqlTX3sipI6kMmMVnXx+Cs2eMXcjUeQftKFNUyW0Eb06iYutwGbqtJtTs0IdRjA28oOp3Z6bAwwFW76WazIcwD2aOkukWnWSoRnRIsW0CItjjgQrHaWoWDC/1DrsHoH+4f0gDhcUZt6zalhjKuCYfRohs0+UvWitQ/Bi7DTQLQam/ybq75yUvnJyEGf0qhb1fJXS/c6wGMIMTl3ux+Wi1kzPl5MIkysYYIZfcvy2jkc7SLnKcnodMzNeZFGhxsOoGHTCs11o0mCCE7UUfWRMNTcsgOtPPPUbQ6FFJE+Pfaxm3xaZuHZ7KU9NQhBcTF7Q5rDAz9bTQ5LMY4WbirHrj3KZ+aRvHq57T3Ro56FR0YqUHG78BPUaBYZENdo1XYsSFMHk9hKP9J+cbZFMrMRuMjqKif+WKjqSNf14Bke8EHUzyGD+cqLF6UDzCzP8yJgpCaheiqwAzKrG2GXiDHM+WHOeJiyg2+fpXtyy+auABXsvrU4WuCQNu5jlOWITsZlC8Dl0+hwl+PD/INm+85dxx2LAE5ZnT244cGuSEyqn6WeOn4yT8S8aw2PYCiHq/lUWzAVmSlE193VGcFkOMNzwSo39ESXQ10nL1npMYKV/mgj6HakGw/fJUWBnFe8ZJQhMHStNCbi2LGsX5WTERgJmCREAqoyoDt3mQhwMhDUvindZbUanoxtlFrrK6TEHylcNBgvNDH/yn/d+jrH0cxVdwI+1eFJsVo1bfUdRFr1UIpq0is+ias8R/FJNKZi8PTwuq5SqyzQnaOeikwj2g6EYtKOjRGpmX66OqwhF/JydBhIMGEzCpP2Sd73DYB/rrvqKKu5dbepEhjeA3HEF/+uv2wT5FHrkPK6r2QEuu6gGH/u+4uO0uzGDDdg7HQ4X90aWkmcnm8RgYO/6xCvjoLQxIWloRhblqSelESFxEwbOXgcXR0MbrU+7jcMRmc7IdTY9zn4MquLU2hzTLRnay7yqszMSa7bRErJwK3ho6i6M6+QSxMjJuZRhERYMOa7sG+j5/zDtffUwpLNvFTzGnCm7xIoxKWjC5/BVU8lqw6gBIUsGFa5+utXgvamZIwMeSb6M1U92GlrrWHbgmCN8I8+eenmBnoUSvY2IhpBgVKNQsxvRyfA86Cfp4KJdsbuFRvpPUOqDUj1ZUw8DrI9rPudl031gSUHxrFaiKEDDzdTzz0Up4EfJwMoeXXVXMQd4NvOslcyPwdCryB9SJRH3IhgZrcxWlyBrooZ5Y7vkCl4HfQM2IyZBMrubN1Acv0YfBHW3cw8JjtzEvni7QbANPiaG5G+c0uVO+YZAQwXax24ONDp5OUZUiKlN2rz+BAYFFXUjjt8aBnDr/VUtMdLKtCMVnEItTJlx7gLVgZDXi0PebQkibKeRjhTiyLtNilR0UjHKioiwhm8GJvWnkUQS+bFCpfvHuamEoSo1m2627gccPMoX7Gf1pJgYu1Pki4XbNNHBgQjRQB31NQK2BR1202BQ8MY8QXNWTTbI55dFXrsFr3vADJG8oiB4YZgQuw8oCO6b2ewH5zsGQ9ZVHlGPl2TV+A/M++9puJ0tefWg9aMxl03Z5g4P81irlZhQH4EILD7ya8PzKpcNOTtsHykH+xoZFo4V31vHygzXbzjHaSUHF+8xzDUUvMlpjhLxzDVftozJgvbkKzxTGb6tgqQdReQYBB/VHeHFiGdk2uZ9msP0XXBcxOobz+TGXZUndRga/OGqo0+hiiu3Ga1lZKsuAlCf3oDZ1ydmX7h5WJAZXtMay6C8xV15SXNGCS6Mc4fY2uMgRUjAnlR0arAxQgxswQKdmAzoVJEdWeyPKyZ0NRjY6qlEq/lyb+7FiO2AcNQbGx7YuyANU4Jlo4HChMJL3tJAgCvqu6H2/c4osQhfbfKeoEJOGoSca+nwDDQu6NUDNjhQ04lSPLuv5hZKoV71zxy87lfI88pb2H9sgItkingYmDEQm2jDMOtq7wZxwIY2OxxBPLRVzPB9IUUt9HLSEUQyhefJQwUJ1Rxr+Tq+PYUxGGl4jigmaAwcGKPA9hCid4qiHsGPbVUijEoyTHZs25FpSZyjO+mVbJN0DF8k3NL7DcORI2GwTLloDKACkGFiJqj25lj7nJ8GWhDViAvxFSvnm+s2B7/pRzbhb6lzRRp0Qof8cxTjDaYgPYyn/GBOYQhDzJNiMnM1gHTesPDX/6M47N5MzMUQ42emULwOMjYevJtY/zoVm1lNrW8m5huReBPeqAQ+gia6VUMjboOFopEVVSS0YZwQPYri8SD80RpVb629OiISLRFKGskH3b1qunk3mrehQz9gXv4WUEBD3I+wIl00qLbFBEPSaRr2OvLnVHBIK0ZBi6Qiere8bFJv64Vb84yvzSBukMG2oh1Nw5XnFVqRafuvHzMXhgYjOAcPeGDGxyiy3BsrGnGyYTXBq6PS+aGx/uK2ryBBrDUo2OWrAlOGBtLnKyQabg4+9Y2kGPUQ2xwfaV5O0B1RwWSHbWZ8HQX6E1hE9/BpcYw1NiAlds2KqDa29nBuRRZ+suo9VMWKwO1Gd2h4EyLjLlOP+ZHbddLQpxp8hY1RhFmqlL3WALMuKZOKASUM+EXoKUh9eqJujM95uSoBDEim6Oxdfnf8a7GNCabkx3GNYFgRCMh3YvrBOIaawmtWv7kM/hTBar6eCI+MHsUEqoYPZ7SjCWMy8r7xNg4NyeaVwxaqLXSEqwfdJtaxqcHf5XQGVTHQTv9YzIyVFJf159otu6KJq4YvmcjmmZljp+OZsGgmYEGk7UhlmZW2MS203wQcS77fNIJgAn1iQISoKMG1UXSiv5GKMqINHlBGFrY3QZRbLxkOR6++mv0XA6/bnfxuLsSOWHxTQJlC5Tu1g3irpo5jYGkNS5JsurG/HguT4JtGcW6R09vq0hc7qG7e6jUQzmCMqzIWl/aQNIaaWMbJif3bc8fO3q+vOyhYcwNcWnypdy1tqPAwphEhBcW/PrXCRdpRnem/9O9bSAU6jajRy1d+KNVlI+Ae/xy++4bSOKPCe3RYiBJ5FXdlotwnJraT8NV1AIDPXcp9HX30BumvaCdVmksYSYXPlxH9zGWHL6tq0dk6FH9FCbjxG4WMAasp0L3ZGOG7CAz8zahRbvWrv9cv8O1bHHvsu3FYsfLWsRnmOW1l3lYHZnmImh0EctjfQjGlqR72qoehHnuP8s9poAOGLeMryqr2AbIGIVAtIrr/ZVgABbX53C4DJm2h/O+5fhkxvUuKZT0Z8yrTcoRkBPJgUd7HaCZCdymMzn6NGkUy3U8eg1+CkBUDYrefV/IsTdanZhCF7J0xzoirAEZ7OWrtpBvTo5W+cOfIcV68cajktBC/b5XR1Vkd+eW0bWAhjnoExhr28G0eqg7onLU/APSz3yXiUTVPQj2dvCtzisd4WfRneuTquaIOCLda17s8jUpmbzQvQll/iSGN+nZ1WQZ6GQUvRKMMCl6yzgeAGBcPcYxkEcWYUSVTAZKJVtGpu14r82eCCoF1X3/4Z/2kBlHtSOXfcRaTnu0Dn5VzRp9x7++oxUQ0ZHMUfqokqnRyV/01eUiFVqeSi+SF6PF1TCliJz/vJaRuw6N1nUmA/QPcCOQzPu+sT4f5kH/IRW82zdZF3qttCz/UmIalnpmYcKTFstAdN1TUiSsmcfYGnDRlDHjV0RX/MmcQbIN3SK6Iwkb0iuiatKZritjS2SFXYa451HGULBkkUGVXUwKRuzsT2PNwwjRn9Ciy90ml1k6BnHo/go0RCK1wxkkRLE53re8oLir7DFF8MaUXVw9VKY1UMuWm7ghrF+Rfbo6I8gs2atQg6kHzN6wW5KmSBsuQ9JcV7dbM9furswEBI33xURU6G3/Wt3CXX+B7A6S96zaDtcGXtRMNOdGRETXaC9iuDla841W26FTqzRos+tP7lkVGdSmmLRCU193taLN5/WiZI7WHL05xD0JGBNVuFNxO/TWK5x0rhNb0GAoh6+U3uthBo2M1naSezhqPNtOUPLO77VxSh8RR95y14xLPHDvLicjdKPRJJ1qoVx7XeafDvX9lZkR7LhNuTg9LkY8o+7ORFvbah2QJbiCpxKjTcWON0+kzfgrzD2SCgi3box/9h3ERqnZjSNjvD+iNw6ZS54Pm7RDUUpGbQBrq0FVGxddiqL7m3U42Ewyeo/32hrgiUOwo+qtBWD8N+jaYggHOGa7Q7CyMlcPVx1Y+bwOP6fjI06Gzriz3b/dVoRoxqS4eE45qD5foWiv+TdxAe4mp380QRHRAZmwCt5e4qsqpZGqidIarZvN0Ho7NYv3aqbDPfV6KKDsFNXRVCKiol7cvvtQTJaUnfur90J8MrF7bK1ndpOPHdarTzeyIAgZqphgPBm0aiCiguQmsiCQU9Ul1D2lHvIAjtpkZ+cQ22ihzde2sDBNkrVaUWPXDY6ZDgIS7UkkoT0CKMheNTnHCqKm06Ct2Yek3FZL9nfpU3j80XwzheCxydW2+G0RYdHMcnuz6mtC4ziDLJKlmxjZTmrCGtqWvvNFI9Z5065GwVohtyxW/BaX/OQVmossc/cIL4C1BRzzXNM/0RukFUvCtmbqhL4bvZr8YYCD2E1KQxzzFHISPlyIv3px80wV1deff3VHqIgYr96W2cLYgmuL/n2TtDNOXLiwr7fVSkDTtET0/EZ7tajjYofYg0Ms489sBrUKQ/m3mr8rSCifEfDt2Y3WIoVIT0nGEmDNrXvdJnCGd8lXelsHxCbJitrMXtoCqLvNLRHiCkvwBAYydI95GjOPudemCg1PiYYF0EliWDgLDU4xjar6c11L34FauTRvX9xTDeTYCjhywUZLTMytiDg19DmIIuIPLwrVHdHMyySE42UXEfjr/r+Aa4lgqgoTtIciEkKSPXWCQZ77LuTD0tpk40PHs6oC5t+PfaxmzUWbVxgThaWpks7tO34M80BPXN1hO81KbM21MWXeTNEUx7186W9A9PT9gz1TLKniZubYIVhYnDl4Z7W9vi2u6DHPX35+f8Xm35hg/vCQ45DIh4bU55Y7QTddbwfic0+0ypyi2NgFd94vEtbc/i+XhYBF5KHuvq5/jWzBmtg9Fs0tSXCczDGcu6DaTjJLWZyKLKAYeqARlTUJtvoqFJU5WlZOem2/zCzDT9yV13LX4YiNSAC8J+TosaPuucMK2AJron8RQo4Ur/HqwgaGmTtv0p78FmzhshktT3RvHJ8xv4gtVtpBTBFRBpwCx14al3lvVs9oF2bulCKRFr+Fxb1kx4jmar7jvEfqicnQUlM3lLOpXQkz5h6C/ZmTuBRzIcngX30PNmOC7ZQDfTQK+/brxHM8CViWBM/1JgC6rTUakto+JU060mn13hHM45a4AlO3RabErlVF73h+vvghVzC6NI8a6LLQ32IQHcnDFNDNYZj47vT1u7jIZk8USSsaJ4QWQ7WG244xctCxC8eLQ4Mz+9jn9sK01+DC8OWqeI9LuIYdJGA91IZKZVXkZCOpZyDCQNUGf7wI4y48Nwr5d4LntP6cQADTmIfft5f0RPZkHnXZdruPrMnfbkrVvMzctHS7MHlTM9zgw6b0NeHmwcy91f2OF7EQX4YAXb1zE7znaszuOlPIW7aXTnKUjvio2hRAgq2u6rRpzM5ko13/jcAx1q7k0bPE5CWOdMvVkQkfM72OK/+oeygIzyASAZSaiMev4zjw+pqE1HpktAvXHW7Q+3ifjKExsZuIlrwuMKtuF7JPiSiIEwI+QL4hyVfF4N+a4DZVjwjrHVY+W95yCeJTGjkyFq3AyRyXzHVwirGx4njRuvuxcFL/D0cu8a5M5SeVAAAAAElFTkSuQmCC")), unsafe_allow_html=True)

    ltab, rtab, ptab = st.tabs(["🔐  Login", "🏢  Register Firm", "💳  Payment / Renewal"])

    with ltab:
        c1, c2, c3 = st.columns([1.2, 2.8, 1.2])
        with c2:
            st.markdown('<div style="height:3px"></div>', unsafe_allow_html=True)
            st.text_input("Login ID", key="_lu", placeholder="Enter your login ID")
            st.text_input("Password", key="_lp", type="password", placeholder="Enter your password")
            st.button("🔒  Sign In", on_click=_do_login, type="primary", use_container_width=True)
            if st.session_state._login_error:
                st.error(st.session_state._login_error)

    with rtab:
        st.markdown("### New Firm Registration")
        st.caption("Create your firm registration request and receive your registration key.")
        a,b = st.columns(2)
        a.text_input("Firm Name ✱", key="_rfname")
        b.text_input("Owner Username ✱", key="_rowner")
        a.text_input("Password ✱", key="_rpass", type="password")
        b.text_input("Mobile No. ✱", key="_rmobile", max_chars=15)
        a.text_input("Email", key="_remail")
        b.text_input("Firm Address", key="_raddress")
        st.button("📝  Generate Registration Request", on_click=_do_register, type="primary", use_container_width=True)
        if st.session_state.get("_reg_error"):
            st.error(st.session_state._reg_error)
        if st.session_state.get("_reg_result"):
            rr = st.session_state._reg_result
            st.success(st.session_state.get("_reg_success", "Registration submitted."))
            st.code(rr["key"], language=None)
            st.warning("Copy this registration key. Its validity is controlled by the Super User. Trial starts only after authentication.")

    with ptab:
        st.markdown("### Payment / Renewal")
        st.caption("Select your package and submit payment details. Activation occurs only after the credited amount is verified.")
        username = st.text_input("Firm Owner / Username", key="_renew_user")
        package_catalog = _package_catalog()
        labels = list(package_catalog.keys())
        pkg = st.selectbox("Package", labels, format_func=lambda x: f"{package_catalog[x]['label']} — ₹{package_catalog[x]['price']:,.2f}", key="_renew_pkg")
        base_included = int((_load_registry().get("firms", {}).get(_find_firm_for_user(st.session_state.get("_renew_user",""))[0], {}).get("included_users", _system_policy()["included_users"]) if _find_firm_for_user(st.session_state.get("_renew_user",""))[0] else _system_policy()["included_users"]) or _system_policy()["included_users"])
        extra = st.number_input(f"Extra Users beyond included {base_included}", min_value=0, max_value=100, value=0, step=1, key="_renew_extra")
        amount = package_catalog[pkg]["price"] + extra * _extra_user_price()
        st.metric("Required Exact Amount", f"₹{amount:,.2f}")
        paydate = st.date_input("Payment Date", value=date.today(), key="_renew_date")
        paymode = st.selectbox("Mode of Payment", ["UPI","NEFT","RTGS","IMPS","Bank Transfer","Cheque","Cash","Card","Other"], key="_renew_mode")
        payref = st.text_input("Payment Reference / UTR", key="_renew_ref")
        if st.button("💳  Submit Payment Request", type="primary", use_container_width=True):
            if float(package_catalog[pkg]["price"]) <= 0:
                st.error("Package pricing is not configured yet. Please contact the Super User.")
                st.stop()
            fid, firm = _find_firm_for_user(username)
            if not fid:
                st.error("Firm/user not found.")
            else:
                reg = _load_registry(); reqs = reg.get("payment_requests",{}) or {}
                rid = f"PAY-{secrets.token_hex(6).upper()}"
                reqs[rid] = {"request_id":rid,"firm_id":fid,"username":username,"package":pkg,"months":package_catalog[pkg]["months"],"extra_users":int(extra),"required_amount":amount,"payment_reference":payref.strip(),"status":"PENDING","created_at":_now_iso()}
                reqs[rid]["payment_mode"] = paymode
                reqs[rid]["payment_date"] = str(paydate)
                reg["payment_requests"] = reqs; _save_registry(reg)
                _notify_admin("Payment Request", f"Firm: {firm.get('firm_name')}\\nFirm ID: {fid}\\nPackage: {package_catalog[pkg]['label']}\\nRequired: ₹{amount:,.2f}\\nMode: {paymode}\\nReference: {payref}", firm.get("mobile", ""))
                st.success("Payment request recorded. Access will start only after the credited amount is verified against the selected package.")
    st.stop()

# ─── SUPER USER CONSOLE (completely separate from firm utility) ───────────────
if st.session_state.get("_auth_role") == "SUPERUSER":
    st.markdown("## 👑 Super User Control Center")
    st.caption("Global administration only. Firm users never enter this console.")
    a,b,c=st.columns(3)
    reg=_load_registry(); firms=reg.get("firms",{}) or {}; registrations=reg.get("registrations",{}) or {}
    a.metric("Firms", len(firms)); b.metric("Pending Registrations", sum(1 for x in registrations.values() if x.get("status")=="PENDING")); c.metric("Payment Requests", len(reg.get("payment_requests",{}) or {}))
    st.markdown("---")
    at1,at2,at3,at4,at5,at6,at7 = st.tabs(["📝 Registrations","🏢 Firms","💰 Payments","📒 Party Ledger","💵 Pricing","🧾 Logs","⚙️ System Control"])
    with at1:
        for rid, req in list(registrations.items())[::-1]:
            if req.get("status") not in ("PENDING",):
                continue
            with st.container(border=True):
                st.write(f"**{req.get('firm_name')}** — {req.get('owner_username')} — {req.get('mobile')}")
                st.caption(f"Request: {rid} | Registration Key: {req.get('registration_key')} | Expires: {req.get('expires_at')} | IP: {req.get('registration_ip')}")
                entered_key = st.text_input("Enter 12-character Registration Key to authenticate", key=f"regkey_{rid}", max_chars=12)
                if st.button("✅ Authenticate & Start 3-Day Trial", key=f"approve_{rid}"):
                    if _hash_secret(entered_key.strip()) != req.get("registration_key_hash"):
                        st.error("❌ Registration Key mismatch. Trial has NOT been activated.")
                    else:
                        ok,msg=_approve_registration(rid); (st.success(msg) if ok else st.error(msg)); st.rerun()
    with at2:
        for fid, firm in firms.items():
            firm,_=_refresh_subscription_status(fid)
            with st.container(border=True):
                st.write(f"**{firm.get('firm_name')}** (`{fid}`)")
                st.caption(f"Owner: {firm.get('owner_username')} | Status: {firm.get('subscription_status')} | Trial: {firm.get('trial_end','—')} | Subscription: {firm.get('subscription_end','—')} | Users: {int(firm.get('included_users', _system_policy()['included_users']) or _system_policy()['included_users']) + int(firm.get('extra_users',0) or 0)}")
                if fid != "XYZ":
                    nk=st.text_input("New 24-character activation key (optional)", value=firm.get("activation_key", ""), key=f"ak_{fid}")
                    if st.button("🔑 Regenerate 24-char Key", key=f"regen_{fid}"):
                        firm["activation_key"]=_generate_key(24); firm["activation_key_hash"]=_hash_secret(firm["activation_key"]); reg["firms"][fid]=firm; _save_registry(reg); st.success("New activation key generated."); st.rerun()
                if fid != "XYZ":
                    with st.expander("👤 Manage Firm Users", expanded=False):
                        fu = _all_firm_users(fid)
                        for _uname, _ud in fu.items():
                            uc1,uc2,uc3=st.columns([2.5,1.5,1])
                            uc1.write(f"{_uname} · {_ud.get('role','User')} · {'ACTIVE' if _ud.get('active',True) else 'DISABLED'}")
                            if uc2.button("Enable/Disable", key=f"toggle_{fid}_{_uname}"):
                                _ud["active"] = not bool(_ud.get("active", True)); fu[_uname]=_ud; _save_firm_users(fid,fu); _audit("USER_STATUS_CHANGED",fid,st.session_state.get("_logged_user",""),{"user":_uname,"active":_ud["active"]}); st.rerun()
                            if uc3.button("Reset Key", key=f"ukey_{fid}_{_uname}"):
                                _ud["user_key"]=_generate_key(12); _ud["user_key_hash"]=_hash_secret(_ud["user_key"]); fu[_uname]=_ud; _save_firm_users(fid,fu); st.success(f"New 12-char User Key for {_uname}: {_ud['user_key']}")
    with at3:
        for rid, req in list((reg.get("payment_requests",{}) or {}).items())[::-1]:
            with st.container(border=True):
                st.write(f"**{rid}** — {req.get('firm_id')} — {req.get('package')} — Required ₹{float(req.get('required_amount',0)):,.2f}")
                st.caption(f"Payment Date: {req.get('payment_date','—')} | Mode: {req.get('payment_mode','—')} | Reference: {req.get('payment_reference','—')} | Status: {req.get('status')} | Extra users: {req.get('extra_users',0)}")
                credited=st.number_input("Credited Amount", min_value=0.0, value=float(req.get("credited_amount",0) or 0), key=f"cred_{rid}")
                if st.button("🏦 Verify Credited Amount & Activate", key=f"verify_{rid}"):
                    required=float(req.get("required_amount",0) or 0)
                    if credited + 1e-9 < required:
                        req["status"]="REJECTED_SHORT_PAYMENT"; req["credited_amount"]=credited; reg["payment_requests"][rid]=req; _save_registry(reg); st.error(f"Short payment. Required ₹{required:,.2f}; credited ₹{credited:,.2f}. Access NOT activated.")
                    else:
                        fid=req["firm_id"]; firm=reg["firms"][fid]
                        try:
                            start_dt = datetime.combine(date.fromisoformat(str(req.get("payment_date", date.today()))), datetime.min.time())
                        except Exception:
                            start_dt = datetime.now()
                        if start_dt > datetime.now():
                            st.error("Payment Date cannot be in the future."); st.stop()
                        end_dt=_subscription_end(start_dt, int(req["months"]))
                        firm["subscription_status"]="ACTIVE"; firm["subscription_start"]=start_dt.isoformat(timespec="seconds"); firm["subscription_end"]=end_dt.isoformat(timespec="seconds"); firm["extra_users"]=int(req.get("extra_users",0)); firm["activation_key"]=_generate_key(24); firm["activation_key_hash"]=_hash_secret(firm["activation_key"]); reg["firms"][fid]=firm
                        req["status"]="PAID_ACTIVATED"; req["credited_amount"]=credited; req["verified_at"]=_now_iso(); reg["payment_requests"][rid]=req
                        led=reg.get("party_ledger",[]) or []; led.append({"date":_now_iso(),"firm_id":fid,"party":firm.get("firm_name"),"type":"CREDIT","amount":credited,"mode":req.get("payment_mode","Verified Payment"),"reference":req.get("payment_reference",""),"package":req.get("package")}); reg["party_ledger"]=led[-5000:]; _save_registry(reg)
                        _notify_admin("Subscription Activated", f"Firm {firm.get('firm_name')} activated until {end_dt}. Amount ₹{credited:,.2f}.", firm.get("mobile", ""))
                        _notify_firm(firm, "CCI Subscription Activated", f"Payment verified: ₹{credited:,.2f}.\nPackage: {req.get('package')}\nValid from: {start_dt.strftime('%d-%m-%Y %H:%M:%S')}\nValid till: {end_dt.strftime('%d-%m-%Y %H:%M:%S')}\n24-character activation key: {firm.get('activation_key')}")
                        st.success(f"Activated until {end_dt.strftime('%d-%m-%Y %H:%M:%S')}"); st.rerun()
    with at4:
        st.markdown("### Party Account Ledger — Subscription Receipts")
        ledger=pd.DataFrame(reg.get("party_ledger",[]) or [])
        st.dataframe(ledger, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download Party Ledger CSV", ledger.to_csv(index=False).encode("utf-8"), "party_account_ledger.csv", "text/csv")
    with at5:
        st.markdown("### Package Pricing")
        pricing=reg.get("pricing",{}) or {}
        p1,p2,p3,p4=st.columns(4)
        v4=p1.number_input("4 Months", min_value=0.0, value=float(pricing.get("4_MONTHS",0) or 0), key="price4")
        v6=p2.number_input("6 Months", min_value=0.0, value=float(pricing.get("6_MONTHS",0) or 0), key="price6")
        vy=p3.number_input("1 Year", min_value=0.0, value=float(pricing.get("1_YEAR",0) or 0), key="pricey")
        vx=p4.number_input("Extra User", min_value=0.0, value=float(pricing.get("EXTRA_USER",0) or 0), key="pricex")
        if st.button("💾 Save Pricing", type="primary"):
            reg["pricing"]={"4_MONTHS":v4,"6_MONTHS":v6,"1_YEAR":vy,"EXTRA_USER":vx}; _save_registry(reg); st.success("Pricing saved."); st.rerun()
    with at6:
        st.markdown("### Audit Logs")
        logs=pd.DataFrame(reg.get("audit_logs",[]) or [])
        st.dataframe(logs.tail(500), use_container_width=True, hide_index=True)
        st.markdown("### Notifications")
        notes=pd.DataFrame(reg.get("notifications",[]) or [])
        st.dataframe(notes.tail(200), use_container_width=True, hide_index=True)
    with at7:
        st.markdown("### ⚙️ System Control — Frontend Administration")
        st.info(
            "All day-to-day administration is controlled from this screen. "
            "No per-firm Firebase/GitHub editing is required."
        )

        settings = _load_system_settings()
        s1, s2 = st.columns(2)
        new_sid = s1.text_input("Super User ID", value=str(settings.get("superuser_id", "superuser")), key="sys_sid")
        new_spw = s2.text_input("New Super User Password", type="password", key="sys_spw",
                                 placeholder="Leave blank to keep current password")

        s3, s4, s5 = st.columns(3)
        reg_hours = s3.number_input(
            "Registration Key Validity (Hours)", min_value=1, max_value=168,
            value=int(settings.get("registration_key_hours", 2)), step=1, key="sys_reg_hours"
        )
        trial_days = s4.number_input(
            "Free Trial (Days)", min_value=1, max_value=90,
            value=int(settings.get("trial_days", 3)), step=1, key="sys_trial_days"
        )
        included_users = s5.number_input(
            "Users Included in Base Package", min_value=1, max_value=100,
            value=int(settings.get("included_users", 3)), step=1, key="sys_included_users"
        )

        st.markdown("#### Notification / Operations")
        st.caption(
            "Email/SMS/payment-gateway credentials remain infrastructure secrets; "
            "firm registration, pricing, subscription, users, rights, keys, payments and logs "
            "are managed here through the application."
        )

        if st.button("💾 Save System Control Settings", type="primary", key="save_sys_settings"):
            if not new_sid.strip():
                st.error("Super User ID cannot be blank.")
            else:
                settings["superuser_id"] = new_sid.strip()
                if new_spw.strip():
                    if len(new_spw.strip()) < 8:
                        st.error("Super User password should be at least 8 characters.")
                        st.stop()
                    settings["superuser_password_hash"] = _hash_secret(new_spw.strip())
                settings["registration_key_hours"] = int(reg_hours)
                settings["trial_days"] = int(trial_days)
                settings["included_users"] = int(included_users)
                _save_system_settings(settings)

                # Keep existing firms unchanged; only future registrations
                # use the new included-user policy. This avoids touching
                # existing calculation/master data.
                _audit("SYSTEM_SETTINGS_UPDATED", "", st.session_state.get("_logged_user",""),
                       {"registration_key_hours": int(reg_hours),
                        "trial_days": int(trial_days),
                        "included_users": int(included_users),
                        "superuser_id_changed": new_sid.strip()})
                st.success("System settings saved. Future firm registrations will use these values.")
                st.rerun()

        st.markdown("#### Database / Tenant Operations")
        st.write("**XYZ Company migration:** existing legacy data remains under the XYZ Company tenant.")
        st.write("**New firms:** all firm masters/data are created from the application and automatically stored under that firm's tenant.")
        st.write("**No manual Firebase document creation is required for a new firm.**")
        st.write("**No GitHub update is required when registering, renewing, changing pricing, or adding users.**")

    if st.button("🚪 Super User Logout", type="primary"):
        st.session_state.authenticated=False; st.session_state._auth_role=""; st.session_state._logged_user=""; st.rerun()
    st.stop()
# ─── SESSION STATE ────────────────────────────────────────────────────────────
_ensure_xyz_migration()
if "masters" not in st.session_state:
    st.session_state.masters = fs_load()
if "proj_msg" not in st.session_state:
    st.session_state.proj_msg = ""
if "cont_msg" not in st.session_state:
    st.session_state.cont_msg = ""
if "edit_contract_idx" not in st.session_state:
    st.session_state.edit_contract_idx = None
if "editing_contract_no" not in st.session_state:
    st.session_state.editing_contract_no = None
if "clear_contract_flag" not in st.session_state:
    st.session_state.clear_contract_flag = False

def persist():
    fs_save(st.session_state.masters)

# ─── SAFE FLOAT ───────────────────────────────────────────────────────────────
def sf(val, default=0.0):
    try:
        return float(val) if val not in ('', None) else default
    except:
        return default

# ─── INDIAN NUMBER FORMAT (₹ 2,22,25,262.00 style) ────────────────────────────
def fmt_inr(value, symbol="₹", decimals=2, dash_on_zero=True):
    """Format a number Indian-style: last 3 digits, then groups of 2
    (e.g. 22225262 -> 2,22,25,262.00). Returns '—' for empty/zero when
    dash_on_zero is True, matching the existing GRN-detail style."""
    try:
        if value is None or value == "":
            return "—"
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if dash_on_zero and num == 0:
        return "—"
    neg = num < 0
    num = abs(num)
    whole = int(num)
    s = str(whole)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        s = ",".join(parts) + "," + last3
    if decimals > 0:
        frac = f"{num:.{decimals}f}".split(".")[1]
        return f"{symbol}{'-' if neg else ''}{s}.{frac}"
    return f"{symbol}{'-' if neg else ''}{s}"

# ─── PARSE EXCEL ──────────────────────────────────────────────────────────────
def parse_excel(file_bytes):
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    sheets = xl.sheet_names
    cont = pd.read_excel(xl, sheet_name=sheets[0], header=0)
    # PUR CONT DETAILS supports an optional Group column. Keep it for
    # Contract Master group-level condition matching.
    if cont.shape[1] >= 5:
        cont = cont.iloc[:, :5].copy()
        cont.columns = ["Contract_No","Effective_Date","Bales","Branch","Group"]
    else:
        cont = cont.iloc[:, :4].copy()
        cont.columns = ["Contract_No","Effective_Date","Bales","Branch"]
        cont["Group"] = ""
    cont = cont.dropna(subset=["Contract_No"])
    cont["Group"] = cont["Group"].fillna("").astype(str).str.strip()
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
    # Columns: Contract No | Mode of Transaction | Payment Date | Payment Amount
    # (Backward compatible: if the sheet doesn't yet have the Mode of
    # Transaction column (col H), fall back to the old 3-column layout
    # Contract No | Payment Date | Payment Amount and leave mode blank.)
    if raw2.shape[1] >= 8:
        pay = raw2.iloc[1:,[4,5,6,7]].copy()
        pay.columns = ["Contract_No","Mode_Of_Transaction","Payment_Date","Payment_Amount"]
    else:
        pay = raw2.iloc[1:,[4,5,6]].copy()
        pay.columns = ["Contract_No","Payment_Date","Payment_Amount"]
        pay["Mode_Of_Transaction"] = ""
    pay = pay.dropna(subset=["Contract_No","Payment_Amount"])
    pay = pay[~pay["Contract_No"].astype(str).str.lower().str.contains("total|nan")]
    pay["Payment_Date"]   = pd.to_datetime(pay["Payment_Date"], errors="coerce")
    pay["Payment_Amount"] = pd.to_numeric(pay["Payment_Amount"], errors="coerce")
    pay["Mode_Of_Transaction"] = pay["Mode_Of_Transaction"].apply(lambda x: str(x).strip() if pd.notna(x) else "")
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
    # Preserve the exact uploaded GRN row sequence.
    return cont, emd, pay, grn

# ─── CALCULATIONS ─────────────────────────────────────────────────────────────
def _norm_key(v):
    """Normalize Contract No / Group values for reliable matching."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip().upper()


def _get_mc_for_contract(cn, all_contracts, upload_group=""):
    """
    Per-upload-row master resolution.
    Priority: INDIVIDUAL CONTRACT -> UPLOAD GROUP -> DEFAULT.
    Group can be stored in a dedicated Master Group field OR, for the
    existing data format, as Master Contract_No == the group name.
    """
    cn_key = _norm_key(cn)
    group_key = _norm_key(upload_group)

    # 1) Exact individual Contract No always wins.
    if cn_key:
        for c in all_contracts:
            master_cn = _norm_key(c.get("contract_no", c.get("Contract_No", "")))
            if master_cn == cn_key:
                return c, "CONTRACT"

    # 2) If no individual contract exists, use the Group from upload sheet.
    if group_key:
        # Dedicated Group field in Master.
        for c in all_contracts:
            master_group = _norm_key(
                c.get("group", "") or c.get("Group", "") or
                c.get("group_name", "") or c.get("group_no", "") or
                c.get("group_code", "")
            )
            if master_group == group_key:
                return c, "GROUP"

        # Existing Master format: GROUP-A is saved in Contract No.
        for c in all_contracts:
            master_cn = _norm_key(c.get("contract_no", c.get("Contract_No", "")))
            master_group = _norm_key(
                c.get("group", "") or c.get("Group", "") or
                c.get("group_name", "") or c.get("group_no", "") or
                c.get("group_code", "")
            )
            if master_cn == group_key and not master_group:
                return c, "GROUP"

    # 3) Only after both individual and group fail, use DEFAULT.
    for c in all_contracts:
        master_cn = _norm_key(c.get("contract_no", c.get("Contract_No", "")))
        if master_cn in ("DEFAULT", "DFLT", "DEFAULT CONTRACT"):
            return c, "DEFAULT"

    return None, "NOT_DEFINED"

def run_calculations(cont, emd, pay, grn, mc_or_contracts):
    """
    mc_or_contracts: either a single mc dict (legacy) OR a list of all contracts.
    When a list is passed, per-row EITHER/OR lookup is done automatically.
    """
    # Support both old single-mc call and new multi-mc list call
    _contracts_list = mc_or_contracts if isinstance(mc_or_contracts, list) else None
    _single_mc      = mc_or_contracts if not isinstance(mc_or_contracts, list) else None

    def _mc(cn, upload_group=""):
        """Returns (master_dict_or_None, match_rule_str)."""
        if _contracts_list is not None:
            return _get_mc_for_contract(cn, _contracts_list, upload_group)
        # Legacy single-mc path
        if _single_mc is not None:
            return _single_mc, "CONTRACT"
        return None, "NOT_DEFINED"

    # Pre-compute maps (contract-level, not row-level)
    # mc_dummy not used — slabs are read per-row only from matched master
    pass  # dummy removed; _mc now returns (master, rule)

    # ── PRE-COMPUTE MAPS (all keys normalized: str + strip + upper) ──────────
    # This ensures Excel values like 123 / "123" / " RAY-110 " all match correctly.

    total_emd_map = {}
    for cn_raw, amt in emd.groupby("Contract_No")["EMD_Amount"].sum().items():
        total_emd_map[str(cn_raw).strip().upper()] = amt

    # Effective Date MAP: source = PUR CONT DETAILS sheet (uploaded Excel).
    # CC_Free_End = Effective_Date (this map) + cc_free_days (Contract Master).
    eff_date_map = {}
    for _, r in cont.iterrows():
        key = str(r["Contract_No"]).strip().upper()
        eff_date_map[key] = r["Effective_Date"]

    per_bale_emd = {}
    for _, r in cont.iterrows():
        key = str(r["Contract_No"]).strip().upper()
        b = r["Bales"] if pd.notna(r["Bales"]) and r["Bales"] > 0 else 1
        per_bale_emd[key] = total_emd_map.get(key, 0) / b

    emd_pool = {}
    for cn_raw, g in emd.groupby("Contract_No"):
        key = str(cn_raw).strip().upper()
        emd_pool[key] = g[["EMD_Date","EMD_Amount"]].copy().reset_index(drop=True)
        emd_pool[key]["Remaining"] = emd_pool[key]["EMD_Amount"].astype(float)

    pay_pool = {}
    for cn_raw, g in pay.groupby("Contract_No"):
        key = str(cn_raw).strip().upper()
        pay_pool[key] = g[["Payment_Date","Payment_Amount","Mode_Of_Transaction"]].copy().reset_index(drop=True)
        pay_pool[key]["Remaining"] = pay_pool[key]["Payment_Amount"].astype(float)

    branch_map = {}
    group_map = {}
    for _, r in cont.drop_duplicates("Contract_No").iterrows():
        key = _norm_key(r.get("Contract_No", ""))
        branch_map[key] = str(r.get("Branch", "") or "").strip()
        # IMPORTANT: Group belongs to PUR CONT DETAILS, not GRN BOOKING.
        # Build a Contract -> Group map so every GRN row can inherit its
        # upload group's master conditions.
        group_map[key] = str(r.get("Group", "") or "").strip()
    results = []
    for _, row in grn.iterrows():
        cn = str(row["Contract_No"]).strip()

        # ── PER-ROW MASTER LOOKUP (CONTRACT > GROUP only; no DEFAULT) ──────
        # GRN BOOKING does NOT contain Group. Group is defined in PUR CONT DETAILS,
        # therefore it MUST be obtained from the Contract -> Group map above.
        cn_key_for_group = _norm_key(cn)
        row_group  = group_map.get(cn_key_for_group, "")
        row_mc, match_rule = _mc(cn, row_group)

        # ── If no master matched → skip calculation, record remark ──────────
        if row_mc is None:
            # Build a zero-value result row with a clear remark
            bales_val  = row.get("Accepted_Qty_AUM", 0)
            mat_val    = row.get("Material_Amount", 0)
            igst_val   = row.get("IGST", 0)
            branch_val = branch_map.get(cn_key_for_group, "")
            eff_date_val = eff_date_map.get(cn_key_for_group, pd.NaT)
            lift_date  = row.get("Party_Bill_Date", pd.NaT)
            results.append({
                "Contract_No": cn,
                "GRN_No": row.get("GRN_No", ""),
                "Effective_Date": eff_date_val,
                "Party_Bill_Date": lift_date,
                "Bales": int(bales_val) if not pd.isna(bales_val) else 0,
                "Material_Amount": round(float(mat_val), 2) if mat_val else 0.0,
                "GST_On_Material": round(float(igst_val), 2) if igst_val else 0.0,
                "Total_Bill_Amount": round(float(mat_val or 0) + float(igst_val or 0), 2),
                "Payment_Amount": 0.0,
                "Per_Bale_EMD": 0.0, "EMD_Allocated": 0.0,
                "EMD_Date": pd.NaT, "Net_Amount": 0.0,
                "Payment_Date": pd.NaT, "EMD_Days": 0, "EMD_Interest": 0.0,
                "CD_Due_Date": pd.NaT, "CD_Days": 0, "CD_Pct": 0.0, "Cash_Discount": 0.0,
                "Late_Lift_Days": 0, "Late_Lifting_Chg": 0.0, "Late_Lifting_GST": 0.0,
                "CC_Free_End": pd.NaT, "CC_Days": 0,
                "Carry_Charges": 0.0, "Carry_GST": 0.0,
                "_cc_slab_breakdown": [],
                "Branch": branch_val,
                "Group": row_group,
                "Matched_Master_Contract": "",
                "Matched_Master_Group": "",
                "Match_Rule": "NOT_DEFINED",
                "Remark": "⚠️ No individual contract or group master found — calculation skipped",
            })
            continue  # skip to next GRN row

        # ── Slabs from matched master ────────────────────────────────────────
        emd_rate     = sf(row_mc.get("emd_percent"), 5.0)
        cd_slabs     = [{"days":sf(s.get("days")),"pct":sf(s.get("pct"))} for s in row_mc.get("cd_slabs",[])]
        ll_slabs     = [{"days":sf(s.get("days")),"pct":sf(s.get("pct"))} for s in row_mc.get("ll_slabs",[])]
        ll_gst       = sf(row_mc.get("ll_gst"), 5.0)
        cc_slabs     = [{"days":sf(s.get("days")),"pct":sf(s.get("pct"))} for s in row_mc.get("cc_slabs",[])]
        cc_gst       = sf(row_mc.get("cc_gst"), 5.0)
        cc_free_days = int(sf(row_mc.get("cc_free_days"), 0))
        ll_compound  = bool(row_mc.get("ll_compound", False))
        cc_compound  = bool(row_mc.get("cc_compound", False))
        # ─────────────────────────────────────────────────────────────────────

        cn_key = str(cn).strip().upper()   # normalized key for all map lookups

        bales = row["Accepted_Qty_AUM"]
        pbe   = per_bale_emd.get(cn_key, 0)
        mat   = row["Material_Amount"]
        # Branch comes from PUR CONT DETAILS (uploaded Excel).
        branch = branch_map.get(cn_key, "")

        igst  = row["IGST"]
        lift_date = row["Party_Bill_Date"]
        # Effective Date: ALWAYS from uploaded PUR CONT DETAILS sheet.
        eff_date  = eff_date_map.get(cn_key, pd.NaT)

        gst_on_mat  = round(igst, 2)
        total_bill  = round(mat + gst_on_mat, 2)

        emd_need = round(pbe * bales, 2)
        emd_alloc, emd_date = 0.0, pd.NaT
        pool = emd_pool.get(cn_key)
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

        net_amt = round(total_bill - emd_alloc, 2)
        pay_alloc, pay_date, pay_mode = 0.0, pd.NaT, ""
        ppool = pay_pool.get(cn_key)
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
                if pd.isna(pay_date) or d > pay_date:
                    pay_date = d
                    pay_mode = ppool.at[idx,"Mode_Of_Transaction"]

        emd_days, emd_interest = 0, 0.0
        if not pd.isna(emd_date) and not pd.isna(pay_date) and emd_alloc > 0:
            emd_days     = (pay_date - emd_date).days
            emd_interest = round(((emd_alloc * emd_rate / 100) / 365) * emd_days, 2)

        # ── CASH DISCOUNT ─────────────────────────────────────────────────────
        # Formula:
        #   CD Due Date  = Effective Date + highest CD slab days
        #   Eligibility  = Payment Date <= CD Due Date
        #   Diff Days    = CD Due Date − Payment Date
        #   CD %         = % from the highest (largest days) CD slab
        #   CD Amount    = Material Amount × CD% × (Diff Days ÷ 365)
        #
        # Effective Date source:
        #   Specific contract match → from Contract Master
        #   DEFAULT contract        → from uploaded Excel (PUR CONT DETAILS)
        # ──────────────────────────────────────────────────────────────────────
        cd_amount, cd_days_used, cd_pct_used = 0.0, 0, 0.0
        cd_due_date = pd.NaT
        cd_due_days = 0

        if cd_slabs and not pd.isna(eff_date):
            # Step 1: CD Due Date = Effective Date + highest slab days
            valid_slabs = [s for s in cd_slabs if sf(s.get("days"), 0) > 0]
            if valid_slabs:
                # Highest-days slab wins for both the due date and the rate
                best_slab  = max(valid_slabs, key=lambda x: sf(x.get("days"), 0))
                cd_due_days = int(sf(best_slab.get("days"), 0))
                cd_pct_used = sf(best_slab.get("pct"), 0)
                cd_due_date = eff_date + pd.Timedelta(days=cd_due_days)

                # Step 2: Check eligibility
                if not pd.isna(pay_date) and pay_date <= cd_due_date:
                    # Step 3: Diff Days = CD Due Date − Payment Date
                    diff_days    = max((cd_due_date - pay_date).days, 0)
                    cd_days_used = diff_days
                    # Step 4: CD Amount = Mat × CD% × (Diff Days ÷ 365)
                    cd_amount    = round((mat * cd_pct_used / 100) * (diff_days / 365), 2)
                else:
                    # Payment after CD Due Date → no discount
                    cd_days_used = 0
                    cd_pct_used  = 0.0
                    cd_amount    = 0.0

        ll_charges, ll_gst_amt, late_lift_days = 0.0, 0.0, 0
        if not pd.isna(pay_date) and not pd.isna(lift_date):
            free_end = pay_date + pd.Timedelta(days=15)
            if lift_date > free_end:
                late_lift_days = (lift_date - free_end).days
                s1 = ll_slabs[0] if len(ll_slabs)>0 else {"days":30,"pct":0.50}
                s2 = ll_slabs[1] if len(ll_slabs)>1 else {"days":30,"pct":0.75}
                s3 = ll_slabs[2] if len(ll_slabs)>2 else {"days":9999,"pct":1.00}
                rem = late_lift_days; ll_base = 0.0
                if ll_compound:
                    # Compound prorata: each slab charges compound on cumulative amount
                    running = mat
                    d1 = min(rem, s1["days"])
                    slab1_charge = running * (s1["pct"]/100) * (d1/30)
                    ll_base += slab1_charge; running += slab1_charge; rem -= d1
                    if rem > 0:
                        d2 = min(rem, s2["days"])
                        slab2_charge = running * (s2["pct"]/100) * (d2/30)
                        ll_base += slab2_charge; running += slab2_charge; rem -= d2
                    if rem > 0:
                        slab3_charge = running * (s3["pct"]/100) * (rem/30)
                        ll_base += slab3_charge
                else:
                    # Simple prorata (original logic)
                    d1 = min(rem, s1["days"]); ll_base += mat*(s1["pct"]/100)*(d1/30); rem -= d1
                    if rem > 0:
                        d2 = min(rem, s2["days"]); ll_base += mat*(s2["pct"]/100)*(d2/30); rem -= d2
                    if rem > 0:
                        ll_base += mat*(s3["pct"]/100)*(rem/30)
                ll_charges = round(ll_base, 2)
                ll_gst_amt = round(ll_charges * ll_gst / 100, 2)

        # ── CARRYING CHARGES ──
        # CC Days  = Payment Date − CC Free End (actual days, no cap)
        # CC Days <= 0 → No charges
        # CC Days > 0  → Compound prorata across slabs (unlimited):
        #   Slab 1 (day  1– 30): mat × 1.25% × (days_in_slab / 30)
        #   Slab 2 (day 31– 60): (running) × 1.35% × (days_in_slab / 30)
        #   Slab 3 (day 61– 90): (running) × 1.35% × (days_in_slab / 30)
        #   Slab 4 (day 91–120): (running) × 1.35% × (days_in_slab / 30)
        #   ... and so on every 30 days until all remaining days are consumed.
        #   Each slab charges on the RUNNING amount (principal + all prior slab charges).
        # ── CARRYING CHARGES ────────────────────────────────────────────────
        # Rule:
        #   CC_Free_End  = Effective_Date (from uploaded PUR CONT DETAILS)
        #                  + cc_free_days (from Contract Master CC section)
        #   CC_Days      = Payment_Date - CC_Free_End   (if > 0, else 0)
        #   CC_Days <= 0 → No charges
        #   CC_Days >  0 → Compound prorata across unlimited 30-day slabs
        # ─────────────────────────────────────────────────────────────────────
        cc_charges, cc_gst_amt, cc_days = 0.0, 0.0, 0
        cc_slab_breakdown = []
        cc_free_end = pd.NaT

        if not pd.isna(eff_date):
            # CC_Free_End = Effective Date + CC Free Days (from Contract Master)
            cc_free_days_int = max(int(float(cc_free_days or 0)), 0)
            cc_free_end = eff_date + pd.Timedelta(days=cc_free_days_int)

            if not pd.isna(pay_date):
                cc_days_raw = (pay_date - cc_free_end).days
                if cc_days_raw > 0:
                    cc_days = cc_days_raw
                    s1c = cc_slabs[0] if len(cc_slabs) > 0 else {"days": 30, "pct": 1.25}
                    s2c = cc_slabs[1] if len(cc_slabs) > 1 else {"days": 30, "pct": 1.35}

                    rem     = cc_days
                    running = mat
                    total   = 0.0
                    slab_n  = 0

                    while rem > 0:
                        slab_n += 1
                        if slab_n == 1:
                            rate      = s1c["pct"] / 100
                            slab_days = int(s1c["days"]) if s1c["days"] > 0 else 30
                        else:
                            rate      = s2c["pct"] / 100
                            slab_days = int(s2c["days"]) if s2c["days"] > 0 else 30

                        d       = min(rem, slab_days)
                        charge  = running * rate * (d / 30)

                        day_from = (slab_n - 1) * 30 + 1
                        day_to   = day_from + d - 1
                        label    = f"{day_from}-{day_to}"
                        cc_slab_breakdown.append((label, round(charge, 2)))

                        running += charge
                        total   += charge
                        rem     -= d

                    cc_charges = round(total, 2)
                    cc_gst_amt = round(cc_charges * cc_gst / 100, 2)

        results.append({
            "Contract_No":cn, "GRN_No":row["GRN_No"], "Branch": branch,
            "Group": row_group if _norm_key(row_group) else ("INDIVIDUAL" if match_rule == "CONTRACT" else ""),
            "Matched_Master_Contract": str(row_mc.get("contract_no", row_mc.get("Contract_No", "")) or ""),
            "Matched_Master_Group": (
                str(
                    row_mc.get("group", "") or row_mc.get("Group", "") or
                    row_mc.get("group_name", "") or row_mc.get("group_no", "") or
                    row_mc.get("group_code", "") or ""
                )
                if match_rule == "GROUP" else
                ("INDIVIDUAL CONTRACT" if match_rule == "CONTRACT" else "")
            ),
            "Match_Rule": match_rule,
            "Remark": "",
            "Effective_Date":eff_date, "Party_Bill_Date":lift_date,
            "Bales":int(bales), "Material_Amount":round(mat,2),
            "GST_On_Material":gst_on_mat, "Total_Bill_Amount":total_bill,
            "Payment_Amount":round(pay_alloc,2),
            "Per_Bale_EMD":round(pbe,2), "EMD_Allocated":round(emd_alloc,2),
            "EMD_Date":emd_date, "Net_Amount":round(net_amt,2),
            "Payment_Date":pay_date, "Payment_Mode":pay_mode,
            "EMD_Days":emd_days, "EMD_Interest":emd_interest,
            "CD_Days":cd_days_used, "CD_Pct":cd_pct_used, "CD_Due_Date":cd_due_date,
            "Cash_Discount":cd_amount,
            "Late_Lift_Days":late_lift_days, "Late_Lifting_Chg":ll_charges,
            "Late_Lifting_GST":ll_gst_amt, "CC_Free_End":cc_free_end,
            "CC_Days":cc_days, "Carry_Charges":cc_charges, "Carry_GST":cc_gst_amt,
            "_cc_slab_breakdown": cc_slab_breakdown,  # dynamic slab columns built in df_to_excel_bytes & display
        })
    return pd.DataFrame(results)

def fmt_date(v):
    try:
        if pd.isna(v): return "—"
        return pd.Timestamp(v).strftime("%d-%m-%Y")
    except:
        return "—"

# ─── COLUMN LABEL PRETTIFIER ───────────────────────────────────────────────
# Converts internal snake_case column names into clean, human-readable
# headers for both the on-screen tables and the exported Excel sheets.
_PRETTY_COL_MAP = {
    "Contract_No": "Contract No",
    "GRN_No": "GRN No",
    "Branch": "Branch",
    "Group": "Group",
    "Matched_Master_Contract": "Matched Master Contract",
    "Matched_Master_Group": "Matched Master Group",
    "Match_Rule": "Match Rule",
    "Effective_Date": "Effective Date",
    "Party_Bill_Date": "Party Bill Date",
    "Bales": "Bales",
    "Material_Amount": "Material Amount",
    "GST_On_Material": "GST On Material",
    "Total_Bill_Amount": "Total Bill Amount",
    "Payment_Amount": "Payment Amount",
    "Per_Bale_EMD": "Per Bale EMD",
    "EMD_Allocated": "EMD Allocated",
    "EMD_Date": "EMD Date",
    "Net_Amount": "Net Amount",
    "Payment_Date": "Payment Date",
    "Payment_Mode": "Mode Of Transaction",
    "EMD_Days": "EMD Days",
    "EMD_Interest": "EMD Interest",
    "CD_Days": "CD Days",
    "CD_Pct": "CD %",
    "CD_Due_Date": "CD Due Date",
    "Cash_Discount": "Cash Discount",
    "Late_Lift_Days": "Late Lift Days",
    "Late_Lifting_Chg": "Late Lifting Charges",
    "Late_Lifting_GST": "Late Lifting GST",
    "CC_Free_End": "CC Free End",
    "CC_Days": "CC Days",
    "Carry_Charges": "Carrying Charges",
    "Carry_GST": "Carrying GST",
    # Contract-wise / Branch-wise summary aggregate names
    "GRNs": "GRNs",
    "Contracts": "Contracts",
    "Total_Bales": "Total Bales",
    "Total_Material": "Total Material",
    "Total_GST": "Total GST",
    "Total_Bill": "Total Bill",
    "Total_Payment": "Total Payment",
    "Total_EMD": "Total EMD Allocated",
    "Total_EMD_Interest": "Total EMD Interest",
    "Total_Cash_Disc": "Total Cash Discount",
    "Total_LL": "Total Late Lifting",
    "Total_LL_GST": "Total Late Lifting GST",
    "Total_CC": "Total Carrying Charges",
    "Total_CC_GST": "Total Carrying GST",
    "Material": "Material",
    "GST": "GST",
    "Payment": "Payment",
    "EMD_Alloc": "EMD Allocated",
    "Cash_Disc": "Cash Discount",
    "LL_Chg": "Late Lifting Charges",
    "LL_GST": "Late Lifting GST",
    "CC_Chg": "Carrying Charges",
    "CC_GST": "Carrying GST",
    "Shortage_Excess": "Shortage / Excess",
    "Shortage_Excess_Mark": "Shortage/Excess Status",
    "Receivable_Payable": "Receivable / Payable",
    "Receivable_Payable_Mark": "Receivable/Payable Status",
    "Actual_Payment_Total": "Actual Payment (Uploaded Sheet)",
    "Total_Payment_And_EMD": "Total Payment + EMD",
    "Total_Payable": "Total Payable",
    "Remark": "Remark",
}

import re as _re
def pretty_col(col):
    """Map one internal column name to a clean display label."""
    m = _re.match(r"^CC_Slab(\d+)_(.+)$", col)
    if m:
        return f"CC Slab {m.group(1)} ({m.group(2)})"
    if col in _PRETTY_COL_MAP:
        return _PRETTY_COL_MAP[col]
    return col.replace("_", " ")

def pretty_columns(df):
    """Return a copy of df with human-readable column headers."""
    return df.rename(columns={c: pretty_col(c) for c in df.columns})

def branch_wise_summary(result_df, pay=None):
    """Aggregate the GRN-wise result into a Branch-level summary."""
    if "Branch" not in result_df.columns:
        return pd.DataFrame()
    bs = result_df.groupby("Branch").agg(
        Contracts=("Contract_No","nunique"),
        GRNs=("GRN_No","count"), Total_Bales=("Bales","sum"),
        Total_Material=("Material_Amount","sum"),
        Total_GST=("GST_On_Material","sum"),
        Total_Bill=("Total_Bill_Amount","sum"),
        Total_Payment=("Payment_Amount","sum"),
        Total_EMD=("EMD_Allocated","sum"),
        Total_EMD_Interest=("EMD_Interest","sum"),
        Total_Cash_Disc=("Cash_Discount","sum"),
        Total_LL=("Late_Lifting_Chg","sum"),
        Total_LL_GST=("Late_Lifting_GST","sum"),
        Total_CC=("Carry_Charges","sum"),
        Total_CC_GST=("Carry_GST","sum"),
    ).reset_index()
    # Total Payment + EMD = Total Payment kiya + Total EMD Allocated
    bs["Total_Payment_And_EMD"] = bs["Total_Payment"] + bs["Total_EMD"]
    # Total amount we (the purchaser) actually owe CCI (the vendor) = base bill
    # + charges WE pay to CCI (Late Lifting, Carrying) − amounts WE receive from
    # CCI (Cash Discount, interest on our EMD deposit).\
    # Total Payable = Total Bill + Late Lifting + LL GST + Carrying + CC GST − Cash Discount − EMD Interest
    bs["Total_Payable"] = (
        bs["Total_Bill"] + bs["Total_LL"] + bs["Total_LL_GST"]
        + bs["Total_CC"] + bs["Total_CC_GST"]
        - bs["Total_Cash_Disc"] - bs["Total_EMD_Interest"]
    )
    # Actual Payment from uploaded Payment sheet — used ONLY for Receivable/Payable
    # branch_map: contract → branch (from result_df)
    if pay is not None:
        branch_map_cn = result_df.drop_duplicates("Contract_No").set_index("Contract_No")["Branch"].to_dict()
        pay_cn_key = pay.copy()
        pay_cn_key["_key"] = pay_cn_key["Contract_No"].astype(str).str.strip().str.upper()
        pay_cn_key["_branch"] = pay_cn_key["_key"].map(
            {str(k).strip().upper(): v for k, v in branch_map_cn.items()}
        )
        branch_actual_pay = pay_cn_key.groupby("_branch")["Payment_Amount"].sum()
        bs["Actual_Payment_Total"] = bs["Branch"].map(branch_actual_pay).fillna(0)
    else:
        bs["Actual_Payment_Total"] = bs["Total_Payment"]
    # Receivable / Payable = Total Payable − Actual Payment (uploaded sheet) − Total EMD Allocated
    bs["Receivable_Payable"] = bs["Total_Payable"] - bs["Actual_Payment_Total"] - bs["Total_EMD"]
    bs["Receivable_Payable_Mark"] = bs["Receivable_Payable"].apply(
        lambda x: "PAYABLE" if pd.notna(x) and float(x) > 0
        else ("RECEIVABLE" if pd.notna(x) and float(x) < 0 else "CLEAR")
    )
    bs_total = {c: "" for c in bs.columns}
    bs_total["Branch"] = "GRAND TOTAL"
    skip_cols = {"Branch", "Receivable_Payable_Mark"}
    for c in bs.columns:
        if c not in skip_cols:
            bs_total[c] = pd.to_numeric(bs[c], errors="coerce").sum()
    _total_rp = bs_total.get("Receivable_Payable", 0)
    bs_total["Receivable_Payable_Mark"] = (
        "PAYABLE" if pd.notna(_total_rp) and float(_total_rp) > 0
        else ("RECEIVABLE" if pd.notna(_total_rp) and float(_total_rp) < 0 else "CLEAR")
    )
    bs = pd.concat([bs, pd.DataFrame([bs_total])], ignore_index=True)
    return bs

def df_to_excel_bytes(result_df, cont, emd, pay, grn):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:

        # ── Build dynamic slab columns from _cc_slab_breakdown ──────────────
        # Find the maximum number of slabs across all rows
        max_slabs = result_df["_cc_slab_breakdown"].apply(len).max() if "_cc_slab_breakdown" in result_df.columns else 0
        slab_col_names = []
        for i in range(max_slabs):
            # Generate label from first row that has this slab; fallback to position
            try:
                lbl = result_df["_cc_slab_breakdown"].dropna().iloc[0][i][0]
            except Exception:
                d_from = i * 30 + 1; d_to = d_from + 29
                lbl = f"{d_from}-{d_to}"
            col = f"CC_Slab{i+1}_{lbl}"
            slab_col_names.append(col)
            result_df[col] = result_df["_cc_slab_breakdown"].apply(
                lambda x: x[i][1] if isinstance(x, list) and len(x) > i else 0.0
            )

        base_cols = [
            "Contract_No","GRN_No","Branch","Group","Effective_Date","Party_Bill_Date","Bales",
            "Material_Amount","GST_On_Material","Total_Bill_Amount","Payment_Amount",
            "Per_Bale_EMD","EMD_Allocated","EMD_Date","Net_Amount","Payment_Date","Payment_Mode",
            "EMD_Days","EMD_Interest","CD_Days","CD_Pct","CD_Due_Date","Cash_Discount",
            "Late_Lift_Days","Late_Lifting_Chg","Late_Lifting_GST",
            "CC_Free_End","CC_Days","Carry_Charges","Carry_GST",
        ]
        cols = base_cols + slab_col_names

        # Payment_Mode is produced by the calculation engine, but older
        # cached/result paths may not contain it.  Never let one optional
        # column abort the complete Excel export.
        if "Payment_Mode" not in result_df.columns:
            result_df["Payment_Mode"] = ""

        # Use reindex instead of [] so an absent optional column cannot raise
        # KeyError.  This is especially important for Mode Of Transaction,
        # which is optional in some calculation/result paths.
        detail_export = result_df.reindex(columns=cols).copy()
        # Add a clear GRAND TOTAL row at the bottom of the detail report.
        detail_total = {c: "" for c in detail_export.columns}
        detail_total["Contract_No"] = "GRAND TOTAL"
        for c in ["Bales","Material_Amount","GST_On_Material","Total_Bill_Amount",
                  "Payment_Amount","EMD_Allocated","EMD_Interest","Cash_Discount",
                  "Late_Lifting_Chg","Late_Lifting_GST","Carry_Charges","Carry_GST"]:
            if c in detail_export.columns:
                detail_total[c] = pd.to_numeric(detail_export[c], errors="coerce").sum()
        detail_export = pd.concat([detail_export, pd.DataFrame([detail_total])], ignore_index=True)
        # Force date-only Excel values BEFORE writing.  This prevents Excel from
        # inheriting pandas datetime display (HH:MM:SS).  Python date objects
        # are written as true Excel dates and formatted as DD-MM-YYYY.
        _excel_date_cols = ["Effective_Date", "Party_Bill_Date", "EMD_Date", "Payment_Date", "CD_Due_Date", "CC_Free_End"]
        for _dc in _excel_date_cols:
            if _dc in detail_export.columns:
                detail_export[_dc] = pd.to_datetime(detail_export[_dc], errors="coerce").dt.date
        # Convert calculation-report date cells to explicit DD-MM-YYYY TEXT before Excel write.
        # This guarantees Excel cannot display a trailing 00:00:00, while preserving
        # the exact existing row order (no sorting/reordering is performed).
        _report_date_cols = ["Effective_Date", "Party_Bill_Date", "EMD_Date", "Payment_Date", "CD_Due_Date", "CC_Free_End"]
        for _dc in _report_date_cols:
            if _dc in detail_export.columns:
                def _date_text(_v):
                    if pd.isna(_v) or _v == "":
                        return ""
                    try:
                        return pd.Timestamp(_v).strftime("%d-%m-%Y")
                    except Exception:
                        return str(_v).split(" ")[0]
                detail_export[_dc] = detail_export[_dc].apply(_date_text)
        pretty_columns(detail_export).to_excel(w, sheet_name="GRN Calculation", index=False)
        _ws_tmp = w.book["GRN Calculation"]
        _hdr_tmp = {c.value: c.column for c in _ws_tmp[1]}
        for _h in ["Effective Date", "Party Bill Date", "EMD Date", "Payment Date", "CD Due Date", "CC Free End"]:
            if _h in _hdr_tmp:
                for _cell in _ws_tmp.iter_cols(min_col=_hdr_tmp[_h], max_col=_hdr_tmp[_h], min_row=2, max_row=_ws_tmp.max_row):
                    for _c in _cell:
                        if _c.value is not None:
                            _c.number_format = "dd-mm-yyyy"
        summary = result_df.groupby("Contract_No").agg(
            GRNs=("GRN_No","count"), Total_Bales=("Bales","sum"),
            Total_Material=("Material_Amount","sum"),
            Total_GST=("GST_On_Material","sum"),
            Total_Bill=("Total_Bill_Amount","sum"),
            Total_Payment=("Payment_Amount","sum"),
            Total_EMD=("EMD_Allocated","sum"),
            Total_EMD_Interest=("EMD_Interest","sum"),
            Total_Cash_Disc=("Cash_Discount","sum"),
            Total_LL=("Late_Lifting_Chg","sum"),
            Total_LL_GST=("Late_Lifting_GST","sum"),
            Total_CC=("Carry_Charges","sum"),
            Total_CC_GST=("Carry_GST","sum"),
        ).reset_index()
        # Shortage / Excess (original, simple) = Total Bill − Total Payment − Total EMD Allocated
        # Positive → SHORTAGE (payment still pending)  |  Negative → EXCESS (overpaid, refund due)
        summary["Shortage_Excess"] = (
            summary["Total_Bill"] - summary["Total_Payment"] - summary["Total_EMD"]
        )
        summary["Shortage_Excess_Mark"] = summary["Shortage_Excess"].apply(
            lambda x: "SHORTAGE" if pd.notna(x) and float(x) > 0
            else ("EXCESS" if pd.notna(x) and float(x) < 0 else "CLEAR")
        )
        # Total amount WE owe CCI (the vendor) = base bill + charges WE pay to
        # CCI (Late Lifting, Carrying) − amounts WE receive from CCI (Cash
        # Discount, interest on our EMD deposit).
        summary["Total_Payable"] = (
            summary["Total_Bill"] + summary["Total_LL"] + summary["Total_LL_GST"]
            + summary["Total_CC"] + summary["Total_CC_GST"]
            - summary["Total_Cash_Disc"] - summary["Total_EMD_Interest"]
        )
        # Actual payment from uploaded Payment sheet per contract (for Rec/Pay only)
        _pay_cn_map = pay.groupby(pay["Contract_No"].astype(str).str.strip().str.upper())["Payment_Amount"].sum()
        summary["Actual_Payment_Total"] = summary["Contract_No"].astype(str).str.strip().str.upper().map(_pay_cn_map).fillna(0)
        # Receivable / Payable = Total Payable − Actual Payment (uploaded sheet) − Total EMD Allocated
        # Positive → PAYABLE (still owe to CCI)  |  Negative → RECEIVABLE (CCI owes refund)
        summary["Receivable_Payable"] = summary["Total_Payable"] - summary["Actual_Payment_Total"] - summary["Total_EMD"]
        summary["Receivable_Payable_Mark"] = summary["Receivable_Payable"].apply(
            lambda x: "PAYABLE" if pd.notna(x) and float(x) > 0
            else ("RECEIVABLE" if pd.notna(x) and float(x) < 0 else "CLEAR")
        )
        # Branch is contract-level data from PUR CONT DETAILS.
        branch_map = cont.drop_duplicates("Contract_No").set_index("Contract_No")["Branch"].to_dict()
        summary.insert(1, "Branch", summary["Contract_No"].map(branch_map).fillna(""))
        summary_total = {c: "" for c in summary.columns}
        summary_total["Contract_No"] = "GRAND TOTAL"
        _skip = {"Contract_No", "Branch", "Shortage_Excess_Mark", "Receivable_Payable_Mark"}
        for c in summary.columns:
            if c not in _skip:
                summary_total[c] = pd.to_numeric(summary[c], errors="coerce").sum()
        _total_se = summary_total.get("Shortage_Excess", 0)
        summary_total["Shortage_Excess_Mark"] = (
            "SHORTAGE" if pd.notna(_total_se) and float(_total_se) > 0
            else ("EXCESS" if pd.notna(_total_se) and float(_total_se) < 0 else "CLEAR")
        )
        _total_rp = summary_total.get("Receivable_Payable", 0)
        summary_total["Receivable_Payable_Mark"] = (
            "PAYABLE" if pd.notna(_total_rp) and float(_total_rp) > 0
            else ("RECEIVABLE" if pd.notna(_total_rp) and float(_total_rp) < 0 else "CLEAR")
        )
        summary = pd.concat([summary, pd.DataFrame([summary_total])], ignore_index=True)
        pretty_columns(summary).to_excel(w, sheet_name="Summary", index=False)

        # ── Branch-wise Summary ──────────────────────────────────────────────
        branch_summary = branch_wise_summary(result_df, pay=pay)
        if not branch_summary.empty:
            pretty_columns(branch_summary).to_excel(w, sheet_name="Branch Summary", index=False)

        cont.to_excel(w, sheet_name="PUR CONT", index=False)
        emd.to_excel(w, sheet_name="EMD Payments", index=False)
        pay.to_excel(w, sheet_name="Final Payments", index=False)
        grn.to_excel(w, sheet_name="GRN Booking", index=False)

        # ── FINAL EXCEL DATE FORMAT SAFETY ──────────────────────────────────
        # All exported date columns are forced to date-only values and shown
        # as DD-MM-YYYY.  This changes only the display/value type, never row
        # order or the underlying calculation sequence.
        _date_columns_by_sheet = {
            "GRN Calculation": ["Effective Date", "Party Bill Date", "EMD Date", "Payment Date", "CD Due Date", "CC Free End"],
            "PUR CONT": ["Effective_Date"],
            "EMD Payments": ["EMD_Date"],
            "Final Payments": ["Payment_Date"],
            "GRN Booking": ["Party_Bill_Date", "Final_Indent_Date"],
        }
        for _ws_name, _headers in _date_columns_by_sheet.items():
            # GRN Calculation dates are already written as explicit DD-MM-YYYY text above.
            if _ws_name == "GRN Calculation":
                continue
            _ws = w.book[_ws_name]
            _header_pos = {cell.value: cell.column for cell in _ws[1]}
            for _header in _headers:
                _col_idx = _header_pos.get(_header)
                if _col_idx is None:
                    continue
                for _row in range(2, _ws.max_row + 1):
                    _c = _ws.cell(_row, _col_idx)
                    if _c.value is None:
                        continue
                    try:
                        _d = pd.to_datetime(_c.value, errors="coerce", dayfirst=True)
                        if not pd.isna(_d):
                            _c.value = _d.date()
                            _c.number_format = "dd-mm-yyyy"
                    except Exception:
                        pass

    buf.seek(0)
    return buf.getvalue()

# ─── TOP HEADER ───────────────────────────────────────────────────────────────
# Premium header + live clock with seconds + Logout button.
import datetime as _dt
import streamlit.components.v1 as components

_now = _dt.datetime.now()
_clock_str = _now.strftime("%d %b %Y  |  %H:%M:%S")
_logged_user = st.session_state.get("_logged_user", "")
_firm_id_header = st.session_state.get("_firm_id", "")
_firm_header = (_load_registry().get("firms", {}).get(_firm_id_header, {}).get("firm_name", "") if _firm_id_header else "SUPER USER")

hc1, hc2 = st.columns([4.6, 1.4])
with hc1:
    st.markdown(f"""
    <div class="top-header">
      <div class="top-header-left">
        <img src="data:image/png;base64,{LOGO_B64}" class="logo-img" alt="Softview">
        <div>
          <div class="top-header-title">🧮 CCI Working Calculation Utility</div>
          <div class="top-header-sub">Softview Technologies · Enterprise Financial Suite</div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
        <span class="top-header-badge">✨ PREMIUM ENTERPRISE EDITION</span>
        <span style="font-size:11.5px;color:rgba(255,255,255,0.92);font-weight:700;letter-spacing:.04em">👤 {_logged_user.upper()} · {_firm_header.upper()}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)
with hc2:
    components.html(f"""
    <div style="background:linear-gradient(135deg,#fff7ed,#ffffff);border:1px solid #fdba74;border-radius:12px;padding:8px 12px;text-align:center;box-shadow:0 4px 14px rgba(154,52,18,0.10);margin-bottom:5px">
      <div style="font-size:9px;color:#c2410c;font-weight:800;letter-spacing:.08em;text-transform:uppercase">🕐 Live Time</div>
      <span id="clock" style="font:800 13px monospace;color:#9a3412">{_clock_str}</span>
    </div>
    <script>
    (function(){{
      function tick(){{
        const n=new Date();
        const m=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const p=x=>String(x).padStart(2,'0');
        const el=document.getElementById('clock');
        if(el) el.textContent='🕐 '+p(n.getDate())+' '+m[n.getMonth()]+' '+n.getFullYear()+' | '+p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());
      }}
      tick(); setInterval(tick,1000);
    }})();
    </script>
    """, height=48, scrolling=False)
    if st.button("🚪 Logout", key="top_logout", use_container_width=True):
        st.session_state.authenticated = False
        st.session_state._logged_user = ""
        st.session_state._firm_id = ""
        st.session_state._auth_role = ""
        st.session_state._user_rights = []
        st.session_state._login_error = ""
        st.session_state.edit_contract_idx = None
        st.session_state.editing_contract_no = None
        st.rerun()

st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

# ─── TABS ─────────────────────────────────────────────────────────────────────
tab_masters, tab_upload, tab_results, tab_help, tab_users = st.tabs([
    "  📋  Masters  ",
    "  📤  Upload & Calculate  ",
    "  📊  Results  ",
    "  📖  Formula Guide  ",
    "  👤  User Master  "
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: MASTERS
# ══════════════════════════════════════════════════════════════════════════════
with tab_masters:
    left_col, right_col = st.columns([1.1, 0.9], gap="large")

    with left_col:
        # ── PROJECT MASTER ──
        with st.expander("🏗️  Project Master", expanded=False):
            if st.session_state.proj_msg:
                st.markdown(f'<div class="success-msg">✅ {st.session_state.proj_msg}</div>', unsafe_allow_html=True)
                st.session_state.proj_msg = ""

            c1, c2 = st.columns(2)
            pname = c1.text_input("Project Name ✱", key="pname", placeholder="e.g. CCI-RAYGADA-2025")
            psess = c2.text_input("Session", key="psess", placeholder="e.g. 2024-25")
            c3, c4 = st.columns(2)
            pfrom = c3.date_input("From Period ✱", key="pfrom", value=None)
            pto   = c4.date_input("To Period ✱", key="pto", value=None)
            pstat = st.selectbox("Status", ["open","closed"], key="pstat")

            if st.button("💾  Save Project", type="primary", key="btn_proj"):
                if not st.session_state.pname or not st.session_state.pfrom or not st.session_state.pto:
                    st.error("❌ Project Name, From Period and To Period are required.")
                else:
                    st.session_state.masters["projects"].append({
                        "name": st.session_state.pname,
                        "session": st.session_state.psess,
                        "from_period": str(st.session_state.pfrom),
                        "to_period": str(st.session_state.pto),
                        "status": st.session_state.pstat
                    })
                    persist()
                    st.session_state.proj_msg = f"Project '{st.session_state.pname}' saved to Firebase!"
                    st.rerun()

        # ── CONTRACT MASTER ──
        with st.expander("📄  Contract Master", expanded=False):
            if st.session_state.cont_msg:
                st.markdown(f'<div class="success-msg">✅ {st.session_state.cont_msg}</div>', unsafe_allow_html=True)
                st.session_state.cont_msg = ""

            open_projs = [p["name"] for p in st.session_state.masters["projects"] if p["status"]=="open"]
            if not open_projs:
                st.warning("⚠️ Please create an Open project first.")
            else:
                # Apply a pending contract edit BEFORE any widgets with these keys are created.
                pending_edit = st.session_state.pop("_pending_contract_edit", None)
                if pending_edit is not None:
                    def _edit_date(v):
                        try:
                            return date.fromisoformat(str(v)[:10]) if v else None
                        except Exception:
                            return None

                    # Clear old widget state first, then seed the new values safely.
                    _edit_keys = [
                        "cproj","cparty","cno","cgroup","cdt","ceff","cbales","emd_d","emd_p",
                        "cd1d","cd1p","cd2d","cd2p","cd3d","cd3p","cd_gst",
                        "ll1d","ll1p","ll2d","ll2p","ll3d","ll3p","ll_gst","ll_compound",
                        "cc_free","cc1d","cc1p","cc2d","cc2p","cc_gst","cc_compound"
                    ]
                    for _k in _edit_keys:
                        st.session_state.pop(_k, None)

                    _proj = pending_edit.get("project", open_projs[0])
                    st.session_state.cproj = _proj if _proj in open_projs else open_projs[0]
                    st.session_state.cparty = pending_edit.get("party", "")
                    st.session_state.cno = pending_edit.get("contract_no", "")
                    st.session_state.cgroup = pending_edit.get("group", "")
                    st.session_state.cdt = _edit_date(pending_edit.get("contract_date"))
                    st.session_state.ceff = _edit_date(pending_edit.get("effective_date"))
                    st.session_state.cbales = int(pending_edit.get("bales", 0) or 0)
                    st.session_state.emd_p = float(pending_edit.get("emd_percent", 5.0) or 5.0)
                    st.session_state.emd_d = int(pending_edit.get("emd_days", 365) or 365)

                    cd_s = pending_edit.get("cd_slabs", []) or []
                    for _n in range(1, 4):
                        _s = cd_s[_n-1] if len(cd_s) >= _n else {}
                        st.session_state[f"cd{_n}d"] = int(_s.get("days", 0) or 0)
                        st.session_state[f"cd{_n}p"] = float(_s.get("pct", 0.0) or 0.0)
                    st.session_state.cd_gst = float(pending_edit.get("cd_gst", 18.0) or 18.0)

                    ll_s = pending_edit.get("ll_slabs", []) or []
                    _ll_defaults = [(30, 0.50), (30, 0.75), (9999, 1.00)]
                    for _n, (_dd, _pp) in enumerate(_ll_defaults, 1):
                        _s = ll_s[_n-1] if len(ll_s) >= _n else {}
                        st.session_state[f"ll{_n}d"] = int(_s.get("days", _dd) or _dd)
                        st.session_state[f"ll{_n}p"] = float(_s.get("pct", _pp) or _pp)
                    st.session_state.ll_gst = float(pending_edit.get("ll_gst", 5.0) or 5.0)
                    st.session_state.ll_compound = "Applicable" if pending_edit.get("ll_compound") else "Not Applicable"

                    st.session_state.cc_free = int(pending_edit.get("cc_free_days", 0) or 0)
                    cc_s = pending_edit.get("cc_slabs", []) or []
                    _cc_defaults = [(30, 1.25), (30, 1.35)]
                    for _n, (_dd, _pp) in enumerate(_cc_defaults, 1):
                        _s = cc_s[_n-1] if len(cc_s) >= _n else {}
                        st.session_state[f"cc{_n}d"] = int(_s.get("days", _dd) or _dd)
                        st.session_state[f"cc{_n}p"] = float(_s.get("pct", _pp) or _pp)
                    st.session_state.cc_gst = float(pending_edit.get("cc_gst", 5.0) or 5.0)
                    st.session_state.cc_compound = "Applicable" if pending_edit.get("cc_compound") else "Not Applicable"

                c1, c2 = st.columns(2)
                cproj  = c1.selectbox("Project ✱", open_projs, key="cproj")
                cparty = c2.text_input("Party Name", key="cparty", placeholder="e.g. ABC Cotton Ltd.")
                c3, c4, c5 = st.columns(3)
                cno    = c3.text_input("Contract No ✱", key="cno", placeholder="e.g. RAY-110425")
                cgroup = c4.text_input("Group", key="cgroup", placeholder="e.g. GROUP-A")
                cdt    = c5.date_input("Contract Date", key="cdt", value=None)
                c6, c7 = st.columns(2)
                ceff   = c6.date_input("Effective Date", key="ceff", value=None)
                cbales = c7.number_input("Contracted Bales", key="cbales", min_value=0, value=0)

                st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
                st.markdown('<div class="sec-label">📌 EMD Slab</div>', unsafe_allow_html=True)
                e1, e2 = st.columns(2)
                emd_d = e1.number_input("Days", key="emd_d", min_value=0, value=365)
                emd_p = e2.number_input("Interest % p.a.", key="emd_p", min_value=0.0, value=5.0, step=0.01)

                st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
                with st.expander("💸 Cash Discount (CD) Slabs — Click to expand/collapse", expanded=False):
                    cd1a,cd1b = st.columns(2)
                    cd1d = cd1a.number_input("Slab 1 Days", key="cd1d", min_value=0, value=0)
                    cd1p = cd1b.number_input("Slab 1 %", key="cd1p", min_value=0.0, value=0.0, step=0.01)
                    cd2a,cd2b = st.columns(2)
                    cd2d = cd2a.number_input("Slab 2 Days", key="cd2d", min_value=0, value=0)
                    cd2p = cd2b.number_input("Slab 2 %", key="cd2p", min_value=0.0, value=0.0, step=0.01)
                    cd3a,cd3b = st.columns(2)
                    cd3d = cd3a.number_input("Slab 3 Days", key="cd3d", min_value=0, value=0)
                    cd3p = cd3b.number_input("Slab 3 %", key="cd3p", min_value=0.0, value=0.0, step=0.01)
                    cd_gst = st.number_input("CD GST %", key="cd_gst", min_value=0.0, value=18.0, step=0.01)

                st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
                with st.expander("⏰ Late Lifting (LL) Slabs — Click to expand/collapse", expanded=False):
                    ll1a,ll1b = st.columns(2)
                    ll1d = ll1a.number_input("Slab 1 Days", key="ll1d", min_value=0, value=30)
                    ll1p = ll1b.number_input("Slab 1 %/month", key="ll1p", min_value=0.0, value=0.50, step=0.01)
                    ll2a,ll2b = st.columns(2)
                    ll2d = ll2a.number_input("Slab 2 Days", key="ll2d", min_value=0, value=30)
                    ll2p = ll2b.number_input("Slab 2 %/month", key="ll2p", min_value=0.0, value=0.75, step=0.01)
                    ll3a,ll3b = st.columns(2)
                    ll3d = ll3a.number_input("Slab 3 Days", key="ll3d", min_value=0, value=9999)
                    ll3p = ll3b.number_input("Slab 3 %/month", key="ll3p", min_value=0.0, value=1.00, step=0.01)
                    ll_gst = st.number_input("LL GST %", key="ll_gst", min_value=0.0, value=5.0, step=0.01)
                    st.markdown("""
                    <div class="prorata-label">🔁 LL Compound Prorata Basis</div>
                    """, unsafe_allow_html=True)
                    st.markdown("""
                    <div class="prorata-hint">Applicable: Slab 2 charges on (Mat + Slab 1 charge), Slab 3 on cumulative. Not Applicable: Simple prorata per slab.</div>
                    """, unsafe_allow_html=True)
                    ll_compound = st.selectbox(
                        "LL Compound Prorata",
                        options=["Not Applicable", "Applicable"],
                        index=0,
                        key="ll_compound",
                        label_visibility="collapsed"
                    )

                st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
                with st.expander("🚛 Carrying Charges (CC) Slabs — Click to expand/collapse", expanded=False):
                    st.caption("📐 CC Days = Payment Date − (Effective Date + Free Period Days)  |  CC Days > 0 → Charges apply")
                    cc_free = st.number_input("CC Free Days — Contract Master",
                        key="cc_free", min_value=0, value=0,
                        help="This value is saved in this Contract Master and CC calculation uses this contract's value only. No DEFAULT/60-day fallback is used.")
                    cc1a,cc1b = st.columns(2)
                    cc1d = cc1a.number_input("Slab 1 Days", key="cc1d", min_value=0, value=30)
                    cc1p = cc1b.number_input("Slab 1 %/month", key="cc1p", min_value=0.0, value=1.25, step=0.01)
                    cc2a,cc2b = st.columns(2)
                    cc2d = cc2a.number_input("Slab 2 Days", key="cc2d", min_value=0, value=30)
                    cc2p = cc2b.number_input("Slab 2 %/month", key="cc2p", min_value=0.0, value=1.35, step=0.01)
                    cc_gst = st.number_input("CC GST %", key="cc_gst", min_value=0.0, value=5.0, step=0.01)
                    st.markdown("""
                    <div class="prorata-label">🔁 CC Compound Prorata Basis</div>
                    """, unsafe_allow_html=True)
                    st.markdown("""
                    <div class="prorata-hint">Applicable: Slab 2 charges on (Mat + Slab 1 charge). Not Applicable: Simple prorata per slab.</div>
                    """, unsafe_allow_html=True)
                    cc_compound = st.selectbox(
                        "CC Compound Prorata",
                        options=["Not Applicable", "Applicable"],
                        index=0,
                        key="cc_compound",
                        label_visibility="collapsed"
                    )

                st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)

                # Editing mode indicator
                editing_idx = st.session_state.edit_contract_idx
                if editing_idx is not None:
                    st.markdown(f'<div style="background:rgba(217,119,6,0.2);border:1px solid rgba(217,119,6,0.5);border-radius:8px;padding:8px 14px;color:#fbbf24;font-size:12px;font-weight:600;margin-bottom:10px">✏️ Editing Contract — changes will overwrite the existing record</div>', unsafe_allow_html=True)

                btn_col1, btn_col2 = st.columns([1, 1])
                with btn_col1:
                    save_label = "💾  Update Contract" if editing_idx is not None else "💾  Save Contract Master"
                    if st.button(save_label, type="primary", key="btn_cont", use_container_width=True):
                        if not st.session_state.cno:
                            st.error("❌ Contract No is required.")
                        else:
                            contract = {
                                "project": st.session_state.cproj,
                                "party": st.session_state.cparty,
                                "contract_no": st.session_state.cno,
                                "group": str(st.session_state.cgroup or "").strip(),
                                "contract_date": str(st.session_state.cdt) if st.session_state.cdt else "",
                                "effective_date": str(st.session_state.ceff) if st.session_state.ceff else "",
                                "bales": int(st.session_state.cbales),
                                "emd_days": int(st.session_state.emd_d),
                                "emd_percent": float(st.session_state.emd_p),
                                "cd_slabs": [s for s in [
                                    {"days":int(st.session_state.cd1d),"pct":float(st.session_state.cd1p)},
                                    {"days":int(st.session_state.cd2d),"pct":float(st.session_state.cd2p)},
                                    {"days":int(st.session_state.cd3d),"pct":float(st.session_state.cd3p)},
                                ] if s["days"]>0],
                                "cd_gst": float(st.session_state.cd_gst),
                                "ll_slabs": [
                                    {"days":int(st.session_state.ll1d),"pct":float(st.session_state.ll1p)},
                                    {"days":int(st.session_state.ll2d),"pct":float(st.session_state.ll2p)},
                                    {"days":int(st.session_state.ll3d),"pct":float(st.session_state.ll3p)},
                                ],
                                "ll_gst": float(st.session_state.ll_gst),
                                "ll_compound": st.session_state.ll_compound == "Applicable",
                                "cc_free_days": int(st.session_state.cc_free),
                                "cc_slabs": [
                                    {"days":int(st.session_state.cc1d),"pct":float(st.session_state.cc1p)},
                                    {"days":int(st.session_state.cc2d),"pct":float(st.session_state.cc2p)},
                                ],
                                "cc_gst": float(st.session_state.cc_gst),
                                "cc_compound": st.session_state.cc_compound == "Applicable",
                            }
                            if editing_idx is not None:
                                # UPDATE THE SAME CONTRACT RECORD.
                                # The original contract number is the stable identity,
                                # so editing never creates a second record.
                                original_no = st.session_state.get("editing_contract_no")
                                target_idx = None
                                for _i, _existing in enumerate(st.session_state.masters["contracts"]):
                                    if str(_existing.get("contract_no", "")).strip().upper() == str(original_no or "").strip().upper():
                                        target_idx = _i
                                        break

                                if target_idx is None:
                                    st.error("❌ Original contract record was not found. No new contract was created.")
                                else:
                                    # Prevent changing an edited contract into another existing number.
                                    new_no = str(contract.get("contract_no", "")).strip().upper()
                                    duplicate = any(
                                        _i != target_idx and
                                        str(_existing.get("contract_no", "")).strip().upper() == new_no
                                        for _i, _existing in enumerate(st.session_state.masters["contracts"])
                                    )
                                    if duplicate:
                                        st.error(f"❌ Contract No '{new_no}' already exists. Use that contract's Edit button instead.")
                                    else:
                                        st.session_state.masters["contracts"][target_idx] = contract
                                        st.session_state.edit_contract_idx = None
                                        st.session_state.editing_contract_no = None
                                        st.session_state.cont_msg = f"Contract '{contract['contract_no']}' updated successfully!"
                                        persist()
                                        st.rerun()
                            else:
                                # NEW CONTRACT: never silently overwrite an existing record.
                                new_no = str(contract.get("contract_no", "")).strip().upper()
                                duplicate = any(
                                    str(_existing.get("contract_no", "")).strip().upper() == new_no
                                    for _existing in st.session_state.masters["contracts"]
                                )
                                if duplicate:
                                    st.error(f"❌ Contract No '{new_no}' already exists. Click Edit on the existing contract to change it.")
                                else:
                                    st.session_state.masters["contracts"].append(contract)
                                    st.session_state.cont_msg = f"Contract '{contract['contract_no']}' saved to Firebase!"
                                    persist()
                                    st.rerun()
                with btn_col2:
                    if st.button("🗑️  Clear Fields", key="btn_clear_cont", use_container_width=True):
                        _saved_masters = st.session_state.masters
                        # Delete all widget keys so they reset to defaults on rerun
                        clear_keys = ["cparty","cno","cgroup","cbales","emd_d","emd_p",
                                      "cd1d","cd1p","cd2d","cd2p","cd3d","cd3p","cd_gst",
                                      "ll1d","ll1p","ll2d","ll2p","ll3d","ll3p","ll_gst",
                                      "ll_compound","cc_free","cc1d","cc1p","cc2d","cc2p",
                                      "cc_gst","cc_compound","cdt","ceff","cproj"]
                        for k in clear_keys:
                            if k in st.session_state: del st.session_state[k]
                        st.session_state.edit_contract_idx = None
                        st.session_state.editing_contract_no = None
                        st.session_state.masters = _saved_masters
                        st.rerun()

    # ── RIGHT PREVIEW ──
    with right_col:
        st.markdown('<div class="sec-label">🗂️ Saved Projects</div>', unsafe_allow_html=True)
        projs = st.session_state.masters["projects"]
        if not projs:
            st.caption("No projects saved yet.")
        else:
            for i, p in enumerate(projs):
                pill = '<span class="pill-open">OPEN</span>' if p["status"]=="open" else '<span class="pill-closed">CLOSED</span>'
                with st.container():
                    st.markdown(f"""
                    <div class="contract-card">
                      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
                        <span style="font-weight:700;color:#fff;font-size:14px">{p['name']}</span>
                        {pill}
                      </div>
                      <div style="font-size:12px;color:rgba(255,255,255,0.5)">
                        {p.get('session','')} &nbsp;|&nbsp; {p['from_period']} → {p['to_period']}
                      </div>
                    </div>""", unsafe_allow_html=True)
                    bc1, bc2, bc3 = st.columns(3)
                    tog = "🔒 Close" if p["status"]=="open" else "🔓 Open"
                    if bc1.button(tog, key=f"tpj{i}", use_container_width=True):
                        st.session_state.masters["projects"][i]["status"] = "closed" if p["status"]=="open" else "open"
                        persist(); st.rerun()
                    if bc2.button("✏️ Extend", key=f"epj{i}", use_container_width=True):
                        st.session_state[f"edit_proj_{i}"] = True
                    if bc3.button("🗑 Delete", key=f"dpj{i}", use_container_width=True):
                        st.session_state.masters["projects"].pop(i); persist(); st.rerun()
                    if st.session_state.get(f"edit_proj_{i}"):
                        nd = st.date_input("New To Date", key=f"nd_{i}")
                        if st.button("✅ Save", key=f"spj{i}"):
                            st.session_state.masters["projects"][i]["to_period"] = str(nd)
                            persist(); st.session_state[f"edit_proj_{i}"] = False; st.rerun()

        st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sec-label">📋 Contract Masters</div>', unsafe_allow_html=True)
        conts = st.session_state.masters["contracts"]
        if not conts:
            st.caption("No contracts saved yet.")
        else:
            for i, c in enumerate(conts):
                _cd_slabs = c.get('cd_slabs', []) or []
                _ll_slabs = c.get('ll_slabs', []) or []
                _cc_slabs = c.get('cc_slabs', []) or []
                _ll_cpd = '✓' if c.get('ll_compound') else '✗'
                _cc_cpd = '✓' if c.get('cc_compound') else '✗'

                def _slab_txt(slabs, suffix=""):
                    if not slabs: return "—"
                    return "  |  ".join([f"S{n}: {s.get('days',0)}d @ {s.get('pct',0)}%{suffix}" for n,s in enumerate(slabs,1)])

                st.markdown(f"""
                <div style="background:#fff;border:1px solid #e2e8f0;border-left:3px solid #c2410c;border-radius:8px;padding:8px 12px;margin-bottom:6px;font-size:11.5px;color:#374151">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                    <span style="font-weight:700;font-size:13px;color:#c2410c">{c.get('contract_no','—')}</span>
                    <span style="color:#64748b;font-size:11px">{c.get('project','—')}</span>
                  </div>
                  <div style="color:#475569;margin-bottom:3px">{c.get('party','—')}</div>
                  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:2px 10px;color:#64748b;font-size:11px;margin-bottom:4px">
                    <span>📅 Eff: <b>{c.get('effective_date','—')}</b></span>
                    <span>🌾 Bales: <b>{c.get('bales','—')}</b></span>
                    <span>💰 EMD: <b>{c.get('emd_percent','—')}% pa</b></span>
                    <span>🚛 CC Free: <b>{c.get('cc_free_days',0)}d</b></span>
                    <span>📌 EMD Days: <b>{c.get('emd_days','—')}</b></span>
                    <span>🏢 CC GST: <b>{c.get('cc_gst',0)}%</b></span>
                  </div>
                  <div style="font-size:10.5px;color:#64748b;border-top:1px solid #f1f5f9;padding-top:3px;display:grid;grid-template-columns:1fr;gap:1px">
                    <span>💸 CD: {_slab_txt(_cd_slabs)} | GST: {c.get('cd_gst',0)}%</span>
                    <span>⏰ LL: {_slab_txt(_ll_slabs,'/mo')} | GST: {c.get('ll_gst',0)}% | Cpd: {_ll_cpd}</span>
                    <span>🚛 CC: {_slab_txt(_cc_slabs,'/mo')} | Cpd: {_cc_cpd}</span>
                  </div>
                </div>
                """, unsafe_allow_html=True)

                cc_b1, cc_b2 = st.columns(2)
                if cc_b1.button("✏️ Edit Contract", key=f"ec{i}", use_container_width=True):
                    cx = st.session_state.masters["contracts"][i]
                    st.session_state.edit_contract_idx = i
                    st.session_state.editing_contract_no = str(cx.get("contract_no", "")).strip()
                    st.session_state["_pending_contract_edit"] = dict(cx)
                    st.rerun()
                if cc_b2.button("🗑 Delete Contract", key=f"dc{i}", use_container_width=True):
                    if st.session_state.edit_contract_idx == i:
                        st.session_state.edit_contract_idx = None
                        st.session_state.editing_contract_no = None
                    st.session_state.masters["contracts"].pop(i)
                    persist()
                    st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: UPLOAD & CALCULATE
# ══════════════════════════════════════════════════════════════════════════════
with tab_upload:
    col_u1, col_u2 = st.columns([1.2, 0.8], gap="large")
    with col_u1:
        st.markdown('<div class="sec-label">📋 Select Contract Master</div>', unsafe_allow_html=True)
        contracts = st.session_state.masters["contracts"]
        if not contracts:
            st.warning("⚠️ No contracts saved. Please add a contract in the Masters tab.")
        else:
            cont_opts = {f"{c['contract_no']} | {c.get('party','')}": c for c in contracts}
            filter_options = [""] + list(cont_opts.keys())
            sel_lbl = st.selectbox(
                "Contract Filter",
                filter_options,
                index=0,
                format_func=lambda x: "— Blank / Use Upload Contract & Group —" if x == "" else x
            )
            sel_mc = cont_opts.get(sel_lbl) if sel_lbl else None
            mc = sel_mc
            if mc:
                st.markdown(f"""
                <div class="contract-card" style="margin-bottom:14px">
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px;color:rgba(255,255,255,0.6)">
                    <span>📌 EMD Rate: <strong style="color:#fdba74">{mc.get('emd_percent','—')}% p.a.</strong></span>
                    <span>🌾 Bales: <strong style="color:#fff">{mc.get('bales','—')}</strong></span>
                    <span>🚛 CC Free: <strong style="color:#fdba74">{mc.get('cc_free_days',0)} days</strong></span>
                    <span>📅 Eff Date: <strong style="color:#fff">{mc.get('effective_date','—')}</strong></span>
                  </div>
                </div>""", unsafe_allow_html=True)
            else:
                st.info("Blank filter: every uploaded contract is matched individually as Contract → Group → DEFAULT.")

            st.markdown('<div class="sec-label">📂 Upload Excel File</div>', unsafe_allow_html=True)
            st.caption("📋 Excel must have 3 sheets: PUR CONT DETAILS  |  EMD PAYMENT DETAILS  |  GRN BOOKING")
            uploaded_file = st.file_uploader("Choose Excel file (.xlsx)", type=["xlsx","xls"], label_visibility="collapsed")

            if uploaded_file:
                st.markdown(f'<div class="success-msg">📎 {uploaded_file.name} — ready to calculate</div>', unsafe_allow_html=True)
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("⚙️  Run Calculation", type="primary", use_container_width=True):
                    with st.spinner("⏳ Processing calculations..."):
                        try:
                            fb = uploaded_file.read()
                            cont_df, emd_df, pay_df, grn_df = parse_excel(fb)
                            result_df  = run_calculations(cont_df, emd_df, pay_df, grn_df, contracts)
                            excel_bytes = df_to_excel_bytes(result_df, cont_df, emd_df, pay_df, grn_df)
                            st.session_state["result_df"]   = result_df
                            st.session_state["pay_df"]      = pay_df
                            st.session_state["excel_bytes"] = excel_bytes
                            st.markdown(f'<div class="success-msg">✅ {len(result_df)} GRNs calculated successfully! Switch to Results tab.</div>', unsafe_allow_html=True)
                        except Exception as e:
                            import traceback
                            st.error(f"❌ {e}")
                            st.code(traceback.format_exc())

    with col_u2:
        st.markdown('<div class="sec-label">📝 Expected Excel Format</div>', unsafe_allow_html=True)
        for title, cols in [
            ("Sheet 1 — PUR CONT DETAILS", "Contract No. | EFFECTIVE DATE | BALES | BRANCH-CCI | GROUP"),
            ("Sheet 2 — EMD PAYMENT DETAILS", "Contract No. | EMD DATE | EMD AMOUNT | [blank] | Contract No. | MODE OF TRANSACTION | PAYMENT DATE | PAYMENT AMOUNT"),
            ("Sheet 3 — GRN BOOKING", "contract no | Party Bill Date | GRN | Accepted Qty(AUM) | Accepted Qty | Material Amount | IGST | Party Bill Amount | Other Amount | FINAL INDENT DATE"),
        ]:
            st.markdown(f"""
            <div class="contract-card">
              <div style="font-size:11px;font-weight:700;color:#fdba74;margin-bottom:6px">{title}</div>
              <div style="font-size:11px;color:rgba(255,255,255,0.5);font-family:monospace">{cols}</div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: RESULTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_results:
    if "result_df" not in st.session_state or st.session_state["result_df"] is None:
        st.info("📭 No results yet. Upload an Excel file and run calculations.")
    else:
        df = st.session_state["result_df"]
        _pay_df_ui = st.session_state.get("pay_df", None)
        tot_bill = df["Total_Bill_Amount"].sum()
        tot_emd  = df["EMD_Interest"].sum()
        tot_cd   = df["Cash_Discount"].sum()
        tot_ll   = df["Late_Lifting_Chg"].sum()
        tot_cc   = df["Carry_Charges"].sum()
        tot_grns = len(df)

        st.markdown(f"""
        <div class="metric-row">
          <div class="metric-card blue">
            <div class="metric-icon">📋</div>
            <div class="metric-val">{tot_grns}</div>
            <div class="metric-lbl">Total GRNs</div>
          </div>
          <div class="metric-card green">
            <div class="metric-icon">💰</div>
            <div class="metric-val">{fmt_inr(tot_bill, decimals=0)}</div>
            <div class="metric-lbl">Total Bill Amt</div>
          </div>
          <div class="metric-card teal">
            <div class="metric-icon">📈</div>
            <div class="metric-val">{fmt_inr(tot_emd, decimals=0)}</div>
            <div class="metric-lbl">EMD Interest</div>
          </div>
          <div class="metric-card purple">
            <div class="metric-icon">💸</div>
            <div class="metric-val">{fmt_inr(tot_cd, decimals=0)}</div>
            <div class="metric-lbl">Cash Discount</div>
          </div>
          <div class="metric-card red">
            <div class="metric-icon">⏰</div>
            <div class="metric-val">{fmt_inr(tot_ll, decimals=0)}</div>
            <div class="metric-lbl">Late Lifting</div>
          </div>
          <div class="metric-card orange">
            <div class="metric-icon">🚛</div>
            <div class="metric-val">{fmt_inr(tot_cc, decimals=0)}</div>
            <div class="metric-lbl">Carry Charges</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        if "excel_bytes" in st.session_state:
            st.download_button(
                label="⬇️  Download Excel Report",
                data=st.session_state["excel_bytes"],
                file_name="CCI_Calculation_Output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary"
            )

        st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sec-label">📊 GRN-Wise Detail</div>', unsafe_allow_html=True)

        disp = df.copy()
        # Branch is shown immediately after Contract No.
        if "Branch" in disp.columns:
            _cols = list(disp.columns)
            _cols.remove("Branch")
            _pos = _cols.index("Contract_No") + 1 if "Contract_No" in _cols else 0
            _cols.insert(_pos, "Branch")
            disp = disp[_cols]
        # Build dynamic slab columns from _cc_slab_breakdown (same logic as Excel export)
        if "_cc_slab_breakdown" in disp.columns:
            max_slabs = disp["_cc_slab_breakdown"].apply(lambda x: len(x) if isinstance(x, list) else 0).max()
            for i in range(max_slabs):
                try:
                    lbl = disp["_cc_slab_breakdown"].dropna().iloc[0][i][0]
                except Exception:
                    d_from = i * 30 + 1; lbl = f"{d_from}-{d_from+29}"
                col = f"CC_Slab{i+1}_{lbl}"
                disp[col] = disp["_cc_slab_breakdown"].apply(
                    lambda x: x[i][1] if isinstance(x, list) and len(x) > i else 0.0
                )
        # Drop internal list column — PyArrow cannot serialize Python lists
        disp = disp.drop(columns=[c for c in disp.columns if c.startswith("_")], errors="ignore")
        for col in ["Effective_Date","Party_Bill_Date","EMD_Date","Payment_Date","CD_Due_Date","CC_Free_End"]:
            if col in disp.columns:
                disp[col] = disp[col].apply(fmt_date)
        for col in ["Material_Amount","GST_On_Material","Total_Bill_Amount","Payment_Amount",
                    "Per_Bale_EMD","EMD_Allocated","Net_Amount","EMD_Interest",
                    "Cash_Discount","Late_Lifting_Chg","Late_Lifting_GST","Carry_Charges","Carry_GST"]:
            disp[col] = disp[col].apply(lambda x: fmt_inr(x))

        # Clear GRN detail totals row (all numeric amounts and bales).
        detail_total = {c: "" for c in disp.columns}
        detail_total["Contract_No"] = "GRAND TOTAL"
        for c in ["Bales","Material_Amount","GST_On_Material","Total_Bill_Amount",
                  "Payment_Amount","EMD_Allocated","EMD_Interest","Cash_Discount",
                  "Late_Lifting_Chg","Late_Lifting_GST","Carry_Charges","Carry_GST"]:
            if c in disp.columns:
                vals = pd.to_numeric(df[c], errors="coerce")
                detail_total[c] = fmt_inr(vals.sum()) if c != "Bales" else int(vals.sum())
        disp = pd.concat([disp, pd.DataFrame([detail_total])], ignore_index=True)
        st.dataframe(pretty_columns(disp), use_container_width=True, height=440, hide_index=True)

        st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sec-label">📋 Contract-wise Summary</div>', unsafe_allow_html=True)
        summary = df.groupby("Contract_No").agg(
            GRNs=("GRN_No","count"), Bales=("Bales","sum"),
            Material=("Material_Amount","sum"), GST=("GST_On_Material","sum"),
            Total_Bill=("Total_Bill_Amount","sum"), Payment=("Payment_Amount","sum"),
            EMD_Alloc=("EMD_Allocated","sum"), EMD_Interest=("EMD_Interest","sum"),
            Cash_Disc=("Cash_Discount","sum"), LL_Chg=("Late_Lifting_Chg","sum"),
            LL_GST=("Late_Lifting_GST","sum"), CC_Chg=("Carry_Charges","sum"),
            CC_GST=("Carry_GST","sum"),
        ).reset_index()
        # Shortage / Excess (original, simple) = Total Bill − Payment − EMD Allocated
        # Positive → SHORTAGE (payment still pending)  |  Negative → EXCESS (overpaid, refund due)
        summary["Shortage_Excess"] = summary["Total_Bill"] - summary["Payment"] - summary["EMD_Alloc"]
        summary["Shortage_Excess_Mark"] = summary["Shortage_Excess"].apply(
            lambda x: "SHORTAGE" if pd.notna(x) and float(x) > 0
            else ("EXCESS" if pd.notna(x) and float(x) < 0 else "CLEAR")
        )
        # Total amount WE owe CCI (the vendor) = base bill + charges WE pay to
        # CCI (Late Lifting, Carrying) − amounts WE receive from CCI (Cash
        # Discount, interest on our EMD deposit).
        summary["Total_Payable"] = (
            summary["Total_Bill"] + summary["LL_Chg"] + summary["LL_GST"]
            + summary["CC_Chg"] + summary["CC_GST"]
            - summary["Cash_Disc"] - summary["EMD_Interest"]
        )
        # Actual payment from uploaded Payment sheet per contract (for Rec/Pay only)
        if _pay_df_ui is not None:
            _pay_cn_map_ui = _pay_df_ui.groupby(_pay_df_ui["Contract_No"].astype(str).str.strip().str.upper())["Payment_Amount"].sum()
            summary["Actual_Payment_Total"] = summary["Contract_No"].astype(str).str.strip().str.upper().map(_pay_cn_map_ui).fillna(0)
        else:
            summary["Actual_Payment_Total"] = summary["Payment"]
        # Receivable / Payable = Total Payable − Actual Payment (uploaded sheet) − EMD Allocated
        # Positive → PAYABLE (still owe to CCI)  |  Negative → RECEIVABLE (CCI owes refund)
        summary["Receivable_Payable"] = summary["Total_Payable"] - summary["Actual_Payment_Total"] - summary["EMD_Alloc"]
        summary["Receivable_Payable_Mark"] = summary["Receivable_Payable"].apply(
            lambda x: "PAYABLE" if pd.notna(x) and float(x) > 0
            else ("RECEIVABLE" if pd.notna(x) and float(x) < 0 else "CLEAR")
        )
        branch_map_ui = None
        # Branch mapping is preserved with the uploaded PUR CONT DETAILS data.
        if "Branch" in df.columns:
            branch_map_ui = df.drop_duplicates("Contract_No").set_index("Contract_No")["Branch"].to_dict()
            summary.insert(1, "Branch", summary["Contract_No"].map(branch_map_ui).fillna(""))
        summary_total = {c: "" for c in summary.columns}
        summary_total["Contract_No"] = "GRAND TOTAL"
        _skip_ui = {"Contract_No", "Branch", "Shortage_Excess_Mark", "Receivable_Payable_Mark"}
        for c in summary.columns:
            if c not in _skip_ui:
                summary_total[c] = pd.to_numeric(summary[c], errors="coerce").sum()
        _total_se_ui = summary_total.get("Shortage_Excess", 0)
        summary_total["Shortage_Excess_Mark"] = (
            "SHORTAGE" if pd.notna(_total_se_ui) and float(_total_se_ui) > 0
            else ("EXCESS" if pd.notna(_total_se_ui) and float(_total_se_ui) < 0 else "CLEAR")
        )
        _total_rp_ui = summary_total.get("Receivable_Payable", 0)
        summary_total["Receivable_Payable_Mark"] = (
            "PAYABLE" if pd.notna(_total_rp_ui) and float(_total_rp_ui) > 0
            else ("RECEIVABLE" if pd.notna(_total_rp_ui) and float(_total_rp_ui) < 0 else "CLEAR")
        )
        summary = pd.concat([summary, pd.DataFrame([summary_total])], ignore_index=True)
        _summary_money_cols = [c for c in summary.columns if c not in ("Contract_No","Branch","GRNs","Bales","Shortage_Excess_Mark","Receivable_Payable_Mark")]
        for c in _summary_money_cols:
            summary[c] = summary[c].apply(lambda x: fmt_inr(x, dash_on_zero=False))
        for _cnt_col in ("Bales", "GRNs"):
            if _cnt_col in summary.columns:
                summary[_cnt_col] = summary[_cnt_col].apply(lambda x: "" if x == "" else int(pd.to_numeric(x, errors="coerce") or 0))
        st.dataframe(pretty_columns(summary), use_container_width=True, hide_index=True)

        st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sec-label">🏢 Branch-wise Summary</div>', unsafe_allow_html=True)
        branch_summary_ui = branch_wise_summary(df, pay=_pay_df_ui).copy()
        if not branch_summary_ui.empty:
            _branch_money_cols = [c for c in branch_summary_ui.columns if c not in ("Branch","Contracts","GRNs","Total_Bales","Receivable_Payable_Mark")]
            for c in _branch_money_cols:
                branch_summary_ui[c] = branch_summary_ui[c].apply(lambda x: fmt_inr(x, dash_on_zero=False))
            for _cnt_col in ("Contracts", "GRNs", "Total_Bales"):
                if _cnt_col in branch_summary_ui.columns:
                    branch_summary_ui[_cnt_col] = branch_summary_ui[_cnt_col].apply(lambda x: "" if x == "" else int(pd.to_numeric(x, errors="coerce") or 0))
            st.dataframe(pretty_columns(branch_summary_ui), use_container_width=True, hide_index=True)
        else:
            st.info("Branch data not available for this upload.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: FORMULA GUIDE
# ══════════════════════════════════════════════════════════════════════════════
with tab_help:
    st.markdown("""
    <style>
    .fg-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .fg-card { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:10px 13px; }
    .fg-title { font-size:11px; font-weight:700; color:#c2410c; text-transform:uppercase; letter-spacing:.07em; margin-bottom:5px; }
    .fg-line { font-size:11.5px; color:#374151; font-family:'Courier New',monospace; line-height:1.65; margin:0; }
    .fg-note { font-size:10.5px; color:#6b7280; margin-top:4px; line-height:1.5; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="sec-label" style="margin-bottom:8px">📖 Formula Reference</div>', unsafe_allow_html=True)

    # Formula Guide kept aligned with the actual run_calculations() logic.
    formula_cards = [
        ("📥 Data & Date Sources", [
            "Effective Date = PUR CONT DETAILS (uploaded Excel) per Contract No.",
            "Branch = PUR CONT DETAILS (uploaded Excel) per Contract No.",
            "Group = PUR CONT DETAILS (uploaded Excel) per Contract No.",
            "Master priority = exact Contract No > Group > DEFAULT.",
            "GST on Material = IGST from GRN BOOKING row.",
            "Party Bill / Lifting Date = Party Bill Date from GRN BOOKING.",
            "CC Free Days / slabs / GST / compound flag = matched Contract Master.",
        ], "PUR CONT DETAILS is the authoritative source for Effective Date and Branch."),
        ("📌 Per Bale EMD & FIFO Allocation", [
            "Per Bale EMD = Contract Total EMD Amount ÷ Contracted Bales.",
            "EMD Required for GRN = Per Bale EMD × GRN Bales (Accepted Qty AUM).",
            "EMD Allocation = FIFO draw from EMD vouchers for that Contract until the GRN requirement is met.",
            "EMD Date = latest EMD voucher date used for that GRN.",
        ], "Each EMD voucher keeps a Remaining balance for subsequent GRNs."),
        ("💰 EMD Interest", [
            "EMD Days = Payment Date − EMD Date.",
            "EMD Interest = EMD Allocated × (EMD Rate % ÷ 365) × EMD Days.",
        ], "Calculated only when EMD Date, Payment Date and EMD allocation are available."),
        ("💵 Bill & Net Amount", [
            "GST on Material = IGST from GRN Booking.",
            "Total Bill Amount = Material Amount + GST on Material.",
            "Net Amount = Total Bill Amount − EMD Allocated.",
        ], "Material Amount and IGST come from the uploaded GRN Booking data."),
        ("💳 Payment Allocation", [
            "Payment is allocated FIFO from Final Payments against the Contract.",
            "Payment allocation stops when Net Amount is fully covered.",
            "Payment Date = latest payment date actually used for that GRN.",
            "Mode of Transaction = mode belonging to that latest applied payment.",
        ], "Each payment voucher keeps a Remaining balance for subsequent GRNs."),
        ("💸 Cash Discount (CD)", [
            "CD Due Date = Effective Date + highest valid CD slab days.",
            "Eligibility = Payment Date ≤ CD Due Date.",
            "CD Days = CD Due Date − Payment Date.",
            "CD % = percentage of the highest-days valid CD slab.",
            "Cash Discount = Material Amount × CD % × (CD Days ÷ 365).",
            "Payment after CD Due Date → Cash Discount = 0.",
        ], "Effective Date is from PUR CONT DETAILS; CD slab days and % are from Contract Master."),
        ("⏰ Late Lifting Charges (LL)", [
            "Free Period End = Payment Date + 15 days.",
            "Late Lift Days = Lifting Date − Free Period End (only when positive).",
            "Each slab charge = applicable base × slab % × (days in slab ÷ 30).",
            "Compound = ON → next slab uses Principal + all prior slab charges as running base.",
            "Compound = OFF → every slab uses the original Material Amount as base.",
            "Late Lifting GST = Late Lifting Charges × LL GST % ÷ 100.",
        ], "LL slab days, percentages, GST and compound setting come from Contract Master; no fixed slab rate is displayed."),
        ("🚛 Carrying Charges (CC)", [
            "CC Free End = Effective Date + CC Free Days from Contract Master.",
            "CC Days = Payment Date − CC Free End (only when positive).",
            "Slab 1 charge = Material Amount × Slab 1 % × (days ÷ 30).",
            "Slab 2+ charge = Running Amount × applicable slab % × (days ÷ 30).",
            "Running Amount = Principal + all previous slab charges (compounding).",
            "CC continues through unlimited 30-day slabs until all CC Days are consumed.",
            "Carrying GST = Carrying Charges × CC GST % ÷ 100.",
        ], "CC Free Days, slab days, percentages, GST and compound setting come from Contract Master. No fixed 60-day CC free period is used."),
        ("📊 Shortage / Excess", [
            "Shortage / Excess = Total Bill − Total Payment − Total EMD Allocated.",
            "Positive → SHORTAGE (payment still pending).",
            "Negative → EXCESS (overpaid / refund due).",
            "Zero → CLEAR.",
        ], "This intentionally excludes LL, CC, CD and EMD Interest."),
        ("💼 Total Payable", [
            "Total Payable = Total Bill + Total LL + Total LL GST + Total CC + Total CC GST − Total Cash Discount − Total EMD Interest.",
        ], "Charges are added; Cash Discount and EMD Interest are deducted."),
        ("↔️ Receivable / Payable", [
            "Receivable / Payable = Total Payable − (Total Payment + Total EMD Allocated).",
            "Positive → PAYABLE (amount still owed to CCI).",
            "Negative → RECEIVABLE (amount refundable / owed by CCI).",
            "Zero → CLEAR.",
        ], "Same logic is used for Contract-wise and Branch-wise summaries."),
        ("🏢 Branch-wise Summary", [
            "Contracts = distinct Contract No. per Branch.",
            "GRNs = count of calculated GRN rows per Branch.",
            "Bales, Material, GST, Bill, Payment, EMD, Interest, CD, LL and CC totals = sum of GRN-level values.",
            "Receivable / Payable uses the same Total Payable formula as Contract Summary.",
        ], "Branch is sourced from PUR CONT DETAILS and then aggregated after calculation."),
    ]

    cards_html = []
    for title, lines, note in formula_cards:
        rows = ''.join(f'<div class="fg-line">{line}</div>' for line in lines)
        cards_html.append(f'<div class="fg-card"><div class="fg-title">{title}</div>{rows}<div class="fg-note">{note}</div></div>')

    st.markdown('<div class="fg-grid">' + ''.join(cards_html) + '</div>', unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: USER MASTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_users:
    current_user = st.session_state.get("_logged_user", "")
    if not _has_right("user_master") or st.session_state.get("_user_role") not in ("Firm Admin", "Admin", "Manager"):
        st.warning("⚠️ Only the firm's authorised User Master role can manage users inside this firm.")
    else:
        st.markdown('<div class="sec-label">👤 User Management</div>', unsafe_allow_html=True)
        st.caption("Add, edit or delete application users. Admin account cannot be deleted.")

        all_users = _load_users()

        if st.button("🔑 Generate New 12-Character User Key", key="generate_user_key"):
            st.session_state["new_user_key"] = _generate_key(12)
            st.success(f"Generated User Key: {st.session_state['new_user_key']}")
        with st.expander("➕  Add New User", expanded=False):
            ua1, ua2 = st.columns(2)
            new_uname = ua1.text_input("Username ✱", key="new_uname", placeholder="e.g. user1")
            new_upass = ua2.text_input("Password ✱", key="new_upass", type="password", placeholder="Min 6 chars")
            ua3, ua4 = st.columns(2)
            new_umobile = ua3.text_input("Mobile No.", key="new_umobile", placeholder="e.g. 9876543210")
            new_uemail = ua4.text_input("Email ID", key="new_uemail", placeholder="e.g. user@example.com")
            ua5, ua6 = st.columns(2)
            new_user_key = ua5.text_input("12-Character User Key ✱", key="new_user_key", placeholder="Paste generated 12-character key")
            new_user_role = ua6.selectbox("Role", ["User", "Manager", "Viewer"], key="new_user_role")
            rights_options = ["masters","upload","results","help","user_master"]
            new_user_rights = st.multiselect("Rights within this Firm", rights_options, default=["upload","results"], key="new_user_rights")
            firm_now = (_load_registry().get("firms", {}).get(st.session_state.get("_firm_id", ""), {}) or {})
            base_users = int(firm_now.get("included_users", _system_policy()["included_users"]) or _system_policy()["included_users"])
            included_limit = base_users + int(firm_now.get("extra_users", 0) or 0)
            st.caption(f"Included users: {base_users} + paid extra users: {int(firm_now.get('extra_users',0) or 0)} = {included_limit} total.")
            if st.button("💾  Add User", type="primary", key="btn_add_user"):
                nu = st.session_state.new_uname.strip().lower()
                np = st.session_state.new_upass.strip()
                nm = st.session_state.new_umobile.strip()
                ne = st.session_state.new_uemail.strip()
                if not nu or not np:
                    st.error("❌ Username and Password are required.")
                elif len(np) < 6:
                    st.error("❌ Password must be at least 6 characters.")
                elif nu in all_users:
                    st.error(f"❌ Username '{nu}' already exists.")
                elif len(all_users) >= included_limit:
                    st.error(f"❌ User limit reached ({included_limit}). Please pay for additional users before creating another user.")
                elif len(new_user_key.strip()) != 12:
                    st.error("❌ User Key must be exactly 12 characters.")
                elif not new_user_key.strip():
                    st.error("❌ User Key is required.")
                else:
                    all_users[nu] = {"password": np, "mobile": nm, "email": ne, "user_key": new_user_key.strip(), "user_key_hash": _hash_secret(new_user_key.strip()), "role": new_user_role, "rights": new_user_rights, "active": True}
                    if _save_users(all_users):
                        _audit("USER_CREATED", st.session_state.get("_firm_id",""), current_user, {"new_user":nu,"role":new_user_role})
                        st.success(f"✅ User '{nu}' added successfully!")
                        st.rerun()

        st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sec-label">📋 Registered Users</div>', unsafe_allow_html=True)

        # Load login history. Show a real error instead of silently hiding it.
        login_hist = {}
        login_hist_error = ""
        try:
            doc = db.collection("cci_utility").document("login_history").get()
            if doc.exists:
                raw_hist = doc.to_dict().get("history", {}) or {}
                if isinstance(raw_hist, dict):
                    login_hist = raw_hist
            else:
                login_hist = {}
        except Exception as e:
            login_hist_error = str(e)

        if login_hist_error:
            st.error(f"⚠️ Firebase Login History read error: {login_hist_error}")

        for uname in list(all_users.keys()):
            is_admin = uname == "admin"
            udata = all_users[uname] if isinstance(all_users[uname], dict) else {"password": all_users[uname], "mobile": "", "email": ""}
            umob = str(udata.get("mobile", ""))
            uemail = str(udata.get("email", ""))
            _user_hist = login_hist.get(uname, [])
            if not isinstance(_user_hist, list):
                _user_hist = [str(_user_hist)] if _user_hist else []

            with st.container(border=True):
                h1, h2 = st.columns([3.2, 1.2])
                with h1:
                    badge = " 👑 ADMIN" if is_admin else ""
                    st.markdown(f"**{uname}**{badge}")
                    contact = " · ".join(x for x in [f"📱 {umob}" if umob else "", f"✉️ {uemail}" if uemail else ""] if x)
                    st.caption(contact if contact else "No contact info")
                    st.caption(f"Role: {udata.get('role','User')} · Rights: {', '.join(udata.get('rights',[]) or []) or 'None'} · User Key: {udata.get('user_key','—')}")
                with h2:
                    if _user_hist:
                        st.caption(f"🕘 {len(_user_hist)} login record(s)")
                    else:
                        st.caption("No login history recorded yet")

                if _user_hist:
                    with st.expander("Recent Login History", expanded=False):
                        for h in list(_user_hist)[-10:][::-1]:
                            st.write(f"🕐 {h}")

                p1, p2, p3 = st.columns([2, 1, 1])
                new_pass_key = f"chpass_{uname}"
                new_p = p1.text_input("New password", key=new_pass_key, type="password", placeholder="Enter new password to change", label_visibility="collapsed")
                if p2.button("🔑 Change Password", key=f"chpbtn_{uname}", use_container_width=True):
                    np2 = st.session_state.get(new_pass_key, "").strip()
                    if len(np2) < 6:
                        st.error(f"❌ Password must be at least 6 characters for '{uname}'.")
                    else:
                        udata["password"] = np2
                        all_users[uname] = udata
                        if _save_users(all_users):
                            st.success(f"✅ Password changed for '{uname}'!")
                            st.rerun()
                if not is_admin:
                    if p3.button("🗑 Delete", key=f"delbtn_{uname}", use_container_width=True):
                        del all_users[uname]
                        if _save_users(all_users):
                            st.success(f"✅ User '{uname}' deleted.")
                            st.rerun()
                else:
                    p3.caption("🔒 Admin protected")

                c1, c2, c3 = st.columns([2, 2, 1])
                mob_key = f"mob_{uname}"
                email_key = f"eml_{uname}"
                c1.text_input("📱 Mobile", key=mob_key, value=umob, placeholder="Mobile no.", label_visibility="collapsed")
                c2.text_input("✉️ Email", key=email_key, value=uemail, placeholder="Email ID", label_visibility="collapsed")
                if c3.button("💾 Update", key=f"updbtn_{uname}", use_container_width=True):
                    udata["mobile"] = st.session_state.get(mob_key, "").strip()
                    udata["email"] = st.session_state.get(email_key, "").strip()
                    all_users[uname] = udata
                    if _save_users(all_users):
                        st.success(f"✅ Contact info updated for '{uname}'!")
                        st.rerun()

        st.markdown('<div class="sv-divider"></div>', unsafe_allow_html=True)
        st.markdown(f"""
        <div style="background:#fff7ed;border:1px solid #fdba74;border-radius:10px;padding:12px 16px;font-size:12px;color:#78716c">
        🔒 Currently logged in as: <strong style="color:#c2410c">{current_user}</strong> &nbsp;·&nbsp; Total users: <strong style="color:#1c1917">{len(all_users)}</strong>
        </div>""", unsafe_allow_html=True)

# ── FOOTER ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="sv-footer">
  Powered by <strong style="color:#c2410c">Softview Technologies</strong> &nbsp;·&nbsp; CCI Working Calculation Utility v2.0
</div>
""", unsafe_allow_html=True)
