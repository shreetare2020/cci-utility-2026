
Claude finished the response

Ran 3 commands
Ran 3 commands
No formulas in this file, so recalc isn't necessary, but let's quick-verify the file opens fine and columns line up correctly.


Ran 2 commands, read a file · 1 note
Ran 2 commands, read a file · 1 note
Ye lo sample Excel — 3 sheets ke saath, bilkul wahi format jo updated code expect karta hai:

Sheet 2 "EMD PAYMENT DETAILS" mein column F (highlighted yellow rows) mein "MODE OF TRANSACTION" hai, exactly Payment Date se pehle:

Contract No. | EMD DATE | EMD AMOUNT | (blank) | Contract No. | MODE OF TRANSACTION | PAYMENT DATE | PAYMENT AMOUNT

Yellow highlighted rows sirf example ke liye hain (RTGS, NEFT jaise sample values) — apna real data isi column position pe daal dena, values chahe RTGS/NEFT/Cheque/Cash/UPI/DD ya kuch bhi ho, format waisa hi rahega. Baaki 2 sheets (PUR CONT DETAILS, GRN BOOKING) bhi standard format mein hain taaki tum poora file test kar sako.


Cci upload sample
Spreadsheet · XLSX 


Message collapsed
Ye error isliye aa raha hai kyunki tumhari upload ki hui Excel mein abhi Column H (Mode of Transaction ke baad wali Payment Amount) exist hi nahi karta — matlab tumhari sheet mein ab bhi sirf purana format hai (Contract No | Payment Date | Payment Amount = columns E,F,G), naya "Mode of Transaction" column add nahi kiya gaya.

Main code ko thoda smart bana deta hoon taaki agar column missing ho to crash na ho, balki gracefully purane format pe fall back kare:


Ran 5 commands, viewed a file, edited a file · 2 notes
Ran 5 commands, viewed a file, edited a file · 2 notes
Fix kar diya — ab code backward compatible hai:

Agar tumhari uploaded Excel mein Sheet 2 mein 8 columns hain (yaani Mode of Transaction column H tak add ho chuka hai) → naya format use hoga.
Agar abhi bhi purana 7-column wala format hai (Mode of Transaction add nahi kiya) → automatically purane format pe fallback ho jayega aur crash nahi hoga, bas Mode of Transaction blank rahega us case mein.
Jo error tumne bheja tha, uska matlab tha ki tumne jo Excel upload kiya wahi purane format mein tha (Mode of Transaction column nahi tha) — isliye code column H maang raha tha jo exist hi nahi karta tha.

Ab do options hain:

Naya format use karo — jo sample Excel maine diya tha usi structure mein apna real data daalo (Mode of Transaction column F mein).
Purane format se hi chalao — ab crash nahi hoga, bas Mode of Transaction blank aayega result mein.
Test karke dekh lo dobara upload karke.


Cci working app
PY 
isme yaar wo jo result me box wale lables hei na wo bade bade dikh rahe ek lable niche k line me araha thoda ekdam decent banao na taki lage thoda acha exectutive


Ran a command, viewed a file
Ran a command, viewed a file
Found the issue — grid was hardcoded for 5 columns but we now have 6 cards, so they're squeezing and labels are wrapping. Let me fix the grid to fit 6 and make the whole thing more compact/executive looking.


Ran 3 commands, edited a file, read a file · 1 note
Ran 3 commands, edited a file, read a file · 1 note
Fix kar diya — asli problem ye thi ki grid CSS abhi bhi purane 5 columns ke liye fixed tha, lekin ab 6 boxes hain (Cash Discount add karne ke baad), isliye squeeze ho rahe the aur label niche wrap ho raha tha. Changes:

Grid ko 6 columns ka kar diya taaki sab boxes ek row mein sahi se fit ho jayein.
Padding aur font size chhota kiya (value 22px→17px, label 11px→9.5px, icon 21px→16px) — ab decent, compact, "executive" look milega, bulky nahi lagega.
Text overflow handle kiya (nowrap + ellipsis) taaki label kabhi bhi 2 lines mein na tute.
Mobile responsive bhi bana diya — chhoti screen pe 3 aur phir 2 columns mein adjust ho jayega, taki mobile pe bhi acha dikhe.
Ab Results tab check kar lo — boxes chhote, saaf aur professional dikhenge.


Cci working app
PY 





Claude is AI and can make mistakes. Please double-check responses.
Cci working app · PY
Showing this large file as plain text to keep the page responsive. The full content is still here.
"""
CCI WORKING CALCULATION UTILITY
Softview Technologies | Streamlit + Firebase
Run: streamlit run cci_working_app.py
"""

import io, json, os, base64 as b64lib
from datetime import date
import pandas as pd
import streamlit as st

# ─── FIREBASE ────────────────────────────────────────────────────────────────
import firebase_admin
from firebase_admin import credentials, firestore

# ─── FIREBASE INIT (works both locally and on Streamlit Cloud) ───────────────
# Local:  place your service account JSON as  firebase_key.json  next to app
# Cloud:  add credentials in Streamlit Secrets as [firebase] section

FIREBASE_KEY_FILE = "firebase_key.json"   # local JSON file path

def init_firebase():
    if not firebase_admin._apps:
        # ── Try Streamlit Secrets first (Streamlit Cloud) ──
        try:
            cred_dict = dict(st.secrets["firebase"])
            cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(cred_dict)
        except Exception:
            # ── Fallback: local JSON file (localhost) ──
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

