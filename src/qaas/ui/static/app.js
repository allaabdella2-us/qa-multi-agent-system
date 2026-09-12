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

/* The roster's own layers, in the order the router walks them (router.py's
 * phase pipeline). Grouping by layer rather than sorting a flat grid is what
 * makes fifteen cards read as a pipeline instead of a pile: a person looking
 * for "is discovery done" scans one band, not fifteen cards. A layer the
 * config does not know still gets a band, because a ledger written by an
 * earlier roster names agents no config can place. */
const LAYERS = [
  ["control",     "map"],
  ["discovery",   "find"],
  ["triage",      "reproduce &amp; file"],
  ["remediation", "fix, review &amp; verify"],
  ["reporting",   "report"],
];

/* Two lines, and a third only when there is something to say.
 *
 * The card used to carry its own layer label under the name, which the band
 * header above it already states for every card in it -- fifteen agents cost a
 * screen and a half, and the repeated word was a whole line of that. What is
 * left is what changes: the name, the state, and the three numbers. */
function agentCard(a) {
  const label = { running: `turn ${a.turns || "—"}`, done: "done",
                  failed: "failed", skipped: "skipped", queued: "queued",
                  never_ran: "never ran" }[a.status];
  const note = a.error ? `<div class="err" title="${esc(a.error)}">${esc(a.error)}</div>`
    : a.status === "running" && a.last_tool
      ? `<div class="tool">▸ ${esc(a.last_tool)}</div>`
    : a.status === "skipped" && a.reason
      ? `<div class="why" title="${esc(a.reason)}">${esc(a.reason)}</div>`
    : "";
  return `<div class="card" data-status="${a.status}" data-agent="${esc(a.name)}"
               title="${esc(a.name)} — ${esc(a.layer || "not in this roster")}">
    <div class="top">
      <span class="pip"></span>
      <span class="name">${esc(a.name)}</span>
      <span class="state">${esc(label)}</span>
    </div>
    <div class="nums">
      <b>${money(a.cost_usd)}</b>
      <span><b>${a.findings}</b>f</span>
      ${a.denials ? `<span class="warn-chip"><b>${a.denials}</b>d</span>` : ""}
      ${a.layer ? "" : '<span class="warn-chip">retired</span>'}
    </div>${note}
  </div>`;
}

function layerBand(title, caption, members) {
  const running = members.filter((a) => a.status === "running").length;
  const settled = members.filter((a) => a.status === "done").length;
  const cost = members.reduce((sum, a) => sum + (a.cost_usd || 0), 0);
  const state = running ? "running" : settled === members.length ? "done" : "idle";
  return `<section class="band" data-state="${state}">
    <div class="band-head">
      <span class="band-name">${title}</span>
      <span class="band-caption">${caption}</span>
      <span class="band-stat">${settled}/${members.length}</span>
      <span class="band-stat band-cost">${money(cost)}</span>
    </div>
    <div class="agents">${members.map(agentCard).join("")}</div>
  </section>`;
}

function renderAgents(v) {
  const agents = Object.values(v.agents);
  const within = (a, b) =>
    (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || a.name.localeCompare(b.name);

  const known = new Set(LAYERS.map(([name]) => name));
  const bands = LAYERS
    .map(([name, caption]) => [name, caption, agents.filter((a) => a.layer === name).sort(within)])
    .filter(([, , members]) => members.length);

  // Agents whose layer this config cannot place -- a ledger from an earlier
  // roster -- get their own band rather than being dropped or mixed in.
  const stray = agents.filter((a) => !known.has(a.layer)).sort(within);
  if (stray.length) bands.push(["unplaced", "not in this roster", stray]);

  $("agents").innerHTML =
    bands.map(([name, caption, members]) => layerBand(name, caption, members)).join("") ||
    '<div class="empty">no agents dispatched yet</div>';

  const retired = agents.filter((a) => !a.layer).length;
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

/* `title` is markup, so every caller escapes what it interpolates. The
 * severity tag has to sit *in* the title rather than under it -- it is the
 * first thing anyone reads, and a drawer that opens on a blocker should say so
 * before the sentence explaining it. */
function openDrawer(title, html) {
  $("drawer-title").innerHTML = title;
  $("drawer-body").innerHTML = html;
  $("drawer-body").scrollTop = 0;
  $("drawer").hidden = false;
}

/* An agent writes prose with commands, paths and URLs embedded in it, and a
 * reproduction step is routinely a shell one-liner. Rendered as one run of
 * body text it becomes the wall of undifferentiated white this drawer used to
 * show -- and the long tokens overflowed the panel rather than wrapping. So
 * two passes: lift anything command-shaped onto its own monospace line, and
 * mark the remaining inline code so it reads as code. Backticks first, because
 * an agent that fenced its own snippet has already told us where it ends. */
const CODE_LINE = /^\s*(?:\$ |curl |git |npm |npx |pnpm |yarn |python3? |pytest |node |docker |psql |sed |awk |grep |GET |POST |PUT |PATCH |DELETE )/;

function prose(text) {
  const value = String(text ?? "").trim();
  if (!value) return '<p class="muted">—</p>';
  return value.split(/\n{2,}/).map((para) => {
    const lines = para.split("\n");
    // A paragraph whose lines are *all* command-shaped is a block, not prose.
    if (lines.every((line) => CODE_LINE.test(line) || !line.trim())) {
      return `<pre class="snippet">${esc(para.trim())}</pre>`;
    }
    return `<p>${inlineCode(para)}</p>`;
  }).join("");
}

/* Backticked spans become <code>; bare paths, dotted filenames and URLs are
 * marked too, since agents rarely bother with backticks. `esc` runs first so
 * nothing here can inject markup. */
function inlineCode(text) {
  let html = esc(text);
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/(https?:\/\/[^\s<>"']+)/g, '<code class="url">$1</code>');
  html = html.replace(
    /(^|[\s(])((?:[\w.-]+\/)+[\w.-]+\.\w+(?::\d+)?)/g, '$1<code>$2</code>');
  return html;
}

function stepList(steps) {
  if (!steps || !steps.length) return '<p class="muted">—</p>';
  return `<ol class="steps">${steps.map((step) => {
    const value = String(step ?? "").trim();
    // Split a step into its instruction and the command that carries it out,
    // so a curl line is readable instead of reflowed into the sentence.
    const lines = value.split("\n");
    const head = [], code = [];
    for (const line of lines) (CODE_LINE.test(line) ? code : head).push(line);
    return `<li>${head.length ? inlineCode(head.join(" ").trim()) : ""}` +
           `${code.length ? `<pre class="snippet">${esc(code.join("\n").trim())}</pre>` : ""}</li>`;
  }).join("")}</ol>`;
}

async function showFinding(id) {
  const e = await getJSON(`/api/runs/${state.runId}/findings/${id}`);
  const repro = e.reproduction || {};
  const evidence = (e.evidence || []).map((ev) => {
    const name = ev.uri.startsWith("artifact://") ? ev.uri.split("/").slice(3).join("/") : null;
    return name
      ? `<button class="evidence-link" data-artifact="${esc(name)}">▸ ${esc(name)}</button>
         ${ev.note ? `<p class="note">${inlineCode(ev.note)}</p>` : ""}`
      : `<div class="evidence-link">${esc(ev.uri)}</div>`;
  }).join("") || '<p class="muted">none</p>';

  const ticket = (e.jira || {}).key;
  const confidence = Number(e.confidence ?? 0);

  openDrawer(`<span class="sev-tag sev-${esc(e.severity)}">${esc(e.severity)}</span>
              <span class="drawer-name">${esc(e.title)}</span>`, `
    <div class="facts">
      <div class="fact"><span>confidence</span><b>${confidence.toFixed(2)}</b>
        <div class="conf-bar"><i style="width:${Math.round(confidence * 100)}%"></i></div></div>
      <div class="fact"><span>found by</span><b>${esc(e.discovered_by)}</b></div>
      <div class="fact"><span>domain</span><b>${esc(e.domain)}</b></div>
      <div class="fact"><span>ticket</span>
        <b class="${ticket ? "ok" : "muted"}">${esc(ticket || "not filed")}</b></div>
    </div>

    <h3>summary</h3>${prose(e.summary)}

    <h3>reproduction</h3>
    <p><span class="pill pill-${esc(repro.status)}">${esc(repro.status || "unattempted")}</span>
       ${repro.failing_test ? `<code>${esc(repro.failing_test)}</code>` : ""}</p>
    ${stepList(repro.steps)}

    <h3>location</h3>
    <ul class="paths">${(e.location.paths || []).map((path) =>
      `<li><code>${esc(path)}</code></li>`).join("") || '<li class="muted">—</li>'}</ul>

    <h3>evidence</h3>${evidence}

    <h3>suggested fix area</h3>${prose(e.suggested_fix_area)}

    <h3>identity</h3>
    <dl class="kv">
      <dt>class</dt><dd>${esc(e["class"])}</dd>
      <dt>fingerprint</dt><dd class="wrap">${esc((e.dedupe || {}).fingerprint || "—")}</dd>
      <dt>envelope</dt><dd class="wrap">${esc(e.id || "—")}</dd>
    </dl>`);
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
  openDrawer(`<span class="drawer-name">${esc(name)}</span>`,
              `<div class="artifact">${body}</div>`);
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

/* ---------- configuration ----------
 *
 * The runs view answers "what happened". This one answers "what would happen":
 * the roster, the prompts in force, the servers, the models, the skills, the
 * hooks, the rails and the governors. It is the same object graph `qaas
 * validate`, `qaas prompts list`, `qaas doctor` and `qaas run --dry-run`
 * already print, in one place instead of four terminal tables.
 *
 * Read-only, like every other part of this page. There is no form here and no
 * route to post one to: `/api/config` is a GET, and a test asserts the route
 * table holds nothing but GETs.
 */

const SECTIONS = [
  { key: "agents",      icon: "i-agents",     label: "agents",
    caption: "the roster: model, tools, prompt, rails" },
  { key: "prompts",     icon: "i-prompts",    label: "prompts",
    caption: "which file each agent is given, and from where" },
  { key: "mcp_servers", icon: "i-mcp",        label: "mcp servers",
    caption: "in-process, stdio and declared — and who may use each" },
  { key: "models",      icon: "i-models",     label: "models",
    caption: "model choice per agent, grouped by model" },
  { key: "skills",      icon: "i-skills",     label: "skills",
    caption: "procedure, as a plugin skill" },
  { key: "hooks",       icon: "i-hooks",      label: "hooks",
    caption: "the three events, and what each one may block" },
  { key: "guardrails",  icon: "i-guardrails", label: "guardrails",
    caption: "the write-permission matrix, per agent" },
  { key: "run_modes",   icon: "i-modes",      label: "run modes",
    caption: "who runs, how many at once, under what cap" },
  { key: "thresholds",  icon: "i-loops",      label: "thresholds & loops",
    caption: "the governors, and what each one costs" },
  { key: "target",      icon: "i-target",     label: "target",
    caption: "the application under test" },
  { key: "settings",    icon: "i-settings",   label: "settings",
    caption: "tracker, config layers, which variables are set" },
];

const cfgState = { data: null, section: "agents", item: 0, filter: "" };

const yes = (value) => value
  ? '<span class="flag on">yes</span>' : '<span class="flag">no</span>';
const chips = (values, extra = "") => (values || []).length
  ? `<div class="chips">${values.map((v) =>
      `<span class="chip ${extra}">${esc(v)}</span>`).join("")}</div>`
  : '<p class="muted">none</p>';
const rules = (values) => (values || []).length
  ? `<ul class="paths">${values.map((v) =>
      `<li><code>${esc(v)}</code></li>`).join("")}</ul>`
  : '<p class="muted">none</p>';

/* Where a value came from, said in one line. "I edited the YAML and nothing
 * changed" is answered by this badge: a nearer layer shadows it. */
function sourceLine(source) {
  if (!source || !source.path) return "";
  const shadowed = (source.shadows || []).length
    ? ` <span class="shadowing">shadows ${source.shadows.map(esc).join(", ")}</span>` : "";
  return `<div class="source"><span class="layer-tag l-${esc(source.layer)}">${
    esc(source.layer)}</span><code>${esc(source.path)}</code>${shadowed}</div>`;
}

function configRows(section, data) {
  switch (section) {
    case "agents":
    case "guardrails": return data.agents || [];
    case "prompts":    return data.prompts || [];
    case "mcp_servers":return data.mcp_servers || [];
    case "models":     return data.models || [];
    case "skills":     return data.skills || [];
    case "hooks":      return data.hooks || [];
    case "run_modes":  return data.run_modes || [];
    case "thresholds": return data.thresholds || [];
    case "target":     return data.target ? [data.target] : [];
    case "settings":   return data.settings ? [data.settings] : [];
    default: return [];
  }
}

function rowTitle(section, row) {
  if (section === "models") return row.model;
  if (section === "hooks") return row.event;
  if (section === "target") return row.name || "no target";
  if (section === "settings") return row.project || "project";
  return row.name;
}

function rowSub(section, row) {
  switch (section) {
    case "agents": return `${row.layer} · ${row.model}`;
    case "guardrails": return row.policy.read_only ? "read-only" :
      `${(row.policy.write_paths || []).length} write paths`;
    case "prompts": return row.agent ? `${row.agent} · ${row.chars} chars`
                                     : `${row.chars} chars`;
    case "mcp_servers": return `${row.kind} · ${row.used_by.length} agents`;
    case "models": return `${row.count} agents`;
    case "skills": return row.used_by.length
      ? `${row.used_by.length} agents` : "unused";
    case "hooks": return `blocking: ${row.blocking}`;
    case "run_modes": return `${row.agents.length} agents · ${
      hms(row.max_wall_clock_s)}`;
    case "thresholds": return String(row.value);
    default: return "";
  }
}

function renderConfigNav() {
  $("config-nav").innerHTML = SECTIONS.map((s) => {
    const count = cfgState.data ? configRows(s.key, cfgState.data).length : 0;
    return `<button data-section="${s.key}" class="${
      s.key === cfgState.section ? "on" : ""}">
      <svg class="i"><use href="#${s.icon}"/></svg>
      <span class="nav-label">${s.label}</span>
      <span class="nav-count">${count}</span>
    </button>`;
  }).join("") +
    `<button id="reset-overrides" class="reset">reset overrides</button>`;
}

function renderConfigList() {
  const rows = configRows(cfgState.section, cfgState.data || {});
  const needle = cfgState.filter.toLowerCase();
  const shown = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => !needle ||
      JSON.stringify(row).toLowerCase().includes(needle));
  $("config-items").innerHTML = shown.map(({ row, index }) =>
    `<button class="config-item ${index === cfgState.item ? "on" : ""}"
             data-index="${index}">
      <span class="ci-name">${esc(rowTitle(cfgState.section, row))}</span>
      <span class="ci-sub">${esc(rowSub(cfgState.section, row))}</span>
    </button>`).join("") ||
    '<div class="empty">nothing matches</div>';
}

function renderConfigDetail() {
  const rows = configRows(cfgState.section, cfgState.data || {});
  const row = rows[cfgState.item];
  const section = SECTIONS.find((s) => s.key === cfgState.section);
  if (!row) {
    $("config-detail").innerHTML =
      `<div class="empty">${esc(section ? section.caption : "")}</div>`;
    return;
  }
  $("config-detail").innerHTML =
    `<header class="cd-head">
       <h2>${esc(rowTitle(cfgState.section, row))}</h2>
       <p>${esc(section.caption)}</p>
     </header>` + DETAIL[cfgState.section](row);
}

const DETAIL = {
  agents: (a) => `
    <p class="lede">${esc(a.role)}</p>
    <div class="facts">
      <div class="fact"><span>layer</span><b>${esc(a.layer)}</b></div>
      ${field(a.name, "model", "model", a.model, MODELS)}
      ${field(a.name, "effort", "effort", a.effort, EFFORTS)}
      ${field(a.name, "max_turns", "max turns", a.max_turns)}
    </div>
    <p class="muted">Model, effort and the turn cap are editable and write to
      <code>overrides.yaml</code>. Everything below is not: an agent's policy,
      tools, servers and output contract are what the guardrails enforce, and
      they are edited in the config file on purpose.</p>
    <h3>tools</h3>${chips(a.builtin_tools)}
    <h3>mcp servers <small>${a.mcp_servers.length}/6</small></h3>
    ${chips(a.mcp_servers, "chip-mcp")}
    <h3>skills</h3>${chips(a.skills, "chip-skill")}
    <h3>output contract</h3>
    ${a.must_call.length
      ? `<p>The Stop hook blocks a turn that ends without calling these.</p>${
          chips(a.must_call, "chip-must")}`
      : '<p class="muted">none — this agent may stop whenever it likes</p>'}
    <h3>prompt</h3>
    <p><code>${esc(a.prompt.name)}</code> · ${a.prompt.chars} chars</p>
    ${sourceLine(a.prompt.source)}
    ${a.prompt.append
      ? `<p>plus an override block: <code>${esc(a.prompt.append)}</code></p>` : ""}
    <h3>defined in</h3>${sourceLine(a.source) || '<p class="muted">packaged</p>'}`,

  guardrails: (a) => `
    <p class="lede">Enforced by the <code>PreToolUse</code> hook in
      <code>guardrails.py</code>, in code, never requested in a prompt. A denial
      returns a reason and is logged; it never kills the turn.</p>
    <div class="facts">
      <div class="fact"><span>read only</span><b>${a.policy.read_only ? "yes" : "no"}</b></div>
      <div class="fact"><span>may open pr</span><b>${a.policy.may_open_pr ? "yes" : "no"}</b></div>
      <div class="fact"><span>max diff files</span><b>${a.policy.max_diff_files ?? "—"}</b></div>
      <div class="fact"><span>max diff lines</span><b>${a.policy.max_diff_lines ?? "—"}</b></div>
    </div>
    <h3>may write</h3>
    <p>Target-relative. A pattern written <code>*/x/*</code> will not match a
       repository's own root-level <code>x/</code>.</p>
    ${rules(a.policy.write_paths)}
    <h3>branches</h3>${rules(a.policy.branch_patterns)}
    <h3>forbidden — these stop at a human</h3>${rules(a.policy.forbidden_paths)}
    <h3>protected</h3>${rules(a.policy.protected_paths)}
    <h3>tickets</h3>
    <dl class="kv">
      <dt>may create</dt><dd>${yes(a.policy.may_create_tickets)}</dd>
      <dt>may transition</dt><dd>${yes(a.policy.may_transition_tickets)}</dd>
    </dl>`,

  prompts: (p) => `
    ${p.role ? `<p class="lede">${esc(p.role)}</p>` : ""}
    <div class="facts">
      <div class="fact"><span>agent</span><b>${esc(p.agent || "all")}</b></div>
      <div class="fact"><span>size</span><b>${p.chars}</b></div>
    </div>
    <h3>in force from</h3>${sourceLine(p.source) || '<p class="muted">not found</p>'}
    ${p.append ? `<h3>override block</h3>
      <p><code>${esc(p.append)}</code> is inserted between the agent block and
         the shared house rules — never after, because the house rules must stay
         the last word.</p>` : ""}
    <h3>make it yours</h3>
    <pre class="snippet">qaas prompts eject ${esc(p.agent || "")}
qaas prompts diff</pre>`,

  mcp_servers: (m) => `
    <div class="facts">
      <div class="fact"><span>kind</span><b>${esc(m.kind)}</b></div>
      <div class="fact"><span>origin</span><b>${m.declared ? "declared" : "builtin"}</b></div>
      <div class="fact"><span>agents</span><b>${m.used_by.length}</b></div>
    </div>
    <h3>runs</h3><pre class="snippet">${esc(m.runs || "—")}</pre>
    <h3>available to</h3>
    ${m.used_by.length ? chips(m.used_by, "chip-agent") :
      `<p class="warn">Declared and named by nobody. Declaring a server grants
        nothing — an agent receives it only by naming it in its own
        <code>mcp_servers</code> list, so this is usually a typo.</p>`}
    ${m.env && m.env.length ? `<h3>environment</h3>
      <p>Read from the environment at launch. Values are never shown here.</p>
      ${chips(m.env, "chip-env")}` : ""}
    ${m.declared ? `<p class="warn">A server's tools are allowed wholesale once
      an agent names it. qaas checks that the agent declared the server; it
      cannot inspect what a third-party server's tools actually do.
      <b>A server you declare is a server you trust.</b></p>` : ""}`,

  models: (m) => `
    <div class="facts">
      <div class="fact"><span>agents</span><b>${m.count}</b></div>
    </div>
    <h3>used by</h3>
    <table class="grid-table"><thead><tr>
      <th>agent</th><th>layer</th><th>effort</th><th>turns</th><th>cap</th>
    </tr></thead><tbody>${m.agents.map((a) => `<tr>
      <td><b>${esc(a.name)}</b></td><td>${esc(a.layer)}</td>
      <td>${esc(a.effort)}</td><td>${a.max_turns}</td>
      <td>${a.max_budget_usd == null ? "—" : money(a.max_budget_usd)}</td>
    </tr>`).join("")}</tbody></table>
    <p class="muted">Model is per-agent configuration — <code>model:</code> in
      each agent's YAML. Today every provider is Claude through the Claude Agent
      SDK; making the model a choice is the roadmap.</p>`,

  skills: (s) => `
    <p class="lede">${esc(s.summary || "—")}</p>
    <div class="facts">
      <div class="fact"><span>loaded as</span><b>${esc(s.qualified || s.name)}</b></div>
      <div class="fact"><span>size</span><b>${s.chars}</b></div>
      <div class="fact"><span>agents</span><b>${s.used_by.length}</b></div>
    </div>
    <h3>given to</h3>
    ${s.used_by.length ? chips(s.used_by, "chip-agent")
      : '<p class="muted">no agent lists this skill</p>'}
    <h3>file</h3><p><code>${esc(s.path || "—")}</code></p>
    <p class="muted">Skills reach an agent as a Claude Code plugin, so they
      travel inside the wheel. The name is rewritten to
      <code>${esc(s.qualified || "qaas:" + s.name)}</code>: the SDK matches skill
      names down two channels with different rules, and the unqualified name
      loads on one but never matches the allow rule on the other.</p>`,

  hooks: (h) => `
    <div class="facts">
      <div class="fact"><span>event</span><b>${esc(h.event)}</b></div>
      <div class="fact"><span>can block</span><b>${esc(h.blocking)}</b></div>
    </div>
    <h3>what it does</h3><p>${esc(h.what)}</p>
    <h3>handlers</h3>${chips(h.handlers.split(", "))}`,

  run_modes: (m) => `
    <div class="facts">
      <div class="fact"><span>trigger</span><b>${esc(m.trigger)}</b></div>
      <div class="fact"><span>wall clock</span><b>${hms(m.max_wall_clock_s)}</b></div>
      <div class="fact"><span>concurrency</span><b>${m.max_concurrency}</b></div>
      <div class="fact"><span>budget cap</span>
        <b>${m.max_budget_usd == null ? "none" : money(m.max_budget_usd)}</b></div>
    </div>
    <h3>roster <small>${m.agents.length}</small></h3>${chips(m.agents, "chip-agent")}
    <h3>files tickets</h3><p>${yes(m.files_tickets)}</p>
    <h3>run it</h3><pre class="snippet">qaas run --mode ${esc(m.name)} --dry-run</pre>
    ${m.max_budget_usd == null ? `<p class="muted">No shipped mode sets a dollar
      cap, which is why the dashboard's progress bar measures elapsed time and
      never money: a percentage of nothing would be invented.</p>` : ""}`,

  thresholds: (t) => `
    <div class="facts">
      ${field(null, t.name, "value", t.value,
              t.name === "reproduce_min_severity" ? SEVERITIES : null)}
    </div>
    <h3>what it does</h3><p>${esc(t.note)}</p>
    <p class="muted">Editing writes to <code>overrides.yaml</code> beside your
      config. Run <code>qaas score</code> afterwards — it is the only way to
      know whether the change helped, rather than just changed something.</p>`,

  target: (t) => !t.loaded
    ? `<p class="lede">No target profile is loaded. <code>qaas init .</code>
         inspects a repository and writes one.</p>`
    : `<p class="lede">${esc(t.description || "—")}</p>
    <div class="facts">
      <div class="fact"><span>mode</span><b>${esc(t.environment_mode)}</b></div>
      <div class="fact"><span>branch</span><b>${esc(t.default_branch || "—")}</b></div>
      <div class="fact"><span>auth</span><b>${esc(t.auth_mode)}</b></div>
    </div>
    <h3>root</h3><p><code>${esc(t.root)}</code></p>
    <h3>capabilities</h3>
    <dl class="kv">${Object.entries(t.capabilities || {}).map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${yes(v)}</dd>`).join("")}</dl>
    <h3>urls</h3>
    <dl class="kv">
      <dt>web</dt><dd>${esc(t.web_url || "—")}</dd>
      <dt>api</dt><dd>${esc(t.api_url || "—")}</dd>
    </dl>
    ${(t.roles || []).length ? `<h3>roles</h3>
      <table class="grid-table"><thead><tr>
        <th>role</th><th>username</th><th>password from</th><th>set</th>
      </tr></thead><tbody>${t.roles.map((r) => `<tr>
        <td><b>${esc(r.role)}</b></td><td>${esc(r.username || "—")}</td>
        <td><code>${esc(r.password_env || "—")}</code></td>
        <td>${yes(r.set)}</td></tr>`).join("")}</tbody></table>
      <p class="muted">Credentials never live in a profile: it names environment
        variables, and this page reports only whether each one is set.</p>` : ""}`,

  settings: (s) => `
    <div class="facts">
      <div class="fact"><span>tracker</span><b>${esc(s.tracker)}</b></div>
      <div class="fact"><span>vcs</span><b>${esc(s.vcs || "—")}</b></div>
      <div class="fact"><span>state root</span><b>${esc(s.state_root)}</b></div>
    </div>
    ${s.tracker_override ? `<p class="warn">
      <code>QAAS_TRACKER=${esc(s.tracker_override)}</code> is set in this
      environment and overrides the file.</p>` : ""}
    <h3>config layers</h3>
    <p>Nearest first. <code>system.yaml</code> is <b>first hit wins whole</b>;
       agents, prompts and skills union by name with the nearer layer shadowing.</p>
    <ol class="layers">${(s.config_dirs || []).map((d) => `<li>
      <span class="layer-tag l-${esc(d.layer)}">${esc(d.layer)}</span>
      <code>${esc(d.path)}</code>
      ${d.exists ? "" : '<span class="muted">— absent</span>'}</li>`).join("")}</ol>
    <h3>prompts from</h3>${rules(s.prompt_dirs)}
    <h3>plugin</h3>${rules(s.plugin_dirs)}
    <h3>environment</h3>
    <p>Whether each variable is set. Values are never read into this page.
       <code>qaas tracker-check</code> validates them, and prints nothing secret
       either.</p>
    <table class="grid-table"><thead><tr><th>variable</th><th>set</th></tr></thead>
    <tbody>${(s.env || []).map((e) => `<tr>
      <td><code>${esc(e.name)}</code></td><td>${yes(e.set)}</td>
    </tr>`).join("")}</tbody></table>`,
};

async function loadConfig() {
  if (cfgState.data) return;
  try {
    cfgState.data = await getJSON("/api/config");
  } catch (err) {
    $("config-detail").innerHTML =
      `<div class="empty">configuration unavailable: ${esc(err)}</div>`;
    return;
  }
  renderConfig();
}

function renderConfig() {
  renderConfigNav();
  renderConfigList();
  renderConfigDetail();
}

function showView(view) {
  $("grid-main").hidden = view !== "runs";
  $("config").hidden = view !== "config";
  for (const button of $("views").querySelectorAll("button")) {
    button.classList.toggle("on", button.dataset.view === view);
  }
  if (view === "config") loadConfig();
}

$("views").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) showView(button.dataset.view);
});

$("config-nav").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-section]");
  if (!button) return;
  cfgState.section = button.dataset.section;
  cfgState.item = 0;
  cfgState.filter = "";
  $("config-search").value = "";
  renderConfig();
});

$("config-items").addEventListener("click", (event) => {
  const button = event.target.closest(".config-item");
  if (!button) return;
  cfgState.item = Number(button.dataset.index);
  renderConfigList();
  renderConfigDetail();
});

$("config-search").addEventListener("input", (event) => {
  cfgState.filter = event.target.value;
  renderConfigList();
});

/* ---------- theme ----------
 *
 * Three states, not two: explicit light, explicit dark, and no choice at all —
 * which is the default and follows the operating system. The button cycles
 * between the two explicit states, because someone who clicks a theme switch
 * wants the theme they picked to stick, including when their system flips at
 * sunset.
 *
 * `index.html` reads the stored value in a blocking inline script so the first
 * paint is already correct; this only handles the click.
 */
$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const current = root.dataset.theme ||
    (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = current === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try {
    localStorage.setItem("qaas-theme", next);
  } catch (err) {
    // A private window or blocked site data. The theme still applies for this
    // page; it just will not survive a reload, which is better than throwing.
  }
  // Nothing else to repaint: the timeline is drawn SVG, but every fill it
  // writes is a `var(--k-*)` string rather than a resolved colour, so it
  // follows the tokens without being redrawn. Keep it that way.
});

/* ---------- editing an override ----------
 *
 * The only writing this page does. It changes what a model *is* -- which model
 * an agent runs, a turn cap, a threshold -- and never what an agent is
 * *allowed to do*. The server refuses anything outside its tunable set, so
 * this is a convenience over `overrides.yaml` rather than a second authority:
 * every field here has an equivalent line in that file, and deleting the file
 * undoes all of it.
 */

const MODELS = ["claude-opus-5", "claude-opus-4-7", "claude-sonnet-5",
                "claude-haiku-4-5-20251001"];
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const SEVERITIES = ["trivial", "minor", "major", "critical", "blocker"];

/* An editable fact. A select where the values are a closed set, a text box
 * where they are a number -- and the same card shape as a read-only fact, so
 * the panel does not become a form. */
function field(agent, name, label, value, options) {
  const attrs = `data-agent="${agent ? esc(agent) : ""}" data-field="${esc(name)}"`;
  const control = options
    ? `<select class="edit" ${attrs}>${
        options.concat(options.includes(String(value)) ? [] : [String(value)])
          .map((o) => `<option${o === String(value) ? " selected" : ""}>${esc(o)}</option>`)
          .join("")}</select>`
    : `<input class="edit" type="text" inputmode="decimal"
              value="${esc(String(value ?? ""))}" ${attrs}>`;
  return `<div class="fact editable"><span>${esc(label)}</span>${control}</div>`;
}

async function saveOverride(section, agent, name, value) {
  const detail = $("config-detail");
  detail.classList.add("saving");
  try {
    const response = await fetch("/api/config/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section, agent, values: { [name]: value } }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || response.statusText);
    // Re-fetch rather than patching in place: a nearer layer can still shadow
    // the field, and showing what was asked for instead of what a run would
    // now see is the one thing this panel must not do.
    cfgState.data = await getJSON("/api/config");
    renderConfig();
    toast("saved to overrides.yaml");
  } catch (err) {
    toast(String(err.message || err), true);
  } finally {
    detail.classList.remove("saving");
  }
}

function toast(text, bad = false) {
  let node = $("toast");
  if (!node) {
    node = document.createElement("div");
    node.id = "toast";
    document.body.appendChild(node);
  }
  node.textContent = text;
  node.className = bad ? "bad" : "";
  node.classList.add("on");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("on"), bad ? 6000 : 2200);
}

$("config-detail").addEventListener("change", (event) => {
  const control = event.target.closest(".edit");
  if (!control) return;
  const agent = control.dataset.agent || null;
  const name = control.dataset.field;
  let value = control.value.trim();
  // A number typed into a text box arrives as a string, and the loader's
  // `extra="forbid"` model would reject it. An empty box means "drop the
  // override", which the server reads as null.
  if (value === "") value = null;
  else if (control.tagName === "INPUT" && value !== "" && !Number.isNaN(Number(value))) {
    value = Number(value);
  }
  saveOverride(agent ? "agents" : "thresholds", agent, name, value);
});

/* One button to undo every override at once, since the file is the unit. */
$("config-nav").addEventListener("click", async (event) => {
  if (!event.target.closest("#reset-overrides")) return;
  try {
    const response = await fetch("/api/config/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reset: true }),
    });
    if (!response.ok) throw new Error((await response.json()).error);
    cfgState.data = await getJSON("/api/config");
    renderConfig();
    toast("overrides cleared");
  } catch (err) {
    toast(String(err.message || err), true);
  }
});
