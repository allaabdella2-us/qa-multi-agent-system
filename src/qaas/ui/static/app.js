/* qaas dashboard.
 *
 * No framework, no bundler, no CDN: the project has no JS toolchain for its own
 * code, and a dashboard that needed one would not survive `pip install`.
 *
 * State arrives two ways and only two. A `snapshot` is the whole RunView; every
 * later message patches it. On an EventSource reconnect the snapshot is fetched
 * again rather than trusting deltas accumulated across a gap we cannot see.
 */

const $ = (id) => document.getElementById(id);

/* KIND_STYLE (cli.py:24-33), so the web view and `qaas trace` agree. */
const KIND_CLASS = {
  run_started: "k-bold", run_finished: "k-bold",
  agent_started: "k-cyan", agent_finished: "k-cyan",
  denial: "k-yellow", stop_blocked: "k-yellow", contract_unmet: "k-yellow",
  skipped: "k-dim", tool_call: "k-dim", dry_run: "k-dim",
  escalation: "k-red", agent_error: "k-red", tool_error: "k-red",
  regression: "k-red", reopened: "k-red",
  envelope: "k-magenta", reproduction: "k-magenta", ticket: "k-magenta",
  verdict: "k-green", verified: "k-green", review: "k-green",
};
const QUIET = new Set(["tool_call", "dry_run"]);

const state = { runId: null, view: null, tab: "findings", source: null, artifacts: [] };

/* ---------- helpers ---------- */

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function hms(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
           : `${m}:${String(s % 60).padStart(2, "0")}`;
}
const money = (n) => `$${(n || 0).toFixed(2)}`;
const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "";

async function getJSON(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  return response.ok ? body : Promise.reject(body.error || response.statusText);
}

/* ---------- header ---------- */

function renderHeader(v) {
  $("run-id").textContent = v.run_id;
  $("mode").textContent = v.mode || "—";
  $("target").innerHTML = v.target_name
    ? `${esc(v.target_name)} <span class="sha">@ ${esc((v.target_sha || "").slice(0, 7))}</span>` +
      (v.target_dirty ? ' <span class="dirty">dirty</span>' : "")
    : "—";

  const status = $("status");
  const live = !v.completed;
  status.className = "status" + (live ? " live" : v.stopped_early ? " stopped" : "");
  $("status-text").textContent = live ? "live" : v.stopped_early ? "stopped early" : "complete";

  $("elapsed").textContent = hms(v.elapsed_s);
  // The cap is wall clock, never dollars: no shipped run mode sets a budget
  // ceiling (system.yaml:11-15), so a "% of budget" bar would be invented.
  $("cap").textContent = v.wall_clock_s ? `/ ${hms(v.wall_clock_s)}` : "";
  const fill = $("clock-fill");
  const ratio = v.wall_clock_s ? v.elapsed_s / v.wall_clock_s : 0;
  fill.style.width = `${Math.min(100, ratio * 100)}%`;
  fill.classList.toggle("over", ratio >= 1);
  $("cost").textContent = money(v.cost_usd);
}

/* ---------- phase rail ---------- */

function renderPhases(v) {
  $("phases").innerHTML = v.phases.map((p, i) => {
    const status = (v.phase_status || {})[p] || "pending";
    return `<div class="phase" data-status="${status}" title="${p}: ${status}">
      <span class="bead"></span><span class="name">${p}</span>
    </div>${i < v.phases.length - 1 ? '<span class="link"></span>' : ""}`;
  }).join("");
}

function renderCounters(v) {
  const c = v.counts || {};
  const n = (k) => c[k] || 0;
  const chips = [
    ["findings", v.findings.length, ""],
    ["filed", Object.keys(v.tickets).length, ""],
    ["denials", v.denials.length, v.denials.length ? "warn" : ""],
    ["escalations", v.escalations.length, v.escalations.length ? "bad" : ""],
    ["tool calls", n("tool_call"), ""],
    ["agents", Object.keys(v.agents).length, ""],
  ];
  $("counters").innerHTML = chips
    .map(([k, val, cls]) => `<span class="counter ${cls}"><b>${val}</b> ${k}</span>`)
    .join("");
}

/* ---------- agent grid ---------- */

const ORDER = { running: 0, done: 1, failed: 2, queued: 3, never_ran: 4, skipped: 5 };

function agentCard(a) {
  const label = { running: `turn ${a.turns || "—"}`, done: "done",
                  failed: "failed", skipped: "skipped", queued: "queued",
                  never_ran: "never ran" }[a.status];
  return `<div class="card" data-status="${a.status}" data-agent="${esc(a.name)}">
    <div class="name">${esc(a.name)}</div>
    <div class="layer ${a.layer ? "" : "unknown"}">${esc(a.layer || "not in this roster")}</div>
    <div class="state"><span class="pip"></span>${esc(label)}</div>
    <div class="nums">
      <span><b>${money(a.cost_usd)}</b></span>
      <span><b>${a.findings}</b> find</span>
      ${a.denials ? `<span class="warn-chip"><b>${a.denials}</b> denied</span>` : ""}
    </div>
    ${a.status === "running" && a.last_tool ? `<div class="tool">▸ ${esc(a.last_tool)}</div>` : ""}
    ${a.status === "skipped" && a.reason ? `<div class="why">${esc(a.reason)}</div>` : ""}
    ${a.error ? `<div class="err" title="${esc(a.error)}">${esc(a.error)}</div>` : ""}
  </div>`;
}

function renderAgents(v) {
  const agents = Object.values(v.agents);
  agents.sort((a, b) =>
    (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) ||
    v.phases.indexOf(a.phase) - v.phases.indexOf(b.phase) ||
    a.name.localeCompare(b.name));
  $("agents").innerHTML = agents.map(agentCard).join("") ||
    '<div class="empty">no agents dispatched yet</div>';
  const retired = agents.filter((a) => !a.layer).length;
  // A ledger from an earlier roster names agents no config knows. Saying so is
  // better than a grid of cards with a blank layer and no explanation.
  $("agent-hint").textContent = retired
    ? `— ${retired} named by this run are not in the current roster` : "";
}

function patchAgents(changed) {
  for (const [name, a] of Object.entries(changed)) {
    state.view.agents[name] = a;
    const node = document.querySelector(`.card[data-agent="${CSS.escape(name)}"]`);
    if (node) node.outerHTML = agentCard(a);
    else return renderAgents(state.view);   // a name the grid has never drawn
  }
}

/* ---------- timeline ---------- */

function renderTimeline(v) {
  const svg = $("timeline");
  const lanes = Object.values(v.agents).filter((a) => a.started_at);
  if (!lanes.length || !v.started) { svg.innerHTML = ""; svg.setAttribute("height", 0); return; }

  // A resumed run carries a second `run_started`, and `v.started` is that later
  // one -- so agents dispatched in the first session began before it. The axis
  // starts at the earliest thing it has to draw, or their bars fall off it.
  const starts = lanes.map((a) => new Date(a.started_at).getTime());
  const t0 = Math.min(new Date(v.started).getTime(), ...starts);
  const t1 = Math.max(
    t0 + Math.max(v.elapsed_s, 1) * 1000,
    ...lanes.map((a) => new Date(a.finished_at || Date.now()).getTime()));
  const span = Math.max(1, t1 - t0);

  const LEFT = 92, ROW = 20, PAD = 10, W = 1000;
  const width = W - LEFT - PAD;
  const x = (ms) => LEFT + ((ms - t0) / span) * width;
  const height = lanes.length * ROW + 26;

  const colour = { done: "var(--k-green)", failed: "var(--k-red)",
                   running: "var(--accent)", skipped: "var(--k-dim)" };
  const marks = { envelope: ["var(--k-magenta)", 3], denial: ["var(--k-yellow)", 2.5],
                  escalation: ["var(--k-red)", 3], ticket: ["var(--k-magenta)", 2.5] };

  let out = "";
  lanes.forEach((a, i) => {
    const y = i * ROW + 6;
    const start = new Date(a.started_at).getTime();
    const end = new Date(a.finished_at || Date.now()).getTime();
    out += `<text class="lane-label" x="4" y="${y + 10}">${esc(a.name.slice(0, 12))}</text>`;
    out += `<rect class="lane-bg" x="${LEFT}" y="${y + 3}" width="${width}" height="8" rx="2"/>`;
    out += `<rect class="bar" x="${x(start)}" y="${y + 3}" fill="${colour[a.status] || "var(--k-dim)"}"
             width="${Math.max(2, x(end) - x(start))}" height="8" opacity=".85"><title>${
             esc(a.name)} — ${a.status}, ${money(a.cost_usd)}, ${hms(a.duration_s)}</title></rect>`;
  });

  // Event markers ride on their agent's lane, so a refusal is visibly *inside*
  // the agent that was refused rather than on a separate track.
  const laneOf = Object.fromEntries(lanes.map((a, i) => [a.name, i]));
  for (const e of v.recent || []) {
    const [fill, r] = marks[e.kind] || [];
    if (!fill || !(e.agent in laneOf)) continue;
    const y = laneOf[e.agent] * ROW + 13;
    out += `<circle cx="${x(new Date(e.at).getTime())}" cy="${y}" r="${r}" fill="${fill}">
            <title>${esc(e.kind)} — ${esc(e.text)}</title></circle>`;
  }

  const axisY = lanes.length * ROW + 10;
  out += `<line class="axis" x1="${LEFT}" y1="${axisY}" x2="${W - PAD}" y2="${axisY}"/>`;
  for (let f = 0; f <= 1; f += 0.25) {
    const px = LEFT + f * width;
    out += `<line class="axis" x1="${px}" y1="${axisY}" x2="${px}" y2="${axisY + 3}"/>
            <text class="axis-label" x="${px}" y="${axisY + 13}" text-anchor="middle">${
            hms((span * f) / 1000)}</text>`;
  }

  svg.setAttribute("viewBox", `0 0 ${W} ${height}`);
  svg.setAttribute("height", height);
  svg.innerHTML = out;
}

/* ---------- tabs ---------- */

function renderTabs(v) {
  $("n-findings").textContent = v.findings.length;
  $("n-denials").textContent = v.denials.length;
  $("n-tickets").textContent = Object.keys(v.tickets).length;
  $("n-escalations").textContent = v.escalations.length;
  $("n-artifacts").textContent = state.artifacts.length;
}

const PANELS = {
  findings: (v) => v.findings.length ? v.findings.map((f) => `
    <button class="row row-find" data-finding="${esc(f.id)}">
      <span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span>
      <span class="dom">${esc(f.domain)}</span>
      <span class="title">${esc(f.title)}</span>
      <span class="by">${esc(f.discovered_by)} · ${f.confidence.toFixed(2)} ·
        ${f.ticket_key ? `<span class="filed">${esc(f.ticket_key)}</span>`
                       : f.fileable ? "fileable" : `<span class="held">held</span>`}</span>
    </button>`).join("")
    : `<div class="empty">no findings yet</div>`,

  guardrails: (v) => v.denials.length ? v.denials.map((d) => `
    <div class="row row-den">
      <span class="by">${clock(d.at)}</span>
      <span class="dom">${esc(d.agent)} · ${esc(d.tool)}</span>
      <span class="reason">${esc(d.reason)}${
        d.args && d.args.command ? ` <code>${esc(d.args.command).slice(0, 120)}</code>` : ""}</span>
    </div>`).join("")
    // Not an error state: a discovery agent being refused Bash is the matrix
    // working. An empty feed means nothing tried to step outside it.
    : `<div class="empty">nothing was refused — no agent tried to step outside its allowlist</div>`,

  tickets: (v) => Object.keys(v.tickets).length ? Object.values(v.tickets).map((t) => `
    <div class="row row-tick">
      <span class="dom">${esc(t.key)}</span>
      <span class="sev sev-${esc(t.severity || "minor")}">${esc(t.severity || "")}</span>
      <span class="verdict verdict-${esc(t.verdict || "none")}">${
        esc(t.verdict || "no verdict")}</span>
      <span class="reason">${t.reopens ? `reopened ${t.reopens}×` : ""}${
        t.restricted ? " · restricted project" : ""}</span>
    </div>`).join("")
    : `<div class="empty">no tickets filed</div>`,

  escalations: (v) => v.escalations.length
    ? v.escalations.map((e) => `<div class="row"><span class="reason">${esc(e)}</span></div>`).join("")
    : `<div class="empty">nothing escalated to a human</div>`,

  artifacts: () => state.artifacts.length ? state.artifacts.map((a) => `
    <button class="row row-den" data-artifact="${esc(a.name)}">
      <span class="dom">${esc(a.suffix || "file")}</span>
      <span class="by">${a.bytes} B</span>
      <span class="title">${esc(a.name)}</span>
    </button>`).join("")
    : `<div class="empty">no artifacts</div>`,

  score: () => `<div class="empty">loading…</div>`,
};

function renderPanel() {
  const v = state.view;
  $("panel").innerHTML = PANELS[state.tab](v);
  if (state.tab === "score") loadScore();
}

async function loadScore() {
  try {
    const s = await getJSON(`/api/runs/${state.runId}/score`);
    const pct = (x) => `${Math.round((x || 0) * 100)}%`;
    $("panel").innerHTML = `<div class="metrics">
      ${[["found", `${s.found} of ${s.of}`], ["recall", pct(s.recall)],
         ["precision", pct(s.precision)], ["false positives", s.false_positives],
         ["duplicates", s.duplicates], ["severity agreement", pct(s.severity_agreement)],
         ["cost", money(s.cost_usd)],
         ["per accepted", s.cost_per_accepted == null ? "—" : money(s.cost_per_accepted)]]
        .map(([k, val]) => `<div class="metric"><div class="v">${esc(val)}</div>
                            <div class="k">${k}</div></div>`).join("")}
    </div>${s.missed && s.missed.length
      ? `<h2 class="section-title">missed</h2><div class="reason">${esc(s.missed.join(", "))}</div>`
      : ""}`;
  } catch (error) {
    $("panel").innerHTML = `<div class="empty">${esc(error)}</div>`;
  }
}

/* ---------- drawer ---------- */

function openDrawer(title, html) {
  $("drawer-title").textContent = title;
  $("drawer-body").innerHTML = html;
  $("drawer").hidden = false;
}

async function showFinding(id) {
  const e = await getJSON(`/api/runs/${state.runId}/findings/${id}`);
  const repro = e.reproduction || {};
  const evidence = (e.evidence || []).map((ev) => {
    const name = ev.uri.startsWith("artifact://") ? ev.uri.split("/").slice(3).join("/") : null;
    return name
      ? `<button class="evidence-link" data-artifact="${esc(name)}">▸ ${esc(name)}</button>
         <p>${esc(ev.note || "")}</p>`
      : `<div class="evidence-link">${esc(ev.uri)}</div>`;
  }).join("") || "<p>none</p>";

  openDrawer(e.title, `
    <h3>summary</h3><p>${esc(e.summary)}</p>
    <h3>identity</h3>
    <dl class="kv">
      <dt>severity</dt><dd class="sev sev-${esc(e.severity)}">${esc(e.severity)}</dd>
      <dt>domain / class</dt><dd>${esc(e.domain)} · ${esc(e["class"])}</dd>
      <dt>confidence</dt><dd>${e.confidence}</dd>
      <dt>found by</dt><dd>${esc(e.discovered_by)}</dd>
      <dt>reproduction</dt><dd>${esc(repro.status)}${
        repro.failing_test ? ` · ${esc(repro.failing_test)}` : ""}</dd>
      <dt>fingerprint</dt><dd>${esc((e.dedupe || {}).fingerprint || "—")}</dd>
      <dt>ticket</dt><dd>${esc((e.jira || {}).key || "not filed")}</dd>
    </dl>
    <h3>location</h3>
    <ul>${(e.location.paths || []).map((p) => `<li><code>${esc(p)}</code></li>`).join("") ||
      "<li>—</li>"}</ul>
    <h3>steps</h3>
    <ul>${(repro.steps || []).map((s) => `<li>${esc(s)}</li>`).join("") || "<li>—</li>"}</ul>
    <h3>evidence</h3>${evidence}
    <h3>suggested fix area</h3><p>${esc(e.suggested_fix_area || "—")}</p>`);
}

async function showArtifact(name) {
  const url = `/api/runs/${state.runId}/artifacts/${name.split("/").map(encodeURIComponent).join("/")}`;
  const response = await fetch(url);
  const type = response.headers.get("content-type") || "";
  // Artifacts are agent-authored and are served as text or an image, never as
  // HTML. Inserting one as markup would undo that on the way back in.
  const body = type.startsWith("image/")
    ? `<img src="${esc(url)}" alt="${esc(name)}">`
    : esc(await response.text());
  openDrawer(name, `<div class="artifact">${body}</div>`);
}

/* ---------- ledger feed ---------- */

function evRow(e) {
  return `<div class="ev new">
    <span class="t">${clock(e.at)}</span>
    <span><span class="who ${KIND_CLASS[e.kind] || ""}">${esc(e.agent || "router")}</span>
      <span class="kind ${KIND_CLASS[e.kind] || ""}">${esc(e.kind)}</span>
      <div class="txt">${esc(e.text)}</div></span>
  </div>`;
}

function renderFeed(v) {
  const quiet = $("quiet").checked;
  const rows = (v.recent || []).filter((e) => !quiet || !QUIET.has(e.kind)).slice(-200).reverse();
  $("feed").innerHTML = rows.map(evRow).join("") || '<div class="empty">nothing yet</div>';
}

function pushFeed(e) {
  if ($("quiet").checked && QUIET.has(e.kind)) return;
  const feed = $("feed");
  feed.insertAdjacentHTML("afterbegin", evRow(e));
  while (feed.children.length > 200) feed.lastElementChild.remove();
}

/* ---------- run rail ---------- */

async function renderRuns() {
  const runs = await getJSON("/api/runs?limit=40");
  $("run-list").innerHTML = runs.map((r) => `
    <button class="run-item ${r.run_id === state.runId ? "on" : ""}" data-run="${esc(r.run_id)}">
      <div class="when">${esc((r.started || r.run_id).slice(0, 16).replace("T", " "))}
        ${r.live ? '<span class="live-dot">●</span>' : ""}</div>
      <div class="meta"><span>${esc(r.mode || "—")}</span><span>${r.findings}f</span>
        <span>${money(r.cost_usd)}</span></div>
    </button>`).join("") || '<div class="empty">no runs</div>';
}

/* ---------- wiring ---------- */

function renderAll(v) {
  state.view = v;
  renderHeader(v); renderPhases(v); renderCounters(v);
  renderAgents(v); renderTimeline(v); renderTabs(v); renderPanel(); renderFeed(v);
}

/* The timeline redraws the whole SVG, so it is throttled rather than run per
 * ledger line: a burst of tool calls would otherwise rebuild it dozens of times
 * a second. Leaving it out of the patch entirely was worse -- an agent's bar
 * stayed "running" orange long after its card had gone green. */
let timelinePending = null;
function refreshTimeline() {
  if (timelinePending) return;
  timelinePending = setTimeout(() => {
    timelinePending = null;
    renderTimeline(state.view);
  }, 800);
}

function applyPatch(p) {
  const v = state.view;
  Object.assign(v, {
    elapsed_s: p.elapsed_s, cost_usd: p.cost_usd, phase: p.phase,
    phase_status: p.phase_status, counts: p.counts, completed: p.completed,
    stopped_early: p.stopped_early, escalations: p.escalations,
  });
  renderHeader(v); renderPhases(v); renderCounters(v); renderTabs(v);
  if (p.agents && Object.keys(p.agents).length) patchAgents(p.agents);
  refreshTimeline();
}

function connect(runId) {
  if (state.source) state.source.close();
  const source = new EventSource(`/api/runs/${runId}/stream`);
  state.source = source;

  source.addEventListener("snapshot", (m) => renderAll(JSON.parse(m.data)));
  source.addEventListener("patch", (m) => applyPatch(JSON.parse(m.data)));
  source.addEventListener("ledger", (m) => {
    const e = JSON.parse(m.data);
    state.view.recent.push(e);
    pushFeed(e);
  });
  source.addEventListener("finding", (m) => {
    state.view.findings.push(JSON.parse(m.data));
    state.view.findings.sort((a, b) => a.severity_rank - b.severity_rank || b.confidence - a.confidence);
    renderTabs(state.view);
    if (state.tab === "findings") renderPanel();
  });
  source.addEventListener("ticket", (m) => {
    const t = JSON.parse(m.data);
    state.view.tickets[t.key] = t;
    renderTabs(state.view);
    if (state.tab === "tickets") renderPanel();
  });
  source.addEventListener("done", () => {
    source.close(); state.source = null;
    // The stream is the only thing that ended. Re-fetch so the finished view is
    // the server's, not one assembled from deltas.
    getJSON(`/api/runs/${runId}`).then(renderAll).then(renderRuns);
  });
  // EventSource reconnects by itself; the deltas missed during the gap are not
  // recoverable, so the snapshot is fetched again rather than assumed.
  source.onerror = () => { if (source.readyState === EventSource.CONNECTING)
    getJSON(`/api/runs/${runId}`).then(renderAll).catch(() => {}); };
}

async function openRun(runId) {
  state.runId = runId;
  history.replaceState(null, "", `#${runId}`);
  renderAll(await getJSON(`/api/runs/${runId}`));
  state.artifacts = await getJSON(`/api/runs/${runId}/artifacts`).catch(() => []);
  renderTabs(state.view);
  await renderRuns();
  connect(runId);
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    state.tab = tab.dataset.tab;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b === tab));
    return renderPanel();
  }
  const run = event.target.closest("[data-run]");
  if (run) return openRun(run.dataset.run);
  const finding = event.target.closest("[data-finding]");
  if (finding) return showFinding(finding.dataset.finding);
  const artifact = event.target.closest("[data-artifact]");
  if (artifact) return showArtifact(artifact.dataset.artifact);
  if (event.target.closest("#drawer-close")) $("drawer").hidden = true;
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("drawer").hidden = true; });
$("quiet").addEventListener("change", () => renderFeed(state.view));

(async function start() {
  try {
    const runId = location.hash.slice(1) ||
      (await getJSON("/api/runs/live")).run_id;
    await openRun(runId);
  } catch (error) {
    document.querySelector(".stage").innerHTML =
      `<div class="empty">${esc(error)}</div>`;
  }
  // The header clock must move between ledger lines: an agent can think for a
  // minute without writing one, and a frozen clock reads as a dead run.
  setInterval(() => {
    if (state.view && !state.view.completed) {
      state.view.elapsed_s += 1;
      renderHeader(state.view);
    }
  }, 1000);
})();
