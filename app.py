import streamlit as st
import pandas as pd
import io
import re
import os
import shutil
import unicodedata
import hashlib
from collections import defaultdict
from PyPDF2 import PdfReader, PdfWriter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from datetime import datetime

# ===============================
# AUTHENTICATION (LOGIN/SIGNUP)
# ===============================

DATA_DIR = os.getenv("DATA_DIR", "./data")
USERS_FILE = os.path.join(DATA_DIR, "users.csv")
os.makedirs(DATA_DIR, exist_ok=True)

def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def check_hashes(password, hashed_text):
    if make_hashes(password) == hashed_text:
        return True
    return False

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            return pd.read_csv(USERS_FILE)
        except:
            pass
    return pd.DataFrame(columns=["username", "password"])

def save_users(df):
    df.to_csv(USERS_FILE, index=False)

def create_user(username, password):
    users = load_users()
    if username in users["username"].values:
        return False
    new_user = pd.DataFrame([{"username": username, "password": make_hashes(password)}])
    users = pd.concat([users, new_user], ignore_index=True)
    save_users(users)
    return True

def authenticate(username, password):
    users = load_users()
    user_row = users[users["username"] == username]
    if not user_row.empty:
        if check_hashes(password, user_row.iloc[0]["password"]):
            return True
    return False

def login_screen():
    st.title("🔐 Login to Manifest Sorter")
    st.caption("Secure your store data on the cloud.")
    
    menu = ["Login", "Sign Up"]
    choice = st.sidebar.selectbox("Authentication Menu", menu)

    if choice == "Login":
        st.subheader("Login to your account")
        username = st.text_input("Username")
        password = st.text_input("Password", type='password')
        if st.button("Login"):
            if authenticate(username, password):
                st.session_state['logged_in'] = True
                st.session_state['username'] = username
                st.success(f"Logged in as {username}")
                st.rerun()
            else:
                st.error("Incorrect Username/Password")
                
    elif choice == "Sign Up":
        st.subheader("Create a New Account")
        new_user = st.text_input("Username")
        new_password = st.text_input("Password", type='password')
        if st.button("Sign Up"):
            if new_user and new_password:
                if create_user(new_user, new_password):
                    st.success("✅ Account created successfully!")
                    st.info("Please go to the Login menu in the sidebar to log in.")
                else:
                    st.warning("⚠️ Username already exists. Please choose another one.")
            else:
                st.warning("Please enter both username and password.")


# ===============================
# ACCOUNT MANAGEMENT (SCOPED TO USER)
# ===============================

def get_accounts_file():
    user = st.session_state['username']
    return os.path.join(DATA_DIR, f"accounts_{user}.csv")

def load_accounts():
    file_path = get_accounts_file()
    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path)
            if "account_id" in df.columns and "account_name" in df.columns:
                return df
        except Exception:
            pass
    return pd.DataFrame(columns=["account_id", "account_name"])

def save_accounts(df):
    df.to_csv(get_accounts_file(), index=False)

def create_account(name):
    accounts = load_accounts()
    name = name.strip()
    if name == "":
        return None, "Store name cannot be empty"
    if name in accounts["account_name"].values:
        return None, f"Store '{name}' already exists"
    
    user = st.session_state['username']
    base_id = f"{user}_{name.lower().replace(' ', '_').replace('-', '_')}"
    account_id = base_id
    i = 2
    while account_id in accounts["account_id"].values:
        account_id = f"{base_id}_{i}"
        i += 1
        
    new_row = pd.DataFrame([{"account_id": account_id, "account_name": name}])
    accounts = pd.concat([accounts, new_row], ignore_index=True)
    save_accounts(accounts)
    return account_id, None

def get_account_dir(account_id):
    path = os.path.join(DATA_DIR, account_id)
    os.makedirs(path, exist_ok=True)
    return path

def delete_account(account_id):
    accounts = load_accounts()
    accounts = accounts[accounts["account_id"] != account_id]
    save_accounts(accounts)
    
    acc_dir = get_account_dir(account_id)
    if os.path.exists(acc_dir):
        shutil.rmtree(acc_dir, ignore_errors=True)

# ===============================
# UTILITIES & CONSTANTS
# ===============================

_MIN_SKU_OVERLAP_RATIO = 0.72
_MIN_DIGIT_SUB_LEN = 5
_MAX_MANIFEST_SUFFIX_OVER_VARIANT = 14

_HEADER_RE = re.compile(
    r'^\s*(Picklist|Supplier\s+Name|Date\s*:|SKU\s+Color|S\.\s*No\.|Sub\s+Order|'
    r'AWB|Courier\s*:|Total\s+Quantity|Qty\.?\s*Size|Packed)\b',
    re.I,
)

def normalize_sku_key(text):
    if text is None:
        return ""
    s = unicodedata.normalize("NFC", str(text)).strip()
    s = re.sub(r"\s+", " ", s)
    return s.casefold()

def _mostly_digits(s):
    s = s.replace(" ", "")
    if not s:
        return False
    return all(ch.isdigit() for ch in s)

def variants_match(norm_variant, norm_manifest):
    nv, nm = norm_variant, norm_manifest
    if not nv or not nm:
        return False
    if nv == nm:
        return True

    v0, m0 = nv.replace(" ", ""), nm.replace(" ", "")

    if _mostly_digits(nv) or _mostly_digits(nm):
        if v0 == m0:
            return True
        if _mostly_digits(nv) and len(v0) >= _MIN_DIGIT_SUB_LEN and v0 in m0:
            return True
        if _mostly_digits(nm) and len(m0) >= _MIN_DIGIT_SUB_LEN and m0 in v0:
            return True
        return False

    if nv in nm:
        if len(nv) >= _MIN_SKU_OVERLAP_RATIO * len(nm):
            return True
        if (
            len(nv) >= 10
            and nm.startswith(nv)
            and (len(nm) - len(nv)) <= _MAX_MANIFEST_SUFFIX_OVER_VARIANT
        ):
            return True

    if nm in nv:
        if len(nm) >= _MIN_SKU_OVERLAP_RATIO * len(nv):
            return True

    return False

# ===============================
# 1. REPORT GENERATION FUNCTIONS
# ===============================

def train_from_excel(file_path):
    df = pd.read_excel(file_path, header=None)
    main_row = df.iloc[0]
    sub_row = df.iloc[1]
    mapping = []

    for col in df.columns:
        main = str(main_row[col]).strip()
        sub = str(sub_row[col]).strip()
        if main == 'nan' or sub == 'nan':
            continue
        variants = df.iloc[2:, col].dropna().tolist()
        norm_variants = []
        for v in variants:
            k = normalize_sku_key(v)
            if k and k not in norm_variants:
                norm_variants.append(k)
        norm_variants.sort(key=len, reverse=True)
        mapping.append({
            "main": main.upper(),
            "sub": sub.upper(),
            "variants": norm_variants,
        })
    return mapping

def _strip_logistics_prefix(prefix):
    parts = prefix.split()
    i = 0
    n = len(parts)
    while i < n:
        p = parts[i]
        remaining = n - i
        if p.isdigit() and len(p) <= 3:
            i += 1
            continue
        if p.isdigit() and len(p) >= 9:
            if remaining > 1:
                i += 1
                continue
            break
        if re.fullmatch(r'\d+_\d+', p):
            if remaining > 1:
                i += 1
                continue
            break
        if re.match(r'^VL\d+$', p, re.I) or re.match(r'^SF[A-Z0-9]+$', p, re.I):
            if remaining > 1:
                i += 1
                continue
            break
        break
    return ' '.join(parts[i:]).strip()

def _try_picklist_line(line):
    m = re.search(r"^(.+)\s+Free\s+Size\s+(\d+)\s*$", line.strip(), re.I)
    if not m:
        return None
    body, qty_s = m.group(1).strip(), m.group(2)
    if not body:
        return None
    parts = body.split()
    if not parts:
        return None
    sku = parts[0] if len(parts) == 1 else " ".join(parts[:-1]).strip()
    low = sku.casefold()
    if low in ("sku", "total", "quantity", "size", "packed") or not sku:
        return None
    return sku, int(qty_s)

def _try_courier_tail(line):
    m = re.search(r'(.+)\s+(\d{1,7})\s+Free\s*Size\s*$', line, re.I)
    if not m:
        return None
    prefix, qty_s = m.group(1).strip(), m.group(2)
    if int(qty_s) < 1:
        return None
    sku = _strip_logistics_prefix(prefix)
    if not sku:
        return None
    return sku, int(qty_s)

def _try_simple_tail_qty(line):
    if "free" in line.lower():
        return None
    line = line.strip()
    m = re.search(r"\s+(\d{1,10})\s*$", line)
    if not m:
        return None
    sku = line[: m.start()].strip()
    if len(sku) < 1 or not re.search(r"[^\s]", sku):
        return None
    return sku, int(m.group(1))

def extract_line_sku_qty(line):
    line = (line or '').strip()
    if not line or len(line) < 4:
        return None
    if _HEADER_RE.match(line):
        return None
    if re.fullmatch(r'\(\d+\)', line):
        return None
    for fn in (_try_picklist_line, _try_courier_tail, _try_simple_tail_qty):
        got = fn(line)
        if got:
            return got
    return None

def _merge_broken_pdf_lines(lines):
    out = []
    i = 0
    while i < len(lines):
        cur = (lines[i] or "").strip()
        nxt = (lines[i + 1] or "").strip() if i + 1 < len(lines) else ""
        if cur and nxt and extract_line_sku_qty(cur) is None:
            if re.match(r"^\d{1,10}\s+Free\s*Size", nxt, re.I):
                merged = f"{cur} {nxt}"
                if extract_line_sku_qty(merged):
                    out.append(merged)
                    i += 2
                    continue
        if cur:
            out.append(cur)
        i += 1
    return out

def extract_from_pdf(pdf_file_or_path):
    data = []
    reader = PdfReader(pdf_file_or_path)
    for page in reader.pages:
        text = page.extract_text() or ""
        raw_lines = text.split("\n")
        lines = _merge_broken_pdf_lines(raw_lines)
        for line in lines:
            got = extract_line_sku_qty(line)
            if got:
                data.append(got)
    return data

def match_and_group(mapping, manifest_data):
    result = defaultdict(lambda: defaultdict(int))
    for raw_sku, qty in manifest_data:
        nm = normalize_sku_key(raw_sku)
        if not nm:
            continue
        best_main, best_sub, best_len = None, None, -1
        for item in mapping:
            for v in item["variants"]:
                if not v:
                    continue
                if variants_match(v, nm):
                    if len(v) > best_len:
                        best_len = len(v)
                        best_main = item["main"]
                        best_sub = item["sub"]
        if best_main is not None:
            result[best_main][best_sub] += qty
    return result

def generate_pdf_report(result, output_path_or_buf):
    doc = SimpleDocTemplate(output_path_or_buf)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], alignment=TA_LEFT, fontSize=20, leading=26, spaceAfter=8)
    normal_style = ParagraphStyle("normal", parent=styles["Normal"], alignment=TA_LEFT, fontSize=16, leading=22, spaceAfter=6)
    elements = []
    total = 0
    for main, subs in result.items():
        elements.append(Paragraph(f"<b>{main}</b>", title_style))
        for sub, qty in subs.items():
            elements.append(Paragraph(f"{sub} → {qty}", normal_style))
            total += qty
        elements.append(Spacer(1, 16))
    total_style = ParagraphStyle("total", parent=styles["Title"], alignment=TA_LEFT, fontSize=22, leading=28, spaceBefore=10)
    elements.append(Paragraph(f"<b>Total: {total}</b>", total_style))
    doc.build(elements)

# ===============================
# 2. LABEL SORTING FUNCTIONS
# ===============================

def normalize_courier(text):
    return re.sub(r"\s+", "", str(text).lower())

def extract_label_data(text):
    if not text:
        return None, 1, "Other"
    text = re.sub(r"\s+", " ", text).strip()
    qty = 1
    match = re.search(r"Free\s*Size\s*(\d+)", text, re.IGNORECASE)
    if match:
        qty = int(match.group(1))
    partners = [("valmoplus", "ValmoPlus"), ("ecomexpress", "Ecom Express"), ("xpressbees", "Xpress Bees"), ("delhivery", "Delhivery"), ("shadowfax", "Shadowfax"), ("valmo", "Valmo")]
    courier = "Other"
    clean_text_courier = normalize_courier(text)
    for key, value in partners:
        if key in clean_text_courier:
            courier = value
            break
    sku = None
    order_match = re.search(r"Order\s*No\.?\s*(.*?)\s*(Free\s*Size|Size)", text, re.IGNORECASE | re.DOTALL)
    if order_match:
        sku = " ".join(order_match.group(1).split())
    if not sku:
        sku_match = re.search(r"SKU\s*(.*?)\s*Size", text, re.IGNORECASE | re.DOTALL)
        if sku_match:
            sku = " ".join(sku_match.group(1).split())
    return sku, qty, courier

def get_sorted_indices(pages, df):
    final = []
    used = set()
    for p in pages:
        if p["qty"] > 1:
            final.append(p["index"])
            used.add(p["index"])
    for col in df.columns:
        skus = df[col].dropna().astype(str).str.strip().tolist()
        for sku in skus:
            for p in pages:
                if p["index"] not in used and p["sku"]:
                    if p["sku"].lower() == sku.lower():
                        final.append(p["index"])
                        used.add(p["index"])
    for p in pages:
        if p["index"] not in used:
            final.append(p["index"])
    return final

def process_sort_pipeline(reader, df, selected_couriers=None):
    all_pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        sku, qty, courier = extract_label_data(text)
        all_pages.append({"index": i, "sku": sku, "qty": qty, "courier": courier})

    if selected_couriers:
        selected_set = set(normalize_courier(c) for c in selected_couriers)
        pages = [p for p in all_pages if normalize_courier(p["courier"]) in selected_set]
    else:
        pages = all_pages

    if not pages:
        return None

    indices = get_sorted_indices(pages, df)
    writer = PdfWriter()
    for idx in indices:
        if idx < len(reader.pages):
            writer.add_page(reader.pages[idx])
    
    if len(writer.pages) == 0:
        return None
        
    return writer

# ===============================
# STREAMLIT UI
# ===============================

def main():
    st.set_page_config(page_title="Manifest & Label Sorter", page_icon="📋", layout="wide")
    os.makedirs("data", exist_ok=True)
    
    # Check Login State
    if 'logged_in' not in st.session_state:
        st.session_state['logged_in'] = False
        
    if not st.session_state['logged_in']:
        login_screen()
        return

    # --- Authenticated View ---
    st.sidebar.markdown(f"👤 Logged in as: **{st.session_state['username']}**")
    if st.sidebar.button("Logout"):
        st.session_state['logged_in'] = False
        st.rerun()
        
    st.sidebar.divider()
    
    # --- Account Manager (Scoped to User) ---
    accounts_df = load_accounts()

    st.sidebar.title("🏢 Store Manager")

    # Create new store account
    with st.sidebar.expander("➕ Create New Store", expanded=accounts_df.empty):
        new_acc_name = st.sidebar.text_input("Store Name", key="new_acc_name_global", placeholder="e.g. Shop A, Store B")
        if st.sidebar.button("Create Store", key="btn_create_acc_global"):
            if new_acc_name.strip():
                acc_id, err = create_account(new_acc_name)
                if err:
                    st.error(err)
                else:
                    st.success(f"✅ Store '{new_acc_name}' created!")
                    st.rerun()
            else:
                st.warning("Enter a store name")

    accounts_df = load_accounts()

    if not accounts_df.empty:
        # Store selector
        account_options = dict(zip(accounts_df["account_name"], accounts_df["account_id"]))
        selected_account_name = st.sidebar.selectbox(
            "Select Store",
            list(account_options.keys()),
            key="sel_account_global"
        )
        selected_account_id = account_options[selected_account_name]
        
        # Delete store
        with st.sidebar.expander("🗑 Delete Store"):
            st.warning(f"This will permanently delete **{selected_account_name}** and ALL its data.")
            if st.sidebar.button("❌ Delete This Store", key="btn_del_account_global"):
                delete_account(selected_account_id)
                st.success("Store deleted")
                st.rerun()
    else:
        selected_account_id = None
        st.sidebar.info("Create a store to start.")
        st.stop()

    # --- Main UI ---
    st.title("📋 Mix Feature - Manifest & Label Sorter")
    st.caption(f"Advanced Manifest Analysis & Label Sorting for **{selected_account_name}**")

    # Path to save training data persistently
    acc_dir = get_account_dir(selected_account_id)
    train_save_path = f"{acc_dir}/mix_train.xlsx"

    st.subheader("Generate Grouped Report & Sort Labels")
    st.write("Match manifest PDF with training Excel to see item counts, and sort your labels in one go!")
    
    has_saved_train = os.path.exists(train_save_path)
    
    # 1. Manage Training Excel
    if has_saved_train:
        st.info(f"✅ Using saved Training Excel for {selected_account_name}.")
        with st.expander("📝 View & Edit Saved Training Data"):
            try:
                df_t = pd.read_excel(train_save_path, header=None)
                edited_t = st.data_editor(df_t, use_container_width=True, key="mix_train_editor")
                if st.button("💾 Save Changes to Training Data"):
                    edited_t.to_excel(train_save_path, index=False, header=False)
                    st.success("Changes saved!")
                    st.rerun()
            except Exception as e:
                st.error(f"Error reading saved file: {e}")
        
        if st.button("🗑️ Delete Saved Training Excel"):
            os.remove(train_save_path)
            st.rerun()

    # Upload Training Excel
    train_file = st.file_uploader(
        "📂 Upload Training Excel" if not has_saved_train else "🔄 Replace Training Excel", 
        type=["xlsx"], 
        help="Upload the mapping file containing main product, sub-variant, and SKUs."
    )
    
    if train_file:
        with open(train_save_path, "wb") as f:
            f.write(train_file.getbuffer())
        st.success(f"✅ Training Excel replaced and saved for {selected_account_name}!")
        st.rerun()

    target_train = train_save_path if has_saved_train else None

    # 2. Independent Actions
    st.divider()
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📊 1. Generate Report")
        manifest_file = st.file_uploader("📄 Manifest PDF", type=["pdf"], key="mix_manifest")
        
        if st.button("🚀 Generate Grouped Report"):
            if target_train and manifest_file:
                with st.spinner("Analyzing Manifest..."):
                    try:
                        mapping = train_from_excel(target_train)
                        manifest_data = extract_from_pdf(manifest_file)
                        result = match_and_group(mapping, manifest_data)
                        
                        output_buf = io.BytesIO()
                        generate_pdf_report(result, output_buf)
                        
                        # Save to account folder
                        output_dir = os.path.join(acc_dir, "mix_outputs")
                        os.makedirs(output_dir, exist_ok=True)
                        date_str = datetime.now().strftime('%Y-%m-%d')
                        file_name = f"{selected_account_name}_{date_str}_Report.pdf".replace(" ", "_")
                        save_path = os.path.join(output_dir, file_name)
                        
                        with open(save_path, "wb") as f:
                            f.write(output_buf.getvalue())
                        
                        st.success(f"✅ Report generated and saved to {selected_account_name}'s folder!")
                        st.download_button(
                            "📥 Download Report",
                            output_buf.getvalue(),
                            file_name=file_name,
                            mime="application/pdf"
                        )
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.warning("Please ensure Training Excel is uploaded and Manifest PDF is provided.")
                
    with col2:
        st.subheader("🏷️ 2. Sort Labels")
        label_pdf = st.file_uploader("📄 Label PDF", type=["pdf"], key="mix_label_pdf")
        couriers = st.multiselect(
            "🚚 Filter by Courier (Leave empty for ALL)",
            ["Valmo", "ValmoPlus", "Ecom Express", "Xpress Bees", "Delhivery", "Shadowfax"]
        )
        
        if st.button("🔄 Sort & Download Labels"):
            if target_train and label_pdf:
                with st.spinner("Sorting labels..."):
                    try:
                        reader = PdfReader(label_pdf)
                        df_train = pd.read_excel(target_train)
                        
                        writer = process_sort_pipeline(reader, df_train, couriers)
                        
                        if writer:
                            output_buf = io.BytesIO()
                            writer.write(output_buf)
                            
                            # Save to account folder
                            output_dir = os.path.join(acc_dir, "mix_outputs")
                            os.makedirs(output_dir, exist_ok=True)
                            date_str = datetime.now().strftime('%Y-%m-%d')
                            courier_str = "_".join(couriers) if couriers else "ALL"
                            file_name = f"{selected_account_name}_{date_str}_{courier_str}_Labels.pdf".replace(" ", "_")
                            save_path = os.path.join(output_dir, file_name)
                            
                            with open(save_path, "wb") as f:
                                f.write(output_buf.getvalue())
                                
                            st.success(f"✅ Labels sorted and saved to {selected_account_name}'s folder!")
                            st.download_button(
                                "📥 Download Sorted Labels",
                                output_buf.getvalue(),
                                file_name=file_name,
                                mime="application/pdf"
                            )
                        else:
                            st.error("No labels found for the selected criteria.")
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.warning("Please ensure Training Excel is uploaded and Label PDF is provided.")

if __name__ == "__main__":
    main()
