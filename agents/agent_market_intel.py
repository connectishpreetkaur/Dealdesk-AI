"""
DealDesk AI — Agent 2: Market Intelligence
==========================================
Pulls:
1. Live macro data from FRED API
2. Current cap rates + property comps via Gemini web search grounding
3. HomeHarvest sales & rent comps (US properties)
4. Market benchmarking via Gemini + RAG
"""

import os, sys, json, time, requests
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini_client import gemini_generate, parse_json_robust, retrieve_book_context_safe
from dotenv import load_dotenv
load_dotenv()

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

FRED_KEY = os.getenv("FRED_API_KEY")

# ── FRED macro data ───────────────────────────────────────────────────────────
def fetch_fred_data() -> dict:
    """Fetch live macro indicators from Federal Reserve FRED API."""
    market_data = {}
    if not FRED_KEY:
        warn("No FRED_API_KEY — using placeholder macro data")
        return {
            "fed_funds_rate":    {"value": 5.33, "date": "2024-01", "source": "placeholder"},
            "ten_year_treasury": {"value": 4.25, "date": "2024-01", "source": "placeholder"},
            "cpi":               {"value": 3.1,  "date": "2024-01", "source": "placeholder"},
            "unemployment":      {"value": 3.7,  "date": "2024-01", "source": "placeholder"},
        }

    print("  Fetching live macro data from FRED (Federal Reserve)...")
    series = {
        "fed_funds_rate":    "FEDFUNDS",
        "ten_year_treasury": "GS10",
        "cpi":               "CPIAUCSL",
        "unemployment":      "UNRATE",
        "gdp_growth":        "A191RL1Q225SBEA",
        "commercial_re_index": "COMREAINTUSQ159N",
    }
    for label, sid in series.items():
        try:
            r   = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": sid, "api_key": FRED_KEY, "file_type": "json",
                        "limit": 1, "sort_order": "desc", "observation_start": "2023-01-01"},
                timeout=10
            )
            obs = r.json().get("observations", [])
            if obs and obs[0]["value"] != ".":
                market_data[label] = {
                    "value":  float(obs[0]["value"]),
                    "date":   obs[0]["date"],
                    "source": "FRED"
                }
                ok(f"FRED {label}: {obs[0]['value']} ({obs[0]['date']})")
            time.sleep(0.3)
        except Exception as e:
            warn(f"FRED {label}: {e}")

    return market_data

# ── HomeHarvest comps ─────────────────────────────────────────────────────────
def fetch_homeharvest_comps(location: str, property_type: str) -> dict:
    """
    Pull real sales and rental comps using HomeHarvest (Realtor.com scraper).
    US properties only. Gracefully fails for international.
    """
    comps_data = {"available": False, "reason": "Not attempted", "sales": [], "rentals": []}

    # Check if location appears to be US
    us_indicators = [
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
        "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
        "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
        "VA","WA","WV","WI","WY","DC"
    ]
    location_upper = location.upper()
    
    # Check for state abbreviations
    state_match = any(f", {state}" in location_upper or f" {state} " in location_upper
                      or f" {state}," in location_upper or location_upper.endswith(f" {state}")
                      for state in us_indicators)
    
    # Check for known US city names in the location string
    us_city_hints = ["chicago", "new york", "los angeles", "houston", "phoenix", "philadelphia",
                     "san antonio", "san diego", "dallas", "austin", "jacksonville", "fort worth",
                     "columbus", "charlotte", "indianapolis", "san francisco", "seattle", "denver",
                     "nashville", "oklahoma", "miami", "atlanta", "minneapolis", "suburban",
                     "illinois", "texas", "california", "florida", "new york", "ohio", "georgia",
                     "dupage", "cook county", "harris county", "maricopa", "wayne county"]
    city_match = any(hint in location_upper.lower() for hint in us_city_hints)
    
    is_us = state_match or city_match

    if not is_us:
        warn(f"HomeHarvest: '{location}' appears non-US — skipping comps")
        comps_data["reason"] = "International property — HomeHarvest US only"
        return comps_data
    
    # Clean up the location string for HomeHarvest
    # Extract just "City, State" from verbose location strings
    import re
    city_state_match = re.search(r'([A-Za-z\s]+),\s*([A-Z]{2})', location)
    if city_state_match:
        location = f"{city_state_match.group(1).strip()}, {city_state_match.group(2)}"
    elif "chicago" in location.lower() or "dupage" in location.lower():
        location = "Villa Park, IL"  # clean fallback for Chicago suburbs
    elif "suburb" in location.lower():
        # Try to extract first recognizable city name
        parts = [p.strip() for p in location.split(",")]
        location = parts[0] if parts else location
    warn(f"HomeHarvest: querying cleaned location: '{location}'")

    try:
        from homeharvest import scrape_property
        import pandas as pd

        # Extract city/state from location string
        city_state = location.strip()
        print(f"  Fetching property comps from Realtor.com for {city_state}...")

        # Map property type to HomeHarvest listing type
        listing_type_map = {
            "multifamily": "sold",
            "office":      "sold",
            "retail":      "sold",
            "industrial":  "sold",
            "mixed-use":   "sold",
        }
        listing_type = listing_type_map.get(property_type.lower(), "sold")

        # Fetch recently sold properties
        try:
            sold_props = scrape_property(
                location=city_state,
                listing_type="sold",
                past_days=180,  # last 6 months
            )
            if sold_props is not None and len(sold_props) > 0:
                # Take top 5 most relevant
                top_sold = sold_props.head(5)
                sales_list = []
                for _, row in top_sold.iterrows():
                    sales_list.append({
                        "address":    str(row.get("street", "N/A")),
                        "price":      float(row.get("list_price", 0) or 0),
                        "beds":       str(row.get("beds", "N/A")),
                        "sqft":       float(row.get("sqft", 0) or 0),
                        "price_sqft": float(row.get("price_per_sqft", 0) or 0),
                        "sold_date":  str(row.get("sold_date", "N/A")),
                        "status":     "SOLD"
                    })
                comps_data["sales"] = sales_list
                ok(f"HomeHarvest: {len(sales_list)} sold comps found")
            else:
                warn("HomeHarvest: No sold comps found for this location")
        except Exception as e:
            warn(f"HomeHarvest sold query failed: {e}")

        # Fetch for-rent listings for rent comps
        try:
            rent_props = scrape_property(
                location=city_state,
                listing_type="for_rent",
                past_days=30,
            )
            if rent_props is not None and len(rent_props) > 0:
                top_rent = rent_props.head(5)
                rent_list = []
                for _, row in top_rent.iterrows():
                    rent_list.append({
                        "address":    str(row.get("street", "N/A")),
                        "rent":       float(row.get("list_price", 0) or 0),
                        "beds":       str(row.get("beds", "N/A")),
                        "sqft":       float(row.get("sqft", 0) or 0),
                        "rent_sqft":  float(row.get("price_per_sqft", 0) or 0),
                        "status":     "FOR_RENT"
                    })
                comps_data["rentals"] = rent_list
                ok(f"HomeHarvest: {len(rent_list)} rental comps found")
        except Exception as e:
            warn(f"HomeHarvest rental query failed: {e}")

        comps_data["available"] = len(comps_data["sales"]) > 0 or len(comps_data["rentals"]) > 0
        comps_data["reason"]    = "Successfully fetched" if comps_data["available"] else "No data found"

    except ImportError:
        warn("HomeHarvest not installed — run: pip install homeharvest")
        comps_data["reason"] = "HomeHarvest not installed"
    except Exception as e:
        warn(f"HomeHarvest failed: {e}")
        comps_data["reason"] = str(e)

    return comps_data

# ── Gemini web search for cap rates ──────────────────────────────────────────
def fetch_web_cap_rates(location: str, property_type: str) -> dict:
    """
    Use Gemini with Google Search grounding to get current cap rates
    and market conditions for the specific market.
    """
    print(f"  Searching web for current {property_type} cap rates in {location}...")

    from google import genai
    from google.genai import types
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    client  = genai.Client(api_key=api_key)

    prompt = f"""Search for and provide current commercial real estate market data for {location}.
Specifically find:
1. Current {property_type} cap rates in {location} (2024-2025 data)
2. Recent comparable {property_type} sales in {location} with prices
3. Current vacancy rates for {property_type} in {location}
4. Rent growth trends for {property_type} in {location}
5. New supply pipeline for {property_type} in {location}

Return a JSON object with this structure:
{{
  "market_cap_rate_low": decimal or null,
  "market_cap_rate_mid": decimal or null,
  "market_cap_rate_high": decimal or null,
  "vacancy_rate_market": decimal or null,
  "rent_growth_yoy": decimal or null,
  "market_trend": "string describing current market direction",
  "supply_pipeline": "string describing new construction/supply",
  "recent_sales_summary": "string summarizing comparable sales found",
  "data_sources": ["list of sources found"],
  "market_notes": ["3-5 key market observations"]
}}

Return ONLY the JSON object. No markdown."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=2048,
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        raw    = response.text.strip()
        result = parse_json_robust(raw)
        ok(f"Web search: cap rate range {result.get('market_cap_rate_low')} - {result.get('market_cap_rate_high')}")
        return result
    except Exception as e:
        warn(f"Web search cap rates failed: {e} — using Gemini knowledge only")
        # Fallback to knowledge-based estimate
        fallback_prompt = f"""Based on your knowledge of CRE markets, what are typical {property_type} 
cap rates in {location} as of 2024-2025?
Return ONLY a JSON object:
{{
  "market_cap_rate_low": decimal,
  "market_cap_rate_mid": decimal, 
  "market_cap_rate_high": decimal,
  "market_trend": "string",
  "market_notes": ["note1", "note2"]
}}"""
        try:
            raw    = gemini_generate(fallback_prompt, temperature=0.1)
            return parse_json_robust(raw)
        except:
            return {"market_cap_rate_mid": None, "market_notes": ["Market data unavailable"]}

# ── Main market intel agent ───────────────────────────────────────────────────
def agent_market_intel(deal: dict) -> dict:
    header("AGENT 2 — Market Intelligence")

    location      = deal.get("location", "United States")
    property_type = deal.get("property_type", "multifamily")

    # 1. FRED macro data
    market_data = fetch_fred_data()

    # 2. Web search for current cap rates (Issue 7 fix)
    web_comps = fetch_web_cap_rates(location, property_type)
    market_data["web_cap_rates"] = web_comps

    # 3. HomeHarvest property comps (Issue 8)
    hh_comps = fetch_homeharvest_comps(location, property_type)
    market_data["property_comps"] = hh_comps

    # 4. Gemini market benchmarking with all gathered data
    ctx = retrieve_book_context_safe(
        f"cap rate benchmarks {property_type} market conditions "
        f"interest rates real estate cycle vacancy analysis"
    )

    fed_rate   = market_data.get("fed_funds_rate", {}).get("value", "N/A")
    treasury   = market_data.get("ten_year_treasury", {}).get("value", "N/A")
    cap_in     = deal.get("cap_rate_inplace")
    cap_mkt    = deal.get("cap_rate_market")
    web_cap_mid = web_comps.get("market_cap_rate_mid")

    prompt = f"""You are a senior CRE market analyst with deep expertise in {property_type} markets.
Analyse this deal's market context using all available data.

MACRO ENVIRONMENT (Federal Reserve data):
- Fed Funds Rate: {fed_rate}%
- 10-Year Treasury: {treasury}%
- CPI: {market_data.get("cpi", {}).get("value", "N/A")}
- Unemployment: {market_data.get("unemployment", {}).get("value", "N/A")}%

SUBJECT PROPERTY:
- Type: {property_type}
- Location: {location}
- In-place cap rate: {cap_in}
- Stated market cap rate: {cap_mkt}
- Asking price: {str(deal.get("asking_price") or "N/A")}
- NOI: {str(deal.get("noi") or "N/A")}

CURRENT MARKET DATA (from web search):
- Market cap rate range: {web_comps.get("market_cap_rate_low")} - {web_comps.get("market_cap_rate_high")}
- Market vacancy: {web_comps.get("vacancy_rate_market")}
- Rent growth YoY: {web_comps.get("rent_growth_yoy")}
- Market trend: {web_comps.get("market_trend", "N/A")}

PROPERTY COMPS:
{json.dumps(hh_comps.get("sales", [])[:3], indent=2) if hh_comps.get("sales") else "No comp data available for this market"}

ANALYTICAL FRAMEWORK (apply these principles, do not cite them):
{ctx[:1200]}

Return ONLY a valid JSON object:
{{
  "market_assessment": "2-3 sentence professional market assessment",
  "cap_rate_verdict": "is the in-place cap rate attractive/fair/expensive vs current market?",
  "cap_rate_spread_vs_treasury": "spread analysis — is the risk premium adequate?",
  "interest_rate_impact": "how does the current rate environment affect this deal's debt service and valuation?",
  "market_cycle_position": "expansion|peak|contraction|recovery",
  "demand_supply_outlook": "professional assessment of supply/demand dynamics",
  "market_risk_level": "Low|Medium|High",
  "comparable_cap_rates": {{"low": 0.04, "mid": 0.05, "high": 0.06}},
  "rent_growth_outlook": "professional rent growth assessment",
  "market_notes": ["5 specific, data-backed market observations — be concrete, not generic"]
}}"""

    raw    = gemini_generate(prompt, temperature=0.2, max_tokens=2048)
    market_data["analysis"] = parse_json_robust(raw)
    ok("Market intelligence complete")
    return market_data
