"""
DealDesk AI — Full Pipeline v2 (All 8 Issues Fixed)
=====================================================
Agent 1: Doc Parser          → Gemini Vision (handles scanned PDFs)
Agent 2: Market Intelligence → FRED + Web Search + HomeHarvest comps
Agent 3: Location Intel      → OSM fixed Overpass query
Agent 4: Financial Dashboard → Pure Python
Agent 5: Risk Engine         → Gemini (sees ALL prior outputs)
Orchestrator: Memo Writer    → Gemini (applies frameworks, doesn't cite)

Total Gemini calls: 4-5 depending on PDF type
"""

import os, sys, json, time, argparse
import fitz, requests
from pinecone import Pinecone
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini_client import gemini_generate, gemini_embed, parse_json_robust, retrieve_book_context_safe

# Import new fixed agents
from agents.agent_doc_parser    import agent_doc_parser
from agents.agent_market_intel  import agent_market_intel
from agents.agent_location_v2   import agent_location_intelligence
from agents.agent_risk          import agent_risk_engine

load_dotenv()

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def err(m):    print(f"{RED}  ✗  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

def _num(val, default=0):
    """Safely extract a numeric value — handles scalars and nested dicts like {'value': 850000}.
    Gemini sometimes returns metrics as {'value': 850000} instead of plain 850000."""
    if val is None:
        return default
    if isinstance(val, dict):
        return val.get("value", val.get("amount", val.get("rate", val.get("amount_usd", default))))
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

PINECONE_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME   = os.getenv("PINECONE_INDEX_NAME", "dealdesk-knowledge")

def extract_pdf_text(path: str) -> str:
    doc  = fitz.open(path)
    text = "\n\n".join(
        f"[PAGE {i+1}]\n{p.get_text()}"
        for i, p in enumerate(doc) if p.get_text().strip()
    )
    doc.close()
    return text

# ══════════════════════════════════════════════════════
# AGENT 4 HELPERS — Price & Debt Estimation
# Used when OM withholds asking price (bid-process OMs)
# ══════════════════════════════════════════════════════
def _estimate_price_from_evidence(noi: float, assessed_value: float = None,
                                   units: int = None, market: dict = None):
    """
    Three-method price triangulation when asking price is absent from OM.
    Returns: (midpoint_estimate, (low, high), method_breakdown_dict)

    Method 1 — Tax-Implied Floor:
        Assessed value ÷ 0.3333 = county market value appraisal.
        Treated as a FLOOR only — income assets trade well above assessed value.

    Method 2 — Cap Rate Back-Calculation:
        Uses market cap rate from Agent 2 web search; falls back to
        5.50% (suburban Chicago Class B multifamily 2025 consensus).

    Method 3 — Debt Yield Triangulation:
        Lenders require ~8.5% debt yield on institutional multifamily.
        NOI / 0.085 = max loan → ÷ 0.65 LTV = implied purchase price.

    Method 4 — Per-Unit Comp:
        Suburban Chicago value-add Class B: $280K–$320K/unit observed range.
        Only used when unit count is known.
    """
    estimates = {}

    # Method 1 — Tax floor (excluded from averaging)
    if assessed_value and assessed_value > 0:
        estimates["tax_implied_floor"] = round(assessed_value / 0.3333)

    # Method 2 — Cap rate back-calculation
    cap_mkt = None
    if market:
        web_caps = market.get("web_cap_rates", {})
        # Try several common key names Agent 2 might return
        for key in ("multifamily", "cap_rate", "value", "residential"):
            raw = _num(web_caps.get(key))
            if raw and 0.03 < raw < 0.12:
                cap_mkt = raw
                break
    if not cap_mkt:
        cap_mkt = 0.055   # 5.5% — suburban Chicago Class B consensus 2025
    if noi and cap_mkt:
        estimates["cap_rate_implied"] = round(noi / cap_mkt)

    # Method 3 — Debt yield triangulation
    if noi:
        loan_from_dy    = noi / 0.085         # institutional debt yield floor
        estimates["debt_yield_implied"] = round(loan_from_dy / 0.65)

    # Method 4 — Per-unit comp (only when unit count available)
    if units and units > 0:
        estimates["per_unit_comp"] = round(units * 300_000)  # $300K/unit midpoint

    # Average methods 2-4 (skip tax floor)
    averaging = {k: v for k, v in estimates.items() if k != "tax_implied_floor"}
    if not averaging:
        return None, None, estimates

    values   = list(averaging.values())
    midpoint = round(sum(values) / len(values))
    low      = round(min(values) * 0.90)   # 10% below lowest method
    high     = round(max(values) * 1.10)   # 10% above highest method

    return midpoint, (low, high), estimates


def _estimate_debt_service(loan_amount: float,
                            rate: float = 0.065,
                            years: int  = 30) -> float:
    """
    Standard mortgage payment formula.
    Default: 6.5% interest rate, 30-year amortisation.
    Returns annual debt service.
    """
    if not loan_amount:
        return None
    r = rate / 12
    n = years * 12
    monthly = loan_amount * (r * (1 + r) ** n) / ((1 + r) ** n - 1)
    return round(monthly * 12)


# ══════════════════════════════════════════════════════
# AGENT 4 — FINANCIAL DASHBOARD (pure Python)
# ══════════════════════════════════════════════════════
def agent_financial_dashboard(deal: dict, market: dict,
                               price_override: float = None) -> dict:
    header("AGENT 4 — Financial Dashboard")

    # ── Pull raw values from Agent 1 output ──────────────────────────────────
    noi    = _num(deal.get("noi"))
    gri    = _num(deal.get("gross_rental_income"))
    opex   = _num(deal.get("operating_expenses"))
    loan   = _num(deal.get("loan_amount"))
    debt   = _num(deal.get("debt_service_annual"))
    cap_in = _num(deal.get("cap_rate_inplace"))
    units  = _num(deal.get("num_units") or deal.get("units") or deal.get("unit_count"))
    assessed_value = _num(deal.get("assessed_value") or deal.get("tax_assessed_value"))

    # price_override wins; otherwise use OM; otherwise estimate
    price = _num(price_override or deal.get("asking_price"))

    # Tracking flags for transparency
    price_estimated = False
    loan_estimated  = False
    debt_estimated  = False
    price_confidence_range   = None
    price_method_breakdown   = {}

    # ── STEP 1: Estimate price if missing ────────────────────────────────────
    if not price:
        warn("No asking price found in OM — running 3-method price estimation")
        price, price_confidence_range, price_method_breakdown = \
            _estimate_price_from_evidence(
                noi=noi,
                assessed_value=assessed_value,
                units=int(units) if units else None,
                market=market
            )
        if price:
            price_estimated = True
            lo, hi = price_confidence_range if price_confidence_range else (None, None)
            warn(f"Estimated price: ${price:,.0f}  "
                 f"(range ${lo:,.0f} – ${hi:,.0f})" if lo else
                 f"Estimated price: ${price:,.0f}")
        else:
            warn("Price estimation failed — metrics requiring price will be N/A")

    # ── STEP 2: Estimate loan if missing ─────────────────────────────────────
    if not loan and price:
        loan = round(price * 0.65)   # Standard 65% LTV for institutional multifamily
        loan_estimated = True
        warn(f"No loan amount in OM — assuming 65% LTV: ${loan:,.0f}")

    # ── STEP 3: Estimate debt service if missing ──────────────────────────────
    if not debt and loan:
        debt = _estimate_debt_service(loan, rate=0.065, years=30)
        debt_estimated = True
        warn(f"No debt service in OM — estimated 6.5%/30yr: ${debt:,.0f}/yr")

    # ── STEP 4: Market cap rate (from Agent 2 or fallback) ───────────────────
    cap_mkt = _num(deal.get("cap_rate_market"))
    if not cap_mkt or not (0.03 < cap_mkt < 0.12):
        web_caps = market.get("web_cap_rates", {})
        for key in ("multifamily", "cap_rate", "value", "residential"):
            raw = _num(web_caps.get(key))
            if raw and 0.03 < raw < 0.12:
                cap_mkt = raw
                break
    if not cap_mkt or not (0.03 < cap_mkt < 0.12):
        cap_mkt = 0.055   # 5.50% — suburban Chicago Class B consensus 2025

    # ── STEP 5: Core metric calculations ─────────────────────────────────────
    equity        = round(price - loan)       if price and loan                    else None
    cf            = round(noi - debt)         if noi   and debt                    else None
    coc           = cf / equity               if cf    and equity and equity > 0   else None
    dy            = noi / loan                if noi   and loan                    else None
    expr          = opex / gri               if opex  and gri                     else None
    grm           = price / gri              if price and gri                     else None
    beo           = (opex + debt) / gri      if opex  and debt  and gri           else None
    dscr          = round(noi / debt, 2)     if noi   and debt                    else None
    ltv           = round(loan / price, 4)   if loan  and price                   else None
    implied_value = round(noi / cap_mkt)     if noi   and cap_mkt                 else None
    value_gap     = round(implied_value - price) \
                                             if implied_value and price            else None
    ppu           = round(price / units)     if price and units                   else \
                    deal.get("price_per_unit")

    # ── STEP 6: Cap rate sensitivity table ───────────────────────────────────
    cap_sensitivity = {}
    if noi:
        for cap in [0.045, 0.0475, 0.050, 0.0525, 0.055,
                    0.0575, 0.060, 0.0625, 0.065]:
            iv = round(noi / cap)
            cap_sensitivity[f"{cap*100:.2f}%"] = {
                "implied_value": iv,
                "per_unit":      round(iv / units) if units else None,
                "vs_estimate":   round(iv - price) if price else None,
            }

    # ── STEP 7: Vacancy sensitivity ──────────────────────────────────────────
    sensitivity = {}
    if gri and opex and debt:
        for v in [0.05, 0.10, 0.15, 0.20, 0.25]:
            noi_s = gri * (1 - v) - opex
            cf_s  = noi_s - debt
            sensitivity[f"vacancy_{int(v*100)}pct"] = {
                "vacancy_rate": f"{int(v*100)}%",
                "noi":          round(noi_s),
                "cash_flow":    round(cf_s),
                "dscr":         round(noi_s / debt, 2) if debt else None,
                "covers_debt":  cf_s > 0,
            }

    # ── DISPLAY ───────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Price Discovery:{RESET}")
    if price_estimated:
        print(f"    {'Source':<26} {YELLOW}⚠ ESTIMATED — not in OM{RESET}")
        print(f"    {'Working price':<26} ${price:,.0f}")
        if price_confidence_range:
            lo, hi = price_confidence_range
            print(f"    {'Confidence range':<26} ${lo:,.0f} – ${hi:,.0f}")
        for method, val in price_method_breakdown.items():
            if val:
                label = method.replace("_", " ").title()
                print(f"    {'  · ' + label:<26} ${val:,.0f}")
    else:
        print(f"    {'Source':<26} OM (actual asking price)")
        print(f"    {'Asking price':<26} ${price:,.0f}")

    print(f"\n  {BOLD}Computed Metrics:{RESET}")
    if loan_estimated:
        print(f"    {YELLOW}{'  Loan (assumed 65% LTV)':<26} ${loan:,.0f}{RESET}")
    if debt_estimated:
        print(f"    {YELLOW}{'  Debt Svc (6.5%/30yr est)':<26} ${debt:,.0f}/yr{RESET}")

    est_flag = lambda estimated: f" {YELLOW}[est]{RESET}" if estimated else ""
    metrics_display = [
        ("Cash-on-Cash",
            f"{coc*100:.2f}%{est_flag(price_estimated or loan_estimated)}"  if coc           else "N/A"),
        ("Debt Yield",
            f"{dy*100:.2f}%{est_flag(loan_estimated)}"                       if dy            else "N/A"),
        ("Break-even Occ.",
            f"{beo*100:.1f}%{est_flag(debt_estimated)}"                      if beo           else "N/A"),
        ("GRM",
            f"{grm:.2f}x{est_flag(price_estimated)}"                         if grm           else "N/A"),
        ("DSCR",
            f"{dscr:.2f}x{est_flag(debt_estimated)}"                         if dscr          else "N/A"),
        ("Implied Value",
            f"${implied_value:,.0f}"                                          if implied_value else "N/A"),
        ("Value Gap",
            f"${value_gap:+,.0f}{est_flag(price_estimated)}"                 if value_gap is not None else "N/A"),
        ("Price / Unit",
            f"${ppu:,.0f}{est_flag(price_estimated)}"                         if ppu           else "N/A"),
        ("Cap Rate Used",
            f"{cap_mkt*100:.2f}%"),
        ("LTV",
            f"{ltv*100:.1f}%{est_flag(loan_estimated)}"                      if ltv           else "N/A"),
    ]
    for label, val in metrics_display:
        print(f"    {label:<26} {GREEN}{val}{RESET}")

    if price_estimated or loan_estimated or debt_estimated:
        print(f"\n    {YELLOW}[est] = computed using estimated inputs, not OM data{RESET}")
        print(f"    {YELLOW}Run with price_override=<actual_price> once known{RESET}")

    ok("Financial dashboard computed")

    return {
        "price_discovery": {
            "source":            "estimated" if price_estimated else "om_actual",
            "working_price":     price,
            "price_estimated":   price_estimated,
            "confidence_range":  list(price_confidence_range) if price_confidence_range else None,
            "method_breakdown":  {k: v for k, v in price_method_breakdown.items() if v},
            "assumptions":       {
                "loan_estimated":  loan_estimated,
                "debt_estimated":  debt_estimated,
                "ltv_assumed":     0.65 if loan_estimated else None,
                "rate_assumed":    0.065 if debt_estimated else None,
                "cap_rate_source": "web_search" if not _num(deal.get("cap_rate_market")) else "om",
                "cap_rate_used":   cap_mkt,
            },
        },
        "core_metrics": {
            "noi":              noi,
            "cap_rate_inplace": cap_in,
            "cap_rate_market":  cap_mkt,
            "dscr":             dscr,
            "ltv":              ltv,
            "loan_amount":      loan,
            "asking_price":     price,
            "equity_required":  equity,
            "debt_service":     debt,
            "units":            int(units) if units else None,
        },
        "computed_metrics": {
            "cash_flow_before_tax":     cf,
            "cash_on_cash_pct":         round(coc * 100, 2) if coc          else None,
            "debt_yield_pct":           round(dy  * 100, 2) if dy           else None,
            "gross_rent_multiplier":    round(grm, 2)        if grm          else None,
            "expense_ratio_pct":        round(expr * 100, 2) if expr         else None,
            "break_even_occupancy_pct": round(beo * 100, 1)  if beo          else None,
            "price_per_unit":           ppu,
            "implied_value_at_mkt_cap": implied_value,
            "value_gap_vs_asking":      value_gap,
        },
        "cap_rate_sensitivity":  cap_sensitivity,
        "sensitivity_analysis":  sensitivity,          # kept for orchestrator compatibility
        "macro_context": {
            "fed_funds_rate":    market.get("fed_funds_rate",    {}).get("value"),
            "ten_year_treasury": market.get("ten_year_treasury", {}).get("value"),
            "cpi":               market.get("cpi",               {}).get("value"),
            "unemployment":      market.get("unemployment",      {}).get("value"),
        },
    }

# ══════════════════════════════════════════════════════
# ORCHESTRATOR — INVESTMENT MEMO WRITER
# Issue 4 fix: apply frameworks, never cite by name
# ══════════════════════════════════════════════════════
def orchestrator_write_memo(deal, market, location, risk, dashboard, memo_ex=""):
    header("ORCHESTRATOR — Writing Investment Memo")

    # Get book context — applied as analytical lens, not citations
    book_ctx = retrieve_book_context_safe(
        "investment memo IC recommendation CRE underwriting deal thesis "
        "cap rate NOI DSCR cash flow risk assessment"
    )

    # Market comps context
    hh_comps  = market.get("property_comps", {})
    web_caps  = market.get("web_cap_rates", {})
    sales_list = hh_comps.get("sales", [])
    rent_list  = hh_comps.get("rentals", [])

    comps_ctx = ""
    if sales_list:
        comps_ctx += "\n\nHOMEHARVEST COMPARABLE SALES (live MLS data):\n"
        for c in sales_list[:5]:
            comps_ctx += (f"  • {c.get('full_street_line','?')} | "
                         f"${_num(c.get('list_price',0)):,.0f} list | "
                         f"${_num(c.get('sold_price',0)):,.0f} sold | "
                         f"{c.get('beds','?')}bd/{c.get('full_baths','?')}ba | "
                         f"{c.get('sqft','?')} sqft | {c.get('status','?')}\n")
    if rent_list:
        comps_ctx += "\n\nHOMEHARVEST COMPARABLE RENTALS (live MLS data):\n"
        for c in rent_list[:5]:
            comps_ctx += (f"  • {c.get('full_street_line','?')} | "
                         f"${_num(c.get('list_price',0)):,.0f}/mo | "
                         f"{c.get('beds','?')}bd/{c.get('full_baths','?')}ba | "
                         f"{c.get('sqft','?')} sqft\n")
    if not comps_ctx:
        comps_ctx = "\n\nHOMEHARVEST COMPS: Not available for this market/property type."

    # Pre-build sensitivity table as markdown
    sens = dashboard.get("sensitivity_analysis", {})
    if sens:
        sens_table = "| Vacancy | NOI | Cash Flow | DSCR | Covers Debt? |\n"
        sens_table += "|---------|-----|-----------|------|-------------|\n"
        for v in sens.values():
            ok_str = "✅ YES" if v.get("covers_debt") else "❌ NO"
            sens_table += (f"| {v.get('vacancy_rate','?')} | "
                          f"${(v.get('noi') or 0):,.0f} | "
                          f"${(v.get('cash_flow') or 0):,.0f} | "
                          f"{v.get('dscr','N/A')}x | {ok_str} |\n")
    else:
        sens_table = "_Sensitivity table not available — GRI or OPEX data missing from OM._"

    # Cap rate sensitivity table
    cap_sens = dashboard.get("cap_rate_sensitivity", {})
    if cap_sens:
        cap_table = "| Cap Rate | Implied Value | Per Unit | vs Estimate |\n"
        cap_table += "|----------|--------------|----------|-------------|\n"
        for cap, row in cap_sens.items():
            gap = row.get("vs_estimate")
            gap_str = f"${gap:+,.0f}" if gap is not None else "N/A"
        cap_table += (f"| {cap} | ${(row.get('implied_value') or 0):,.0f} | "
                     f"${(row.get('per_unit') or 0):,.0f} | {gap_str} |\n")
    else:
        cap_table = "_Cap rate sensitivity not available._"

    # Price discovery context for memo
    pd_info = dashboard.get("price_discovery", {})
    price_note = ""
    if pd_info.get("price_estimated"):
        cr = pd_info.get("confidence_range")
        price_note = (f"\n⚠ PRICE NOTE: No asking price in OM. "
                      f"Working estimate: ${pd_info.get('working_price',0):,.0f} "
                      f"(range ${cr[0]:,.0f}–${cr[1]:,.0f}). "
                      f"Flag this in Section 3 and explain the estimation methodology."
                      if cr else
                      f"\n⚠ PRICE NOTE: No asking price in OM. "
                      f"Working estimate: ${pd_info.get('working_price',0):,.0f}. "
                      f"Flag this in Section 3.")

    memo_format = f"\nEXAMPLE MEMO FORMAT AND TONE:\n{memo_ex[:5000]}" if memo_ex else ""

    prompt = f"""You are a senior analyst at a top-tier CRE private equity fund with 15 years of experience.
You have deeply studied real estate finance, underwriting principles, and investment analysis.
Write a complete Investment Committee Memorandum for this deal.

CRITICAL INSTRUCTION ON ANALYTICAL APPROACH:
You have internalized professional CRE underwriting frameworks and financial analysis principles.
Apply this expertise as your own professional judgment throughout the memo.
NEVER cite books, authors, or frameworks by name (no "Per Geltner", no "According to Linneman").
Instead, reason like an experienced underwriter — state conclusions with conviction backed by numbers.
The expertise should be invisible — only the insight should show.

ALL AGENT DATA:

DEAL METRICS (from Offering Memorandum):
{json.dumps(deal, indent=2)}

MARKET INTELLIGENCE:
- Market Assessment: {market.get("analysis", {}).get("market_assessment", "N/A")}
- Cap Rate Verdict: {market.get("analysis", {}).get("cap_rate_verdict", "N/A")}
- Market Cycle: {market.get("analysis", {}).get("market_cycle_position", "N/A")}
- Market Risk: {market.get("analysis", {}).get("market_risk_level", "N/A")}
- Web Cap Rate Range: {web_caps.get("market_cap_rate_low")} - {web_caps.get("market_cap_rate_high")}
- Market Notes: {json.dumps(market.get("analysis", {}).get("market_notes", []), indent=2)}
{comps_ctx}

MACRO ENVIRONMENT (Federal Reserve data):
- Fed Funds Rate: {market.get("fed_funds_rate", {}).get("value", "N/A")}%
- 10Y Treasury: {market.get("ten_year_treasury", {}).get("value", "N/A")}%
- CPI: {market.get("cpi", {}).get("value", "N/A")}
- Unemployment: {market.get("unemployment", {}).get("value", "N/A")}%

LOCATION INTELLIGENCE (OpenStreetMap):
- Location Score: {location.get("location_score")}/10 (Grade: {location.get("location_grade")})
- {location.get("location_summary", "")}
- Category Scores: {json.dumps(location.get("category_scores", {}), indent=2)}
- Key Amenities: {json.dumps(location.get("amenity_counts", {}), indent=2)}

RISK ASSESSMENT (7-Rule Model):
- Composite Score: {risk.get("composite_score")}/10 ({risk.get("risk_level")} Risk)
- Red Flags: {json.dumps(risk.get("red_flags", []), indent=2)}
- Risk Narrative: {risk.get("narrative", "")[:800]}

FINANCIAL METRICS:
{price_note}
- NOI: ${_num(deal.get("noi") or dashboard.get("core_metrics",{}).get("noi",0)):,.0f}
- Asking Price: ${_num(deal.get("asking_price") or dashboard.get("core_metrics",{}).get("working_price",0)):,.0f} {"(ESTIMATED — not in OM)" if pd_info.get("price_estimated") else "(from OM)"}
- Cap Rate (in-place): {round((_num(deal.get("cap_rate_inplace") or dashboard.get("core_metrics",{}).get("cap_rate_inplace")) or 0)*100, 2)}%
- Cap Rate (market): {round((_num(deal.get("cap_rate_market") or dashboard.get("core_metrics",{}).get("cap_rate_market")) or 0)*100, 2)}%
- DSCR: {_num(deal.get("dscr") or dashboard.get("core_metrics",{}).get("dscr") or 0) or "N/A"}x
- LTV: {round((_num(deal.get("ltv") or dashboard.get("core_metrics",{}).get("ltv")) or 0)*100, 1)}%
- Loan Amount: ${_num(deal.get("loan_amount") or dashboard.get("core_metrics",{}).get("loan_amount",0)):,.0f} {"(assumed 65% LTV)" if pd_info.get("assumptions",{}).get("loan_estimated") else ""}
- Annual Debt Service: ${_num(dashboard.get("core_metrics",{}).get("debt_service",0)):,.0f} {"(estimated 6.5%/30yr)" if pd_info.get("assumptions",{}).get("debt_estimated") else ""}
- Cash-on-Cash: {dashboard.get("computed_metrics", {}).get("cash_on_cash_pct") or "N/A"}%
- Debt Yield: {dashboard.get("computed_metrics", {}).get("debt_yield_pct") or "N/A"}%
- Break-even Occupancy: {dashboard.get("computed_metrics", {}).get("break_even_occupancy_pct") or "N/A"}%
- Gross Rent Multiplier: {dashboard.get("computed_metrics", {}).get("gross_rent_multiplier") or "N/A"}x
- Expense Ratio: {dashboard.get("computed_metrics", {}).get("expense_ratio_pct") or "N/A"}%
- Implied Value @ Mkt Cap: ${dashboard.get("computed_metrics", {}).get("implied_value_at_mkt_cap") or "N/A"}
- Value Gap vs Asking: ${dashboard.get("computed_metrics", {}).get("value_gap_vs_asking") or "N/A"}

CAP RATE SENSITIVITY (implied values at different exit caps):
{cap_table}

SENSITIVITY ANALYSIS (pre-formatted as markdown table — copy verbatim into Section 7):
{sens_table}

HOMEHARVEST COMPARABLE MARKET DATA:
{comps_ctx}

PROFESSIONAL KNOWLEDGE BASE (apply as your own expertise):
{book_ctx[:2500]}
{memo_format}

Write the Investment Committee Memo in Markdown. Structure:

# Investment Committee Memorandum
## {deal.get("property_name", "Subject Property")} | {deal.get("location", "Location TBD")}
*Prepared by DealDesk AI · {deal.get("property_type", "CRE").title()} · Strictly Confidential*

---

## 1. Executive Summary
[4-5 sentences. What is the deal, key financial metrics, location score, risk score, and the recommendation.
Be direct — state the recommendation upfront like a real IC memo does.]

## 2. Deal Overview
[Property details, sponsor, location, size, vintage, asset quality. Be specific.]

## 3. Financial Analysis
[Deep dive into NOI, cap rate spread vs market, DSCR, LTV, cash-on-cash, debt yield.
Interpret each metric — what does it mean for this specific deal?
Comment on the cap rate spread vs current treasury rates.
Include implied value analysis if cap rates differ from asking price.]

## 4. Market & Macro Context
[Rate environment analysis — how do current rates affect this deal's debt service?
Local market supply/demand dynamics. Cite the web search cap rate data.
Reference comparable sales if available.]

## 5. Location Analysis
[Location score interpretation. Which amenities support or threaten the investment thesis?
How does location quality interact with the financial metrics?
Be specific — mention actual amenity counts.]

## 6. Risk Assessment
[Walk through the top 3-4 red flags. Explain why each matters for THIS deal.
How do location fundamentals amplify or mitigate financial risks?
Risk score context — what does 5.8/10 actually mean for an IC?]

## 7. Sensitivity Analysis
[Table format. Best case (5% vacancy), base case (10%), stress (20%), severe (25%).
State clearly which scenarios cover debt service and which don't.]

## 8. IC Recommendation
[ONE clear verdict: Approve / Approve with Conditions / Decline / Further Diligence Required]
[3-4 specific, actionable conditions or reasons]
[Return expectations — what IRR profile does this deal represent?]

---
*Data Sources: Offering Memorandum · FRED (Federal Reserve) · OpenStreetMap · Realtor.com · Web Market Data*

WRITING RULES:
- Minimum 700 words across all 8 sections
- Every claim backed by a specific number from the data
- No generic statements — be deal-specific throughout  
- Section 4 MUST reference HomeHarvest comps if available
- Section 7 MUST contain the sensitivity table exactly as provided above
- Section 8 MUST start with a bolded verdict: **VERDICT: [Approve/Decline/etc]**
- If a metric is N/A or 0, explain WHY it is missing and what that means for the deal
- IC Recommendation must include 3-5 numbered, actionable conditions"""

    memo = gemini_generate(prompt, temperature=0.1, max_tokens=16000)  # ✅ FIX: was 4096 — too small for full memo
    ok("Investment memo written")
    return memo

# ══════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════
def run_pipeline(om_pdf_path: str, memo_example_path: str = None,
                 price_override: float = None, output_dir: str = "outputs"):
    header("DEALDESK AI — FULL PIPELINE v2")
    print(f"  Fixes active: Vision PDF · OSM · Web Comps · HomeHarvest · Chrome PDF")
    t0 = time.time()

    if not os.path.exists(om_pdf_path):
        err(f"OM not found: {om_pdf_path}"); sys.exit(1)

    # Load memo example for format reference
    memo_ex = ""
    if memo_example_path and os.path.exists(memo_example_path):
        memo_ex = extract_pdf_text(memo_example_path)[:5000]
        ok("Memo example loaded")
    else:
        memos_dir = "data/memos"
        if os.path.exists(memos_dir):
            memo_files = [f for f in os.listdir(memos_dir) if f.endswith(".pdf")]
            if memo_files:
                try:
                    memo_ex = extract_pdf_text(os.path.join(memos_dir, memo_files[0]))[:5000]
                    ok(f"Auto-loaded memo format: {memo_files[0]}")
                except:
                    pass

    # ── Run agents ────────────────────────────────────────────────────────────
    deal      = agent_doc_parser(pdf_path=om_pdf_path)           # Agent 1 — Vision-enabled
    market    = agent_market_intel(deal)                          # Agent 2 — FRED + Web + HomeHarvest
    location  = agent_location_intelligence(deal)                 # Agent 3 — Fixed OSM
    dashboard = agent_financial_dashboard(deal, market, price_override=price_override)  # Agent 4 — Pure Python
    risk      = agent_risk_engine(deal, market, location, dashboard)  # Agent 5 — All data
    memo      = orchestrator_write_memo(                          # Orchestrator
                    deal, market, location, risk, dashboard, memo_ex
                )

    # ── Save outputs ──────────────────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)

    with open(os.path.join(output_dir, "investment_memo.md"), "w", encoding="utf-8") as f:
        f.write(memo)
    with open(os.path.join(output_dir, "risk_report.json"), "w") as f:
        json.dump(risk, f, indent=2)
    with open(os.path.join(output_dir, "financial_dashboard.json"), "w") as f:
        json.dump(dashboard, f, indent=2)
    with open(os.path.join(output_dir, "deal_metrics.json"), "w") as f:
        json.dump(deal, f, indent=2)
    with open(os.path.join(output_dir, "location_intelligence.json"), "w") as f:
        loc_out = {k: v for k, v in location.items() if k != "map_image_path"}
        json.dump(loc_out, f, indent=2)

    elapsed = round(time.time() - t0, 1)
    header("PIPELINE COMPLETE")
    ok(f"Total time: {elapsed}s")
    ok(f"Risk Score:     {risk.get('composite_score')}/10 ({risk.get('risk_level')})")
    ok(f"Location Score: {location.get('location_score')}/10 ({location.get('location_grade')})")
    ok(f"{output_dir}/investment_memo.md")
    ok(f"{output_dir}/risk_report.json")
    ok(f"{output_dir}/financial_dashboard.json")
    ok(f"{output_dir}/location_intelligence.json")
    ok(f"{output_dir}/deal_metrics.json")
    if location.get("map_image_path"):
        ok(f"{output_dir}/location_map.png")
    print(f"\n  {BOLD}Next:{RESET} python agents/generate_pdf.py\n")

    return {"memo": memo, "risk": risk, "dashboard": dashboard, "location": location, "deal": deal}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DealDesk AI Pipeline")
    parser.add_argument("om_pdf",                                   help="Path to Offering Memorandum PDF")
    parser.add_argument("--memo-example",   default=None,           help="Example IC memo PDF (format reference)")
    parser.add_argument("--price-override", type=float, default=None, help="Known asking price — skips estimation")
    parser.add_argument("--output-dir",     default="outputs",      help="Directory for agent output files")
    args = parser.parse_args()

    run_pipeline(
        om_pdf_path=args.om_pdf,
        memo_example_path=args.memo_example,
        price_override=args.price_override,
        output_dir=args.output_dir,
    )
