"""
DealDesk AI — Day 4: FastAPI Backend
=====================================
Wraps the Python agent pipeline as a REST API.
Bolt.new frontend talks to this.

Start server:
    uvicorn api.main:app --reload --port 8000

Endpoints:
    POST /analyse     → upload OM PDF → get all 3 outputs
    GET  /health      → confirm server is running
    GET  /outputs     → fetch saved outputs from last run
"""

import os, sys, json, tempfile, shutil
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Add parent dir to path so we can import agents
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.run_pipeline import run_pipeline

app = FastAPI(
    title="DealDesk AI",
    description="Multi-agent CRE underwriting co-pilot",
    version="1.0.0"
)

# Allow Bolt.new frontend (localhost + deployed URL) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten this post-demo if deploying
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "online", "product": "DealDesk AI", "version": "1.0.0"}

@app.post("/analyse")
async def analyse_deal(om_pdf: UploadFile = File(...)):
    """
    Upload an OM PDF → runs all 4 agents → returns investment memo,
    risk report, and financial dashboard in one JSON response.
    """
    if not om_pdf.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    # Save uploaded PDF to a temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(om_pdf.file, tmp)
        tmp_path = tmp.name

    try:
        results = run_pipeline(tmp_path)
        return JSONResponse({
            "success":    True,
            "memo":       results["memo"],
            "risk":       results["risk"],
            "dashboard":  results["dashboard"]
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)   # clean up temp file

@app.get("/outputs")
def get_last_outputs():
    """Return the outputs from the most recent pipeline run."""
    output_files = {
        "memo":      "outputs/investment_memo.md",
        "risk":      "outputs/risk_report.json",
        "dashboard": "outputs/financial_dashboard.json",
        "metrics":   "outputs/deal_metrics.json"
    }
    results = {}
    for key, path in output_files.items():
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                results[key] = f.read() if path.endswith(".md") else json.load(f)
    if not results:
        raise HTTPException(status_code=404, detail="No outputs found — run /analyse first")
    return JSONResponse(results)
