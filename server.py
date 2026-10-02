"""
HuntPilot - UI backend (FastAPI)

The React screens talk to this small server. It reuses your existing scripts:
  - "Start hunt"    runs step2_explorer.py in the background and streams its log
  - "Review"        reads and saves reports/qa_review.csv
  - "Send to Jira"  uses the push function from step3_push_to_jira.py

Run it:  uvicorn server:app --port 8001 --reload
"""

import csv
import json
import os
import subprocess
import sys
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()
REPORT_DIR = "reports"
REVIEW_FILE = f"{REPORT_DIR}/qa_review.csv"
STEP2_SCRIPT = os.getenv("STEP2_SCRIPT", "step2_explorer.py")
os.makedirs(f"{REPORT_DIR}/screenshots", exist_ok=True)

app = FastAPI(title="HuntPilot")
app.mount("/reports", StaticFiles(directory=REPORT_DIR), name="reports")

hunt = {"running": False, "log": [], "exit_code": None}


# ---------- Settings ----------
@app.get("/api/config")
def config():
    return {"target_url": os.getenv("TARGET_URL", "http://localhost:8000"),
            "jira_url": os.getenv("JIRA_URL", ""), "jira_project": os.getenv("JIRA_PROJECT_KEY", "QTB"),
            "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            "jira_ready": bool(os.getenv("JIRA_URL") and os.getenv("JIRA_API_TOKEN"))}


# ---------- Hunt ----------
class HuntRequest(BaseModel):
    target_url: str
    users: list[str]
    tests_per_page: int = 6


def _run_hunt(env):
    proc = subprocess.Popen([sys.executable, "-u", STEP2_SCRIPT], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
    for line in proc.stdout:
        hunt["log"].append(line.rstrip())
    hunt["exit_code"] = proc.wait()
    hunt["running"] = False


@app.post("/api/hunt")
def start_hunt(req: HuntRequest):
    if hunt["running"]:
        raise HTTPException(409, "A hunt is already running.")
    if not req.users:
        raise HTTPException(400, "Choose at least one user.")
    env = {**os.environ, "TARGET_URL": req.target_url, "HUNT_USERS": ",".join(req.users),
           "TESTS_PER_PAGE": str(req.tests_per_page), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    hunt.update(running=True, log=[], exit_code=None)
    threading.Thread(target=_run_hunt, args=(env,), daemon=True).start()
    return {"started": True}


@app.get("/api/hunt")
def hunt_status(since: int = 0):
    return {"running": hunt["running"], "exit_code": hunt["exit_code"],
            "lines": hunt["log"][since:], "total": len(hunt["log"])}


@app.get("/api/report")
def report():
    path = f"{REPORT_DIR}/step2_report.json"
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------- Review ----------
def _read_review():
    if not os.path.exists(REVIEW_FILE):
        return [], []
    with open(REVIEW_FILE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def _verdict_col(fields):
    return next(c for c in fields if c.startswith("QA verdict"))


@app.get("/api/findings")
def findings():
    fields, rows = _read_review()
    if not rows:
        return []
    vc = _verdict_col(fields)
    return [{"id": i, "user": r["User"], "page": r["Page"], "title": r["AI bug title"],
             "severity": r["AI severity"], "steps": r.get("Steps to reproduce", ""),
             "test_data": r.get("Test data", ""), "expected": r.get("Expected result", ""),
             "actual": r.get("Actual result", "") or r.get("AI evidence", ""),
             "screenshot": "/" + r["Screenshot"].replace("\\", "/") if r.get("Screenshot") else "",
             "verdict": r.get(vc, ""), "notes": r.get("QA notes", "")} for i, r in enumerate(rows)]


class Review(BaseModel):
    id: int
    verdict: str
    notes: str = ""


@app.put("/api/findings")
def save_reviews(reviews: list[Review]):
    fields, rows = _read_review()
    if not rows:
        raise HTTPException(404, "No review sheet yet. Run a hunt first.")
    vc = _verdict_col(fields)
    for rv in reviews:
        if 0 <= rv.id < len(rows):
            rows[rv.id][vc] = rv.verdict
            rows[rv.id]["QA notes"] = rv.notes
    with open(REVIEW_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return {"saved": len(reviews)}


# ---------- Jira ----------
@app.post("/api/jira")
def send_to_jira():
    import step3_push_to_jira as step3
    if not (step3.JIRA_URL and all(step3.AUTH)):
        raise HTTPException(400, "Jira is not set up. Add JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN to .env.")
    fields, rows = _read_review()
    if not rows:
        raise HTTPException(404, "No review sheet yet.")
    vc = _verdict_col(fields)
    approved = [r for r in rows if (r.get(vc) or "").strip().lower().startswith("real")]
    if not approved:
        raise HTTPException(400, "No findings are marked 'Real bug'.")
    return {"results": step3.push_rows(approved, log=lambda *_: None)}
