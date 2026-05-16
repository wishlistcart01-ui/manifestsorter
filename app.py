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
import requests
import base64

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
    # Treat all symbols (_, -, space) as same for matching
    s = re.sub(r"[^a-zA-Z0-9]", "", s)
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

def train_from_excel(excel_file):
    excel_file.seek(0)
    df = pd.read_excel(excel_file, header=None)
    main_row = df.iloc[0]
    sub_row = df.iloc[1]
    mapping = []

    for col in df.columns:
        main_val = str(main_row[col]).strip()
        sub_val = str(sub_row[col]).strip()
        
        # If both are empty, check if there are any variants in this column
        variants_raw = df.iloc[2:, col].dropna().tolist()
        if not variants_raw:
            continue
            
        # Handle missing headers instead of skipping
        main_name = main_val.upper() if main_val != 'nan' else f"CATALOG {col+1}"
        sub_name = sub_val.upper() if sub_val != 'nan' else "GENERAL"
        
        norm_variants = []
        for v in variants_raw:
            k = normalize_sku_key(v)
            if k and k not in norm_variants:
                norm_variants.append(k)
        
        if not norm_variants:
            continue
            
        norm_variants.sort(key=len, reverse=True)
        mapping.append({
            "main": main_name,
            "sub": sub_name,
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
    # Match SKU followed by Size and Qty
    # Example: "1 NEW_mira_fabric Free Size 1" or "2 DRESS Size M 5"
    m = re.search(r"^(.+?)\s+(?:Free\s+Size|Size\s+[SMLX]+|Size\s+.*?)\s+(\d+)\s*$", line.strip(), re.I)
    if not m:
        return None
    prefix, qty_s = m.group(1).strip(), m.group(2)
    if not prefix:
        return None
    sku = _strip_logistics_prefix(prefix)
    if not sku:
        return None
    return sku, int(qty_s)

def _try_courier_tail(line):
    # Example: "DRESS 1 Free Size"
    m = re.search(r'(.+?)\s+(\d{1,7})\s+(?:Free\s*Size|Size\s*[SMLX]+)\s*$', line, re.I)
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

def extract_from_pdf(pdf_file):
    data = []
    reader = PdfReader(pdf_file)
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
        # Strip system IDs in parentheses before matching
        # Example: "SKU-NAME (12345)" -> "SKU-NAME"
        clean_sku = re.sub(r"\s*\(\d+\)\s*$", "", raw_sku).strip()
        nm = normalize_sku_key(clean_sku)
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

def generate_pdf_report(result, output_buf):
    doc = SimpleDocTemplate(output_buf)
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
    sku = None
    # Improved regex to handle SKUs that might contain the word "SIZE" 
    # and look for the actual "Size" field marker (usually followed by a colon or specific keywords)
    order_match = re.search(r"Order\s*No\.?\s*(.*?)\s*(?=Free\s*Size|Size\s*:|Size\s+[SMLX]+)", text, re.IGNORECASE | re.DOTALL)
    if order_match:
        sku = " ".join(order_match.group(1).split())
    if not sku:
        sku_match = re.search(r"SKU\s*(.*?)\s*(?=Size\s*:|Size\s+[SMLX]+|Free\s*Size)", text, re.IGNORECASE | re.DOTALL)
        if sku_match:
            sku = " ".join(sku_match.group(1).split())
    return sku, qty, courier

def get_sorted_indices(pages, mapping):
    final = []
    used = set()
    
    # 1. First, prioritize items with quantity > 1 (Combo/Bulk)
    for p in pages:
        if p["qty"] > 1:
            final.append(p["index"])
            used.add(p["index"])
            
    # 2. Assign best matches to all remaining pages
    for p in pages:
        if p["index"] in used:
            continue
            
        nm = normalize_sku_key(p["sku"])
        if not nm:
            p["match_info"] = None
            continue
            
        best_match_idx = -1
        best_len = -1
        
        for idx, item in enumerate(mapping):
            for v in item["variants"]:
                if variants_match(v, nm):
                    if len(v) > best_len:
                        best_len = len(v)
                        best_match_idx = idx
        
        p["match_info"] = best_match_idx if best_match_idx != -1 else None

    # 3. Sort by Excel Mapping order (Column by Column)
    for idx in range(len(mapping)):
        for p in pages:
            if p["index"] not in used and p["match_info"] == idx:
                final.append(p["index"])
                used.add(p["index"])
                
    # 4. Add remaining (Unmatched) pages
    for p in pages:
        if p["index"] not in used:
            final.append(p["index"])
            
    return final

def process_sort_pipeline(reader, mapping, selected_couriers=None):
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

    indices = get_sorted_indices(pages, mapping)
    writer = PdfWriter()
    for idx in indices:
        if idx < len(reader.pages):
            writer.add_page(reader.pages[idx])
    
    if len(writer.pages) == 0:
        return None
        
    return writer

# ===============================
# MAIN UI
# ===============================

def main():
    st.set_page_config(page_title="Manifest & Label Sorter", page_icon="📋", layout="wide")
    
    st.title("📋 Manifest & Label Sorter")
    st.caption("Stateless Tool: Upload, Process, and Download. No data is stored on our servers.")
    
    st.divider()
    
    # 1. Upload Training Excel
    st.subheader("Step 1: Upload Training Data")
    train_file = st.file_uploader(
        "📂 Upload Training Excel (Required for both features)", 
        type=["xlsx"], 
        help="Upload the mapping file containing main product, sub-variant, and SKUs."
    )
    
    if not train_file:
        st.info("Please upload your Training Excel file to begin.")
        st.stop()
        
    st.success("✅ Training Data loaded for this session.")
    
    with st.sidebar:
        st.header("🛠️ Debug Tools")
        show_mapping = st.checkbox("Show Loaded Mapping", value=False)
        show_extraction = st.checkbox("Show Extracted SKUs from PDF", value=False)
        
    if show_mapping:
        with st.expander("Loaded Product Mapping", expanded=True):
            mapping_data = train_from_excel(train_file)
            st.write(mapping_data)
    
    st.divider()
    
    # 2. Independent Actions
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📊 1. Generate Grouped Report")
        st.write("Match manifest PDF with training Excel to see item counts.")
        manifest_file = st.file_uploader("📄 Upload Manifest PDF", type=["pdf"], key="mix_manifest")
        
        if st.button("🚀 Generate Report"):
            if manifest_file:
                with st.spinner("Analyzing Manifest..."):
                    try:
                        mapping = train_from_excel(train_file)
                        manifest_data = extract_from_pdf(manifest_file)
                        result = match_and_group(mapping, manifest_data)
                        
                        output_buf = io.BytesIO()
                        generate_pdf_report(result, output_buf)
                        
                        st.success("✅ Report generated!")
                        
                        if show_extraction:
                            with st.expander("Extracted Manifest Data", expanded=False):
                                st.write(manifest_data)
                                
                        date_str = datetime.now().strftime('%Y-%m-%d')
                        st.download_button(
                            "📥 Download Report",
                            output_buf.getvalue(),
                            file_name=f"Manifest_Report_{date_str}.pdf",
                            mime="application/pdf"
                        )
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.warning("Please upload a Manifest PDF.")
                
    with col2:
        st.subheader("🏷️ 2. Sort Labels")
        st.write("Filter and sort your labels based on the training data.")
        label_pdf = st.file_uploader("📄 Upload Label PDF", type=["pdf"], key="mix_label_pdf")
        couriers = st.multiselect(
            "🚚 Filter by Courier (Optional)",
            ["Valmo", "ValmoPlus", "Ecom Express", "Xpress Bees", "Delhivery", "Shadowfax"]
        )
        
        if st.button("🔄 Sort Labels"):
            if label_pdf:
                with st.spinner("Sorting labels..."):
                    try:
                        reader = PdfReader(label_pdf)
                        mapping = train_from_excel(train_file)
                        
                        writer = process_sort_pipeline(reader, mapping, couriers)
                        
                        if writer:
                            output_buf = io.BytesIO()
                            writer.write(output_buf)
                            
                            st.success("✅ Labels sorted!")
                            
                            if show_extraction:
                                with st.expander("Extracted Label Data", expanded=False):
                                    debug_pages = []
                                    for i, page in enumerate(reader.pages):
                                        t = page.extract_text() or ""
                                        s, q, c = extract_label_data(t)
                                        debug_pages.append({"page": i+1, "sku": s, "qty": q, "courier": c})
                                    st.write(debug_pages)
                                    
                            date_str = datetime.now().strftime('%Y-%m-%d')
                            st.download_button(
                                "📥 Download Sorted Labels",
                                output_buf.getvalue(),
                                file_name=f"Sorted_Labels_{date_str}.pdf",
                                mime="application/pdf"
                            )
                        else:
                            st.error("No labels found for the selected criteria.")
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.warning("Please upload a Label PDF.")

if __name__ == "__main__":
    main()
