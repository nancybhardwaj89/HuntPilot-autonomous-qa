import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

// Show user names with a capital letter (the login itself stays lowercase)
const cap = (name) => (name ? name.charAt(0).toUpperCase() + name.slice(1) : name);

const VERDICTS = [
  { value: "Real bug", tone: "real" },
  { value: "Not a bug", tone: "not" },
  { value: "Duplicate", tone: "dup" },
];

export default function App() {
  const [config, setConfig] = useState(null);
  const [tab, setTab] = useState("hunt");
  const [findings, setFindings] = useState([]);
  const [error, setError] = useState("");

  const loadFindings = useCallback(() => api.findings().then(setFindings).catch((e) => setError(e.message)), []);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setError("Can't reach the HuntPilot server. Is it running on port 8001?"));
    loadFindings();
  }, [loadFindings]);

  const reviewed = findings.filter((f) => f.verdict).length;
  const approved = findings.filter((f) => f.verdict.toLowerCase().startsWith("real")).length;

  const steps = [
    { id: "hunt", label: "Hunt", note: "AI explores the app" },
    { id: "review", label: "Review", note: findings.length ? `${reviewed} of ${findings.length} reviewed` : "QA decides" },
    { id: "jira", label: "Send to Jira", note: `${approved} approved` },
  ];

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="mark" aria-hidden="true">◎</span>
          <div>
            <div className="name">HuntPilot</div>
            <div className="tagline">Autonomous bug hunting for web apps</div>
          </div>
        </div>
        <nav className="steps" aria-label="Workflow">
          {steps.map((s, i) => (
            <button key={s.id} className="step" aria-current={tab === s.id ? "step" : undefined} onClick={() => setTab(s.id)}>
              <span className="num">{i + 1}</span>
              <span>
                <span className="step-label">{s.label}</span>
                <span className="step-note">{s.note}</span>
              </span>
            </button>
          ))}
        </nav>
      </header>

      <main>
        {error && <div className="banner error" role="alert">{error}</div>}
        {config && tab === "hunt" && <HuntTab config={config} onDone={() => { loadFindings(); }} goReview={() => setTab("review")} />}
        {tab === "review" && <ReviewTab findings={findings} setFindings={setFindings} goJira={() => setTab("jira")} />}
        {config && tab === "jira" && <JiraTab config={config} findings={findings} />}
      </main>
    </div>
  );
}

/* ---------------- 1. Hunt ---------------- */
function HuntTab({ config, onDone, goReview }) {
  const [url, setUrl] = useState(config.target_url);
  const [users, setUsers] = useState(["alex", "sam"]);
  const [tests, setTests] = useState(6);
  const [log, setLog] = useState([]);
  const [running, setRunning] = useState(false);
  const [exitCode, setExitCode] = useState(null);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const count = useRef(0);
  const logEnd = useRef(null);

  const poll = useCallback(async () => {
    const s = await api.huntStatus(count.current);
    if (s.lines.length) {
      count.current = s.total;
      setLog((l) => [...l, ...s.lines]);
    }
    setRunning(s.running);
    setExitCode(s.exit_code);
    return s;
  }, []);

  // Pick up a hunt that is already running (e.g. after a page refresh)
  useEffect(() => { poll(); api.report().then((r) => r.results && setReport(r)); }, [poll]);

  useEffect(() => {
    if (!running) return;
    const t = setInterval(async () => {
      const s = await poll();
      if (!s.running) {
        clearInterval(t);
        onDone();
        api.report().then(setReport);
      }
    }, 1000);
    return () => clearInterval(t);
  }, [running, poll, onDone]);

  useEffect(() => { logEnd.current?.scrollIntoView({ block: "nearest" }); }, [log]);

  const start = async () => {
    setError("");
    try {
      count.current = 0;
      setLog([]);
      setReport(null);
      await api.startHunt({ target_url: url, users, tests_per_page: Number(tests) });
      setRunning(true);
    } catch (e) {
      setError(e.message);
    }
  };

  const toggleUser = (u) => setUsers((list) => (list.includes(u) ? list.filter((x) => x !== u) : [...list, u]));
  const pages = log.filter((l) => l.includes("LOOK")).length;
  const bugs = log.filter((l) => l.includes("JUDGE  BUG")).length;

  return (
    <section className="grid-hunt">
      <div className="panel">
        <h1>Start a hunt</h1>
        <p className="lead">The agent signs in, explores every page with a form, designs tricky tests and reports what looks broken. Nothing goes to Jira until you review it.</p>
        <label className="field">
          <span>App to test</span>
          <input value={url} onChange={(e) => setUrl(e.target.value)} disabled={running} />
        </label>
        <fieldset className="field">
          <legend>Sign in as</legend>
          <div className="checks">
            {[["alex", "clean app"], ["sam", "seeded bugs"]].map(([u, hint]) => (
              <label key={u} className="check">
                <input type="checkbox" checked={users.includes(u)} onChange={() => toggleUser(u)} disabled={running} />
                {cap(u)} <span className="muted">({hint})</span>
              </label>
            ))}
          </div>
        </fieldset>
        <label className="field">
          <span>Tests per page</span>
          <input type="number" min="1" max="12" value={tests} onChange={(e) => setTests(e.target.value)} disabled={running} className="short" />
        </label>
        {error && <div className="banner error" role="alert">{error}</div>}
        <button className="primary" onClick={start} disabled={running || !users.length}>
          {running ? "Hunting…" : "Start hunt"}
        </button>
        <p className="fine">Model: {config.model}. A full run takes about 6 to 7 minutes.</p>
      </div>

      <div className="panel log-panel">
        <div className="log-head">
          <h2>Live log</h2>
          <span className="muted">
            {running ? `Running: ${pages} page(s) checked, ${bugs} suspected bug(s)` : exitCode === null ? "Idle" : exitCode === 0 ? "Finished" : `Stopped with an error (code ${exitCode})`}
          </span>
        </div>
        <pre className="log" aria-live="polite">
          {log.length ? log.map((l, i) => <div key={i} className={lineTone(l)}>{l || " "}</div>) : <span className="muted">The agent's progress appears here.</span>}
          <span ref={logEnd} />
        </pre>
      </div>

      {report?.results && !running && <RunSummary report={report} goReview={goReview} />}
    </section>
  );
}

function lineTone(l) {
  if (l.includes("JUDGE  BUG")) return "l-bug";
  if (l.includes("LOOK")) return "l-look";
  if (l.includes("Error") || l.includes("Traceback")) return "l-err";
  if (l.startsWith("===")) return "l-head";
  return "";
}

function RunSummary({ report, goReview }) {
  const t = report.total_ai_usage || {};
  return (
    <div className="panel summary">
      <h2>Last run</h2>
      <div className="stats">
        {report.results.map((r) => (
          <div key={r.user} className="stat">
            <div className="stat-num">{r.findings.length}</div>
            <div className="stat-label">findings as {cap(r.user)}{r.minutes ? `, ${r.minutes} min` : ""}</div>
          </div>
        ))}
        <div className="stat">
          <div className="stat-num">{(t.total_tokens || 0).toLocaleString()}</div>
          <div className="stat-label">tokens in {t.calls || 0} AI calls</div>
        </div>
        <div className="stat">
          <div className="stat-num">${(t.cost_usd || 0).toFixed(4)}</div>
          <div className="stat-label">AI cost of this run</div>
        </div>
      </div>
      <button className="primary" onClick={goReview}>Review findings</button>
    </div>
  );
}

/* ---------------- 2. Review ---------------- */
function ReviewTab({ findings, setFindings, goJira }) {
  const [filter, setFilter] = useState("all");
  const [zoom, setZoom] = useState(null);
  const [saveState, setSaveState] = useState("");

  const save = async (f, patch) => {
    const next = { ...f, ...patch };
    setFindings((all) => all.map((x) => (x.id === f.id ? next : x)));
    setSaveState("Saving…");
    try {
      await api.saveReviews([{ id: next.id, verdict: next.verdict, notes: next.notes }]);
      setSaveState("All changes saved");
    } catch (e) {
      setSaveState(`Not saved: ${e.message}`);
    }
  };

  if (!findings.length) {
    return <div className="panel empty"><h1>Nothing to review yet</h1><p>Run a hunt first. Its findings will appear here for you to judge.</p></div>;
  }

  const users = [...new Set(findings.map((f) => f.user))];
  const shown = findings.filter((f) => filter === "all" || (filter === "open" ? !f.verdict : f.user === filter));
  const tally = (v) => findings.filter((f) => f.verdict.toLowerCase().startsWith(v)).length;

  return (
    <section>
      <div className="review-head">
        <div>
          <h1>Review findings</h1>
          <p className="lead">The AI's suggestion is on the left. Your decision is on the right. Only findings you mark as a real bug can be sent to Jira.</p>
        </div>
        <div className="tally">
          <span className="pill real">{tally("real")} real</span>
          <span className="pill not">{tally("not")} not a bug</span>
          <span className="pill dup">{tally("dup")} duplicate</span>
          <span className="pill open">{findings.length - findings.filter((f) => f.verdict).length} open</span>
        </div>
      </div>

      <div className="toolbar">
        <div className="filters" role="group" aria-label="Filter findings">
          {[["all", "All"], ["open", "Not reviewed"], ...users.map((u) => [u, `Found as ${cap(u)}`])].map(([id, label]) => (
            <button key={id} className="chip" aria-pressed={filter === id} onClick={() => setFilter(id)}>{label}</button>
          ))}
        </div>
        <span className="muted" role="status">{saveState}</span>
      </div>

      {shown.map((f) => <FindingCard key={f.id} f={f} save={save} onZoom={setZoom} />)}

      <div className="next">
        <button className="primary" onClick={goJira}>Continue to Jira</button>
      </div>

      {zoom && (
        <div className="lightbox" role="dialog" aria-label="Screenshot" onClick={() => setZoom(null)}>
          <img src={zoom} alt="Screenshot of the page after the test" />
          <button className="secondary" onClick={() => setZoom(null)}>Close</button>
        </div>
      )}
    </section>
  );
}

function FindingCard({ f, save, onZoom }) {
  const [notes, setNotes] = useState(f.notes);
  const steps = f.steps.split("\n").map((s) => s.replace(/^\d+[.)]\s*/, "")).filter(Boolean);
  const data = f.test_data && f.test_data !== "(none)" ? f.test_data.split(";").map((s) => s.trim()).filter(Boolean) : [];
  const tone = VERDICTS.find((v) => f.verdict.toLowerCase().startsWith(v.value.toLowerCase().slice(0, 3)))?.tone;

  return (
    <article className={`card ${tone ? `decided-${tone}` : ""}`}>
      <header className="card-head">
        <h2>{f.title}</h2>
        <div className="meta">
          <span className={`sev sev-${f.severity}`}>Severity: {f.severity}</span>
          <span>Page: {f.page}</span>
          <span>User: {cap(f.user)}</span>
        </div>
      </header>
      <div className="card-body">
        <div className="ai">
          <div className="side-label">AI suggests</div>
          {steps.length > 0 && (<><h3>Steps to reproduce</h3><ol>{steps.map((s, i) => <li key={i}>{s}</li>)}</ol></>)}
          {data.length > 0 && (<><h3>Test data</h3><ul className="data">{data.map((d, i) => <li key={i}>{d}</li>)}</ul></>)}
          <div className="results">
            {f.expected && <div><h3>Expected result</h3><p>{f.expected}</p></div>}
            <div><h3>Actual result</h3><p>{f.actual}</p></div>
          </div>
          {f.screenshot && (
            <button className="shot" onClick={() => onZoom(f.screenshot)} aria-label="Enlarge screenshot">
              <img src={f.screenshot} alt="Screenshot of the page after the test" loading="lazy" />
            </button>
          )}
        </div>
        <div className="qa">
          <div className="side-label">QA decides</div>
          <div className="verdicts" role="group" aria-label="Verdict">
            {VERDICTS.map((v) => (
              <button key={v.value} className={`verdict ${v.tone}`} aria-pressed={f.verdict === v.value}
                onClick={() => save(f, { verdict: f.verdict === v.value ? "" : v.value, notes })}>
                {v.value}
              </button>
            ))}
          </div>
          <label className="field">
            <span>QA notes</span>
            <textarea rows="5" value={notes} placeholder="Why is this real or not? Did you re-test it?"
              onChange={(e) => setNotes(e.target.value)} onBlur={() => notes !== f.notes && save(f, { notes })} />
          </label>
        </div>
      </div>
    </article>
  );
}

/* ---------------- 3. Jira ---------------- */
function JiraTab({ config, findings }) {
  const approved = findings.filter((f) => f.verdict.toLowerCase().startsWith("real"));
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState(null);
  const [error, setError] = useState("");

  const send = async () => {
    if (!window.confirm(`Create ${approved.length} bug(s) in Jira space ${config.jira_project}?`)) return;
    setBusy(true);
    setError("");
    try {
      setResults((await api.sendToJira()).results);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel jira">
      <h1>Send approved bugs to Jira</h1>
      <p className="lead">Each bug is created in space <strong>{config.jira_project}</strong> with steps, expected and actual results, test data and the screenshot, then linked to its user story. Bugs already in Jira are skipped.</p>
      {!config.jira_ready && <div className="banner error">Jira isn't set up yet. Add JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN to your .env file and restart the server.</div>}
      {approved.length === 0 ? (
        <p className="muted">No findings are marked as a real bug yet. Review them first.</p>
      ) : (
        <ul className="approved">{approved.map((f) => <li key={f.id}><span className={`sev sev-${f.severity}`}>{f.severity}</span> [{f.page}] {f.title}</li>)}</ul>
      )}
      {error && <div className="banner error" role="alert">{error}</div>}
      <button className="primary" onClick={send} disabled={busy || !approved.length || !config.jira_ready}>
        {busy ? "Creating bugs…" : `Create ${approved.length} bug(s) in Jira`}
      </button>

      {results && (
        <div className="results-list">
          <h2>Result</h2>
          <ul>
            {results.map((r, i) => (
              <li key={i} className={`res-${r.status}`}>
                <span className="res-status">{r.status === "created" ? "Created" : r.status === "skipped" ? "Already in Jira" : "Failed"}</span>
                {r.key ? <a href={r.url} target="_blank" rel="noreferrer">{r.key}</a> : null}
                <span>{r.title}</span>
                {r.story && <span className="muted">linked to {r.story}</span>}
                {r.error && <span className="err-text">{r.error}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}