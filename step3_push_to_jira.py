"""
HuntPilot - Step 3: Send QA-approved bugs to Jira

The AI finds, a QA engineer approves, and only approved bugs reach Jira.

What it does:
  1. Reads reports/qa_review.csv (filled in by a QA engineer)
  2. Keeps only rows where "QA verdict" says "Real bug"
     ("Not a bug" and "Duplicate" rows are counted but never sent)
  3. Shows you the list and asks for confirmation
  4. For each bug:
       - skips it if the same bug is already in Jira (no duplicates on re-runs)
       - creates a Bug in this format: Title, Steps to reproduce, Actual result,
         Expected result, Test data, Environment, QA notes, Screenshot
       - links it to the matching user story (found by its page-... label)
       - attaches the screenshot

Needs these lines in your .env file:
  JIRA_URL=https://your-site.atlassian.net
  JIRA_EMAIL=you@example.com
  JIRA_API_TOKEN=your_token
  JIRA_PROJECT_KEY=QTB

Run it:  python step3_push_to_jira.py
"""

import csv
import hashlib
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
AUTH = (os.getenv("JIRA_EMAIL", ""), os.getenv("JIRA_API_TOKEN", ""))
PROJECT = os.getenv("JIRA_PROJECT_KEY", "QTB")
REVIEW_FILE = "reports/qa_review.csv"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# Which user story each page belongs to (by the story's label)
PAGE_TO_LABEL = {
    "login": "page-login",
    "dashboard": "page-dashboard",
    "transfer": "page-transfer",
    "bills": "page-bills",
    "loan": "page-loan",
    "profile": "page-profile",
    "accessibility": "page-accessibility",
    "link-check": "page-accessibility",
    "footer/menu": "page-accessibility",
}
SEVERITY_TO_PRIORITY = {"high": "High", "medium": "Medium", "low": "Low"}


# ---------- Jira helpers ----------
def jira(method, path, **kwargs):
    r = requests.request(method, f"{JIRA_URL}{path}", auth=AUTH, timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"Jira {method} {path} failed ({r.status_code}): {r.text[:300]}")
    return r.json() if r.text else {}


def search(jql, max_results=1):
    body = {"jql": jql, "fields": ["summary"], "maxResults": max_results}
    return jira("POST", "/rest/api/3/search/jql", headers=HEADERS, json=body).get("issues", [])


def _para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": str(text)}]}


def _list(items, ordered):
    return {"type": "orderedList" if ordered else "bulletList",
            "content": [{"type": "listItem", "content": [_para(i)]} for i in items]}


def text_to_doc(sections):
    """Jira needs descriptions in its own document format.
    Each section is (heading, content); content is text, or ("steps", [...]) / ("bullets", [...])."""
    content = []
    for heading, body in sections:
        if not body:
            continue
        content.append({"type": "heading", "attrs": {"level": 3},
                        "content": [{"type": "text", "text": heading}]})
        if isinstance(body, tuple):
            kind, items = body
            content.append(_list(items, ordered=(kind == "steps")))
        else:
            content.append(_para(body))
    return {"type": "doc", "version": 1, "content": content}


def split_lines(text, strip_numbers=False):
    lines = [l.strip() for l in re.split(r"[\n;]+" if not strip_numbers else r"\n+", text or "") if l.strip()]
    return [re.sub(r"^\d+[.)]\s*", "", l) for l in lines] if strip_numbers else lines


def fingerprint(row):
    """A short ID for this bug, so running the script twice doesn't create it twice."""
    key = f"{row['Page']}|{row['AI bug title']}".lower()
    return "hp-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def find_story(page):
    label = PAGE_TO_LABEL.get(page)
    if not label:
        return None
    found = search(f'project = "{PROJECT}" AND issuetype = Story AND labels = "{label}"')
    return found[0]["key"] if found else None


def bug_sections(row, story_key):
    """The bug report layout. Falls back to 'AI evidence' for review sheets made by older versions."""
    shot = os.path.basename(row.get("Screenshot") or "")
    if row.get("Steps to reproduce"):
        return [
            ("Steps to reproduce", ("steps", split_lines(row["Steps to reproduce"], strip_numbers=True))),
            ("Actual result", row.get("Actual result")),
            ("Expected result", row.get("Expected result")),
            ("Test data", ("bullets", split_lines(row.get("Test data")))),
            ("Environment", ("bullets", [f"App: {os.getenv('TARGET_URL', '')}", f"User: {row['User']}",
                                         "Found by: HuntPilot (AI), verified by QA"])),
            ("QA notes", row.get("QA notes")),
            ("Screenshot", f"See attachment: {shot}" if shot else ""),
            ("Related story", story_key),
        ]
    return [("Evidence (found by HuntPilot AI)", row.get("AI evidence")), ("QA notes", row.get("QA notes")),
            ("Found as user", row["User"]), ("Screenshot", f"See attachment: {shot}" if shot else ""),
            ("Related story", story_key)]


def create_bug(row, story_key, fp):
    fields = {
        "project": {"key": PROJECT},
        "issuetype": {"name": "Bug"},
        "summary": f"[{row['Page']}] {row['AI bug title']}"[:250],
        "description": text_to_doc(bug_sections(row, story_key)),
        "labels": ["huntpilot", fp] + ([PAGE_TO_LABEL[row["Page"]]] if row["Page"] in PAGE_TO_LABEL else []),
        "priority": {"name": SEVERITY_TO_PRIORITY.get((row["AI severity"] or "").lower(), "Medium")},
    }
    try:
        return jira("POST", "/rest/api/3/issue", headers=HEADERS, json={"fields": fields})["key"]
    except RuntimeError as e:
        if "priority" in str(e).lower():          # some spaces hide the Priority field
            fields.pop("priority")
            return jira("POST", "/rest/api/3/issue", headers=HEADERS, json={"fields": fields})["key"]
        raise


def link_to_story(bug_key, story_key):
    body = {"type": {"name": "Relates"}, "inwardIssue": {"key": bug_key}, "outwardIssue": {"key": story_key}}
    jira("POST", "/rest/api/3/issueLink", headers=HEADERS, json=body)


def attach(bug_key, path):
    if not path or not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        jira("POST", f"/rest/api/3/issue/{bug_key}/attachments",
             headers={"X-Atlassian-Token": "no-check", "Accept": "application/json"},
             files={"file": (os.path.basename(path), f, "image/png")})
    return True


def push_rows(approved, log=print):
    """Send approved review rows to Jira. Returns one result per row (also used by the UI)."""
    results = []
    for r in approved:
        title = r["AI bug title"]
        try:
            fp = fingerprint(r)
            existing = search(f'project = "{PROJECT}" AND labels = "{fp}"')
            if existing:
                key = existing[0]["key"]
                log(f"   SKIP   {key} already exists: {title}")
                results.append({"title": title, "status": "skipped", "key": key, "url": f"{JIRA_URL}/browse/{key}"})
                continue
            story = find_story(r["Page"])
            bug = create_bug(r, story, fp)
            if story:
                link_to_story(bug, story)
            has_shot = attach(bug, r.get("Screenshot"))
            log(f"   CREATED {bug}" + (f" -> linked to {story}" if story else "") +
                (" + screenshot" if has_shot else "") + f": {title}")
            results.append({"title": title, "status": "created", "key": bug, "story": story,
                            "screenshot": has_shot, "url": f"{JIRA_URL}/browse/{bug}"})
        except Exception as e:
            log(f"   ERROR  {title}: {e}")
            results.append({"title": title, "status": "error", "error": str(e)})
    return results


# ---------- Main ----------
def main():
    if not (JIRA_URL and all(AUTH)):
        sys.exit("Missing JIRA_URL, JIRA_EMAIL or JIRA_API_TOKEN in your .env file.")
    if not os.path.exists(REVIEW_FILE):
        sys.exit(f"{REVIEW_FILE} not found. Run step2_explorer.py first.")

    with open(REVIEW_FILE, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    verdict_col = next(c for c in rows[0] if c.startswith("QA verdict")) if rows else None
    approved = [r for r in rows if re.match(r"\s*real", (r.get(verdict_col) or ""), re.I)]
    rejected = [r for r in rows if re.match(r"\s*not", (r.get(verdict_col) or ""), re.I)]
    duplicates = [r for r in rows if re.match(r"\s*dup", (r.get(verdict_col) or ""), re.I)]
    unreviewed = len(rows) - len(approved) - len(rejected) - len(duplicates)

    print(f"QA review: {len(approved)} real bug(s), {len(duplicates)} duplicate(s), "
          f"{len(rejected)} rejected, {unreviewed} not reviewed yet")
    if unreviewed:
        print("   (rows without a QA verdict are NOT sent to Jira)")
    if not approved:
        sys.exit("Nothing to send. Mark rows as 'Real bug' in the QA verdict column first.")
    for r in approved:
        print(f"   - [{r['Page']}] {r['AI bug title']}")
    if input(f"\nCreate these {len(approved)} bug(s) in Jira space {PROJECT}? (y/n) ").strip().lower() != "y":
        sys.exit("Cancelled. Nothing was sent.")

    results = push_rows(approved)
    created = sum(1 for x in results if x["status"] == "created")
    skipped = sum(1 for x in results if x["status"] == "skipped")
    print(f"\nDone: {created} created, {skipped} already in Jira.")
    print(f"Open: {JIRA_URL}/jira/software/projects/{PROJECT}/list")


if __name__ == "__main__":
    main()
