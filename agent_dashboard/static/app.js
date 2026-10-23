"use strict";

const app = document.getElementById("app");
let charts = [];

// ---- helpers ---------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function fmtCost(v) {
  if (v == null) return "–";
  if (v > 0 && v < 0.005) return "<$0.01";
  return "$" + v.toFixed(2);
}
function fmtTokens(v) {
  if (v == null) return "–";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(v);
}
function fmtMs(v) {
  if (v == null) return "–";
  const s = v / 1000;
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
const duration = (p) => p.active_ms ?? p.wall_ms;
const median = (xs) => {
  const s = xs.filter((x) => x != null).sort((a, b) => a - b);
  if (!s.length) return null;
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
/** Change against the median of the comparison group picked below the tiles. */
function deltaText(value, ref) {
  if (value == null || !ref) return "";
  const pct = ((value - ref) / ref) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(0)}% vs group median`;
}

/** Tag categories, in display order. Human wins over the prefix: a hand-added `project:x` is human. */
const TAG_CATEGORIES = [
  ["human", "Human", "added by you on a prompt page or by your auto-tag rules"],
  ["project", "Project", "AI: git repository the prompt ran in"],
  ["model", "Model", "AI: model that answered"],
  ["skill", "Skill", "AI: skill the agent used"],
  ["ai", "Other AI", "AI: slash command, subagent type, subagent persona"],
];
const CATEGORY_INFO = Object.fromEntries(TAG_CATEGORIES.map(([cat, label, help]) => [cat, { label, help }]));
const CATEGORY_ORDER = Object.fromEntries(TAG_CATEGORIES.map(([cat], i) => [cat, i]));

function tagCategory(t) {
  if (t.kind === "human") return "human";
  const prefix = t.tag.split(":")[0];
  return ["project", "model", "skill"].includes(prefix) ? prefix : "ai";
}

/** Category legend; with `active` (a Set, empty = all shown), each entry is a toggle button filtering by category. */
function tagLegend(active) {
  return `<div class="tag-legend">${TAG_CATEGORIES.map(([cat, label, help]) => active
    ? `<button type="button" class="chip cat-${cat}" data-cat="${cat}" aria-pressed="${active.size === 0 || active.has(cat)}" title="${esc(help)}">${label}</button>`
    : `<span class="chip cat-${cat}" title="${esc(help)}"><a>${label}</a></span>`).join("")}</div>`;
}

// ---- keyword rules -----------------------------------------------------------

/** Inputs to give a human tag keywords: prompts containing them get the tag automatically. */
function keywordFields(id) {
  return `<input id="${id}-keywords" placeholder="keywords, comma-separated (optional)" size="32"
      title="Every prompt, past and future, containing these keywords gets the tag (case-insensitive)">
    <select id="${id}-match" title="Prompts must contain all the keywords, or any of them">
      <option value="all">all of them</option><option value="any">any of them</option></select>
    <span id="${id}-preview" class="muted" style="font-size:12px"></span>`;
}

function keywordValues(id) {
  return {
    keywords: document.getElementById(`${id}-keywords`).value.split(",").map((k) => k.trim()).filter(Boolean),
    match: document.getElementById(`${id}-match`).value,
  };
}

/** Live "matches N prompts" count while typing keywords. */
function bindKeywordPreview(id) {
  let timer;
  const update = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const { keywords, match } = keywordValues(id);
      const out = document.getElementById(`${id}-preview`);
      if (!keywords.length) { out.textContent = ""; return; }
      const r = await api(`/api/rules/preview?${new URLSearchParams({ keywords: keywords.join(","), match })}`);
      out.textContent = `matches ${r.matches} prompt${r.matches === 1 ? "" : "s"}`;
    }, 250);
  };
  document.getElementById(`${id}-keywords`).addEventListener("input", update);
  document.getElementById(`${id}-match`).addEventListener("change", update);
}

function ruleText(r) {
  if (r.keywords) return `${r.keywords.map((k) => `<code>${esc(k)}</code>`).join(r.match === "any" ? " or " : " and ")}`;
  return `regex <code>${esc(r.pattern)}</code>`;
}

// ---- cost details --------------------------------------------------------------

/** Agent type, plus the persona it was briefed with: runs of one type with different personas are reported apart. */
function agentName(type, persona) {
  return `<strong>${esc(type)}</strong>${persona ? ` <span class="persona" title="Persona from the start of the subagent's prompt">as ${esc(persona)}</span>` : ""}`;
}

function costNote(r) {
  if (r.cost_source === "claude-code") {
    return `from Claude Code · price table ${fmtCost(r.estimated_cost_usd)}`;
  }
  return r.official_cost_usd != null ? "price table (telemetry incomplete)" : "price-table estimate";
}

/** Claude Code's own cost (OpenTelemetry), per caller and model. */
function telemetryTable(rows) {
  if (!rows.length) return "";
  return `<h2>Cost reported by Claude Code <span class="muted">(OpenTelemetry, includes internal calls)</span></h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Caller</th><th>Model</th><th class="num">Requests</th><th class="num">Input</th><th class="num">Output</th>
        <th class="num">Cache read</th><th class="num">Cache write</th><th class="num">API time</th><th class="num">Cost</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td>${esc(r.source)}</td><td>${esc((r.model || "").replace("claude-", ""))}</td>
        <td class="num">${r.requests}</td><td class="num">${fmtTokens(r.input_tokens)}</td><td class="num">${fmtTokens(r.output_tokens)}</td>
        <td class="num">${fmtTokens(r.cache_read_tokens)}</td><td class="num">${fmtTokens(r.cache_write_tokens)}</td>
        <td class="num">${fmtMs(r.api_ms)}</td><td class="num">${fmtCost(r.cost_usd)}</td></tr>`).join("")}</tbody></table></div>`;
}

function tilesHtml(tiles) {
  return `<div class="tiles">${tiles.map(([l, v, d, note]) => `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div>
    <div class="delta">${d || "&nbsp;"}</div>${note ? `<div class="delta">${note}</div>` : ""}</div>`).join("")}</div>`;
}

/** Chips sorted by category (human first). `removable` adds × on hand-added human tags. */
function tagChips(tags, { removable = false } = {}) {
  const sorted = [...tags].sort((a, b) =>
    CATEGORY_ORDER[tagCategory(a)] - CATEGORY_ORDER[tagCategory(b)] || a.tag.localeCompare(b.tag));
  return sorted.map((t) => {
    const cat = tagCategory(t);
    const remove = removable && t.kind === "human" && !t.from_rule
      ? `<button title="Remove tag" data-remove-tag="${esc(t.tag)}">×</button>` : "";
    const title = t.from_rule ? "Human tag added by one of your auto-tag rules" : `${CATEGORY_INFO[cat].label} tag: ${CATEGORY_INFO[cat].help}`;
    return `<span class="chip cat-${cat}" title="${esc(title)}"><a href="#/tag/${encodeURIComponent(t.tag)}">${esc(t.tag)}</a>${remove}</span>`;
  }).join("");
}

const humanTags = (tags) => tags.filter((t) => t.kind === "human");
const aiTags = (tags) => tags.filter((t) => t.kind === "ai");

function destroyCharts() {
  charts.forEach((c) => c.destroy());
  charts = [];
}

// ---- charts ----------------------------------------------------------------

/** One metric over time. `points` are prompts; `currentId` is highlighted. */
function evolutionChart(canvas, points, metric, format, currentId, othersLabel) {
  const sorted = points.filter((p) => metric(p) != null).sort((a, b) => a.started_at.localeCompare(b.started_at));
  const toXY = (p) => ({ x: new Date(p.started_at).getTime(), y: metric(p), p });
  const current = sorted.filter((p) => p.id === currentId).map(toXY);
  const surface = css("--surface-1");
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      datasets: [
        {
          label: "This prompt", data: current, showLine: false,
          pointRadius: 7, pointHoverRadius: 9, pointBackgroundColor: css("--series-2"),
          pointBorderColor: surface, pointBorderWidth: 2, order: 0,
        },
        {
          label: othersLabel, data: sorted.map(toXY),
          borderColor: css("--series-1"), backgroundColor: css("--series-1"), borderWidth: 2,
          pointRadius: 4, pointHoverRadius: 6, pointBorderColor: surface, pointBorderWidth: 2,
          tension: 0, order: 1,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => !(item.datasetIndex === 1 && item.raw.p.id === currentId),
          callbacks: {
            title: (items) => fmtDate(items[0].raw.p.started_at),
            label: (item) => `${format(item.raw.y)} — ${item.raw.p.text.slice(0, 70)}${item.raw.p.text.length > 70 ? "…" : ""}`,
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          grid: { color: css("--grid") }, border: { color: css("--border") },
          ticks: {
            color: css("--text-muted"), maxTicksLimit: 6,
            callback(v) {
              const d = new Date(v);
              return this.max - this.min < 2 * 86400e3
                ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
                : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
            },
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: css("--grid") }, border: { display: false },
          ticks: { color: css("--text-muted"), callback: (v) => format(v), maxTicksLimit: 5 },
        },
      },
      onClick: (_e, els) => {
        if (els.length) location.hash = `#/prompt/${encodeURIComponent(els[0].element.$context.raw.p.id)}`;
      },
    },
  });
  charts.push(chart);
}

function chartsBlock(points, currentId, othersLabel = "Prompts", idPrefix = "chart") {
  const legend = currentId
    ? `<div class="legend"><span><i style="background:var(--series-2)"></i>This prompt</span><span><i style="background:var(--series-1)"></i>${esc(othersLabel)}</span></div>`
    : "";
  const specs = [
    ["cost", "Cost (USD, incl. subagents)", (p) => p.total_cost_usd, fmtCost],
    ["dur", "Duration (active time, else wall time)", duration, fmtMs],
    ["tok", "Tokens (incl. cache, incl. subagents)", (p) => p.total_tokens, fmtTokens],
  ];
  const html = `${legend}<div class="charts">${specs.map(([id, title]) =>
    `<div class="card"><div class="chart-title">${title}</div><div class="chart-box"><canvas id="${idPrefix}-${id}"></canvas></div></div>`).join("")}</div>`;
  return {
    html,
    mount: () => specs.forEach(([id, , metric, fmt]) => evolutionChart(document.getElementById(`${idPrefix}-${id}`), points, metric, fmt, currentId, othersLabel)),
  };
}

function promptTable(rows, { currentId, showScore } = {}) {
  return `<div class="table-wrap"><table>
    <thead><tr><th>When</th><th class="hide-sm">Project</th><th>Prompt</th>${showScore ? '<th class="num">Similarity</th>' : ""}
      <th class="num">Cost</th><th class="num">Tokens</th><th class="num">Duration</th><th class="num hide-sm">Subagents</th></tr></thead>
    <tbody>${rows.map((p) => `
      <tr class="clickable ${p.id === currentId ? "current" : ""}" data-id="${esc(p.id)}">
        <td class="num">${fmtDate(p.started_at)}</td>
        <td class="hide-sm">${esc(p.project)}</td>
        <td class="prompt-cell"><div class="text">${esc(p.text)}</div>${p.tags ? tagChips(p.tags) : ""}</td>
        ${showScore ? `<td class="num">${p.score != null ? p.score.toFixed(2) : "—"}</td>` : ""}
        <td class="num">${fmtCost(p.total_cost_usd)}</td>
        <td class="num">${fmtTokens(p.total_tokens)}</td>
        <td class="num">${fmtMs(duration(p))}</td>
        <td class="num hide-sm">${p.agent_count || ""}</td>
      </tr>`).join("")}</tbody></table></div>`;
}

function bindRowClicks() {
  app.querySelectorAll("tr.clickable").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    location.hash = `#/prompt/${encodeURIComponent(tr.dataset.id)}`;
  }));
}

// ---- views -----------------------------------------------------------------

const listState = { q: "", tag: "", project: "", offset: 0, limit: 50 };

async function viewPrompts() {
  const [projects, tags] = await Promise.all([api("/api/projects"), api("/api/tags")]);
  const params = new URLSearchParams({ ...listState });
  const data = await api(`/api/prompts?${params}`);
  app.innerHTML = `
    <h1>Prompts <span class="muted">(${data.total})</span></h1>
    <div class="filters">
      <input type="search" id="q" placeholder="Search prompt text…" value="${esc(listState.q)}">
      <select id="project"><option value="">All projects</option>${projects.map((p) => `<option ${p === listState.project ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>
      <select id="tag"><option value="">All tags</option>${TAG_CATEGORIES.map(([cat, label]) =>
        `<optgroup label="${label}">${tags.filter((t) => tagCategory(t) === cat).map((t) => `<option value="${esc(t.tag)}" ${t.tag === listState.tag ? "selected" : ""}>${esc(t.tag)} (${t.prompts})</option>`).join("")}</optgroup>`).join("")}</select>
    </div>
    ${data.items.length ? promptTable(data.items) : `<div class="card muted">No prompts yet. Check the <a href="#/setup">Setup</a> page, then press Refresh.</div>`}
    <div class="pager">
      <button class="btn" id="prev" ${listState.offset === 0 ? "disabled" : ""}>← Newer</button>
      <span class="muted">${data.total ? listState.offset + 1 : 0}–${Math.min(listState.offset + listState.limit, data.total)} of ${data.total}</span>
      <button class="btn" id="next" ${listState.offset + listState.limit >= data.total ? "disabled" : ""}>Older →</button>
    </div>`;
  bindRowClicks();
  let timer;
  document.getElementById("q").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { listState.q = e.target.value; listState.offset = 0; viewPrompts().then(() => {
      const q = document.getElementById("q"); q.focus(); q.setSelectionRange(q.value.length, q.value.length);
    }); }, 250);
  });
  for (const key of ["project", "tag"]) {
    document.getElementById(key).addEventListener("change", (e) => { listState[key] = e.target.value; listState.offset = 0; viewPrompts(); });
  }
  document.getElementById("prev").onclick = () => { listState.offset = Math.max(0, listState.offset - listState.limit); viewPrompts(); };
  document.getElementById("next").onclick = () => { listState.offset += listState.limit; viewPrompts(); };
}

function usageRow(name, r, extra = "") {
  const models = Object.keys(r.models || {}).map((m) => m.replace("claude-", "")).join(", ");
  const tools = Object.entries(r.tools || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k}×${v}`).join(" ");
  return `<tr>
    <td>${name}${extra}</td><td>${esc(models)}</td>
    <td class="num">${fmtTokens(r.input_tokens)}</td><td class="num">${fmtTokens(r.output_tokens)}</td>
    <td class="num">${fmtTokens(r.cache_read_tokens)}</td><td class="num">${fmtTokens(r.cache_write_tokens)}</td>
    <td class="num">${fmtCost(r.cost_usd)}</td><td class="num">${fmtMs(r.wall_ms)}</td>
    <td class="num">${r.api_calls}</td><td class="muted" style="font-size:12px">${esc(tools)}</td></tr>`;
}

function compareLabel({ scope, tag }) {
  if (scope === "tag") return `all prompts tagged ${tag}`;
  if (scope === "similar_tag") return `similar prompts tagged ${tag}`;
  return "similar prompts";
}

function compareSelect(p) {
  const value = (scope, tag) => esc(JSON.stringify({ scope, tag }));
  const cur = p.compare;
  const opt = (scope, tag, label) =>
    `<option value="${value(scope, tag)}" ${cur.scope === scope && cur.tag === tag ? "selected" : ""}>${esc(label)}</option>`;
  const human = humanTags(p.tags);
  return `<select id="compare">
    ${opt("similar", "", "Similar prompts")}
    ${human.map((t) => `<optgroup label="Human tag ${esc(t.tag)}">
      ${opt("tag", t.tag, `All prompts tagged ${t.tag}`)}
      ${opt("similar_tag", t.tag, `Similar prompts tagged ${t.tag}`)}
    </optgroup>`).join("")}
  </select>
  ${human.length ? "" : `<span class="muted" style="font-size:12px">Add a human tag to compare with every prompt sharing it.</span>`}`;
}

async function viewPrompt(id, compare = {}) {
  let p = await api(`/api/prompts/${encodeURIComponent(id)}`);
  // compare by tag only through one of this prompt's own human tags (a stale link falls back to similar)
  if (compare.scope && compare.scope !== "similar" && humanTags(p.tags).some((t) => t.tag === compare.tag)) {
    const query = new URLSearchParams({ compare: compare.scope, tag: compare.tag });
    p = await api(`/api/prompts/${encodeURIComponent(id)}?${query}`);
  }
  const sim = p.similar;
  const what = compareLabel(p.compare);
  const promptHash = (c) => `#/prompt/${encodeURIComponent(id)}` +
    (c.scope && c.scope !== "similar" ? `?${new URLSearchParams({ compare: c.scope, tag: c.tag })}` : "");
  const ref = {
    cost: median(sim.map((s) => s.total_cost_usd)),
    tokens: median(sim.map((s) => s.total_tokens)),
    dur: median(sim.map(duration)),
  };
  const tiles = [
    ["Total cost", fmtCost(p.total_cost_usd), deltaText(p.total_cost_usd, ref.cost), costNote(p)],
    ["Total tokens", fmtTokens(p.total_tokens), deltaText(p.total_tokens, ref.tokens)],
    ["Active time", fmtMs(p.active_ms), deltaText(duration(p), ref.dur)],
    ["Wall time", fmtMs(p.wall_ms), "first prompt → last event", p.api_ms != null ? `API time ${fmtMs(p.api_ms)} (Claude Code)` : ""],
    ["API calls", p.api_calls, `${p.tool_calls} tool calls`],
    ["Subagents", p.agent_count, p.agent_count ? `${fmtCost(p.agent_cost_usd)} · ${fmtTokens(p.agent_tokens)} tok` : ""],
  ];
  const others = what[0].toUpperCase() + what.slice(1);
  const block = chartsBlock([p, ...sim], p.id, others);
  const empty = p.compare.scope === "similar"
    ? `No similar prompt yet. Similarity uses word overlap (threshold on the Setup page) or the same slash command. To group prompts explicitly, give them the same human tag and compare with all prompts tagged with it.`
    : `No other prompt in ${esc(what)}.`;
  const unpriced = (p.unpriced_models || []).length
    ? `<div class="card status-warn" style="margin-top:8px">No price configured for ${esc(p.unpriced_models.join(", "))}: their cost is counted as 0. Add them on the <a href="#/setup">Setup</a> page.</div>` : "";
  app.innerHTML = `
    <div class="muted"><a href="#/">← Prompts</a> · ${esc(p.project)} · ${fmtDate(p.started_at)} · <a href="#/session/${encodeURIComponent(p.session_id)}">session <code>${esc(p.session_id.slice(0, 8))}</code></a>
      ${p.session.title ? `“${esc(p.session.title)}”` : ""}${p.git_branch ? ` · branch ${esc(p.git_branch)}` : ""}</div>
    <div class="card" style="margin-top:8px"><pre class="prompt-text">${esc(p.text)}</pre></div>
    <div class="tag-groups">
      <div><span class="tag-kind">Human tags</span><span id="tags">${tagChips(humanTags(p.tags), { removable: true })}</span>
        <form id="tag-form" class="inline-form">
          <input id="new-tag" placeholder="add a tag, e.g. task:review-branch" size="28" required>
          ${keywordFields("new-tag")}
          <button class="btn">Add</button>
        </form></div>
      <div><span class="tag-kind">AI tags</span>${tagChips(aiTags(p.tags)) || '<span class="muted">none</span>'}</div>
    </div>
    ${unpriced}
    ${tilesHtml(tiles)}

    <h2>Cost per agent</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Agent</th><th>Models</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th><th class="num">Cache write</th>
        <th class="num">Cost</th><th class="num">Wall</th><th class="num">Calls</th><th>Tools</th></tr></thead>
      <tbody>
        ${usageRow("<strong>main</strong>", p)}
        ${p.agents.map((a) => usageRow(agentName(a.agent_type, a.persona), a,
          `<div class="muted" style="font-size:12px">${[a.skill ? `via ${esc(a.skill)}` : "", esc(a.description)].filter(Boolean).join(" · ")}</div>`)).join("")}
      </tbody></table></div>
    <div class="muted" style="font-size:12px; margin-top:4px">From the transcripts, priced with the price table.</div>
    ${telemetryTable(p.telemetry)}

    <h2>Evolution compared with ${compareSelect(p)} <span class="muted">(${sim.length} prompts)</span></h2>
    ${sim.length ? block.html : `<div class="card muted">${empty}</div>`}
    ${sim.length ? `<h2>${esc(others)}</h2>${promptTable([{ ...p, score: null }, ...sim], { currentId: p.id, showScore: true })}` : ""}
  `;
  document.getElementById("compare").onchange = (e) => { location.hash = promptHash(JSON.parse(e.target.value)); };
  if (sim.length) block.mount();
  bindRowClicks();
  document.querySelectorAll("[data-remove-tag]").forEach((b) => b.onclick = async () => {
    const tag = b.dataset.removeTag;
    await api(`/api/prompts/${encodeURIComponent(p.id)}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" });
    // the comparison based on the removed tag no longer applies
    const c = p.compare.tag === tag ? {} : p.compare;
    if (location.hash === promptHash(c)) route(); else location.hash = promptHash(c);
  });
  bindKeywordPreview("new-tag");
  document.getElementById("tag-form").onsubmit = async (e) => {
    e.preventDefault();
    const input = document.getElementById("new-tag");
    if (!input.value.trim()) return;
    await api(`/api/prompts/${encodeURIComponent(p.id)}/tags`, {
      method: "POST", body: { tag: input.value.trim(), ...keywordValues("new-tag") },
    });
    route(); // the new tag adds comparison choices
  };
}

// ---- Tags page: a gallery of tag cards ---------------------------------------------

const SUBTYPE_LABELS = { cmd: "Slash commands", agent: "Subagent types", persona: "Personas" };

/** Second-level grouping inside a category. */
function tagSubtype(t) {
  const parts = t.tag.split(":");
  switch (tagCategory(t)) {
    case "human": return parts.length > 1 ? `${parts[0]}:` : "No prefix";
    case "model": return (parts[1] || "").split("-")[0] || "other";  // opus, sonnet, haiku…
    case "skill": return parts.length > 2 ? `Plugin ${parts[1]}` : "Standalone skills";
    case "project": return "";
    default: return SUBTYPE_LABELS[parts[0]] || parts[0];
  }
}

const TAG_SORTS = {
  recent: ["Recently used", (a, b) => (b.last_used || "").localeCompare(a.last_used || "")],
  prompts: ["Most prompts", (a, b) => b.prompts - a.prompts],
  total: ["Highest total cost", (a, b) => (b.cost_usd || 0) - (a.cost_usd || 0)],
  avg: ["Highest cost per prompt", (a, b) => (b.avg_cost_usd || 0) - (a.avg_cost_usd || 0)],
  name: ["Name", (a, b) => a.tag.localeCompare(b.tag)],
};

/** Per-viewer preferences, kept in browser storage when it is available. */
function loadTagPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem("tags-prefs") || "{}");
    return { view: p.view === "timeline" ? "timeline" : "grouped", sort: TAG_SORTS[p.sort] ? p.sort : "recent", filter: new Set(p.filter || []) };
  } catch { return { view: "grouped", sort: "recent", filter: new Set() }; }
}

function saveTagPrefs() {
  try {
    localStorage.setItem("tags-prefs", JSON.stringify({ view: tagsState.view, sort: tagsState.sort, filter: [...tagsState.filter] }));
  } catch { /* storage unavailable: preferences last for this visit */ }
}

const tagsState = { ...loadTagPrefs(), q: "", open: null, all: [] };

function fmtAgo(iso) {
  if (!iso) return "never";
  const min = (Date.now() - new Date(iso)) / 60000;
  if (min < 1) return "just now";
  if (min < 60) return `${Math.round(min)} min ago`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h ago`;
  if (min < 60 * 24 * 60) return `${Math.round(min / 1440)} days ago`;
  return fmtDate(iso);
}

/** Cost of the latest runs against the earlier ones: arrow + words, never color alone. */
function trendBadge(spark) {
  if (spark.length < 4) return "";
  const half = Math.min(3, Math.floor(spark.length / 2));
  const recent = median(spark.slice(-half));
  const before = median(spark.slice(-2 * half, -half));
  if (!before) return "";
  const pct = Math.round(((recent - before) / before) * 100);
  if (Math.abs(pct) < 5) return `<span class="trend flat" title="Last ${half} runs vs the ${half} before">→ steady</span>`;
  return pct < 0
    ? `<span class="trend down" title="Median cost of the last ${half} runs vs the ${half} before">▼ ${-pct}% cheaper</span>`
    : `<span class="trend up" title="Median cost of the last ${half} runs vs the ${half} before">▲ ${pct}% pricier</span>`;
}

function sparkline(values) {
  if (values.length < 2) return `<div class="spark-empty">Only one run so far: rerun it to see a trend</div>`;
  const w = 200, h = 34, pad = 4;
  const max = Math.max(...values), min = Math.min(...values);
  const x = (i) => values.length === 1 ? w / 2 : pad + (i * (w - 2 * pad)) / (values.length - 1);
  const y = (v) => max === min ? h / 2 : h - pad - ((v - min) * (h - 2 * pad)) / (max - min);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = values.length - 1;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-label="Cost of the last ${values.length} prompts, from ${fmtCost(values[0])} to ${fmtCost(values[last])}">
    <polyline points="${pts}" />
    <circle cx="${x(last).toFixed(1)}" cy="${y(values[last]).toFixed(1)}" r="3.5" /></svg>`;
}

function tagCard(t) {
  const cat = tagCategory(t);
  const [prefix, ...rest] = t.tag.split(":");
  const name = rest.length ? `<span class="tag-prefix">${esc(prefix)}:</span>${esc(rest.join(":"))}` : esc(t.tag);
  const open = tagsState.open === t.tag;
  return `<article class="tag-card cat-${cat} ${open ? "open" : ""}" data-tag="${esc(t.tag)}" tabindex="0" role="button"
      aria-expanded="${open}" aria-label="${esc(t.tag)}: ${t.prompts} prompts, ${fmtCost(t.avg_cost_usd)} per prompt">
    <header><span class="tag-dot"></span><span class="tag-name">${name}</span>
      ${cat === "human" ? '<span class="by-you">yours</span>' : ""}</header>
    <div class="tag-metric"><span class="big">${fmtCost(t.avg_cost_usd)}</span><span class="unit">per prompt</span>${trendBadge(t.spark)}</div>
    ${sparkline(t.spark)}
    <footer><span><strong>${t.prompts}</strong> prompt${t.prompts === 1 ? "" : "s"}</span><span>${fmtCost(t.cost_usd)} total</span>
      <span title="${esc(fmtDate(t.last_used))}">${fmtAgo(t.last_used)}</span></footer>
  </article>`;
}

function cardGrid(tags) {
  return `<div class="tag-grid">${tags.map(tagCard).join("")}</div>`;
}

function groupedHtml(tags) {
  const sort = TAG_SORTS[tagsState.sort][1];
  return TAG_CATEGORIES.map(([cat, label, help]) => {
    const inCat = tags.filter((t) => tagCategory(t) === cat);
    if (!inCat.length) return "";
    const cost = inCat.reduce((sum, t) => sum + (t.cost_usd || 0), 0);
    const subtypes = [...new Set(inCat.map(tagSubtype))].sort((a, b) => a.localeCompare(b));
    return `<section class="tag-section cat-${cat}" aria-labelledby="sec-${cat}">
      <h2 id="sec-${cat}"><span class="tag-dot"></span>${label}<span class="sec-meta">${inCat.length} tag${inCat.length === 1 ? "" : "s"} · ${fmtCost(cost)}</span></h2>
      <p class="sec-help">${esc(help[0].toUpperCase() + help.slice(1))}</p>
      ${subtypes.map((sub) => {
        const inSub = inCat.filter((t) => tagSubtype(t) === sub).sort(sort);
        return `${sub && subtypes.length > 1 ? `<h3 class="subtype">${esc(sub)} <span>${inSub.length}</span></h3>` : ""}${cardGrid(inSub)}`;
      }).join("")}
    </section>`;
  }).join("");
}

function timelineHtml(tags) {
  const sorted = [...tags].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const months = new Map();
  for (const t of sorted) {
    const key = t.created_at ? new Date(t.created_at).toLocaleDateString(undefined, { month: "long", year: "numeric" }) : "Unknown";
    if (!months.has(key)) months.set(key, []);
    months.get(key).push(t);
  }
  return `<ol class="timeline">${[...months].map(([month, list]) => `<li>
    <h2 class="month">${esc(month)} <span class="sec-meta">${list.length} new tag${list.length === 1 ? "" : "s"}</span></h2>
    ${cardGrid(list)}</li>`).join("")}</ol>`;
}

function tagsHero(all) {
  if (!all.length) return "";
  const human = all.filter((t) => t.kind === "human");
  const pricey = [...all].filter((t) => t.prompts >= 2).sort(TAG_SORTS.avg[1])[0];
  const busiest = [...all].filter((t) => tagCategory(t) !== "model").sort(TAG_SORTS.prompts[1])[0];
  const fact = (label, value, sub, tag) => `<div class="hero-fact">
    <span class="label">${label}</span><span class="value">${value}</span>
    <span class="sub">${tag ? `<a href="#" data-open-tag="${esc(tag)}">${esc(sub)}</a>` : sub}</span></div>`;
  return `<div class="tags-hero">
    ${fact("Tags", all.length, `${human.length} created by you`)}
    ${pricey ? fact("Priciest per prompt", fmtCost(pricey.avg_cost_usd), pricey.tag, pricey.tag) : ""}
    ${busiest ? fact("Most used", `${busiest.prompts} prompts`, busiest.tag, busiest.tag) : ""}
  </div>`;
}

function visibleTags() {
  const q = tagsState.q.trim().toLowerCase();
  return tagsState.all.filter((t) =>
    (!tagsState.filter.size || tagsState.filter.has(tagCategory(t))) && (!q || t.tag.toLowerCase().includes(q)));
}

function renderTagResults() {
  destroyCharts();
  const tags = visibleTags();
  if (tagsState.open && !tags.some((t) => t.tag === tagsState.open)) tagsState.open = null;
  const box = document.getElementById("tag-results");
  box.innerHTML = tags.length
    ? (tagsState.view === "timeline" ? timelineHtml(tags) : groupedHtml(tags))
    : `<div class="empty-state"><div class="empty-icon">⌕</div><p>No tag matches${tagsState.q ? ` “${esc(tagsState.q)}”` : ""}.</p>
        <button class="btn" type="button" id="tags-reset">Clear search and filters</button></div>`;
  document.getElementById("tags-count").textContent =
    tags.length === tagsState.all.length ? `${tags.length}` : `${tags.length} of ${tagsState.all.length}`;
  document.getElementById("tags-sort").hidden = tagsState.view === "timeline";
  box.querySelectorAll(".tag-card").forEach((card) => {
    card.onclick = () => toggleTag(card.dataset.tag);
    card.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleTag(card.dataset.tag); }
    };
  });
  const reset = document.getElementById("tags-reset");
  if (reset) reset.onclick = () => {
    tagsState.q = ""; tagsState.filter.clear(); saveTagPrefs();
    document.getElementById("tags-search").value = "";
    syncTagToolbar(); renderTagResults();
  };
  if (tagsState.open) openTagPanel(tagsState.open);
}

function syncTagToolbar() {
  app.querySelector(".tags-toolbar .tag-legend").classList.toggle("filtering", tagsState.filter.size > 0);
  app.querySelectorAll("[data-cat]").forEach((b) =>
    b.setAttribute("aria-pressed", String(!tagsState.filter.size || tagsState.filter.has(b.dataset.cat))));
  app.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === tagsState.view)));
}

function toggleTag(tag) {
  tagsState.open = tagsState.open === tag ? null : tag;
  app.querySelectorAll(".tag-card").forEach((c) => {
    const open = c.dataset.tag === tagsState.open;
    c.classList.toggle("open", open);
    c.setAttribute("aria-expanded", String(open));
  });
  destroyCharts();
  document.getElementById("tag-panel")?.remove();
  if (tagsState.open) openTagPanel(tagsState.open);
}

/** Put the panel after the last card of the opened card's row, so the grid does not reflow. */
function placeTagPanel() {
  const panel = document.getElementById("tag-panel");
  const card = panel && app.querySelector(`.tag-card[data-tag="${CSS.escape(panel.dataset.tag)}"]`);
  if (!card) return;
  panel.remove(); // measure the rows as they are without the panel
  const row = [...card.parentElement.children].filter((el) => el.classList.contains("tag-card") && el.offsetTop === card.offsetTop);
  row[row.length - 1].after(panel);
}

async function openTagPanel(tag) {
  const t = tagsState.all.find((x) => x.tag === tag);
  const card = app.querySelector(`.tag-card[data-tag="${CSS.escape(tag)}"]`);
  if (!t || !card) return;
  const cat = tagCategory(t);
  const panel = document.createElement("div");
  panel.id = "tag-panel";
  panel.className = `tag-panel cat-${cat}`;
  panel.dataset.tag = tag;
  panel.setAttribute("role", "region");
  panel.setAttribute("aria-label", `Details of ${tag}`);
  panel.innerHTML = `<div class="panel-head">${tagChips([t])}<span class="muted">${CATEGORY_INFO[cat].label} tag</span><span class="panel-spacer"></span>
    <a class="btn primary" href="#/tag/${encodeURIComponent(tag)}">Open tag page →</a>
    <button class="btn icon" type="button" data-close-panel aria-label="Close">✕</button></div>
    <div class="panel-body muted">Loading charts…</div>`;
  document.body.append(panel); // placeTagPanel moves it into the grid
  placeTagPanel();
  panel.querySelector("[data-close-panel]").onclick = () => toggleTag(tag);
  const rows = await api(`/api/tags/${encodeURIComponent(tag)}/prompts`);
  if (tagsState.open !== tag || !panel.isConnected) return; // closed or replaced meanwhile
  const block = chartsBlock(rows, null, "Prompts", `panel-${Date.now()}`);
  const recent = [...rows].reverse().slice(0, 5);
  const body = panel.querySelector(".panel-body");
  body.classList.remove("muted");
  body.innerHTML = `
    <div class="panel-stats">
      <div><span class="label">Prompts</span><strong>${rows.length}</strong></div>
      <div><span class="label">Median cost</span><strong>${fmtCost(median(rows.map((r) => r.total_cost_usd)))}</strong></div>
      <div><span class="label">Median duration</span><strong>${fmtMs(median(rows.map(duration)))}</strong></div>
      <div><span class="label">Created</span><strong>${fmtDate(t.created_at)}</strong></div>
    </div>
    ${block.html}
    <h3 class="subtype">Latest prompts</h3>
    <ul class="recent-prompts">${recent.map((r) => `<li><a href="#/prompt/${encodeURIComponent(r.id)}">
      <span class="when">${fmtDate(r.started_at)}</span><span class="text">${esc(r.text)}</span>
      <span class="cost">${fmtCost(r.total_cost_usd)}</span></a></li>`).join("")}</ul>`;
  block.mount();
  const top = panel.getBoundingClientRect().top;
  if (top < 80 || top > innerHeight - 120) panel.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

let tagPanelResize;
window.addEventListener("resize", () => { clearTimeout(tagPanelResize); tagPanelResize = setTimeout(placeTagPanel, 120); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && tagsState.open && document.getElementById("tag-panel")) {
    const tag = tagsState.open;
    toggleTag(tag);
    app.querySelector(`.tag-card[data-tag="${CSS.escape(tag)}"]`)?.focus();
  }
});

async function viewTags() {
  tagsState.all = await api("/api/tags");
  app.innerHTML = `
    <div class="tags-page">
      <div class="tags-intro">
        <h1>Tags <span class="muted" id="tags-count"></span></h1>
        <p>Follow cost and time by task, project, model or skill. Tags <strong>you</strong> create group prompts aiming at the same task;
          the others are derived from what the agent did.</p>
      </div>
      ${tagsHero(tagsState.all)}
      <search class="tags-toolbar">
        <label class="search-box"><span aria-hidden="true">⌕</span>
          <input type="search" id="tags-search" placeholder="Search tags…" value="${esc(tagsState.q)}" aria-label="Search tags"></label>
        <div class="tag-legend" role="group" aria-label="Categories">${TAG_CATEGORIES.map(([cat, label, help]) =>
          `<button type="button" class="chip cat-${cat}" data-cat="${cat}" title="${esc(help)}">${label}</button>`).join("")}</div>
        <span class="toolbar-spacer"></span>
        <label id="tags-sort" class="sort-box">Sort
          <select id="tags-sort-select">${Object.entries(TAG_SORTS).map(([key, [label]]) =>
            `<option value="${key}" ${key === tagsState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <div class="segmented" role="group" aria-label="Layout">
          <button type="button" data-view="grouped">Grouped</button>
          <button type="button" data-view="timeline">Timeline</button>
        </div>
      </search>
      <div id="tag-results"></div>
    </div>`;
  syncTagToolbar();
  renderTagResults();
  let timer;
  document.getElementById("tags-search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { tagsState.q = e.target.value; renderTagResults(); }, 150);
  });
  app.querySelectorAll("[data-cat]").forEach((b) => b.onclick = () => {
    const { filter } = tagsState;
    // from "all shown", a click keeps only that category; clicking the last one shows all again
    filter.has(b.dataset.cat) ? filter.delete(b.dataset.cat) : filter.add(b.dataset.cat);
    if (filter.size === TAG_CATEGORIES.length) filter.clear();
    saveTagPrefs(); syncTagToolbar(); renderTagResults();
  });
  app.querySelectorAll("[data-view]").forEach((b) => b.onclick = () => {
    tagsState.view = b.dataset.view; saveTagPrefs(); syncTagToolbar(); renderTagResults();
  });
  document.getElementById("tags-sort-select").onchange = (e) => { tagsState.sort = e.target.value; saveTagPrefs(); renderTagResults(); };
  app.querySelectorAll("[data-open-tag]").forEach((a) => a.onclick = (e) => {
    e.preventDefault();
    const tag = a.dataset.openTag;
    if (!visibleTags().some((t) => t.tag === tag)) {
      tagsState.q = ""; tagsState.filter.clear(); document.getElementById("tags-search").value = "";
      syncTagToolbar(); renderTagResults();
    }
    if (tagsState.open !== tag) toggleTag(tag);
  });
}

async function viewTag(tag) {
  const enc = encodeURIComponent(tag);
  const [rows, tags, rules] = await Promise.all([api(`/api/tags/${enc}/prompts`), api("/api/tags"), api(`/api/tags/${enc}/rules`)]);
  const info = tags.find((t) => t.tag === tag) || { tag, kind: "ai" };
  const human = tagCategory(info) === "human";
  const total = rows.reduce((s, r) => s + (r.total_cost_usd || 0), 0);
  const block = chartsBlock(rows, null);
  app.innerHTML = `
    <div class="muted"><a href="#/tags">← Tags</a></div>
    <h1><span class="chip cat-${tagCategory(info)}" style="font-size:16px"><a>${esc(tag)}</a></span>
      <span class="muted" style="font-size:13px; font-weight:400">${CATEGORY_INFO[tagCategory(info)].label} tag</span></h1>
    <div class="tiles">
      <div class="tile"><div class="label">Prompts</div><div class="value">${rows.length}</div></div>
      <div class="tile"><div class="label">Total cost</div><div class="value">${fmtCost(total)}</div></div>
      <div class="tile"><div class="label">Median cost</div><div class="value">${fmtCost(median(rows.map((r) => r.total_cost_usd)))}</div></div>
      <div class="tile"><div class="label">Median duration</div><div class="value">${fmtMs(median(rows.map(duration)))}</div></div>
    </div>
    ${human ? `<h2>Assigned automatically to prompts containing</h2>
      <div class="card">
        ${rules.length ? rules.map((r) => `<div class="rule-row"><span>${ruleText(r)}</span>
          <button class="btn" data-delete-rule="${r.index}" title="Stop assigning the tag with these keywords">Remove</button></div>`).join("")
          : '<div class="muted">No keywords yet: only prompts tagged by hand carry this tag.</div>'}
        <form id="rule-form" class="inline-form" style="margin-top:8px">${keywordFields("rule")}<button class="btn">Add keywords</button></form>
      </div>` : ""}
    <h2>Evolution over time</h2>${block.html}
    <h2>Prompts</h2>${promptTable([...rows].reverse())}`;
  block.mount();
  bindRowClicks();
  if (!human) return;
  bindKeywordPreview("rule");
  document.getElementById("rule-form").onsubmit = async (e) => {
    e.preventDefault();
    const values = keywordValues("rule");
    if (!values.keywords.length) return;
    await api(`/api/tags/${enc}/rules`, { method: "POST", body: values });
    viewTag(tag);
  };
  app.querySelectorAll("[data-delete-rule]").forEach((b) => b.onclick = async () => {
    await api(`/api/tags/${enc}/rules/${b.dataset.deleteRule}`, { method: "DELETE" });
    viewTag(tag);
  });
}

// ---- sessions ----------------------------------------------------------------

async function viewSessions() {
  const data = await api("/api/sessions?limit=200");
  app.innerHTML = `
    <h1>Sessions <span class="muted">(${data.total})</span></h1>
    <div class="table-wrap"><table>
      <thead><tr><th>Started</th><th class="hide-sm">Project</th><th>Title</th><th class="num">Prompts</th><th class="num">Cost</th>
        <th class="num hide-sm">Claude Code total</th><th class="num">Tokens</th><th class="num">Active</th><th class="num hide-sm">Subagents</th></tr></thead>
      <tbody>${data.items.map((s) => `<tr class="clickable" data-session="${esc(s.id)}">
        <td class="num">${fmtDate(s.started_at)}</td><td class="hide-sm">${esc(s.project)}</td>
        <td>${esc(s.title || s.id.slice(0, 8))}</td><td class="num">${s.prompts}</td><td class="num">${fmtCost(s.cost_usd)}</td>
        <td class="num hide-sm">${fmtCost(s.reported_cost_usd)}</td><td class="num">${fmtTokens(s.total_tokens)}</td>
        <td class="num">${fmtMs(s.active_ms)}</td><td class="num hide-sm">${s.agent_count || ""}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="muted" style="font-size:12px">"Claude Code total" is the session cost Claude Code stores in the transcript; it includes internal calls (such as WebSearch) that transcripts do not detail.</p>`;
  app.querySelectorAll("tr[data-session]").forEach((tr) => tr.onclick = () => location.hash = `#/session/${encodeURIComponent(tr.dataset.session)}`);
}

async function viewSession(id) {
  const s = await api(`/api/sessions/${encodeURIComponent(id)}`);
  const block = chartsBlock(s.prompts, null);
  const tiles = [
    ["Cost", fmtCost(s.cost_usd), `${s.prompts.length} prompts`, s.official_cost_usd != null ? `${fmtCost(s.official_cost_usd)} from Claude Code telemetry` : "price-table estimate"],
    ["Claude Code total", fmtCost(s.reported_cost_usd), "stored in the transcript", "includes internal calls"],
    ["Tokens", fmtTokens(s.total_tokens), ""],
    ["Active time", fmtMs(s.active_ms), `wall ${fmtMs(s.started_at && s.ended_at ? new Date(s.ended_at) - new Date(s.started_at) : null)}`,
      s.api_ms != null ? `API time ${fmtMs(s.api_ms)}` : ""],
    ["API calls", s.api_calls, `${s.tool_calls} tool calls`],
    ["Subagent runs", s.agent_count || 0, ""],
  ];
  app.innerHTML = `
    <div class="muted"><a href="#/sessions">← Sessions</a> · ${esc(s.project)} · ${fmtDate(s.started_at)} · <code>${esc(s.id)}</code></div>
    <h1>${esc(s.title || "Session")}</h1>
    ${tilesHtml(tiles)}
    <h2>Cost per agent</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Agent</th><th class="num">Runs</th><th>Models</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th>
        <th class="num">Cache write</th><th class="num">Cost</th><th class="num">Calls</th><th class="num">Tool calls</th></tr></thead>
      <tbody>${s.agents.map((a) => `<tr><td>${agentName(a.agent, a.persona)}${a.skills
          ? `<div class="muted" style="font-size:12px">via ${esc(a.skills.split(",").join(", "))}</div>` : ""}</td>
        <td class="num">${a.agent === "main" ? "" : a.runs}</td>
        <td>${esc(Object.keys(a.models).map((m) => m.replace("claude-", "")).join(", "))}</td>
        <td class="num">${fmtTokens(a.input_tokens)}</td><td class="num">${fmtTokens(a.output_tokens)}</td><td class="num">${fmtTokens(a.cache_read_tokens)}</td>
        <td class="num">${fmtTokens(a.cache_write_tokens)}</td><td class="num">${fmtCost(a.cost_usd)}</td><td class="num">${a.api_calls}</td>
        <td class="num">${a.tool_calls}</td></tr>`).join("")}</tbody></table></div>
    <div class="muted" style="font-size:12px; margin-top:4px">From the transcripts, priced with the price table.</div>
    ${telemetryTable(s.telemetry)}
    <h2>Prompts over the session</h2>${block.html}
    <h2>Prompts</h2>${promptTable([...s.prompts].reverse())}`;
  block.mount();
  bindRowClicks();
}

async function viewSetup() {
  const [st, settings] = await Promise.all([api("/api/setup/status"), api("/api/settings")]);
  const ok = (cond) => (cond ? "status-ok" : "status-warn");
  const retentionOk = st.cleanup_period_days != null && st.cleanup_period_days >= 365;
  app.innerHTML = `
    <h1>Setup</h1>
    <div class="card">
      <div class="setup-row"><div class="${ok(st.projects_dir_exists)}"><strong>Claude Code transcripts</strong><br>
        <span class="muted">Claude Code writes every session (and each subagent) to JSONL files. ${st.transcript_count} session files found in <code>${esc(st.projects_dir)}</code>.</span></div></div>
      <div class="setup-row"><div class="${ok(st.prompts > 0)}"><strong>Imported</strong><br>
        <span class="muted">${st.sessions} sessions · ${st.prompts} prompts · ${st.agents} subagent runs · last prompt ${fmtDate(st.last_prompt_at)}. Database <code>${esc(st.db_path)}</code></span></div>
        <div><button class="btn primary" id="ingest">Import now</button> <button class="btn" id="reingest" title="Re-parse every session (after a price or rule change)">Re-import all</button></div></div>
      <div class="setup-row"><div class="${ok(st.hook_installed)}"><strong>Live updates (Claude Code Stop hook)</strong><br>
        <span class="muted">Without the hook the dashboard re-scans every ${settings.auto_ingest_interval}s while it runs. The hook imports right after each Claude answer.
        It adds this command to <code>${esc(st.claude_settings_path)}</code> (a <code>.bak</code> copy is kept):</span><br><code>${esc(st.hook_command)}</code></div>
        <div><button class="btn" id="hook">${st.hook_installed ? "Remove hook" : "Install hook"}</button></div></div>
      <div class="setup-row"><div class="${ok(retentionOk)}"><strong>Transcript retention</strong><br>
        <span class="muted">Claude Code deletes transcripts older than <code>cleanupPeriodDays</code> (default 30). Imported prompts stay in the dashboard database,
        but a longer retention lets you re-import with new prices or rules. Current: ${st.cleanup_period_days ?? "not set (30 days)"}.</span></div>
        <div><button class="btn" id="retention">Set to 3650 days</button></div></div>
      <div class="setup-row"><div class="${ok(st.telemetry.configured && st.telemetry.matched_events > 0)}"><strong>Cost reported by Claude Code (OpenTelemetry)</strong><br>
        <span class="muted">Claude Code can send its own cost for every API call, including internal calls the transcripts do not detail.
        The dashboard then shows that cost for every prompt fully covered by telemetry, and the price-table estimate otherwise.
        Enabling it adds these entries to <code>env</code> in <code>${esc(st.claude_settings_path)}</code>; they apply to Claude Code sessions started afterwards,
        and calls made while the dashboard is not running are not recorded.</span><br>
        ${Object.entries(st.telemetry.env).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}<br>
        <span class="muted">${st.telemetry.events} API calls received · ${st.telemetry.matched_events} linked to an imported prompt · last ${fmtDate(st.telemetry.last_event_at)}</span>
        ${Object.keys(st.telemetry.conflicts).length ? `<br><span class="status-warn">Already set to other values: ${Object.entries(st.telemetry.conflicts).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</span>` : ""}</div>
        <div><button class="btn" id="telemetry">${st.telemetry.configured ? "Disable telemetry" : "Enable telemetry"}</button></div></div>
      ${st.unpriced_models.length ? `<div class="setup-row"><div class="status-warn"><strong>Models without a price</strong><br><span class="muted">${esc(st.unpriced_models.join(", "))} — add them to the price table below.</span></div></div>` : ""}
    </div>

    <h2>Settings</h2>
    <form id="settings" class="card">
      <div class="setup-row"><div><strong>Claude projects directory</strong></div><input name="claude_projects_dir" size="50" value="${esc(settings.claude_projects_dir)}"></div>
      <div class="setup-row"><div><strong>Cost shown</strong><br><span class="muted">Claude Code's own cost when telemetry covers the whole prompt, or always the price table.</span></div>
        <select name="cost_source">
          <option value="claude-code" ${settings.cost_source === "claude-code" ? "selected" : ""}>Claude Code when available</option>
          <option value="prices" ${settings.cost_source === "prices" ? "selected" : ""}>Price table only</option></select></div>
      <div class="setup-row"><div><strong>Similarity threshold</strong><br><span class="muted">0–1 word overlap above which two prompts are shown together.</span></div>
        <input name="similarity_threshold" type="number" min="0" max="1" step="0.05" value="${settings.similarity_threshold}"></div>
      <div class="setup-row"><div><strong>Auto-import interval (seconds)</strong><br><span class="muted">0 disables periodic scanning.</span></div>
        <input name="auto_ingest_interval" type="number" min="0" value="${settings.auto_ingest_interval}"></div>
      <div class="setup-row"><div style="flex-basis:100%"><strong>Auto-tag rules</strong><br><span class="muted">Usually managed from a tag's page (keywords). Raw form: <code>{"tag": "task:x", "keywords": ["review", "branch"], "match": "all"}</code> or a regex <code>{"tag": "task:x", "pattern": "review .*paper"}</code></span>
        <textarea name="tag_rules" rows="4">${esc(JSON.stringify(settings.tag_rules, null, 2))}</textarea></div></div>
      <div class="setup-row"><div style="flex-basis:100%"><strong>Prices (USD per million tokens)</strong><br><span class="muted">Longest model-id prefix wins. Cache writes cost 1.25× (5 min) or 2× (1 h) input; fast mode 2×; web search $10/1k.</span>
        <textarea name="prices" rows="12">${esc(JSON.stringify(settings.prices, null, 2))}</textarea></div></div>
      <div style="padding-top:10px"><button class="btn primary">Save &amp; re-import</button> <span id="saved" class="muted"></span></div>
    </form>`;

  const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); } catch (e) { alert(e.message); } finally { btn.disabled = false; } };
  document.getElementById("ingest").onclick = (e) => busy(e.target, async () => { await api("/api/ingest", { method: "POST" }); viewSetup(); });
  document.getElementById("reingest").onclick = (e) => busy(e.target, async () => { await api("/api/ingest?force=true", { method: "POST" }); viewSetup(); });
  document.getElementById("hook").onclick = (e) => busy(e.target, async () => { await api(`/api/setup/hook?install=${!st.hook_installed}`, { method: "POST" }); viewSetup(); });
  document.getElementById("telemetry").onclick = (e) => busy(e.target, async () => {
    const conflicts = Object.entries(st.telemetry.conflicts);
    if (!st.telemetry.configured && conflicts.length &&
        !confirm(`This replaces your current values:\n${conflicts.map(([k, v]) => `${k}=${v}`).join("\n")}`)) return;
    await api("/api/setup/telemetry", { method: "POST", body: { enable: !st.telemetry.configured } });
    viewSetup();
  });
  document.getElementById("retention").onclick = (e) => busy(e.target, async () => { await api("/api/setup/retention", { method: "POST", body: { days: 3650 } }); viewSetup(); });
  document.getElementById("settings").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let values;
    try {
      values = {
        claude_projects_dir: f.get("claude_projects_dir"),
        cost_source: f.get("cost_source"),
        similarity_threshold: parseFloat(f.get("similarity_threshold")),
        auto_ingest_interval: parseInt(f.get("auto_ingest_interval"), 10),
        tag_rules: JSON.parse(f.get("tag_rules")),
        prices: JSON.parse(f.get("prices")),
      };
    } catch (err) { alert("Invalid JSON: " + err.message); return; }
    await api("/api/settings", { method: "PUT", body: values });
    showCostSource(values.cost_source);
    document.getElementById("saved").textContent = "Saved, re-importing…";
    await api("/api/ingest?force=true", { method: "POST" });
    viewSetup();
  };
}

// ---- router ----------------------------------------------------------------

async function route() {
  destroyCharts();
  const [hash, queryString] = (location.hash.slice(1) || "/").split("?");
  const query = new URLSearchParams(queryString || "");
  const [, view, arg] = hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active",
    a.dataset.nav === ({ tag: "tags", session: "sessions", prompt: "prompts" }[view] || view || "prompts")));
  try {
    if (view === "prompt") await viewPrompt(decodeURIComponent(arg), { scope: query.get("compare"), tag: query.get("tag") });
    else if (view === "tags") await viewTags();
    else if (view === "sessions") await viewSessions();
    else if (view === "session") await viewSession(decodeURIComponent(arg));
    else if (view === "tag") await viewTag(decodeURIComponent(hash.slice("/tag/".length)));
    else if (view === "setup") await viewSetup();
    else await viewPrompts();
  } catch (e) {
    app.innerHTML = `<div class="card status-warn">${esc(e.message)}</div>`;
  }
  window.scrollTo(0, 0);
}

// ---- cost source switch (top bar) ---------------------------------------------

function showCostSource(source) {
  document.querySelectorAll("[data-cost-source]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.costSource === source)));
}

document.querySelectorAll("[data-cost-source]").forEach((b) => b.onclick = async () => {
  if (b.getAttribute("aria-pressed") === "true") return;
  showCostSource(b.dataset.costSource);
  await api("/api/settings", { method: "PUT", body: { cost_source: b.dataset.costSource } });
  await route(); // every cost on the page changes
});

api("/api/settings").then((s) => showCostSource(s.cost_source));

document.getElementById("refresh").onclick = async (e) => {
  e.target.disabled = true;
  try { await api("/api/ingest", { method: "POST" }); await route(); } finally { e.target.disabled = false; }
};
window.addEventListener("hashchange", route);
route();
