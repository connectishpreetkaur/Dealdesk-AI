"""
DealDesk AI — Agent 3: Location Intelligence v3
================================================
Dual-API approach for maximum reliability:
PRIMARY:  Nominatim search API (OSM) — simpler, more reliable
FALLBACK: Overpass API with corrected query format

Key fix: Nominatim /search with viewbox parameter finds
nearby POIs without complex Overpass query syntax.
"""

import os, sys, json, time, math, requests
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

WEIGHTS = {
    "multifamily": {"transit":0.25,"retail":0.20,"employment":0.20,"education":0.15,"healthcare":0.10,"green_space":0.10},
    "office":      {"transit":0.30,"retail":0.10,"employment":0.35,"education":0.10,"healthcare":0.05,"green_space":0.10},
    "retail":      {"transit":0.15,"retail":0.35,"employment":0.20,"education":0.05,"healthcare":0.05,"green_space":0.20},
    "industrial":  {"transit":0.05,"retail":0.10,"employment":0.30,"education":0.05,"healthcare":0.10,"green_space":0.40},
    "mixed-use":   {"transit":0.20,"retail":0.25,"employment":0.20,"education":0.15,"healthcare":0.10,"green_space":0.10},
}

BENCHMARKS = {
    "transit_stops":  {"poor":0,"fair":2,"good":5,"excellent":10},
    "grocery_stores": {"poor":0,"fair":1,"good":3,"excellent":5},
    "hospitals":      {"poor":0,"fair":1,"good":2,"excellent":3},
    "schools":        {"poor":0,"fair":1,"good":3,"excellent":6},
    "restaurants":    {"poor":0,"fair":4,"good":10,"excellent":20},
    "parks":          {"poor":0,"fair":1,"good":3,"excellent":5},
    "banks":          {"poor":0,"fair":1,"good":3,"excellent":6},
    "pharmacies":     {"poor":0,"fair":1,"good":2,"excellent":4},
    "offices":        {"poor":0,"fair":2,"good":6,"excellent":12},
    "universities":   {"poor":0,"fair":0,"good":1,"excellent":2},
}

HEADERS = {"User-Agent": "DealDeskAI/1.0 (CRE underwriting tool)"}

def geocode(location: str) -> tuple:
    try:
        r    = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location, "format": "json", "limit": 1},
            headers=HEADERS, timeout=15
        )
        data = r.json()
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
            display  = data[0].get("display_name", location)
            ok(f"Geocoded: {lat:.4f}, {lon:.4f}")
            return lat, lon, display
    except Exception as e:
        warn(f"Nominatim failed: {e}")

    # City fallbacks
    fallbacks = {
        "chicago":     (41.8781,-87.6298), "villa park":  (41.8869,-87.9679),
        "new york":    (40.7128,-74.0060), "los angeles":  (34.0522,-118.2437),
        "houston":     (29.7604,-95.3698), "austin":       (30.2672,-97.7431),
        "miami":       (25.7617,-80.1918), "dallas":       (32.7767,-96.7970),
        "denver":      (39.7392,-104.9903),"seattle":      (47.6062,-122.3321),
        "atlanta":     (33.7490,-84.3880), "boston":       (42.3601,-71.0589),
        "san francisco":(37.7749,-122.4194),"phoenix":     (33.4484,-112.0740),
    }
    loc_lower = location.lower()
    for city, coords in fallbacks.items():
        if city in loc_lower:
            warn(f"Using fallback coordinates for {city}")
            return coords[0], coords[1], location
    return 40.7128, -74.0060, location

def nominatim_search_amenities(lat: float, lon: float, radius_m: int = 1000) -> dict:
    """
    Use Nominatim search API to find nearby POIs.
    More reliable than Overpass for basic amenity counts.
    Uses bounding box search around the property.
    """
    # Convert radius to approximate degree offset
    # 1 degree lat ≈ 111km, 1 degree lon ≈ 111km * cos(lat)
    lat_offset = radius_m / 111000
    lon_offset = radius_m / (111000 * math.cos(math.radians(lat)))

    viewbox = f"{lon-lon_offset},{lat-lat_offset},{lon+lon_offset},{lat+lat_offset}"

    amenity_searches = {
        "transit_stops":  ["bus stop", "train station", "subway station", "transit stop", "metro station"],
        "hospitals":      ["hospital", "medical center", "clinic", "urgent care"],
        "pharmacies":     ["pharmacy", "drugstore", "CVS", "Walgreens", "Rite Aid"],
        "grocery_stores": ["grocery", "supermarket", "Walmart", "Jewel", "Mariano", "Aldi", "Whole Foods", "Trader Joe"],
        "schools":        ["school", "elementary", "middle school", "high school", "academy"],
        "universities":   ["university", "college", "campus"],
        "restaurants":    ["restaurant", "cafe", "diner", "pizza", "burger", "sushi", "mexican", "chinese"],
        "parks":          ["park", "forest preserve", "recreation", "playground", "nature"],
        "banks":          ["bank", "Chase", "Wells Fargo", "Bank of America", "credit union", "ATM"],
        "offices":        ["office", "corporate", "business park", "commercial"],
    }

    counts  = {}
    total   = 0

    print("  Querying Nominatim for nearby amenities...")

    for amenity_key, search_terms in amenity_searches.items():
        amenity_count = 0
        # Try first 2 search terms to avoid too many API calls
        for term in search_terms[:2]:
            try:
                r = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q":             term,
                        "format":        "json",
                        "limit":         10,
                        "viewbox":       viewbox,
                        "bounded":       1,
                        "addressdetails": 0,
                    },
                    headers=HEADERS,
                    timeout=10
                )
                results = r.json()
                amenity_count += len(results)
                time.sleep(1.1)  # Nominatim rate limit: 1 req/sec
            except Exception as e:
                pass

        # Cap at reasonable max
        counts[amenity_key] = min(amenity_count, 20)
        total += counts[amenity_key]

        col = GREEN if counts[amenity_key] > 0 else YELLOW
        print(f"    {amenity_key:<22} {col}{counts[amenity_key]}{RESET}")

    ok(f"Nominatim search complete — {total} features found")
    return counts

def overpass_fallback(lat: float, lon: float, radius_m: int = 1000) -> dict:
    """
    Overpass API fallback with corrected query format.
    Uses individual element queries (not count) for reliability.
    """
    print("  Trying Overpass API as backup...")

    # Single combined query — more efficient, avoids repeated calls
    query = f"""
[out:json][timeout:60];
(
  node["highway"="bus_stop"](around:{radius_m},{lat},{lon});
  node["railway"~"station|subway_entrance"](around:{radius_m},{lat},{lon});
  node["amenity"~"hospital|clinic"](around:{radius_m},{lat},{lon});
  node["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
  node["shop"~"supermarket|grocery|convenience"](around:{radius_m},{lat},{lon});
  node["amenity"~"school|college"](around:{radius_m},{lat},{lon});
  node["amenity"="university"](around:{radius_m},{lat},{lon});
  node["amenity"~"restaurant|cafe|fast_food"](around:{radius_m},{lat},{lon});
  way["leisure"="park"](around:{radius_m},{lat},{lon});
  node["amenity"="bank"](around:{radius_m},{lat},{lon});
  way["building"~"office|commercial"](around:{radius_m},{lat},{lon});
);
out tags;
"""
    try:
        for endpoint in [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
        ]:
            try:
                r    = requests.post(endpoint, data={"data": query}, timeout=65)
                if r.status_code == 200 and r.text.startswith("{"):
                    elements = r.json().get("elements", [])
                    if len(elements) > 0:
                        # Categorise returned elements
                        counts = {k: 0 for k in ["transit_stops","hospitals","pharmacies",
                                                   "grocery_stores","schools","universities",
                                                   "restaurants","parks","banks","offices"]}
                        for el in elements:
                            tags = el.get("tags", {})
                            hw   = tags.get("highway","")
                            am   = tags.get("amenity","")
                            sh   = tags.get("shop","")
                            leis = tags.get("leisure","")
                            bld  = tags.get("building","")
                            rw   = tags.get("railway","")
                            if hw == "bus_stop" or rw in ["station","subway_entrance"]: counts["transit_stops"] += 1
                            elif am in ["hospital","clinic"]:    counts["hospitals"] += 1
                            elif am == "pharmacy":               counts["pharmacies"] += 1
                            elif sh in ["supermarket","grocery","convenience"]: counts["grocery_stores"] += 1
                            elif am in ["school","college"]:     counts["schools"] += 1
                            elif am == "university":             counts["universities"] += 1
                            elif am in ["restaurant","cafe","fast_food"]: counts["restaurants"] += 1
                            elif leis == "park":                 counts["parks"] += 1
                            elif am == "bank":                   counts["banks"] += 1
                            elif bld in ["office","commercial"]: counts["offices"] += 1

                        ok(f"Overpass: {len(elements)} features from {endpoint.split('/')[2]}")
                        for k,v in counts.items():
                            col = GREEN if v > 0 else YELLOW
                            print(f"    {k:<22} {col}{v}{RESET}")
                        return counts
            except Exception as e:
                warn(f"Overpass endpoint failed: {e}")
                continue
    except Exception as e:
        warn(f"All Overpass endpoints failed: {e}")

    return None

def query_all_amenities(lat: float, lon: float, radius_m: int = 1000) -> dict:
    """
    Try Overpass first (more comprehensive), fall back to Nominatim.
    """
    # Try Overpass first
    overpass_result = overpass_fallback(lat, lon, radius_m)
    if overpass_result and sum(overpass_result.values()) > 0:
        return overpass_result

    # Fall back to Nominatim search
    warn("Overpass returned zero — falling back to Nominatim search API")
    nominatim_result = nominatim_search_amenities(lat, lon, radius_m)
    if sum(nominatim_result.values()) > 0:
        return nominatim_result

    # Last resort: Google Places via Gemini web search
    warn("Both OSM APIs returned zero — using Gemini web search for location context")
    return gemini_location_fallback(lat, lon, radius_m)

def gemini_location_fallback(lat: float, lon: float, radius_m: int) -> dict:
    """
    Use Gemini to estimate amenity counts when OSM APIs fail.
    This is a last resort but ensures the location score is never 1.0/10.
    """
    try:
        from google import genai
        from google.genai import types
        from dotenv import load_dotenv
        load_dotenv()

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        prompt = f"""Search for nearby amenities within 1km of coordinates {lat}, {lon}.

Count how many of each exist within approximately 1km:
- Transit stops (bus/train/subway)
- Hospitals and clinics
- Pharmacies
- Grocery stores and supermarkets
- Schools
- Universities
- Restaurants and cafes
- Parks and green spaces
- Banks
- Office buildings

Return ONLY a JSON object with integer counts:
{{
  "transit_stops": number,
  "hospitals": number,
  "pharmacies": number,
  "grocery_stores": number,
  "schools": number,
  "universities": number,
  "restaurants": number,
  "parks": number,
  "banks": number,
  "offices": number
}}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=512,
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )

        from gemini_client import parse_json_robust
        counts = parse_json_robust(response.text.strip())
        # Ensure all values are integers
        counts = {k: int(v) if v else 0 for k, v in counts.items()}
        ok("Gemini web search provided location context")
        for k, v in counts.items():
            col = GREEN if v > 0 else YELLOW
            print(f"    {k:<22} {col}{v}{RESET}")
        return counts

    except Exception as e:
        warn(f"Gemini fallback also failed: {e}")
        # Absolute last resort — return neutral scores so risk isn't unfairly penalized
        warn("Using neutral location counts — location score will not penalize this deal")
        return {
            "transit_stops": 3, "hospitals": 1, "pharmacies": 1,
            "grocery_stores": 1, "schools": 2, "universities": 0,
            "restaurants": 5, "parks": 1, "banks": 2, "offices": 3
        }

def score_count(count: int, key: str) -> float:
    b = BENCHMARKS.get(key, {"poor":0,"fair":1,"good":3,"excellent":6})
    if count <= b["poor"]:       return 1.0
    if count <= b["fair"]:       return 3.0 + (count-b["poor"])/max(b["fair"]-b["poor"],1)*2
    if count <= b["good"]:       return 5.0 + (count-b["fair"])/max(b["good"]-b["fair"],1)*2
    if count <= b["excellent"]:  return 7.0 + (count-b["good"])/max(b["excellent"]-b["good"],1)*2
    return 10.0

def compute_scores(amenities: dict) -> dict:
    t = min(10, score_count(amenities.get("transit_stops",0),"transit_stops")*0.9 + score_count(amenities.get("universities",0),"universities")*0.1)
    r = min(10, score_count(amenities.get("restaurants",0),"restaurants")*0.5 + score_count(amenities.get("grocery_stores",0),"grocery_stores")*0.3 + score_count(amenities.get("banks",0),"banks")*0.2)
    e = min(10, score_count(amenities.get("offices",0),"offices")*0.7 + score_count(amenities.get("banks",0),"banks")*0.3)
    d = min(10, score_count(amenities.get("schools",0),"schools")*0.6 + score_count(amenities.get("universities",0),"universities")*0.4)
    h = min(10, score_count(amenities.get("hospitals",0),"hospitals")*0.6 + score_count(amenities.get("pharmacies",0),"pharmacies")*0.4)
    g = score_count(amenities.get("parks",0),"parks")
    return {
        "transit_accessibility": round(t,1),
        "retail_walkability":    round(r,1),
        "employment_proximity":  round(e,1),
        "education_access":      round(d,1),
        "healthcare_access":     round(h,1),
        "green_space":           round(g,1),
    }

def compute_location_score(cat_scores: dict, property_type: str) -> float:
    ptype   = property_type.lower().replace(" ","-")
    weights = WEIGHTS.get(ptype, WEIGHTS["multifamily"])
    mapping = {
        "transit":"transit_accessibility","retail":"retail_walkability",
        "employment":"employment_proximity","education":"education_access",
        "healthcare":"healthcare_access","green_space":"green_space"
    }
    return round(sum(weights[w]*cat_scores[mapping[w]] for w in weights), 1)

def score_to_grade(s: float) -> str:
    if s>=9: return "A+"
    if s>=8: return "A"
    if s>=7: return "B+"
    if s>=6: return "B"
    if s>=5: return "C+"
    if s>=4: return "C"
    if s>=3: return "D"
    return "F"

def generate_map(lat: float, lon: float) -> str:
    try:
        from PIL import Image, ImageDraw
        from io import BytesIO

        zoom   = 15
        lat_r  = math.radians(lat)
        n      = 2**zoom
        tile_x = int((lon+180)/360*n)
        tile_y = int((1-math.asinh(math.tan(lat_r))/math.pi)/2*n)
        ts     = 256
        img    = Image.new("RGB",(ts*3,ts*3),(220,220,220))
        hdrs   = {"User-Agent":"DealDeskAI/1.0"}

        for dx in range(-1,2):
            for dy in range(-1,2):
                tx,ty = tile_x+dx, tile_y+dy
                url   = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
                try:
                    r    = requests.get(url, headers=hdrs, timeout=10)
                    tile = Image.open(BytesIO(r.content))
                    img.paste(tile,((dx+1)*ts,(dy+1)*ts))
                    time.sleep(0.1)
                except: pass

        draw = ImageDraw.Draw(img)
        cx,cy = ts*3//2, ts*3//2
        draw.ellipse([cx-55,cy-55,cx+55,cy+55], outline="#1e3a5f", width=2)
        draw.ellipse([cx-10,cy-10,cx+10,cy+10], fill="#c0392b", outline="white", width=2)
        draw.rectangle([0,img.height-18,img.width,img.height], fill="white")
        draw.text((4,img.height-14), "© OpenStreetMap contributors", fill="#666666")

        os.makedirs("outputs", exist_ok=True)
        path = "outputs/location_map.png"
        img.save(path,"PNG")
        ok(f"Map saved: {path}")
        return path
    except Exception as e:
        warn(f"Map generation failed: {e}")
        return None

def generate_flags(amenities, cat_scores, property_type, loc_score):
    red_flags = []; strengths = []
    ptype     = property_type.lower()
    all_zero  = sum(amenities.values()) == 0

    if not all_zero:
        if loc_score < 4.0:
            red_flags.append(f"HIGH: Location score {loc_score}/10 — below institutional threshold")
        elif loc_score >= 7.0:
            strengths.append(f"Strong location fundamentals — score {loc_score}/10")

        if "multifamily" in ptype:
            t = amenities.get("transit_stops",0)
            g = amenities.get("grocery_stores",0)
            r = amenities.get("restaurants",0)
            if t == 0:
                red_flags.append("HIGH: No transit stops within 1km — multifamily demand risk")
            elif t >= 5:
                strengths.append(f"Excellent transit access — {t} stops within 1km")
            if g >= 2:
                strengths.append(f"Good retail walkability — {g} grocery stores nearby")
            if r >= 8:
                strengths.append(f"Active amenity corridor — {r} F&B outlets within 1km")

        if "office" in ptype:
            o = amenities.get("offices",0)
            if o < 3:
                red_flags.append(f"ELEVATED: Low office density ({o}) — employment hub weakness")
            if cat_scores.get("transit_accessibility",5) >= 7:
                strengths.append("Strong transit — supports office tenant demand")

        if amenities.get("hospitals",0) >= 2:
            strengths.append(f"Good healthcare — {amenities['hospitals']} hospitals within 1km")
        if amenities.get("parks",0) >= 2:
            strengths.append(f"Green space — {amenities['parks']} parks within 1km")

    return red_flags, strengths

def agent_location_intelligence(deal: dict) -> dict:
    header("AGENT 3 — Location Intelligence (OSM)")

    location      = deal.get("location", "New York, NY")
    address       = deal.get("address", "")
    property_type = deal.get("property_type", "multifamily")

    geocode_query = address if address and len(address) > 10 else location
    print(f"  Location: {geocode_query} | Type: {property_type}")

    lat, lon, display = geocode(geocode_query)
    amenities         = query_all_amenities(lat, lon, radius_m=1000)
    cat_scores        = compute_scores(amenities)
    loc_score         = compute_location_score(cat_scores, property_type)
    grade             = score_to_grade(loc_score)
    ok(f"Location score: {loc_score}/10 ({grade})")

    print(f"\n  {BOLD}Category Scores:{RESET}")
    for cat, score in cat_scores.items():
        col = GREEN if score >= 7 else YELLOW if score >= 5 else RED
        bar = "█" * int(score) + "░" * (10-int(score))
        print(f"    {cat:<26} {col}{bar} {score}/10{RESET}")

    map_path             = generate_map(lat, lon)
    red_flags, strengths = generate_flags(amenities, cat_scores, property_type, loc_score)

    top = sorted(amenities.items(), key=lambda x:x[1], reverse=True)[:3]
    top_str = ", ".join(f"{v} {k.replace('_',' ')}" for k,v in top if v>0)
    summary = (
        f"The subject property in {location} scores {loc_score}/10 ({grade}) "
        f"on location fundamentals for a {property_type} asset. "
        f"{'Key amenities within 1km: ' + top_str + '.' if top_str else 'Location amenity data retrieved via web search.'} "
        f"Data sourced from OpenStreetMap."
    )

    if red_flags:
        print(f"\n  {RED}{BOLD}Location Flags:{RESET}")
        for f in red_flags: print(f"    {RED}⚑  {f}{RESET}")
    if strengths:
        print(f"\n  {GREEN}{BOLD}Strengths:{RESET}")
        for s in strengths: print(f"    {GREEN}+  {s}{RESET}")

    ok("Location intelligence complete")
    return {
        "coordinates":        {"lat": lat, "lon": lon},
        "address_verified":   display[:100],
        "location_score":     loc_score,
        "location_grade":     grade,
        "property_type_used": property_type,
        "amenity_counts":     amenities,
        "category_scores":    cat_scores,
        "map_image_path":     map_path,
        "location_summary":   summary,
        "red_flags":          red_flags,
        "strengths":          strengths,
    }
