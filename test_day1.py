"""
DealDesk AI — Day 1 Test Script
Uses centralized gemini_client with automatic fallback.
Usage:
  python test_day1.py
  python test_day1.py data/sample_oms/your_om.pdf
"""

import os, sys, json, time
from dotenv import load_dotenv
from gemini_client import gemini_generate, test_connection, parse_json

load_dotenv()

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def err(m):    print(f"{RED}  ✗  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

SAMPLE_OM_TEXT = """
OFFERING MEMORANDUM — SAMPLE DEAL
Maple Grove Apartments | 312 Units | Austin, TX

FINANCIAL HIGHLIGHTS
- Gross Rental Income: $4,320,000
- Vacancy Loss (5.8%): ($250,560)
- Operating Expenses: $2,012,000
- Net Operating Income (NOI): $2,057,440
- Cap Rate (In-Place): 3.52%
- Market Cap Rate: 4.75%
- Debt Service: $1,450,000
- DSCR: 1.42x
- Occupancy: 94.2%
- Asking Price: $58,500,000 ($187,500/unit)

LOAN DETAILS
- Loan Amount: $38,025,000 (65% LTV)
- Interest Rate: 5.85% Fixed, 10-year
- Amortisation: 30 years, Fannie Mae

KEY RISKS
- Deferred maintenance on HVAC ($800K estimated)
- 3 competing developments within 1-mile (890 units)
- Two anchor tenants on month-to-month leases
"""

EXTRACTION_PROMPT = """
You are a senior CRE underwriter. Extract all key metrics from this Offering Memorandum.
Return ONLY a valid JSON object, no markdown, no commentary:
{
  "property_name": "string",
  "property_type": "multifamily|office|retail|industrial|mixed-use",
  "location": "City, State",
  "units_or_sqft": "string",
  "year_built": "string or null",
  "asking_price": number or null,
  "price_per_unit": number or null,
  "noi": number or null,
  "cap_rate_inplace": decimal or null,
  "cap_rate_market": decimal or null,
  "occupancy_rate": decimal or null,
  "dscr": number or null,
  "ltv": decimal or null,
  "loan_amount": number or null,
  "interest_rate": decimal or null,
  "gross_rental_income": number or null,
  "operating_expenses": number or null,
  "vacancy_rate": decimal or null,
  "red_flags": ["list of risks from OM"],
  "strengths": ["list of positives"],
  "missing_data": ["fields not found"]
}
OM TEXT:
""" 

def extract_pdf_text(path):
    try:
        import fitz
        doc  = fitz.open(path)
        text = "\n\n".join(f"[PAGE {i+1}]\n{p.get_text()}" for i,p in enumerate(doc) if p.get_text().strip())
        doc.close()
        return text
    except ImportError:
        err("Run: pip install pymupdf"); sys.exit(1)

def pretty_print(metrics):
    print(f"\n{BOLD}EXTRACTED DEAL METRICS{RESET}\n" + "─"*50)
    fields = [
        ("Property",            metrics.get("property_name")),
        ("Type",                metrics.get("property_type")),
        ("Location",            metrics.get("location")),
        ("Size",                metrics.get("units_or_sqft")),
        ("Asking Price",        f"${metrics.get('asking_price',0):,.0f}" if metrics.get("asking_price") else None),
        ("Price/Unit",          f"${metrics.get('price_per_unit',0):,.0f}" if metrics.get("price_per_unit") else None),
        ("NOI",                 f"${metrics.get('noi',0):,.0f}" if metrics.get("noi") else None),
        ("Cap Rate (In-Place)", f"{metrics.get('cap_rate_inplace',0)*100:.2f}%" if metrics.get("cap_rate_inplace") else None),
        ("Cap Rate (Market)",   f"{metrics.get('cap_rate_market',0)*100:.2f}%" if metrics.get("cap_rate_market") else None),
        ("Occupancy",           f"{metrics.get('occupancy_rate',0)*100:.1f}%" if metrics.get("occupancy_rate") else None),
        ("DSCR",                f"{metrics.get('dscr')}x" if metrics.get("dscr") else None),
        ("LTV",                 f"{metrics.get('ltv',0)*100:.0f}%" if metrics.get("ltv") else None),
        ("Interest Rate",       f"{metrics.get('interest_rate',0)*100:.2f}%" if metrics.get("interest_rate") else None),
    ]
    for label, value in fields:
        if value: print(f"  {label:<22} {GREEN}{value}{RESET}")
        else:     print(f"  {label:<22} {YELLOW}not found{RESET}")
    if metrics.get("red_flags"):
        print(f"\n{RED}{BOLD}RED FLAGS:{RESET}")
        for f in metrics["red_flags"]: print(f"  {RED}⚑  {f}{RESET}")
    if metrics.get("strengths"):
        print(f"\n{GREEN}{BOLD}STRENGTHS:{RESET}")
        for s in metrics["strengths"]: print(f"  {GREEN}+  {s}{RESET}")
    if metrics.get("missing_data"):
        print(f"\n{YELLOW}{BOLD}MISSING DATA:{RESET}")
        for m in metrics["missing_data"]: print(f"  {YELLOW}?  {m}{RESET}")

def main():
    header("DEALDESK AI — DAY 1 ENVIRONMENT TEST")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        err("GEMINI_API_KEY not found in .env"); sys.exit(1)
    ok("GEMINI_API_KEY found in .env")

    header("STEP 1 — Testing Gemini connection (with auto-fallback)")
    print("  Testing all available models...")
    start = time.time()
    if test_connection():
        ok(f"Gemini responding ({time.time()-start:.1f}s)")
    else:
        err("All Gemini models unavailable — try again in 5 minutes")
        sys.exit(1)

    header("STEP 2 — Loading Offering Memorandum")
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        if not os.path.exists(pdf_path):
            err(f"PDF not found: {pdf_path}"); sys.exit(1)
        om_text = extract_pdf_text(pdf_path)
        ok(f"PDF loaded — {len(om_text):,} characters")
    else:
        warn("No PDF provided — using built-in sample OM")
        om_text = SAMPLE_OM_TEXT
        ok(f"Sample OM loaded — {len(om_text):,} characters")

    header("STEP 3 — Extracting deal metrics")
    print("  Analysing OM with auto-fallback enabled...")
    start = time.time()
    try:
        raw     = gemini_generate(EXTRACTION_PROMPT + om_text[:50000], temperature=0.1)
        metrics = parse_json(raw)
        ok(f"Extraction complete ({time.time()-start:.1f}s)")
    except Exception as e:
        err(f"Extraction failed: {e}"); sys.exit(1)

    pretty_print(metrics)

    os.makedirs("outputs", exist_ok=True)
    with open("outputs/day1_test_metrics.json","w") as f:
        json.dump(metrics, f, indent=2)

    header("DAY 1 COMPLETE")
    ok("Metrics saved to outputs/day1_test_metrics.json")
    ok("Auto-fallback active — gemini-2.5-flash → gemini-1.5-flash → gemini-1.5-flash-8b")
    ok("Ready for Day 2 — RAG knowledge base build")
    print(f"\n  {BOLD}Next:{RESET} python knowledge_base/build_rag.py\n")

if __name__ == "__main__":
    main()
