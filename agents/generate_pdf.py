"""
DealDesk AI — PDF Generator (Chrome Headless)
==============================================
Issue 6 fix: WeasyPrint fails on Windows without GTK.
Solution: Use Chrome headless --print-to-pdf (already installed).

Usage:
    python agents/generate_pdf.py

Reads:  outputs/*.json + outputs/investment_memo.md
Writes: outputs/DealDesk_[PropertyName]_Memo.pdf
"""

import os, sys, json, base64, re, subprocess
from datetime import datetime

def load_outputs():
    def jload(p):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f: return json.load(f)
        return {}
    def tload(p):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f: return f.read()
        return ""
    return {
        "memo":      tload("outputs/investment_memo.md"),
        "risk":      jload("outputs/risk_report.json"),
        "dashboard": jload("outputs/financial_dashboard.json"),
        "location":  jload("outputs/location_intelligence.json"),
        "deal":      jload("outputs/deal_metrics.json"),
    }

def encode_map():
    if os.path.exists("outputs/location_map.png"):
        with open("outputs/location_map.png","rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    return None

def md_to_html(text: str) -> str:
    lines   = text.split("\n")
    html    = []
    in_list = False
    for line in lines:
        if line.startswith("### "): html.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            if in_list: html.append("</ul>"); in_list=False
            html.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "): html.append(f"<h1>{line[2:]}</h1>")
        elif line.strip() == "---": html.append("<hr>")
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list: html.append("<ul>"); in_list=True
            c = re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',line[2:])
            html.append(f"<li>{c}</li>")
        elif line.startswith("| "):
            # Markdown table
            if in_list: html.append("</ul>"); in_list=False
            if "---" in line: html.append("<tr class='th-row'>"); continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            row   = "".join(f"<td>{c}</td>" for c in cells)
            html.append(f"<tr>{row}</tr>")
        else:
            if in_list: html.append("</ul>"); in_list=False
            if line.strip():
                l = re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',line)
                l = re.sub(r'\*(.+?)\*',r'<em>\1</em>',l)
                l = re.sub(r'`(.+?)`',r'<code>\1</code>',l)
                html.append(f"<p>{l}</p>")
    if in_list: html.append("</ul>")
    return "\n".join(html)

def rc(s): return "#27ae60" if s<=3 else "#f39c12" if s<=5 else "#e67e22" if s<=7 else "#e74c3c"
def lc(s): return "#27ae60" if s>=7 else "#f39c12" if s>=5 else "#e74c3c"
def bw(s,m=10): return int((s/m)*100)
def fc(v):
    if v is None: return "N/A"
    if v>=1_000_000: return f"${v/1_000_000:.2f}M"
    if v>=1_000: return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"
def fp(v,d=2): return f"{v:.{d}f}%" if v is not None else "N/A"
def fn(v,d=2): return f"{v:.{d}f}" if v is not None else "N/A"

def build_html(data, map_b64):
    deal=data["deal"]; risk=data["risk"]; dash=data["dashboard"]; loc=data["location"]
    prop   = deal.get("property_name","Subject Property")
    ptype  = deal.get("property_type","CRE").title()
    lstr   = deal.get("location","Location TBD")
    date   = datetime.now().strftime("%B %d, %Y")
    rs     = risk.get("composite_score",0); rl=risk.get("risk_level","Medium"); rcol=rc(rs)
    ls     = loc.get("location_score",0);  lg=loc.get("location_grade","N/A"); lcol=lc(ls)
    core   = dash.get("core_metrics",{}); comp=dash.get("computed_metrics",{}); sens=dash.get("sensitivity_analysis",{})
    amen   = loc.get("amenity_counts",{}); cats=loc.get("category_scores",{}); flags=risk.get("red_flags",[])
    ind    = risk.get("individual_scores",{}); macro=dash.get("macro_context",{})

    # Sensitivity rows
    sens_rows=""
    for k,v in sens.items():
        cv=v.get("cash_flow",0) or 0; covers=v.get("covers_debt",False)
        col="#27ae60" if covers else "#e74c3c"
        sens_rows+=f"<tr><td>{v.get('vacancy_rate','')}</td><td>{fc(v.get('noi'))}</td><td style='color:{col};font-weight:600'>{fc(cv)}</td><td>{fn(v.get('dscr'))}</td><td style='color:{col};font-weight:600'>{'✓' if covers else '✗'}</td></tr>"

    # Risk rows
    risk_rows=""
    for k,v in ind.items():
        s=v.get("score",0); col=rc(s)
        risk_rows+=f"<tr><td>{v.get('label',k)}</td><td><div class='sb' style='width:{bw(s)}%;background:{col}'></div></td><td style='color:{col};font-weight:700'>{s}/10</td><td style='color:#888'>{int(v.get('weight',0)*100)}%</td></tr>"

    # Amenity pills
    icons={"transit_stops":"🚌","hospitals":"🏥","schools":"🎓","grocery_stores":"🛒","restaurants":"🍽","parks":"🌳","banks":"🏦","pharmacies":"💊","universities":"🏛","offices":"🏢"}
    pills="".join(f'<span class="pill">{icons.get(k,"📍")} {v} {k.replace("_"," ").title()}</span>' for k,v in amen.items() if v>0)

    # Cat bars
    cbars=""
    for cat,score in cats.items():
        col=lc(score)
        cbars+=f'<div class="cr"><span class="cl">{cat.replace("_"," ").title()}</span><div class="cbg"><div class="cbf" style="width:{bw(score)}%;background:{col}"></div></div><span class="cs" style="color:{col}">{score}/10</span></div>'

    # Flags
    fhtml="".join(f'<div class="flag" style="border-left-color:{"#e74c3c" if "CRITICAL" in f else "#e67e22" if "HIGH" in f else "#f39c12"}">{f}</div>' for f in flags) or '<div class="flag" style="border-left-color:#27ae60">No critical red flags triggered</div>'

    maphtml=f'<img class="map" src="data:image/png;base64,{map_b64}" alt="Location Map"/>' if map_b64 else '<div class="maplh">Map not available</div>'

    memo_html=md_to_html(data["memo"])

    dscr_val=core.get("dscr"); dscr_str=f"{dscr_val}x" if dscr_val else "N/A"
    cap_val=core.get("cap_rate_inplace"); cap_str=fp((cap_val or 0)*100) if cap_val else "N/A"

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Georgia,serif;font-size:10pt;color:#1a1a2e;background:white}}
@page{{margin:0;size:A4}}
.pb{{page-break-after:always}}
.cover{{background:linear-gradient(160deg,#0d2137 0%,#1a3a5c 60%,#0d5c6b 100%);min-height:100vh;padding:60px;color:white;position:relative;display:flex;flex-direction:column}}
.brand{{font-size:13pt;font-weight:700;letter-spacing:.15em;color:#7ecdc8;text-transform:uppercase;margin-bottom:8px}}
.divider{{width:60px;height:3px;background:#c0392b;margin-bottom:48px}}
.ctag{{font-size:9pt;color:#7ecdc8;letter-spacing:.2em;text-transform:uppercase;margin-bottom:16px}}
.ctitle{{font-size:34pt;font-weight:700;color:white;line-height:1.15;margin-bottom:12px}}
.csub{{font-size:15pt;color:#b0c4d8;margin-bottom:40px}}
.scores{{display:flex;gap:20px;margin-bottom:40px;flex-wrap:wrap}}
.sbox{{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);border-radius:8px;padding:16px 24px;text-align:center;min-width:120px}}
.snum{{font-size:30pt;font-weight:700;line-height:1;margin-bottom:4px}}
.slbl{{font-size:7.5pt;color:#b0c4d8;letter-spacing:.1em;text-transform:uppercase}}
.slvl{{font-size:9pt;font-weight:600;margin-top:4px}}
.cmeta{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-top:auto;padding-top:32px;border-top:1px solid rgba(255,255,255,.2)}}
.cml{{font-size:7.5pt;color:#7ecdc8;letter-spacing:.12em;text-transform:uppercase;margin-bottom:3px}}
.cmv{{font-size:11pt;color:white;font-weight:600}}
.cwm{{position:absolute;bottom:28px;right:60px;font-size:7.5pt;color:rgba(255,255,255,.3)}}
.sp{{padding:48px 60px;min-height:100vh}}
.sh{{display:flex;align-items:center;gap:14px;margin-bottom:28px;padding-bottom:14px;border-bottom:2px solid #0d5c6b}}
.sn{{background:#0d2137;color:white;font-size:9pt;font-weight:700;padding:4px 10px;border-radius:4px}}
.st{{font-size:17pt;font-weight:700;color:#0d2137}}
.acc{{color:#c0392b}}
.mband{{background:#0d2137;color:white;border-radius:8px;padding:14px 20px;display:flex;gap:24px;margin-bottom:24px;flex-wrap:wrap}}
.mi{{text-align:center}}
.ml{{font-size:7pt;color:#7ecdc8;letter-spacing:.1em;text-transform:uppercase}}
.mv{{font-size:13pt;font-weight:700}}
.mg{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}}
.mc{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;border-top:3px solid #0d5c6b}}
.ml2{{font-size:7.5pt;color:#666;letter-spacing:.08em;text-transform:uppercase;margin-bottom:5px}}
.mv2{{font-size:15pt;font-weight:700;color:#0d2137}}
.ms{{font-size:7.5pt;color:#888;margin-top:3px}}
table{{width:100%;border-collapse:collapse;margin-bottom:20px;font-size:9pt}}
th{{background:#0d2137;color:white;padding:9px 12px;text-align:left;font-size:7.5pt;letter-spacing:.07em;text-transform:uppercase}}
td{{padding:8px 12px;border-bottom:1px solid #e8ecf0}}
tr:nth-child(even) td{{background:#f8fafc}}
.sbb{{background:#e8ecf0;border-radius:4px;height:8px;width:100%;display:block;margin:2px 0}}
.sb{{height:8px;border-radius:4px;display:block}}
.rc{{background:linear-gradient(135deg,#0d2137,#1a3a5c);color:white;border-radius:10px;padding:24px 28px;display:flex;align-items:center;gap:28px;margin-bottom:24px}}
.rbig{{font-size:44pt;font-weight:700;line-height:1}}
.rbadge{{display:inline-block;background:rgba(255,255,255,.15);border-radius:20px;padding:3px 14px;font-size:9.5pt;font-weight:600;margin-top:4px}}
.rdet{{font-size:8.5pt;color:#b0c4d8;line-height:1.5}}
.flag{{border-left:4px solid #e74c3c;padding:9px 12px;background:#fef9f9;margin-bottom:7px;border-radius:0 5px 5px 0;font-size:8.5pt}}
.lgrid{{display:grid;grid-template-columns:1.2fr 1fr;gap:24px;margin-bottom:20px}}
.map{{width:100%;border-radius:8px;border:1px solid #e2e8f0}}
.maplh{{background:#f0f4f8;border:2px dashed #ccc;border-radius:8px;padding:40px;text-align:center;color:#888;font-size:8.5pt}}
.lsb{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;text-align:center;margin-bottom:14px}}
.lsn{{font-size:32pt;font-weight:700;line-height:1}}
.cr{{display:flex;align-items:center;gap:8px;margin-bottom:7px}}
.cl{{font-size:7.5pt;color:#444;width:130px;flex-shrink:0}}
.cbg{{flex:1;background:#e8ecf0;border-radius:4px;height:7px}}
.cbf{{height:100%;border-radius:4px}}
.cs{{font-size:7.5pt;font-weight:700;width:32px;text-align:right;flex-shrink:0}}
.pills{{display:flex;flex-wrap:wrap;gap:7px;margin-top:14px}}
.pill{{background:#e8f4f8;color:#0d5c6b;border-radius:20px;padding:3px 10px;font-size:7.5pt;font-weight:600}}
.memo h1{{font-size:15pt;color:#0d2137;margin:0 0 4px}}
.memo h2{{font-size:12pt;color:#0d5c6b;margin:18px 0 8px;padding-bottom:4px;border-bottom:1px solid #e2e8f0}}
.memo h3{{font-size:10.5pt;color:#0d2137;margin:12px 0 5px}}
.memo p{{font-size:9pt;line-height:1.65;color:#333;margin-bottom:9px}}
.memo ul{{margin:7px 0 9px 18px}}
.memo li{{font-size:9pt;line-height:1.65;color:#333;margin-bottom:3px}}
.memo hr{{border:none;border-top:1px solid #e2e8f0;margin:18px 0}}
.memo strong{{font-weight:700;color:#0d2137}}
.memo em{{font-style:italic;color:#555}}
.memo table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:8.5pt}}
.memo th,.memo td{{padding:7px 10px;border:1px solid #e2e8f0;text-align:left}}
.memo th{{background:#0d2137;color:white;font-size:7.5pt}}
.pf{{margin-top:32px;padding-top:10px;border-top:1px solid #e2e8f0;display:flex;justify-content:space-between;font-size:7pt;color:#999}}
</style></head><body>

<div class="cover pb">
  <div class="brand">DealDesk AI</div>
  <div class="divider"></div>
  <div class="ctag">Investment Committee Memorandum · Confidential</div>
  <div class="ctitle">{prop}</div>
  <div class="csub">{lstr} &nbsp;·&nbsp; {ptype}</div>
  <div class="scores">
    <div class="sbox"><div class="snum" style="color:{rcol}">{rs}</div><div class="slbl">Risk Score /10</div><div class="slvl" style="color:{rcol}">{rl} Risk</div></div>
    <div class="sbox"><div class="snum" style="color:{lcol}">{ls}</div><div class="slbl">Location Score /10</div><div class="slvl" style="color:{lcol}">Grade: {lg}</div></div>
    <div class="sbox"><div class="snum" style="color:#7ecdc8">{dscr_str}</div><div class="slbl">DSCR</div></div>
    <div class="sbox"><div class="snum" style="color:#7ecdc8">{cap_str}</div><div class="slbl">Cap Rate</div></div>
    <div class="sbox"><div class="snum" style="color:#7ecdc8">{fc(core.get("asking_price"))}</div><div class="slbl">Asking Price</div></div>
  </div>
  <div class="cmeta">
    <div><div class="cml">Asset Type</div><div class="cmv">{ptype}</div></div>
    <div><div class="cml">Prepared</div><div class="cmv">{date}</div></div>
    <div><div class="cml">System</div><div class="cmv">DealDesk AI · 5-Agent</div></div>
  </div>
  <div class="cwm">CONFIDENTIAL · FOR IC USE ONLY</div>
</div>

<div class="sp pb">
  <div class="sh"><span class="sn">03</span><div class="st">Financial <span class="acc">Analysis</span></div></div>
  <div class="mband">
    <div class="mi"><div class="ml">Fed Funds Rate</div><div class="mv">{fp(macro.get("fed_funds_rate"),2)}</div></div>
    <div class="mi"><div class="ml">10Y Treasury</div><div class="mv">{fp(macro.get("ten_year_treasury"),2)}</div></div>
    <div class="mi"><div class="ml">CPI Index</div><div class="mv">{fn(macro.get("cpi"),1)}</div></div>
    <div class="mi"><div class="ml">Unemployment</div><div class="mv">{fp(macro.get("unemployment"),1)}</div></div>
    <div class="mi"><div class="ml">Source</div><div class="mv" style="font-size:8pt">FRED · Federal Reserve</div></div>
  </div>
  <div class="mg">
    <div class="mc"><div class="ml2">Net Operating Income</div><div class="mv2">{fc(core.get("noi"))}</div><div class="ms">Annual NOI</div></div>
    <div class="mc"><div class="ml2">Cap Rate (In-Place)</div><div class="mv2">{fp((core.get("cap_rate_inplace") or 0)*100)}</div><div class="ms">Market: {fp((core.get("cap_rate_market") or 0)*100)}</div></div>
    <div class="mc"><div class="ml2">DSCR</div><div class="mv2">{dscr_str}</div><div class="ms">Min benchmark: 1.25x</div></div>
    <div class="mc"><div class="ml2">LTV</div><div class="mv2">{fp((core.get("ltv") or 0)*100,0)}</div><div class="ms">Loan: {fc(core.get("loan_amount"))}</div></div>
    <div class="mc"><div class="ml2">Cash-on-Cash</div><div class="mv2">{fp(comp.get("cash_on_cash_pct"),1)}</div><div class="ms">After debt service</div></div>
    <div class="mc"><div class="ml2">Debt Yield</div><div class="mv2">{fp(comp.get("debt_yield_pct"),1)}</div><div class="ms">NOI ÷ Loan Amount</div></div>
    <div class="mc"><div class="ml2">Break-even Occ.</div><div class="mv2">{fp(comp.get("break_even_occupancy_pct"),1)}</div><div class="ms">Min to cover debt</div></div>
    <div class="mc"><div class="ml2">Equity Required</div><div class="mv2">{fc(core.get("equity_required"))}</div><div class="ms">Price minus loan</div></div>
  </div>
  <h3 style="color:#0d2137;margin-bottom:10px;font-size:10.5pt">Sensitivity Analysis</h3>
  <table><thead><tr><th>Vacancy</th><th>NOI</th><th>Cash Flow</th><th>DSCR</th><th>Status</th></tr></thead><tbody>{sens_rows}</tbody></table>
  <div class="pf"><span>DealDesk AI · {prop}</span><span>Source: FRED · Gallinelli Framework</span></div>
</div>

<div class="sp pb">
  <div class="sh"><span class="sn">05</span><div class="st">Location <span class="acc">Intelligence</span></div></div>
  <div class="lgrid">
    <div>{maphtml}<p style="font-size:7pt;color:#999;margin-top:5px;text-align:center">© OpenStreetMap contributors · 1km radius</p></div>
    <div>
      <div class="lsb"><div class="ml2">Composite Location Score</div><div class="lsn" style="color:{lcol}">{ls}/10</div><div style="font-size:10pt;font-weight:600;color:{lcol};margin-top:3px">Grade: {lg}</div><div style="font-size:7.5pt;color:#666;margin-top:5px">Weighted for: {loc.get("property_type_used","").title()}</div></div>
      {cbars}
    </div>
  </div>
  <h3 style="color:#0d2137;margin:14px 0 9px;font-size:10.5pt">Amenities Within 1km</h3>
  <div class="pills">{pills}</div>
  <p style="margin-top:14px;font-size:8.5pt;color:#555;line-height:1.6">{loc.get("location_summary","")}</p>
  <div class="pf"><span>DealDesk AI · Location Intelligence · {prop}</span><span>Source: OpenStreetMap · Overpass API</span></div>
</div>

<div class="sp pb">
  <div class="sh"><span class="sn">06</span><div class="st">Risk <span class="acc">Assessment</span></div></div>
  <div class="rc">
    <div><div class="rbig" style="color:{rcol}">{rs}</div><div style="font-size:9pt;color:#b0c4d8">out of 10</div><div class="rbadge">{rl} Risk</div></div>
    <div class="rdet"><strong style="color:white;font-size:9.5pt">7-Rule Integrated Risk Model</strong><br><br>Rules: Tenant Concentration · DSCR · Market Vacancy · Occupancy · Cash Flow · Location · Asset-Location Fit<br><br>Data: OM · FRED · OpenStreetMap · Realtor.com · RAG Knowledge Base</div>
  </div>
  <h3 style="color:#0d2137;margin-bottom:10px;font-size:10.5pt">Individual Risk Scores</h3>
  <table><thead><tr><th>Risk Factor</th><th>Score Bar</th><th>Score</th><th>Weight</th></tr></thead><tbody>{risk_rows}</tbody></table>
  <h3 style="color:#0d2137;margin:14px 0 9px;font-size:10.5pt">Red Flags ({len(flags)})</h3>
  {fhtml}
  <div class="pf"><span>DealDesk AI · Risk Assessment · {prop}</span><span>7-Rule Model · Geltner & Miller Framework</span></div>
</div>

<div class="sp">
  <div class="sh"><span class="sn">IC</span><div class="st">Investment <span class="acc">Memorandum</span></div></div>
  <div class="memo">{memo_html}</div>
  <div class="pf"><span>DealDesk AI · IC Memo · {prop} · {date}</span><span>CONFIDENTIAL — For Investment Committee Use Only</span></div>
</div>

</body></html>"""

def find_chrome():
    """Find Chrome executable on Windows."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        expanded = os.path.expandvars(path)
        if os.path.exists(expanded):
            return expanded
    # Try system PATH
    try:
        result = subprocess.run(["where","chrome"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except:
        pass
    return None

def generate_pdf():
    print("\n\033[1mDEALDESK AI — PDF GENERATOR\033[0m\n" + "─"*50)

    data    = load_outputs()
    map_b64 = encode_map()

    prop_name  = data["deal"].get("property_name","DealDesk")
    safe_name  = "".join(c if c.isalnum() or c in "-_" else "_" for c in prop_name)[:30]
    html_path  = os.path.abspath("outputs/memo_preview.html")
    pdf_path   = os.path.abspath(f"outputs/DealDesk_{safe_name}_Memo.pdf")

    # Build HTML
    print("  Building HTML...")
    html = build_html(data, map_b64)
    with open(html_path,"w",encoding="utf-8") as f:
        f.write(html)
    print(f"\033[92m  ✓  HTML saved: {html_path}\033[0m")

    # Try Chrome headless
    chrome = find_chrome()
    if chrome:
        print(f"  Found Chrome: {chrome}")
        print("  Converting to PDF via Chrome headless...")
        try:
            result = subprocess.run([
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--run-all-compositor-stages-before-draw",
                f"--print-to-pdf={pdf_path}",
                f"--print-to-pdf-no-header",
                f"file:///{html_path.replace(os.sep,'/')}"
            ], capture_output=True, text=True, timeout=60)

            if os.path.exists(pdf_path):
                size_kb = os.path.getsize(pdf_path)//1024
                print(f"\033[92m  ✓  PDF saved: {pdf_path} ({size_kb} KB)\033[0m")
                print(f"\n  \033[1mOpen PDF:\033[0m  {pdf_path}\n")
            else:
                print(f"\033[93m  ⚠  Chrome ran but PDF not found\033[0m")
                print(f"  Chrome output: {result.stderr[:300]}")
                _fallback_instructions(html_path)
        except subprocess.TimeoutExpired:
            print("\033[93m  ⚠  Chrome timed out\033[0m")
            _fallback_instructions(html_path)
        except Exception as e:
            print(f"\033[91m  ✗  Chrome failed: {e}\033[0m")
            _fallback_instructions(html_path)
    else:
        print("\033[93m  ⚠  Chrome not found in standard locations\033[0m")
        _fallback_instructions(html_path)

def _fallback_instructions(html_path):
    print(f"""
  \033[1mManual PDF steps:\033[0m
  1. Open this file in Chrome: {html_path}
  2. Press Ctrl+P
  3. Destination → Save as PDF
  4. More settings → Paper: A4, Margins: None
  5. Save
""")

if __name__ == "__main__":
    generate_pdf()
