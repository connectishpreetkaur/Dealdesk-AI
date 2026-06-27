"""
DealDesk AI — Agent 1: Document Parser (Vision-enabled)
========================================================
FIXES APPLIED:
  BUG FIXED: om_text[:60000] slice was cutting off pages 36-52 of the Hackathon OM.
  Those pages contain the ENTIRE financial section — Rent Roll, NOI, Cash Flow
  Projection, Operating Statement. Gemini never saw them → returned nulls for
  all financial fields → Agent 4 dashboard was all zeros → Agent 5 risk engine
  had no data to score.

  FIX STRATEGY: Two-pass chunked extraction.
    Pass 1 → Property + Executive Summary + Market (pages 1-30, first 55k chars)
    Pass 2 → Financial tables only (pages 41-52, LAST 45k chars)
    Merge both dicts, financial pass 2 values win on conflicts.
  
  This keeps us at exactly 2 Gemini calls for digital PDFs (same as before),
  while guaranteeing the financial tables are always included.
"""

import os, sys, json, base64, fitz
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini_client import gemini_generate, parse_json_robust, retrieve_book_context_safe

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

# ── SCHEMA (unchanged from your original) ────────────────────────────────────
EXTRACTION_SCHEMA = """{
  "property_name": "string",
  "property_type": "multifamily|office|retail|industrial|mixed-use",
  "location": "City, State",
  "address": "full street address if available",
  "units_or_sqft": "string e.g. 312 units or 45,000 sqft",
  "year_built": "string e.g. 2001 or null",
  "asking_price": "number in USD or null",
  "price_per_unit": "number in USD or null",
  "noi": "number in USD or null",
  "gross_rental_income": "number in USD or null",
  "operating_expenses": "number in USD or null",
  "vacancy_rate": "decimal e.g. 0.058 or null",
  "occupancy_rate": "percentage e.g. 90% or null",
  "cap_rate_inplace": "percentage e.g. 5.2% or null",
  "cap_rate_market": "percentage e.g. 5.3% or null",
  "dscr": "number e.g. 1.42 or null",
  "ltv": "decimal e.g. 0.65 or null",
  "loan_amount": "number in USD or null",
  "interest_rate": "percentage e.g. 5.4% or null",
  "loan_term_years": "number or null",
  "amortisation_years": "number or null",
  "debt_service_annual": "number in USD or null",
  "tenants": ["list of tenant names if mentioned"],
  "lease_expirations": ["list of lease expiry info"],
  "capex_noted": ["list of capital expenditure items mentioned"],
  "red_flags_raw": ["verbatim risk statements from the document"],
  "strengths_raw": ["verbatim positive statements from the document"],
  "sponsor_background": "string or null",
  "property_description": "2-3 sentence property description",
  "missing_fields": ["fields you could NOT find in the document"]
}"""

EXTRACTION_PROMPT = """You are a Senior Partner at a top-tier Real Estate Private Equity (REPE) / Real Estate Investment Banking (REIB) firm with decades of experience underwriting multifamily acquisitions.

Analyze the attached Offering Memorandum (OM) as if you are screening a live acquisition opportunity for an investment committee.

Your objective is to extract every material investment detail accurately from the OM. Do not summarize broadly—extract specific facts, figures, assumptions, and observations. If a value is not explicitly stated, return `"Not Mentioned"` rather than making assumptions.

Extract and organize the information into a well-structured JSON object with clear section headings, including but not limited to:

* Property Overview
* Executive Summary
* Investment Highlights
* Location Details
* Property Specifications
* Unit Mix
* Rent Roll Summary
* Financial Performance (Historical & Current)
* T-12 Operating Statement
* Trailing Revenue & Expenses
* NOI
* Cap Rate
* Occupancy
* Vacancy
* Market Rents vs In-Place Rents
* Value-Add Opportunities
* Renovation Program
* Comparable Properties Mentioned
* Debt Assumptions (if provided)
* Market Overview
* Demographic Highlights
* Employment Drivers
* Risks Mentioned in the OM
* Seller Assumptions
* Business Plan
* Exit Strategy (if mentioned)
* Important Dates
* Major Capital Improvements
* Amenities
* Parking
* Utilities
* Construction Details
* Tax Information
* Insurance Information
* Environmental Notes
* Legal Disclosures
* Any tables or numerical data that materially affect investment decisions.

Rules:

* Return **valid JSON only**.
* Do **not** wrap the output in Markdown or code fences.
* Preserve all numbers exactly as written in the OM.
* Convert percentages and currency values into appropriate numeric fields while also preserving the original text where useful.
* Include page references for every extracted field whenever possible.
* If multiple values exist (e.g., multiple occupancy figures), capture all of them with their corresponding page numbers.
* Never invent, infer, estimate, or hallucinate information.
* Ensure the JSON is complete, syntactically valid, and can be parsed directly by `json.loads()` without modification."""

# ── FINANCIAL-ONLY PROMPT (Pass 2 for long OMs) ──────────────────────────────
FINANCIAL_EXTRACTION_PROMPT = """You are a senior CRE underwriter. Extract ONLY financial data from this section of an Offering Memorandum.

Focus exclusively on:
- NOI (T-12, T-6, T-3, T-1, and Pro Forma Year 1)
- Gross Rental Income / Gross Potential Income
- Total Operating Expenses
- Vacancy rate
- Occupancy rate  
- Cap rate (in-place and market)
- Asking price / price per unit
- DSCR, LTV, loan amount, debt service
- Unit mix table (unit type, count, sq ft, market rent, lease rent)
- Rent roll summary totals
- 5-year cash flow projection (NOI per year)
- Value-add renovation costs and rent premiums
- Real estate taxes
- Any other specific dollar figures or percentages

Return valid JSON only. No markdown fences. Extract numbers exactly as written.
Use this schema but you may add extra keys for additional financial data found:
""" + EXTRACTION_SCHEMA


def extract_text_pymupdf(pdf_path: str) -> tuple[str, bool, int]:
    """
    Extract text using PyMuPDF.
    Returns (full_text_with_page_markers, is_scanned, total_chars).
    """
    doc        = fitz.open(pdf_path)
    page_texts = []
    full_text  = ""

    for i, page in enumerate(doc):
        text = page.get_text().strip()
        if text:
            page_texts.append(f"[PAGE {i+1}]\n{text}")
            full_text += text

    doc.close()
    combined   = "\n\n".join(page_texts)
    is_scanned = len(full_text.strip()) < 500
    return combined, is_scanned, len(combined)


def pdf_pages_to_images(pdf_path: str, max_pages: int = 20) -> list[str]:
    """Convert PDF pages to base64-encoded PNG images for Gemini Vision."""
    doc    = fitz.open(pdf_path)
    images = []
    pages  = min(len(doc), max_pages)
    print(f"  Converting {pages} PDF pages to images for Gemini Vision...")

    for i in range(pages):
        page = doc[i]
        mat  = fitz.Matrix(150/72, 150/72)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img_b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
        images.append(img_b64)

    doc.close()
    ok(f"Converted {len(images)} pages to images")
    return images


def extract_with_vision(pdf_path: str) -> dict:
    """Send PDF pages as images to Gemini Vision. Used for scanned PDFs."""
    from google import genai
    from google.genai import types
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    client  = genai.Client(api_key=api_key)
    images  = pdf_pages_to_images(pdf_path, max_pages=25)

    parts = [
        types.Part.from_text(
            EXTRACTION_PROMPT + "\n\nExtract from the following Offering Memorandum pages:"
        )
    ]
    for img_b64 in images:
        parts.append(
            types.Part.from_bytes(
                data=base64.b64decode(img_b64),
                mime_type="image/png"
            )
        )

    print(f"  Sending {len(images)} pages to Gemini Vision...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=32000)
    )

    if not response.text or not response.text.strip():
        raise ValueError("Gemini Vision returned an empty response.")

    return parse_json_robust(response.text.strip())


# ══════════════════════════════════════════════════════════════════════════════
# FIX: TWO-PASS CHUNKED EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

# Threshold: if extracted text exceeds this, use 2-pass chunked extraction
# 60k = ~35 pages. Most OMs have financials in back half → need 2 passes
SINGLE_PASS_LIMIT = 60_000   # chars


def extract_with_text_single_pass(om_text: str) -> dict:
    """Standard single-pass extraction for short OMs (< 60k chars)."""
    ctx = retrieve_book_context_safe(
        "key financial metrics CRE offering memorandum underwriting NOI cap rate DSCR"
    )
    prompt = f"""You are a senior CRE underwriter at a top-tier private equity fund.
Analyse this Offering Memorandum text and extract EVERY financial metric.

EXPERT ANALYTICAL FRAMEWORK:
{ctx[:1500]}

{EXTRACTION_PROMPT}

OFFERING MEMORANDUM TEXT:
{om_text}"""  # ← no slice here; caller already ensures it's under 60k

    raw = gemini_generate(prompt, temperature=0.1, max_tokens=32000)
    if not raw or not raw.strip():
        raise ValueError("Gemini returned empty response during extraction.")
    return parse_json_robust(raw)


def _safe_parse(raw: str, pass_label: str) -> dict:
    """
    Parse Gemini response to dict. On failure, saves raw to disk for debugging
    and returns an empty dict so the pipeline can continue with the other pass.
    """
    if not raw or not raw.strip():
        warn(f"{pass_label}: Gemini returned empty response — skipping this pass.")
        return {}
    try:
        return parse_json_robust(raw)
    except Exception as e:
        warn(f"{pass_label}: JSON parse failed ({e})")
        # Save raw response so you can inspect what Gemini actually returned
        debug_path = f"outputs/{pass_label.replace(' ', '_')}_raw_response.txt"
        os.makedirs("outputs", exist_ok=True)
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(raw)
        warn(f"Raw response saved to {debug_path} — inspect it to understand the failure.")
        warn("Continuing with empty result for this pass — other pass data will be used.")
        return {}


# Tight JSON-only prompt for Pass 1 (property, location, market)
PASS1_PROMPT_TEMPLATE = """Extract data from this Offering Memorandum section. Return ONLY a JSON object — no markdown, no explanation, no text before or after the JSON.

Required JSON keys (use null if not found, never omit a key):
{{
  "property_name": null,
  "property_type": null,
  "location": null,
  "address": null,
  "units_or_sqft": null,
  "year_built": null,
  "asking_price": null,
  "price_per_unit": null,
  "sponsor_background": null,
  "property_description": null,
  "tenants": [],
  "strengths_raw": [],
  "red_flags_raw": [],
  "capex_noted": [],
  "missing_fields": []
}}

OM TEXT:
"""


# Tight JSON-only prompt for Pass 2 (financials only)
PASS2_PROMPT_TEMPLATE = """Extract ONLY financial data from this Offering Memorandum section. Return ONLY a JSON object — no markdown, no explanation, no text before or after the JSON.

Required JSON keys (use null if not found, never omit a key):
{{
  "noi": null,
  "gross_rental_income": null,
  "operating_expenses": null,
  "vacancy_rate": null,
  "occupancy_rate": null,
  "cap_rate_inplace": null,
  "cap_rate_market": null,
  "asking_price": null,
  "price_per_unit": null,
  "dscr": null,
  "ltv": null,
  "loan_amount": null,
  "interest_rate": null,
  "loan_term_years": null,
  "amortisation_years": null,
  "debt_service_annual": null,
  "lease_expirations": [],
  "missing_fields": []
}}

Extract these specific values if present:
- NOI: look for T-12, T-6, T-3, T-1 trailing figures and Pro Forma Year 1
- occupancy_rate: current occupancy as a decimal (e.g. 0.96 for 96%)
- vacancy_rate: as a decimal (e.g. 0.05 for 5%)
- cap_rate_inplace: as a decimal (e.g. 0.052 for 5.2%)
- gross_rental_income: Effective Gross Income or Gross Scheduled Rent from T-12
- operating_expenses: Total Expenses from T-12 operating statement

OM TEXT:
"""


def extract_with_text_two_pass(om_text: str, total_chars: int) -> dict:
    """
    Two-pass extraction for long OMs (> 60k chars).

    Pass 1: First 55k chars  → property, location, market, highlights
    Pass 2: Last  45k chars  → financial tables, NOI, rent roll, cash flow

    Uses tight JSON-only prompts — no markdown headers, no ambiguity.
    If either pass fails to parse, logs raw response and continues with the other.
    Total Gemini calls: 2.
    """
    warn(f"Long OM detected ({total_chars:,} chars > {SINGLE_PASS_LIMIT:,})")
    warn("Using 2-pass chunked extraction to capture financial tables...")

    # ── PASS 1: Property + Market (front half) ────────────────────────────────
    print("  Pass 1/2 — Extracting property, location, market data...")
    front_text = om_text[:55_000]
    prompt1 = PASS1_PROMPT_TEMPLATE + front_text  # avoid .format() — OM text may contain { } chars
    raw1  = gemini_generate(prompt1, temperature=0.0, max_tokens=16000)  # ✅ FIX: was 8000
    pass1 = _safe_parse(raw1, "Pass 1")
    ok(f"Pass 1 complete — {sum(1 for v in pass1.values() if v is not None)} fields")

    # ✅ FIX: pause between passes to avoid Gemini rate limiting
    import time
    print("  Waiting 5s before Pass 2 to avoid rate limiting...")
    time.sleep(5)

    # ── PASS 2: Financials (back half — where the tables live) ────────────────
    print("  Pass 2/2 — Extracting financial tables, NOI, rent roll, cash flow...")
    back_text = om_text[-45_000:]   # LAST 45k — always captures pages 41-52
    prompt2 = PASS2_PROMPT_TEMPLATE + back_text  # avoid .format() — OM text may contain { } chars
    raw2  = gemini_generate(prompt2, temperature=0.0, max_tokens=16000)  # ✅ FIX: was 8000
    pass2 = _safe_parse(raw2, "Pass 2")
    ok(f"Pass 2 complete — {sum(1 for v in pass2.values() if v is not None)} fields")

    # ── MERGE: Pass 2 wins on financial fields, Pass 1 wins on property/location ──
    FINANCIAL_KEYS = {
        "noi", "gross_rental_income", "operating_expenses", "vacancy_rate",
        "occupancy_rate", "cap_rate_inplace", "cap_rate_market", "asking_price",
        "price_per_unit", "dscr", "ltv", "loan_amount", "interest_rate",
        "loan_term_years", "amortisation_years", "debt_service_annual",
        "tenants", "lease_expirations", "capex_noted"
    }

    merged = {**pass1}  # start with pass 1 as base

    for key, val2 in pass2.items():
        if val2 is None or val2 == "Not Mentioned" or val2 == [] or val2 == "":
            continue  # don't overwrite pass1 data with nulls from pass2

        # Financial keys: pass 2 always wins (it saw the actual tables)
        if key in FINANCIAL_KEYS:
            merged[key] = val2
        else:
            # Non-financial: only overwrite if pass1 had nothing
            if not merged.get(key) or merged[key] == "Not Mentioned":
                merged[key] = val2

    ok(f"Merged — {sum(1 for v in merged.values() if v is not None)} total fields")
    return merged


def extract_with_text(om_text: str, total_chars: int = 0) -> dict:
    """
    Main text extraction dispatcher.
    Routes to single-pass or two-pass based on document length.
    """
    actual_len = total_chars or len(om_text)

    if actual_len <= SINGLE_PASS_LIMIT:
        ok(f"Short OM ({actual_len:,} chars) — single-pass extraction")
        return extract_with_text_single_pass(om_text)
    else:
        return extract_with_text_two_pass(om_text, actual_len)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AGENT FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def agent_doc_parser(pdf_path: str = None, om_text: str = None) -> dict:
    """
    Main Agent 1 function.
    Accepts either a PDF path (preferred) or raw text.
    Auto-detects scanned PDFs and uses Vision accordingly.
    Auto-detects long OMs and uses 2-pass chunked extraction.
    """
    header("AGENT 1 — Document Parser")

    # If raw text provided (legacy support)
    if om_text and not pdf_path:
        print("  Using provided text...")
        metrics = extract_with_text(om_text)
        ok(f"Extracted {sum(1 for v in metrics.values() if v is not None)} fields")
        return metrics

    if not pdf_path or not os.path.exists(pdf_path):
        raise FileNotFoundError(f"OM PDF not found: {pdf_path}")

    # Step 1: Extract text with PyMuPDF
    print(f"  Loading: {pdf_path}")
    text, is_scanned, total_chars = extract_text_pymupdf(pdf_path)
    ok(f"PyMuPDF extracted {total_chars:,} chars")

    if is_scanned:
        warn("Scanned/image PDF detected — switching to Gemini Vision")
        warn("This takes 20-40 seconds — Gemini is reading the pages visually")
        try:
            metrics = extract_with_vision(pdf_path)
            ok(f"Vision extraction complete — {sum(1 for v in metrics.values() if v is not None)} fields")
        except Exception as e:
            warn(f"Vision extraction failed: {e}")
            warn("Falling back to text extraction with whatever PyMuPDF found")
            metrics = extract_with_text(text, total_chars)
    else:
        ok(f"Text-based PDF — routing to {'2-pass' if total_chars > SINGLE_PASS_LIMIT else 'single-pass'} extraction")
        metrics = extract_with_text(text, total_chars)

    # Print key metrics summary
    print(f"\n  {BOLD}Key Metrics Found:{RESET}")
    
    # Handle occupancy_rate that might be a string like "96%", a float like 0.96, or a dict like {"value": 0.96}
    occ_raw = metrics.get("occupancy_rate")
    if isinstance(occ_raw, dict):
        occ_raw = occ_raw.get("value", occ_raw.get("rate", occ_raw.get("percentage", None)))
    occ_display = None
    if occ_raw:
        try:
            occ_float = float(str(occ_raw).replace("%",""))
            # If it's already a percentage (e.g. 96.0), display as-is
            # If it's a decimal (e.g. 0.96), multiply by 100
            occ_display = f"{occ_float:.1f}%" if occ_float > 1 else f"{occ_float*100:.1f}%"
        except:
            occ_display = str(occ_raw)

    cap_raw = metrics.get("cap_rate_inplace")
    if isinstance(cap_raw, dict):
        cap_raw = cap_raw.get("value", cap_raw.get("rate", cap_raw.get("percentage", None)))
    cap_display = None
    if cap_raw:
        try:
            cap_float = float(str(cap_raw).replace("%",""))
            cap_display = f"{cap_float:.2f}%" if cap_float > 1 else f"{cap_float*100:.2f}%"
        except:
            cap_display = str(cap_raw)

    # Helper: safely extract a number from a metric that Gemini may return as a nested dict
    def _num(val, default=0):
        """Handles scalars and nested dicts like {'value': 850000} or {'amount': 1200000}."""
        if val is None:
            return default
        if isinstance(val, dict):
            return val.get("value", val.get("amount", val.get("amount_usd", default)))
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    key_fields = [
        ("Property",       metrics.get("property_name")),
        ("Location",       metrics.get("location")),
        ("Type",           metrics.get("property_type")),
        ("Asking Price",   f"${_num(metrics.get('asking_price')):,.0f}" if metrics.get("asking_price") else None),
        ("NOI",            f"${_num(metrics.get('noi')):,.0f}" if metrics.get("noi") else None),
        ("Cap Rate",       cap_display),
        ("DSCR",           f"{_num(metrics.get('dscr'))}x" if metrics.get("dscr") else None),
        ("Occupancy",      occ_display),
    ]
    for label, value in key_fields:
        if value:
            print(f"    {label:<16} {GREEN}{value}{RESET}")
        else:
            print(f"    {label:<16} {YELLOW}not found{RESET}")

    # Warn if critical financial fields are still missing after extraction
    critical_missing = [
        k for k in ["noi", "occupancy_rate", "gross_rental_income"]
        if not metrics.get(k) or metrics.get(k) == "Not Mentioned"
    ]
    if critical_missing:
        warn(f"Critical fields still missing after extraction: {critical_missing}")
        warn("This may mean the financial section uses image-embedded tables.")
        warn("Consider running with Vision extraction instead.")

    if metrics.get("missing_fields"):
        print(f"\n  {YELLOW}OM reports missing: {', '.join(metrics['missing_fields'][:5])}{RESET}")

    return metrics
