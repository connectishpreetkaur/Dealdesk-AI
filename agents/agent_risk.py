"""
DealDesk AI — Agent 5: Risk Scoring Engine (7 Rules)
=====================================================
Sees ALL prior agent outputs:
- Agent 1: Deal metrics from OM
- Agent 2: Market & macro data
- Agent 3: Location intelligence
- Agent 4: Financial dashboard

7 weighted red flag rules:
1. Tenant Concentration Risk     (0.18)
2. DSCR / Loan Risk             (0.22)
3. Market Vacancy Risk          (0.15)
4. Occupancy Risk               (0.15)
5. Weak Cash Flow Risk          (0.12)
6. Location Risk                (0.10)
7. Asset-Location Mismatch      (0.08)
"""

import os, sys, json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini_client import gemini_generate, retrieve_book_context_safe

GREEN  = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"
BOLD   = "\033[1m";  RESET  = "\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

def agent_risk_engine(deal: dict, market: dict,
                      location: dict, dashboard: dict) -> dict:
    header("AGENT 5 — Risk Scoring Engine (7 Rules)")
    print("  Analysing deal across all data sources...")

    red_flags = []
    scores    = {}

    # ── Rule 1: Tenant Concentration ─────────────────────────────────────────
    tenants = deal.get("tenants", [])
    if   len(tenants) <= 1: s1=9;  red_flags.append("CRITICAL: Single/no tenant — extreme concentration risk")
    elif len(tenants) <= 3: s1=6;  red_flags.append(f"HIGH: Only {len(tenants)} tenants — concentration risk")
    elif len(tenants) <= 5: s1=4
    else:                   s1=2
    scores["tenant_concentration"] = {"score": s1, "weight": 0.18,
                                       "label": "Tenant Concentration"}

    # ── Rule 2: DSCR / Loan Risk ──────────────────────────────────────────────
    dscr = deal.get("dscr")
    if   dscr is None:  s2=5; warn("DSCR not found — assigned medium risk")
    elif dscr < 1.0:    s2=10; red_flags.append("CRITICAL: DSCR < 1.0 — NOI cannot cover debt service")
    elif dscr < 1.10:   s2=9;  red_flags.append("CRITICAL: DSCR < 1.10 — dangerously thin debt coverage")
    elif dscr < 1.20:   s2=7;  red_flags.append("HIGH: DSCR < 1.20 — limited cushion for income decline")
    elif dscr < 1.25:   s2=5
    elif dscr < 1.40:   s2=3
    else:               s2=1
    scores["dscr_loan_risk"] = {"score": s2, "weight": 0.22,
                                 "label": "DSCR / Loan Risk"}

    # ── Rule 3: Market Vacancy Risk ───────────────────────────────────────────
    mr = market.get("analysis", {}).get("market_risk_level", "Medium")
    if   mr == "High":   s3=8; red_flags.append("HIGH: Market conditions classified as high risk")
    elif mr == "Medium": s3=5
    else:                s3=2
    scores["market_vacancy_risk"] = {"score": s3, "weight": 0.15,
                                      "label": "Market Vacancy Risk"}

    # ── Rule 4: Occupancy Risk ────────────────────────────────────────────────
    occ = deal.get("occupancy_rate") or (
        1 - deal.get("vacancy_rate", 0) if deal.get("vacancy_rate") else None
    )
    if   occ is None: s4=5
    elif occ < 0.70:  s4=10; red_flags.append("CRITICAL: Occupancy below 70%")
    elif occ < 0.80:  s4=8;  red_flags.append("HIGH: Occupancy below 80%")
    elif occ < 0.85:  s4=6;  red_flags.append("ELEVATED: Occupancy below 85%")
    elif occ < 0.90:  s4=4
    else:             s4=2
    scores["occupancy_risk"] = {"score": s4, "weight": 0.15,
                                 "label": "Occupancy Risk"}

    # ── Rule 5: Weak Cash Flow Risk ───────────────────────────────────────────
    cap_in  = deal.get("cap_rate_inplace")
    cap_mkt = deal.get("cap_rate_market")
    noi     = deal.get("noi", 0) or 0
    debt    = deal.get("debt_service_annual", 0) or 0
    if cap_in and cap_mkt:
        spread = cap_in - cap_mkt
        if   spread < -0.015: s5=9; red_flags.append("CRITICAL: In-place cap rate >150bps below market")
        elif spread < 0:      s5=7; red_flags.append("HIGH: In-place cap rate below market — buying at premium")
        elif spread < 0.005:  s5=5
        else:                 s5=2
    elif noi and debt:
        ratio = noi / debt
        if   ratio < 1.0:  s5=10; red_flags.append("CRITICAL: NOI insufficient to cover debt")
        elif ratio < 1.15: s5=7
        else:              s5=3
    else:
        s5=5
    scores["weak_cashflow_risk"] = {"score": s5, "weight": 0.12,
                                     "label": "Weak Cash Flow Risk"}

    # ── Rule 6: Location Risk ─────────────────────────────────────────────────
    loc_score = location.get("location_score", 5.0)
    loc_grade = location.get("location_grade", "C+")
    if   loc_score < 3.0: s6=10; red_flags.append(f"CRITICAL: Location score {loc_score}/10 ({loc_grade}) — fundamental location risk")
    elif loc_score < 4.0: s6=8;  red_flags.append(f"HIGH: Location score {loc_score}/10 ({loc_grade}) — below institutional threshold")
    elif loc_score < 5.5: s6=6;  red_flags.append(f"ELEVATED: Location score {loc_score}/10 ({loc_grade}) — below average fundamentals")
    elif loc_score < 7.0: s6=4
    elif loc_score < 8.5: s6=2
    else:                 s6=1
    scores["location_risk"] = {"score": s6, "weight": 0.10,
                                "label": "Location Risk"}

    # ── Rule 7: Asset-Location Mismatch ──────────────────────────────────────
    property_type = deal.get("property_type", "multifamily").lower()
    cat_scores    = location.get("category_scores", {})

    s7 = 3  # default — no mismatch
    if "multifamily" in property_type or "residential" in property_type:
        transit = cat_scores.get("transit_accessibility", 5)
        if transit < 3.0:
            s7 = 9
            red_flags.append(f"CRITICAL: Multifamily + transit score {transit}/10 — demand risk amplified")
        elif transit < 5.0:
            s7 = 6
            red_flags.append(f"HIGH: Multifamily transit score {transit}/10 — below market expectation")
        else:
            s7 = 2

    elif "office" in property_type:
        emp = cat_scores.get("employment_proximity", 5)
        if emp < 4.0:
            s7 = 9
            red_flags.append(f"CRITICAL: Office + employment score {emp}/10 — occupancy risk")
        elif emp < 6.0:
            s7 = 6
            red_flags.append(f"HIGH: Office employment proximity score {emp}/10 — below benchmark")
        else:
            s7 = 2

    elif "retail" in property_type:
        walk = cat_scores.get("retail_walkability", 5)
        if walk < 4.0:
            s7 = 10
            red_flags.append(f"CRITICAL: Retail + walkability score {walk}/10 — fundamental viability risk")
        elif walk < 6.0:
            s7 = 7
            red_flags.append(f"HIGH: Retail walkability score {walk}/10 — below market standard")
        else:
            s7 = 2

    elif "industrial" in property_type:
        s7 = 2  # industrial cares less about most location factors

    scores["asset_location_mismatch"] = {"score": s7, "weight": 0.08,
                                          "label": "Asset-Location Mismatch"}

    # ── Composite score ───────────────────────────────────────────────────────
    composite = round(sum(v["score"] * v["weight"] for v in scores.values()), 1)
    level = ("Low" if composite < 3 else
             "Medium" if composite < 6 else
             "High" if composite < 8 else "Critical")

    ok(f"Composite risk score: {composite}/10 ({level})")
    if red_flags:
        print(f"\n  {RED}{BOLD}Red Flags Triggered ({len(red_flags)}):{RESET}")
        for f in red_flags:
            print(f"    {RED}⚑  {f}{RESET}")

    # ── Gemini: integrated narrative (uses all agent data) ────────────────────
    print("\n  Writing integrated risk narrative...")

    # Get book context
    try:
        book_ctx = retrieve_book_context_safe(
            "DSCR thresholds vacancy risk tenant concentration location "
            "underwriting standards real estate risk assessment"
        )
    except:
        book_ctx = "Expert context unavailable — proceeding with data-driven assessment."

    # Pull key data points for narrative
    loc_summary  = location.get("location_summary", "")
    loc_flags    = location.get("red_flags", [])
    market_notes = market.get("analysis", {}).get("market_notes", [])
    fed_rate     = market.get("fed_funds_rate", {}).get("value", "N/A")
    treasury     = market.get("ten_year_treasury", {}).get("value", "N/A")
    cf_metrics   = dashboard.get("computed_metrics", {})
    coc          = cf_metrics.get("cash_on_cash_pct")
    dy           = cf_metrics.get("debt_yield_pct")
    beo          = cf_metrics.get("break_even_occupancy_pct")

    prompt = f"""
You are a senior risk analyst at a top-tier CRE private equity fund.
Write an integrated risk assessment narrative for this deal.
You have access to ALL data sources — deal metrics, macro data, location intelligence, and financial analysis.

RISK SCORES (7-Rule Model):
{json.dumps({k: {"score": v["score"], "label": v["label"]} for k,v in scores.items()}, indent=2)}

COMPOSITE RISK SCORE: {composite}/10 ({level} Risk)

RED FLAGS TRIGGERED ({len(red_flags)}):
{json.dumps(red_flags, indent=2)}

DEAL FUNDAMENTALS:
- DSCR: {dscr} | Occupancy: {occ} | Cap Rate (in-place): {cap_in} | Cap Rate (market): {cap_mkt}
- Cash-on-Cash: {coc}% | Debt Yield: {dy}% | Break-even Occupancy: {beo}%

MACRO ENVIRONMENT:
- Fed Funds Rate: {fed_rate}% | 10Y Treasury: {treasury}%
- Market Risk Level: {mr}
- Market Notes: {market_notes[:2] if market_notes else 'N/A'}

LOCATION INTELLIGENCE:
- Location Score: {loc_score}/10 ({loc_grade})
- {loc_summary}
- Location Red Flags: {loc_flags}

EXPERT CONTEXT FROM CRE LITERATURE (Geltner, Linneman, Gallinelli):
{book_ctx[:1500]}

Write 4 paragraphs of integrated risk assessment in clear professional English.
- Paragraph 1: Overall risk picture and key themes
- Paragraph 2: Financial risk analysis (DSCR, cash flow, cap rate spread)
- Paragraph 3: Location and market risk — explicitly cite the location score and how it interacts with financial metrics
- Paragraph 4: IC recommendation with specific conditions

Rules:
- Cite actual numbers throughout
- Reference the book literature where relevant (e.g., "Per Gallinelli's framework...")
- The location score MUST be cited in the narrative — this shows integrated reasoning
- End with exactly one sentence: IC Recommendation: [Approve / Approve with Conditions / Decline / Further Diligence Required]
- No bullet points. Professional prose only.
"""
    narrative = gemini_generate(prompt, temperature=0.2, max_tokens=2048)
    ok("Risk narrative complete")

    return {
        "composite_score":    composite,
        "risk_level":         level,
        "individual_scores":  scores,
        "red_flags":          red_flags,
        "raw_red_flags_om":   deal.get("red_flags_raw", []),
        "location_red_flags": loc_flags,
        "narrative":          narrative,
        "data_sources_used":  ["OM metrics", "FRED macro", "OSM location", "financial dashboard"]
    }
