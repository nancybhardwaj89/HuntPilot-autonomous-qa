"""
HuntPilot - Step 2 (v3): AI explorer (LangGraph + Groq)

For each page that has a form, the agent works like a tester:
  1. LOOK    - reads the page: visible text and every input field
  2. PLAN    - the LLM designs test ideas (valid, boundary, invalid, double-click...)
               and says what SHOULD happen for each one
  3. RUN     - Playwright types the inputs, submits, and records before/after
  4. JUDGE   - the LLM compares "expected" with "what actually happened"
               and reports only clear bugs, with evidence

The AI never sees the app's source code or the bug list - only the running app.

v2 adds:
  - token usage and cost for every run (printed and saved in the report)
  - a QA review sheet (reports/qa_review.csv): a human marks each AI finding as real or not
  - input-field values in the before/after state, a stricter judge, and better test habits
v3 adds:
  - every finding records Steps to reproduce, Test data, Expected result and Actual result
    (steps and test data are built from what the agent really did, not written by the AI)

Run it:  python step2_explorer.py
"""

import csv
import json
import os
import re
import time
from datetime import date
from typing import TypedDict

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from playwright.sync_api import sync_playwright

# ---------- Settings ----------
load_dotenv()
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000").rstrip("/") + "/"
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
USERS = [u.strip() for u in os.getenv("HUNT_USERS", "alex,sam").split(",") if u.strip()]
PASSWORD = "demo123"
HEADLESS = True          # set to False to watch the browser work
TESTS_PER_PAGE = int(os.getenv("TESTS_PER_PAGE", "6"))  # more tests = more bugs found, but more tokens used
PAUSE_SECONDS = 3        # pause between AI calls, to stay inside Groq's free limits
REPORT_DIR = "reports"

# Price per 1 million tokens in US dollars (check groq.com/pricing - prices change)
PRICE_INPUT_PER_M = 0.15
PRICE_OUTPUT_PER_M = 0.60

llm = ChatGroq(model=MODEL, temperature=0.2, max_retries=6)


# ---------- Token and cost tracking ----------
usage = {}   # per user: {"calls": 0, "input": 0, "output": 0}


def track(user, response):
    meta = getattr(response, "usage_metadata", None) or {}
    u = usage.setdefault(user, {"calls": 0, "input": 0, "output": 0})
    u["calls"] += 1
    u["input"] += meta.get("input_tokens", 0)
    u["output"] += meta.get("output_tokens", 0)


def cost_of(u):
    return u["input"] / 1_000_000 * PRICE_INPUT_PER_M + u["output"] / 1_000_000 * PRICE_OUTPUT_PER_M


# ---------- AI helper ----------
def ask_ai_for_json(prompt, user):
    """Ask the LLM and return its answer as a Python dict (retries once if the JSON is broken)."""
    for _ in range(2):
        response = llm.invoke(prompt)
        track(user, response)
        text = response.content
        time.sleep(PAUSE_SECONDS)
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        prompt += "\n\nYour last answer was not valid JSON. Reply with ONLY the JSON object."
    return {}


PLAN_PROMPT = """You are an expert QA engineer testing a web app (an online bank).
Today's date is {today}.

Page: "{page}"
Visible text on the page (may be cut short):
---
{text}
---
Input fields on this page (use the "testid" as the key when you give inputs):
{fields}

Design up to {n} tests that are most likely to expose REAL bugs on this page.
Think like a tester:
- one normal, valid case (so you can check the result is calculated correctly)
- boundary and invalid values (zero, negative, too large, wrong format, dates in the past)
- if a button submits something that costs money, one test with action "double_click"
- for calculations, pick simple numbers so the correct answer is easy to work out
- for money, use amounts with cents (e.g. 12.34) so you can check the exact result
- for search boxes, try a different letter case and part of a word compared with the text on the page

For select fields give the option VALUE. For date fields use YYYY-MM-DD.
Fields you leave out keep their current value.
Actions: "click" (submit the form once), "double_click" (submit twice quickly),
"none" (just type, do not submit - use this for search boxes).

For "expected", describe exactly what a correct app should do, including numbers
(e.g. "balance goes from X to Y" or "shows an error and nothing changes").

Reply with ONLY this JSON:
{{"tests": [{{"id": "T1", "idea": "...", "inputs": {{"testid": "value"}}, "action": "click", "expected": "..."}}]}}"""


JUDGE_PROMPT = """You are an expert QA engineer reviewing test results for an online bank.
Today's date is {today}.

Page: "{page}"
Each result shows the inputs, what SHOULD happen, and the page BEFORE and AFTER the action.

{results}

Report ONLY clear defects - behaviour a real bank must not have, for example:
money or balances that do not add up, clearly invalid input that is accepted (negative or zero money,
more than the balance, an invalid email, a date in the past), an action applied twice,
wrong calculations, results that are missing or wrong.
Work out any numbers carefully before deciding.
Base every bug on evidence in BEFORE/AFTER. "field_values" shows what is inside the input boxes.
NOT bugs: a clear error message for bad input, cosmetic wording, missing features, missing extra
limits (e.g. no maximum term), search not covering extra columns, or your own design preferences.
If several tests show the same underlying defect, report it ONCE.
If a test behaved correctly, do not mention it.

Reply with ONLY this JSON:
{{"bugs": [{{"test_id": "T1", "title": "short bug title", "severity": "high|medium|low",
"expected_result": "what a correct app should show, with numbers",
"actual_result": "what the app actually showed, with numbers"}}]}}
If there are no bugs, reply {{"bugs": []}}"""


# ---------- Browser helpers ----------
READ_FIELDS_JS = """() => [...document.querySelectorAll('main input, main select')].map(el => {
  const lab = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
  return {
    testid: el.dataset.testid || el.id,
    kind: el.tagName === 'SELECT' ? 'select' : (el.type || 'text'),
    label: lab ? lab.innerText.trim() : (el.getAttribute('aria-label') || el.placeholder || '(no label)'),
    current_value: el.value,
    options: el.tagName === 'SELECT' ? [...el.options].map(o => ({value: o.value, text: o.text})) : undefined
  };
})"""

READ_STATE_JS = """() => {
  const main = document.querySelector('main');
  return {
    message: [...document.querySelectorAll('[data-testid=message]')].map(e => e.innerText).join(' | '),
    dropdowns: [...document.querySelectorAll('main select')].map(s => [...s.options].map(o => o.text).join('; ')),
    field_values: Object.fromEntries([...document.querySelectorAll('main input, main select')]
      .map(el => [el.dataset.testid || el.id, el.value])),
    text: main ? main.innerText.slice(0, 900) : ''
  };
}"""


def sign_in(page, username):
    page.goto(TARGET_URL + "#login")
    page.get_by_test_id("username").fill(username)
    page.get_by_test_id("password").fill(PASSWORD)
    page.get_by_test_id("login-button").click()
    page.wait_for_url("**#dashboard")


def open_fresh(page, route, username):
    """Reload the app (which resets all its data), sign in again and open the page.
    This keeps every test independent: one test's transfer can't affect the next test."""
    page.reload()
    sign_in(page, username)
    page.goto(TARGET_URL + route)
    page.wait_for_timeout(400)


def field(page, testid):
    loc = page.locator(f'[data-testid="{testid}"]')
    return loc if loc.count() else page.locator(f"#{testid}")


def fill_field(page, testid, value):
    el = field(page, testid)
    if not el.count():
        return f"field '{testid}' not found"
    value = str(value)
    if el.evaluate("e => e.tagName") == "SELECT":
        options = el.evaluate("e => [...e.options].map(o => [o.value, o.text])")
        pick = next((v for v, t in options if v == value or value.lower() in t.lower()), None)
        if pick is None:
            return f"option '{value}' not found in '{testid}'"
        el.select_option(pick)
    else:
        el.fill(value)
    return None


def describe_test(test, info, user, page_names):
    """Turn what the agent actually did into human-readable steps and test data."""
    fields = {f["testid"]: f for f in info["fields"]}

    def label_of(testid):
        f = fields.get(testid, {})
        lab = f.get("label", "")
        return lab if lab and lab != "(no label)" else testid.replace("-", " ").capitalize()

    def shown_value(testid, value):
        for o in fields.get(testid, {}).get("options") or []:
            if o["value"] == str(value):
                return o["text"].split(" (")[0]      # drop the balance shown in brackets
        return str(value)

    page_name = page_names.get(info["route"], info["route"].lstrip("#"))
    steps = [f"Open {TARGET_URL}",
             f'Sign in with username "{user}" and password "{PASSWORD}"',
             f'Open the "{page_name}" page']
    data = []
    for testid, value in (test.get("inputs") or {}).items():
        kind = fields.get(testid, {}).get("kind")
        verb = "Select" if kind == "select" else "Enter"
        steps.append(f'{verb} "{shown_value(testid, value)}" in "{label_of(testid)}"')
        data.append(f"{label_of(testid)} = {shown_value(testid, value)}")
    action, button = test.get("action", "click"), test.get("button") or "the submit button"
    if action == "double_click":
        steps.append(f'Double-click "{button}" quickly')
    elif action == "none":
        steps.append("Wait for the page to update (no button needed)")
    else:
        steps.append(f'Click "{button}"')
    steps.append("Observe the message and balances on the page")
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    return numbered, "; ".join(data) or "(none)"


# ---------- The agent (LangGraph) ----------
class HuntState(TypedDict):
    user: str
    routes: list
    page_names: dict
    index: int
    page_info: dict
    tests: list
    results: list
    findings: list


def build_agent(page):
    """Builds the look -> plan -> run -> judge loop. 'page' is the open browser tab."""
    shots = f"{REPORT_DIR}/screenshots"
    os.makedirs(shots, exist_ok=True)

    def look(state):
        route = state["routes"][state["index"]]
        open_fresh(page, route, state["user"])
        info = {"route": route, "fields": page.evaluate(READ_FIELDS_JS), **page.evaluate(READ_STATE_JS)}
        print(f"\n   LOOK   {route}: {len(info['fields'])} input field(s)")
        return {"page_info": info, "tests": [], "results": []}

    def has_fields(state):
        return "plan" if state["page_info"]["fields"] else "next_page"

    def plan(state):
        info = state["page_info"]
        prompt = PLAN_PROMPT.format(today=date.today().isoformat(), page=info["route"], text=info["text"],
                                    fields=json.dumps(info["fields"], indent=1), n=TESTS_PER_PAGE)
        tests = ask_ai_for_json(prompt, state["user"]).get("tests", [])[:TESTS_PER_PAGE]
        print(f"   PLAN   {len(tests)} test(s)")
        for t in tests:
            print(f"          {t.get('id')}: {t.get('idea')}")
        return {"tests": tests}

    def run(state):
        route = state["page_info"]["route"]
        results = []
        for t in state["tests"]:
            open_fresh(page, route, state["user"])
            before = page.evaluate(READ_STATE_JS)
            problems = [p for p in (fill_field(page, k, v) for k, v in (t.get("inputs") or {}).items()) if p]
            action = t.get("action", "click")
            submit = page.locator("main form button").first
            button = submit.inner_text().strip() if submit.count() else ""
            if action != "none" and submit.count():
                submit.dblclick() if action == "double_click" else submit.click()
                page.wait_for_timeout(1500)
            else:
                page.wait_for_timeout(500)
            shot = f"{shots}/{state['user']}_{route.lstrip('#')}_{t.get('id')}.png"
            page.screenshot(path=shot, full_page=True)
            results.append({**t, "button": button, "input_problems": problems, "before": before,
                            "after": page.evaluate(READ_STATE_JS), "screenshot": shot})
        print(f"   RUN    {len(results)} test(s) executed")
        return {"results": results}

    def judge(state):
        info = state["page_info"]
        if not state["results"]:
            return {}
        compact = [{k: r[k] for k in ("id", "idea", "inputs", "action", "expected", "input_problems", "before", "after")}
                   for r in state["results"]]
        prompt = JUDGE_PROMPT.format(today=date.today().isoformat(), page=info["route"],
                                     results=json.dumps(compact, indent=1))
        bugs = ask_ai_for_json(prompt, state["user"]).get("bugs", [])
        by_id = {r["id"]: r for r in state["results"]}
        new = []
        for b in bugs:
            test = by_id.get(b.get("test_id"), {})
            steps, test_data = describe_test(test, info, state["user"], state["page_names"])
            b.update({"page": info["route"].lstrip("#"), "screenshot": test.get("screenshot"),
                      "steps": steps, "test_data": test_data,
                      "expected_result": b.get("expected_result") or test.get("expected", ""),
                      "evidence": f"Expected: {b.get('expected_result', '')} | Actual: {b.get('actual_result', '')}"})
            new.append(b)
            print(f"   JUDGE  BUG [{b.get('severity')}] {b.get('title')}")
        if not new:
            print("   JUDGE  no bugs on this page")
        return {"findings": state["findings"] + new}

    def next_page(state):
        return {"index": state["index"] + 1}

    def more_pages(state):
        return "look" if state["index"] < len(state["routes"]) else END

    graph = StateGraph(HuntState)
    graph.add_node("look", look)
    graph.add_node("plan", plan)
    graph.add_node("run", run)
    graph.add_node("judge", judge)
    graph.add_node("next_page", next_page)
    graph.add_edge(START, "look")
    graph.add_conditional_edges("look", has_fields, ["plan", "next_page"])
    graph.add_edge("plan", "run")
    graph.add_edge("run", "judge")
    graph.add_edge("judge", "next_page")
    graph.add_conditional_edges("next_page", more_pages, ["look", END])
    return graph.compile()


def hunt(playwright, username):
    print(f"\n=== Hunting as '{username}' ===")
    started = time.time()
    browser = playwright.chromium.launch(headless=HEADLESS)
    page = browser.new_context().new_page()
    sign_in(page, username)
    nav = page.eval_on_selector_all("nav a[href^='#']", "els => els.map(a => [a.getAttribute('href'), a.innerText.trim()])")
    routes = [h for h, _ in nav]
    agent = build_agent(page)
    final = agent.invoke({"user": username, "routes": routes, "page_names": dict(nav), "index": 0, "page_info": {},
                          "tests": [], "results": [], "findings": []},
                         {"recursion_limit": 100})
    browser.close()
    u = usage.get(username, {"calls": 0, "input": 0, "output": 0})
    return {"user": username, "pages": routes, "findings": final["findings"],
            "minutes": round((time.time() - started) / 60, 1),
            "ai_usage": {**u, "total_tokens": u["input"] + u["output"], "cost_usd": round(cost_of(u), 5)}}


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    results = []
    with sync_playwright() as p:
        for user in USERS:
            results.append(hunt(p, user))

    total = {k: sum(r["ai_usage"][k] for r in results) for k in ("calls", "input", "output")}
    run_cost = cost_of(total)
    report = {"target": TARGET_URL, "model": MODEL, "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "prices_per_million": {"input": PRICE_INPUT_PER_M, "output": PRICE_OUTPUT_PER_M},
              "total_ai_usage": {**total, "total_tokens": total["input"] + total["output"],
                                 "cost_usd": round(run_cost, 5)},
              "results": results}
    with open(f"{REPORT_DIR}/step2_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # QA review sheet: the AI suggests, a human decides
    with open(f"{REPORT_DIR}/qa_review.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["User", "Page", "AI bug title", "AI severity", "Steps to reproduce", "Test data",
                    "Expected result", "Actual result", "Screenshot",
                    "QA verdict (Real bug / Not a bug / Duplicate)", "QA notes"])
        for r in results:
            for b in r["findings"]:
                w.writerow([r["user"], b.get("page"), b.get("title"), b.get("severity"), b.get("steps"),
                            b.get("test_data"), b.get("expected_result"), b.get("actual_result"),
                            b.get("screenshot"), "", ""])

    print("\n=== Summary ===")
    for r in results:
        a = r["ai_usage"]
        print(f"{r['user']}: {len(r['findings'])} bug(s) reported in {r['minutes']} min "
              f"| {a['calls']} AI calls, {a['total_tokens']:,} tokens, ${a['cost_usd']:.4f}")
        for b in r["findings"]:
            print(f"   - [{b.get('page')}] {b.get('title')}")

    print("\n=== AI usage and cost ===")
    print(f"Model: {MODEL}  (prices: ${PRICE_INPUT_PER_M}/M input, ${PRICE_OUTPUT_PER_M}/M output)")
    print(f"This run: {total['calls']} AI calls, {total['input']:,} input + {total['output']:,} output tokens")
    print(f"Cost of this run:          ${run_cost:.4f}")
    print(f"Cost of 100 runs:          ${run_cost * 100:.2f}")
    print(f"Cost of 1 run a day, 1 yr: ${run_cost * 365:.2f}")
    print(f"\nFull report:     {REPORT_DIR}/step2_report.json")
    print(f"QA review sheet: {REPORT_DIR}/qa_review.csv  <- a tester marks each finding Real bug / Not a bug")


if __name__ == "__main__":
    main()
