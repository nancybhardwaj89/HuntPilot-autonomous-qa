// Small helper for talking to the Python backend
async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

export const api = {
  config: () => call("GET", "/api/config"),
  startHunt: (opts) => call("POST", "/api/hunt", opts),
  huntStatus: (since) => call("GET", `/api/hunt?since=${since}`),
  report: () => call("GET", "/api/report"),
  findings: () => call("GET", "/api/findings"),
  saveReviews: (reviews) => call("PUT", "/api/findings", reviews),
  sendToJira: () => call("POST", "/api/jira"),
};
