"""
DealDesk AI — FastAPI Backend
Wraps the 6-stage CRE analysis pipeline and serves results to the Lovable frontend.

Endpoints:
  POST /run              → upload PDF(s), kick off pipeline, return job_id
  GET  /status/{job_id}  → current stage + progress (SSE-friendly)
  GET  /results/{job_id} → merged JSON of all 5 agent outputs
  GET  /health           → liveness check
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

app = FastAPI(title="DealDesk AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your Lovable URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory job store  (swap for Redis / DB in production)
# ---------------------------------------------------------------------------

JOBS: dict[str, dict] = {}

STAGES = [
    "Parsing OM",
    "Market Intel",
    "Location Score",
    "Financial Dashboard",
    "Risk Engine",
    "Writing Memo",
]

PIPELINE_SCRIPT = Path(__file__).parent / "agents" / "run_pipeline.py"
OUTPUTS_DIR = Path(__file__).parent / "outputs"
UPLOADS_DIR = Path(__file__).parent / "uploads"

UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _job_output_dir(job_id: str) -> Path:
    """Each job writes to its own subdirectory so parallel runs don't clash."""
    d = OUTPUTS_DIR / job_id
    d.mkdir(exist_ok=True)
    return d


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _detect_stage_from_log(log_tail: str) -> int:
    """
    Parse pipeline stdout to figure out which stage we're on.
    run_pipeline.py prints banners like:
        === Agent 1: Doc Parser ===
        === Agent 2: Market Intel ===
        ...
    Returns 0-based stage index (0–5).
    """
    stage = 0
    markers = [
        "agent 1",
        "agent 2",
        "agent 3",
        "agent 4",
        "agent 5",
        "orchestrator",
    ]
    lowered = log_tail.lower()
    for i, m in enumerate(markers):
        if m in lowered:
            stage = i
    return stage


async def _run_pipeline(job_id: str, om_path: Path, memo_path: Optional[Path], price_override: Optional[float]):
    """
    Runs run_pipeline.py as a subprocess, tails stdout to update job stage,
    then harvests outputs when done.
    """
    out_dir = _job_output_dir(job_id)

    cmd = [
        sys.executable,
        str(PIPELINE_SCRIPT),
        str(om_path),
        "--output-dir", str(out_dir),
    ]
    if memo_path:
        cmd += ["--memo-example", str(memo_path)]
    if price_override is not None:
        cmd += ["--price-override", str(price_override)]

    JOBS[job_id]["status"] = "running"
    JOBS[job_id]["stage_index"] = 0
    JOBS[job_id]["stage_name"] = STAGES[0]
    log_buffer: list[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            log_buffer.append(line)

            # Keep only the last 200 lines to limit memory
            if len(log_buffer) > 200:
                log_buffer.pop(0)

            stage_idx = _detect_stage_from_log("\n".join(log_buffer[-20:]))
            JOBS[job_id]["stage_index"] = stage_idx
            JOBS[job_id]["stage_name"] = STAGES[stage_idx]
            JOBS[job_id]["log_tail"] = line  # handy for debugging

        await proc.wait()

        if proc.returncode != 0:
            JOBS[job_id]["status"] = "error"
            full_log = "\n".join(log_buffer)
            JOBS[job_id]["error"] = f"Pipeline exited with code {proc.returncode}.\n\nFULL LOG:\n{full_log}"
            JOBS[job_id]["full_log"] = full_log  # also stored separately
            return

        # ── Harvest all outputs ──────────────────────────────────────────────
        results: dict = {}

        for fname in [
            "financial_dashboard.json",
            "risk_report.json",
            "deal_metrics.json",
            "location_intelligence.json",
        ]:
            data = _read_json(out_dir / fname)
            if data:
                key = fname.replace(".json", "")
                results[key] = data

        memo = _read_text(out_dir / "investment_memo.md")
        if memo:
            results["investment_memo"] = memo

        JOBS[job_id]["results"] = results
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["stage_index"] = 5
        JOBS[job_id]["stage_name"] = STAGES[5]

    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "pipeline_script": str(PIPELINE_SCRIPT)}


@app.post("/run")
async def run_pipeline(
    om_pdf: UploadFile = File(..., description="Offering Memorandum PDF"),
    memo_pdf: Optional[UploadFile] = File(None, description="Example IC Memo PDF (format reference)"),
    price_override: Optional[float] = Form(None, description="Known asking price — skips estimation"),
):
    """
    Accepts the OM PDF (required) and an optional example memo PDF.
    Saves both to disk, kicks off the pipeline as a background task,
    and immediately returns a job_id.
    """
    job_id = str(uuid.uuid4())
    upload_dir = UPLOADS_DIR / job_id
    upload_dir.mkdir(parents=True)

    # Save OM
    om_path = upload_dir / "om.pdf"
    with om_path.open("wb") as f:
        shutil.copyfileobj(om_pdf.file, f)

    # Save optional example memo
    memo_path: Optional[Path] = None
    if memo_pdf and memo_pdf.filename:
        memo_path = upload_dir / "memo_example.pdf"
        with memo_path.open("wb") as f:
            shutil.copyfileobj(memo_pdf.file, f)

    # Initialise job record
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "stage_index": 0,
        "stage_name": STAGES[0],
        "results": None,
        "error": None,
        "log_tail": "",
    }

    # Fire and forget
    asyncio.create_task(_run_pipeline(job_id, om_path, memo_path, price_override))

    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """
    Returns current pipeline stage.
    Poll this every 2–3 seconds from the frontend progress stepper.
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job_id,
        "status": job["status"],           # queued | running | done | error
        "stage_index": job["stage_index"], # 0-5
        "stage_name": job["stage_name"],
        "stages": STAGES,
        "error": job.get("error"),
        "log_tail": job.get("log_tail", ""),
        "full_log": job.get("full_log", ""),
    }


@app.get("/status/{job_id}/stream")
async def stream_status(job_id: str):
    """
    Server-Sent Events stream — optional alternative to polling.
    Connect with EventSource('/status/{job_id}/stream').
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        while True:
            j = JOBS.get(job_id, {})
            payload = json.dumps({
                "status": j.get("status"),
                "stage_index": j.get("stage_index", 0),
                "stage_name": j.get("stage_name", STAGES[0]),
            })
            yield f"data: {payload}\n\n"
            if j.get("status") in ("done", "error"):
                break
            await asyncio.sleep(1.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/results/{job_id}")
async def get_results(job_id: str):
    """
    Returns all 5 agent outputs merged into a single JSON object.
    Shape matches the key structure documented in the Lovable prompt.
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] == "error":
        raise HTTPException(status_code=500, detail=job.get("error", "Pipeline error"))

    if job["status"] != "done":
        raise HTTPException(status_code=202, detail="Pipeline still running")

    return JSONResponse(content=job["results"])


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
