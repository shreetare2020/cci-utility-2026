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
    # Premium login screen.  IMPORTANT: Streamlit widgets are deliberately
    # kept OUTSIDE the raw HTML shell so they render as real controls instead
    # of appearing as literal <div> markup in the browser.
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"]{
        background:linear-gradient(135deg,#eef4fb 0%,#f8fbff 48%,#e8f0f8 100%);
    }
    [data-testid="stHeader"]{background:transparent}
    .block-container{max-width:1400px!important;padding:.35rem .75rem .55rem!important}
    .sv-wrap{
        border:1px solid #d9e3ef;border-radius:26px;overflow:hidden;
        background:#f7fbff;box-shadow:0 24px 65px rgba(5,27,63,.18);
    }
    .sv-header{
        min-height:82px;padding:9px 27px;display:flex;align-items:center;justify-content:space-between;
        background:linear-gradient(110deg,#061a3d 0%,#0b2f68 52%,#eaf2fa 52.2%,#f8fbff 100%);
        border-bottom:2px solid #d9a93b;
    }
    .sv-brand{display:flex;align-items:center;gap:14px}
    .sv-logo-box{width:61px;height:61px;border-radius:11px;background:#fff;padding:5px;box-shadow:0 8px 22px rgba(0,0,0,.20);display:flex;align-items:center;justify-content:center}
    .sv-logo-box img{width:100%;height:100%;object-fit:contain;border-radius:7px}
    .sv-brand-text{color:#fff}
    .sv-brand-title{font-size:25px;font-weight:950;letter-spacing:1px;line-height:1}
    .sv-brand-title span{color:#f0c861}
    .sv-brand-sub{font-size:9px;letter-spacing:2.4px;color:#aebfd7;margin-top:7px;font-weight:800;text-transform:uppercase}
    .sv-trust{padding:10px 18px;border-radius:999px;background:#082451;color:#fff;border:1px solid rgba(217,169,59,.6);font-size:11px;font-weight:800;box-shadow:0 8px 18px rgba(4,25,56,.15)}
    .sv-trust span{color:#f0c861;padding:0 5px}
    .sv-content{padding:25px 28px 24px;background:linear-gradient(135deg,#09265a 0%,#0b316d 44%,#f4f8fc 44.2%,#f8fbff 100%)}
    .sv-left-title{color:#fff;font-size:38px;font-weight:950;line-height:1.04;margin:5px 0 0;letter-spacing:-.7px}
    .sv-left-title em{font-style:normal;color:#f0c861}
    .sv-eyebrow{color:#f0c861;font-size:10px;font-weight:900;letter-spacing:2px;text-transform:uppercase;margin-bottom:14px}
    .sv-rule{width:82px;height:4px;border-radius:4px;background:linear-gradient(90deg,#d9a93b,#f4d37e);margin:16px 0 18px}
    .sv-lead{max-width:520px;color:#dbe8f7;font-size:13px;line-height:1.65}
    .sv-feature-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;max-width:540px;margin-top:23px}
    .sv-feature{min-height:75px;border-radius:14px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.07);padding:10px 7px;text-align:center;backdrop-filter:blur(4px)}
    .sv-feature-icon{width:34px;height:34px;border-radius:10px;margin:auto auto 6px;border:1px solid #d9a93b;display:flex;align-items:center;justify-content:center;font-size:16px;background:rgba(0,0,0,.13)}
    .sv-feature-text{font-size:9px;font-weight:800;color:#edf4fc;line-height:1.25}
    .sv-info{margin-top:20px;color:#9fb7d5;font-size:9px;letter-spacing:.5px}
    .sv-about{margin-top:20px;padding:12px 15px;border-radius:14px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.065);color:#e7eef8}
    .sv-about-title{font-size:11px;font-weight:900;color:#f0c861;margin-bottom:3px}
    .sv-about-text{font-size:9px;line-height:1.55;color:#d0ddef}
    .sv-right-panel{background:rgba(255,255,255,.96);border:1px solid #dce6f1;border-radius:21px;padding:18px 22px 21px;box-shadow:0 18px 45px rgba(8,36,75,.13)}
    .sv-secure-title{text-align:center;color:#0a2858;font-size:22px;font-weight:950;margin:0}
    .sv-secure-sub{text-align:center;color:#73839a;font-size:9px;margin:5px 0 13px}
    .sv-gold-line{height:2px;width:45px;background:#d9a93b;border-radius:2px;margin:7px auto 0}
    .sv-footer{margin:0;background:#061a3d;color:#d6e2f0;border-top:2px solid #d9a93b;min-height:45px;padding:10px 25px;display:flex;align-items:center;justify-content:space-between;font-size:9px}
    .sv-footer strong{color:#fff;letter-spacing:1px} .sv-footer span{color:#f0c861}
    /* Streamlit controls inside the right panel */
    div[data-testid="stTextInput"] label,div[data-testid="stNumberInput"] label,div[data-testid="stDateInput"] label,div[data-testid="stSelectbox"] label{font-size:10px!important;font-weight:800!important;color:#334a68!important}
    div[data-testid="stTextInput"] input,div[data-testid="stNumberInput"] input{border-radius:10px!important;border:1px solid #cbd8e7!important;min-height:42px!important}
    div[data-testid="stTextInput"] input:focus{border-color:#176bc9!important;box-shadow:0 0 0 2px rgba(23,107,201,.10)!important}
    .stButton>button{border-radius:10px!important;font-weight:900!important;min-height:42px!important}
    .stButton>button[kind="primary"]{background:linear-gradient(100deg,#061a3d,#176bc9)!important;border:0!important;box-shadow:0 9px 22px rgba(23,107,201,.22)!important}
    [data-baseweb="tab-list"]{gap:5px!important;background:#edf3f9!important;padding:5px!important;border-radius:12px!important;border:1px solid #dce5ef!important}
    [data-baseweb="tab"]{border-radius:9px!important;min-height:39px!important;padding:5px 12px!important;font-size:10px!important;font-weight:900!important;color:#3b536e!important}
    [data-baseweb="tab"][aria-selected="true"]{background:linear-gradient(135deg,#071a3d,#176bc9)!important;color:#fff!important;box-shadow:0 6px 16px rgba(7,36,79,.18)!important}
    [data-baseweb="tab-highlight"]{background:#d9a93b!important;height:3px!important}
    @media(max-width:900px){
        .sv-content{background:linear-gradient(160deg,#09265a 0%,#0b316d 31%,#f4f8fc 31.2%,#f8fbff 100%)}
        .sv-header{background:linear-gradient(110deg,#061a3d 0%,#0b2f68 58%,#f8fbff 58.2%,#f8fbff 100%)}
        .sv-brand-title{font-size:20px} .sv-trust{display:none} .sv-left-title{font-size:32px}
    }
    @media(max-width:620px){
        .block-container{padding:.2rem .3rem!important}
        .sv-header{padding:9px 13px} .sv-logo-box{width:48px;height:48px} .sv-brand-title{font-size:17px} .sv-brand-sub{font-size:7px;letter-spacing:1.5px}
        .sv-content{padding:18px 13px} .sv-feature-grid{grid-template-columns:repeat(2,1fr)} .sv-left-title{font-size:28px}
        .st-key-login_right_panel{padding:15px 12px 17px!important} .sv-footer{display:block;text-align:center;line-height:1.8}
    }
    </style>
    """, unsafe_allow_html=True)

    # Header is HTML only; all interactive controls below are native Streamlit widgets.
    st.markdown(f"""
    <div class="sv-wrap">
      <div class="sv-header">
        <div class="sv-brand">
          <div class="sv-logo-box"><img src="data:image/jpeg;base64,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" alt="Softview Technologies"></div>
          <div class="sv-brand-text">
            <div class="sv-brand-title">SOFTVIEW <span>TECHNOLOGIES</span></div>
            <div class="sv-brand-sub">Enterprise Business Solutions</div>
          </div>
        </div>
        <div class="sv-trust">🛡️ Secure <span>•</span> Reliable <span>•</span> Trusted</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    left, right = st.columns([1.02, 1.18], gap="large")
    with left:
        with st.container(key="login_left_panel"):
            st.markdown("""
            <div style="padding:25px 15px 12px 28px">
              <div class="sv-eyebrow">SMART BUSINESS • ACCURATE DECISIONS</div>
              <div class="sv-left-title">CCI WORKING<br><em>CALCULATION UTILITY</em></div>
              <div class="sv-rule"></div>
              <div class="sv-lead">A professional workspace for CCI contracts, lifting, EMD, CD/CC, FIFO allocation and structured business reporting — designed for accuracy, control and efficient operations.</div>
              <div class="sv-feature-grid">
                <div class="sv-feature"><div class="sv-feature-icon">📑</div><div class="sv-feature-text">Contracts<br>Management</div></div>
                <div class="sv-feature"><div class="sv-feature-icon">🚚</div><div class="sv-feature-text">GRN &amp; Lifting<br>Tracking</div></div>
                <div class="sv-feature"><div class="sv-feature-icon">💰</div><div class="sv-feature-text">EMD &amp;<br>Payments</div></div>
                <div class="sv-feature"><div class="sv-feature-icon">📊</div><div class="sv-feature-text">CD/CC<br>Charges</div></div>
                <div class="sv-feature"><div class="sv-feature-icon">▱</div><div class="sv-feature-text">FIFO<br>Allocation</div></div>
                <div class="sv-feature"><div class="sv-feature-icon">📋</div><div class="sv-feature-text">Reports &amp;<br>Export</div></div>
              </div>
              <div class="sv-info">Precision • Security • Control • Professional Reporting</div>
              <div class="sv-about"><div class="sv-about-title">ABOUT THE UTILITY</div><div class="sv-about-text">Contracts, GRN/lifting, EMD, CD/CC, carrying cost, cash discount, FIFO allocation and reports — brought together in one controlled business workspace.</div></div>
            </div>        """, unsafe_allow_html=True)

    with right:
        with st.container(key="login_right_panel", border=True):
            st.markdown('<div class="sv-secure-title">SECURE ACCESS</div><div class="sv-gold-line"></div><div class="sv-secure-sub">Sign in to continue to your business workspace</div>', unsafe_allow_html=True)
            ltab, rtab, ptab = st.tabs(["🔐  Login", "🏢  Register Firm", "💳  Payment / Renewal"])
    
            with ltab:
                c1, c2, c3 = st.columns([.55, 3.9, .55])
                with c2:
                    st.text_input("Login ID", key="_lu", placeholder="Enter your login ID")
                    st.text_input("Password", key="_lp", type="password", placeholder="Enter your password")
                    st.button("🔒  Sign In", on_click=_do_login, type="primary", use_container_width=True)
                    if st.session_state._login_error:
                        st.error(st.session_state._login_error)
    
            with rtab:
                st.markdown("**New Firm Registration**")
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
                st.markdown("**Payment / Renewal**")
                st.caption("Select your package and submit payment details. Activation occurs only after the credited amount is verified.")
                username = st.text_input("Firm Owner / Username", key="_renew_user")
                package_catalog = _package_catalog()
                labels = list(package_catalog.keys())
                pkg = st.selectbox("Package", labels, format_func=lambda x: f"{package_catalog[x]['label']} — ₹{package_catalog[x]['price']:,.2f}", key="_renew_pkg")
                base_included = int((_load_registry().get("firms", {}).get(_find_firm_for_user(st.session_state.get("_renew_user", ""))[0], {}).get("included_users", _system_policy()["included_users"]) if _find_firm_for_user(st.session_state.get("_renew_user", ""))[0] else _system_policy()["included_users"]) or _system_policy()["included_users"])
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
                        _notify_admin("Payment Request", f"Firm: {firm.get('firm_name')}\nFirm ID: {fid}\nPackage: {package_catalog[pkg]['label']}\nRequired: ₹{amount:,.2f}\nMode: {paymode}\nReference: {payref}", firm.get("mobile", ""))
                        st.success("Payment request recorded. Access will start only after the credited amount is verified against the selected package.")

    st.markdown("""
    <div class="sv-footer">
      <div>© 2026 <strong>SOFTVIEW TECHNOLOGIES</strong>. All rights reserved.</div>
      <div><span>Innovation</span> &nbsp;|&nbsp; Technology &nbsp;|&nbsp; Excellence</div>
    </div>
    """, unsafe_allow_html=True)
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
