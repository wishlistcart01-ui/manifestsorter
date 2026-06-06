# Bug Fix Report — Manifest Sorter (`app.py`)

> **Date:** 2026-05-24  
> **Branch:** `main`  
> **Commit:** `7c07246`  
> **File affected:** `app.py`

---

## Overview

The **Generate Grouped Report** feature was producing an **incorrect total count (71 instead of 93)** because two bugs in the PDF extraction logic caused multiple SKUs to either be falsely extracted or fail to match against the training Excel.

---

## Bug 1 — `_HEADER_RE` Not Filtering Date/Courier Lines

### Location
```python
# Line 26–30 in app.py
_HEADER_RE = re.compile(...)
```

### Root Cause
The regex used a `\b` (word boundary) at the end of the pattern group:

```python
# BUGGY
_HEADER_RE = re.compile(
    r'^\s*(Picklist|Supplier\s+Name|Date\s*:|SKU\s+Color|S\.\s*No\.|Sub\s+Order|'
    r'AWB|Courier\s*:|Total\s+Quantity|Qty\.?\s*Size|Packed)\b',
    re.I,
)
```

`\b` is a **word boundary** — it only matches between a word character (`\w`) and a non-word character. Patterns like `Date\s*:` and `Courier\s*:` end with `:` which is a **non-word character**. So `\b` always **fails** after `:`, meaning lines like:

```
Date : 21 May, 2026
Courier : Delhivery
```

were **NOT** recognized as headers and fell through to `_try_simple_tail_qty`, which extracted:
- SKU = `Date : 21 May,`
- Qty = `2026` (the year, treated as a quantity!)

This happened **8 times** across 9 PDF pages — once per courier/picklist page.

### Fix
Remove `\b` from the end of the regex. The patterns are specific enough that false positives are not a concern:

```python
# FIXED
_HEADER_RE = re.compile(
    r'^\s*(Picklist|Supplier\s+Name|Date\s*:|SKU\s+Color|S\.\s*No\.|Sub\s+Order|'
    r'AWB|Courier\s*:|Total\s+Quantity|Qty\.?\s*Size|Packed)',
    re.I,
)
```

---

## Bug 2 — Color Word Appended to SKU in Picklist Format

### Location
```python
# Line 149–161 in app.py
def _try_picklist_line(line): ...
```

### Root Cause
The picklist PDF format is:
```
SKU_NAME   COLOR   Free Size   QTY
```
Example:
```
blue_abla_1   Blue   Free Size   11
(1)rangoli_blue_2   Blue   Free Size   5
1104672634   Multicolor   Free Size   1
RR_Maroon   Maroon   Free Size   2
```

The old regex captured **everything before "Free Size"** as the SKU prefix — meaning the **color word was included**:

```python
# BUGGY
m = re.search(
    r"^(.+?)\s+(?:Free\s+Size|Size\s+[SMLX]+|Size\s+.*?)\s+(\d+)\s*$",
    line.strip(), re.I
)
# group(1) = "blue_abla_1 Blue"  ← color word included!
```

This caused normalization mismatches:

| Extracted (wrong) | Normalized | Training SKU | Normalized | Ratio | Match? |
|---|---|---|---|---|---|
| `blue_abla_1 Blue` | `blueabla1blue` (13) | `blue_abla_1` | `blueabla1` (9) | 9/13 = **0.69 < 0.72** | ❌ |
| `(1)rangoli_blue_2 Blue` | `1rangoliblue2blue` | `rangoli_blue_2` | `rangoliblue2` | fails | ❌ |
| `1104672634 Multicolor` | `_strip_logistics_prefix` eats `1104672634` → leaves `Multicolor` | `1104672634` | `1104672634` | no match | ❌ |
| `RR_Maroon Maroon` | `rrmaroonmaroon` | `RR_Maroon` | `rrmaroon` | fails ratio | ❌ |

### Fix
Update `_try_picklist_line` to treat the word **immediately before "Free Size"** as the color (captured by `\S+`) and exclude it from the SKU prefix:

```python
# FIXED
def _try_picklist_line(line):
    # Picklist format: [N]SKU_NAME COLOR_WORD Free Size QTY
    # COLOR_WORD is always a single word (Blue, Red, Multicolor, etc.) before the size info.
    # We capture SKU and COLOR separately so the color is not appended to the SKU.
    # Example: "(1)rangoli_blue_2 Blue Free Size 5" -> sku="(1)rangoli_blue_2", qty=5
    m = re.search(
        r"^(.+?)\s+\S+\s+(?:Free\s+Size|Size\s+[SMLX]+(?:\d+)?|Size\s+\S+)\s+(\d+)\s*$",
        line.strip(), re.I
    )
    if not m:
        return None
    prefix, qty_s = m.group(1).strip(), m.group(2)
    if not prefix:
        return None
    sku = _strip_logistics_prefix(prefix)
    if not sku:
        return None
    return sku, int(qty_s)
```

**Key change:** `\S+` is inserted between the SKU group `(.+?)` and the `Free\s+Size` pattern to silently consume the color word.

---

## Impact / Before vs After

| SKU in Manifest | Before Fix | After Fix | Category |
|---|---|---|---|
| `blue_abla_1 Blue Free Size 11` | ❌ Unmatched | ✅ NAVY BLUE ABLA +11 | ABLA |
| `(1)rangoli_blue_2 Blue Free Size 5` | ❌ Unmatched | ✅ BLUE RANGOLI +5 | RANGOLI |
| `RR_Maroon Maroon Free Size 2` | ❌ Unmatched | ✅ RR MAROON +2 | RR |
| `Mira - 24 Multicolor Free Size 2` | ❌ Unmatched | ✅ MIRA +2 | MIRA |
| `Mira - 324 Multicolor Free Size 1` | ❌ Unmatched | ✅ MIRA +1 | MIRA |
| `1104672634 Multicolor Free Size 1` | ❌ → SKU=`Multicolor` | ✅ Matched by numeric ID | RANGOLI |
| `Date : 21 May, 2026` (×8 pages) | ❌ Fake entry qty=2026 | ✅ Filtered as header | — |

### Total Count
| | Count |
|---|---|
| **Before fix** | 71 |
| **After fix** | **93** |
| **Gain** | +22 |

---

## Remaining Known Issue (Training Data — Not a Code Bug)

**SKU:** `rangoli_gaajari_1` (qty = 3) — still unmatched after code fix.

**Reason:** The manifest PDF spells it with **double 'a'** (`gaajari`) but the training Excel only contains **single 'a'** variants (`gajari`). After normalization they produce different strings:
- PDF: `rangoligaajari1`
- Excel: `rangoligajari*` (all variants)

**Fix:** Add `rangoli_gaajari_1` to the **Gajari Rangoli** column in the training Excel (`Prime TrainSet.xlsx`). This will add the missing +3 to GAJARI RANGOLI bringing the total to **96**.

---

## How Normalization Works (Reference)

All SKU matching goes through `normalize_sku_key()`:

```python
def normalize_sku_key(text):
    s = unicodedata.normalize("NFC", str(text)).strip()
    s = re.sub(r"[^a-zA-Z0-9]", "", s)   # strips _, -, ., /, spaces, etc.
    return s.casefold()                    # lowercase
```

Examples:
| Raw SKU | Normalized |
|---|---|
| `rangoli_blue_2` | `rangoliblue2` |
| `Nayra_1.19` | `nayra119` |
| `rangoli_gajari_/_` | `rangoligajari` |
| `Black Leave -0 45 +` | `blackleave045` |
| `S47PARICOTTON-PINK01` | `s47paricottonpink01` |

SKUs with decimals like `2.1` or `1.19` work correctly — the `.` is stripped during normalization and both PDF and Excel normalize identically.

---

## Matching Threshold Reference

```python
_MIN_SKU_OVERLAP_RATIO = 0.72   # variant must be 72%+ length of manifest SKU to match
_MIN_DIGIT_SUB_LEN = 5          # numeric IDs must be 5+ digits to use substring matching
_MAX_MANIFEST_SUFFIX_OVER_VARIANT = 14  # manifest can be at most 14 chars longer than variant
```
