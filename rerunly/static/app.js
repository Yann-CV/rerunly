"use strict";

const app = document.getElementById("app");
let charts = [];

// ---- helpers ---------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// bumped on every navigation: a page still loading when the user moves on must not draw over the next one
let navigation = 0;
class StaleNavigation extends Error {}
// requests started by a page's own handlers (typing, timers) can also outlive it: nothing to report
addEventListener("unhandledrejection", (e) => { if (e.reason instanceof StaleNavigation) e.preventDefault(); });

async function api(path, opts = {}) {
  const started = navigation;
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const data = await res.json();
  if (started !== navigation) throw new StaleNavigation();
  return data;
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
/** A run against the tag's reference run: "▼ 23% vs ref", green when lower (cheaper, faster, fewer tokens). */
function refDelta(value, ref, { long = false, against } = {}) {
  if (value == null || !ref) return "";
  const pct = Math.round(((value - ref) / ref) * 100);
  const vs = against ?? (long ? "reference" : "ref");
  const text = `${pct === 0 ? "=" : pct < 0 ? `▼ ${-pct}%` : `▲ ${pct}%`}${vs ? ` vs ${vs}` : ""}`;
  return `<span class="pdelta ${pct < 0 ? "delta-good" : pct > 0 ? "delta-bad" : ""}">${text}</span>`;
}

/** Change against the median of the comparison group picked below the tiles. */
function deltaText(value, ref) {
  if (value == null || !ref) return "";
  const pct = ((value - ref) / ref) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(0)}% vs group median`;
}

/** Tag categories, in display order. Human wins over the prefix: a hand-added `project:x` is human. */
const TAG_CATEGORIES = [
  ["human", "Your tags", "added by you on a prompt page or by your auto-tag rules"],
  ["project", "Project", "AI: git repository the prompt ran in"],
  ["model", "Model", "AI: model that answered"],
  ["skill", "Skill", "AI: skill the agent used"],
  ["ai", "Other AI", "AI: slash command, subagent type, subagent persona"],
];
const CATEGORY_INFO = Object.fromEntries(TAG_CATEGORIES.map(([cat, label, help]) => [cat, { label, help }]));
/** "Your tag", "Project tag"…: one tag of the category. */
const tagNoun = (cat) => (cat === "human" ? "Your tag" : `${CATEGORY_INFO[cat].label} tag`);
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

// ---- keyword rule editor: one row per rule, each with its own color ------------------

const RULE_COLORS = 6; // --rule-0 … --rule-5 in style.css, light and dark
const ruleColor = (i) => `var(--rule-${i % RULE_COLORS})`;

// skills seen in the history (their AI tags), offered by skill rules; set by the tag pages
let ruleSkills = [];
const skillsFromTags = (tags) => tags.filter((t) => t.kind === "ai" && t.tag.startsWith("skill:")).map((t) => t.tag.slice("skill:".length)).sort();

/** One rule: a keyword alone or several together (all or any), or a skill the agent used; in prompts or sessions. */
function ruleRowHtml(i, r = {}) {
  const opt = (value, label, current) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
  const kind = r.skill ? "skill" : "keywords";
  // a rule's skill stays offered even when it was not used lately
  const skills = r.skill && !ruleSkills.includes(r.skill) ? [r.skill, ...ruleSkills] : ruleSkills;
  return `<div class="rule-row" data-rule-row data-kind="${kind}" style="--rule-color:${ruleColor(i)}">
    <span class="rule-dot" aria-hidden="true"></span>
    <select class="rr-kind" aria-label="Rule type" title="Match keywords in the text, or a skill the agent used">
      ${opt("keywords", "Keywords", kind)}${opt("skill", "Skill", kind)}</select>
    <select class="rr-skill" aria-label="Skill" title="Prompts where the agent used this skill">
      ${skills.length ? skills.map((sk) => opt(sk, sk, r.skill)).join("") : '<option value="">no skill used yet</option>'}</select>
    <input class="rr-keywords" value="${esc((r.keywords || []).join(", "))}" aria-label="Keywords of rule ${i + 1}"
      placeholder="a keyword, or several separated by commas: review, branch" title="Separate keywords with commas; words separated by spaces form one phrase. Case does not matter.">
    <select class="rr-match" aria-label="Match" title="With several keywords: all of them together, or any of them (saved as one rule per keyword)">
      ${opt("all", "all of them", r.match || "all")}${opt("any", "any of them", r.match)}</select>
    <select class="rr-scope" aria-label="Where" title="Tag each prompt containing them, or every prompt of a session whose title or prompts contain them">
      ${opt("prompt", "in a prompt", r.scope || "prompt")}${opt("session", "in a session", r.scope)}</select>
    <span class="rr-preview" aria-live="polite"></span>
    <button type="button" class="rr-remove" data-remove-row aria-label="Remove this rule" title="Remove this rule">✕</button>
  </div>`;
}

function readRuleRow(row) {
  if (row.dataset.kind === "skill") return { skill: row.querySelector(".rr-skill").value, scope: row.querySelector(".rr-scope").value };
  return {
    keywords: row.querySelector(".rr-keywords").value.split(",").map((k) => k.trim()).filter(Boolean),
    match: row.querySelector(".rr-match").value,
    scope: row.querySelector(".rr-scope").value,
  };
}

/** Live match count of a row. */
function bindRuleRow(row) {
  let timer;
  const update = () => {
    row.dataset.kind = row.querySelector(".rr-kind").value;
    const { keywords = [], match, scope, skill } = readRuleRow(row);
    clearTimeout(timer);
    const out = row.querySelector(".rr-preview");
    if (!keywords.length && !skill) { out.textContent = ""; return; }
    timer = setTimeout(async () => {
      const query = skill ? { skill, scope } : { keywords: keywords.join(","), match, scope };
      const r = await api(`/api/rules/preview?${new URLSearchParams(query)}`);
      // "any" of several keywords is saved as one rule per keyword (the server splits it)
      const split = match === "any" && keywords.length > 1 ? ` · saved as ${keywords.length} rules, one per keyword` : "";
      out.textContent = `${r.matches} ${scope}${r.matches === 1 ? "" : "s"}${split}`;
    }, 250);
  };
  row.addEventListener("input", update);
  row.addEventListener("change", update);
  update();
}

/** Several rules at once (the New tag form): add and remove rows, colors follow the order. */
function bindRulesEditor(box, addButton) {
  const recolor = () => [...box.children].forEach((row, i) => {
    row.style.setProperty("--rule-color", ruleColor(i));
    row.querySelector(".rr-keywords").setAttribute("aria-label", `Keywords of rule ${i + 1}`);
    row.querySelector("[data-remove-row]").hidden = box.children.length === 1;
  });
  const add = (focus) => {
    box.insertAdjacentHTML("beforeend", ruleRowHtml(box.children.length));
    bindRuleRow(box.lastElementChild);
    recolor();
    if (focus) box.lastElementChild.querySelector(".rr-keywords").focus();
  };
  box.addEventListener("click", (e) => {
    const remove = e.target.closest("[data-remove-row]");
    if (remove) { remove.closest("[data-rule-row]").remove(); recolor(); }
  });
  addButton.onclick = () => add(true);
  return {
    reset: () => { box.innerHTML = ""; add(false); },
    read: () => [...box.children].map(readRuleRow).filter((r) => r.skill || r.keywords.length),
  };
}

function ruleText(r) {
  if (r.skill) {
    const where = r.scope === "session" ? "sessions using skill" : "uses skill";
    return `<span class="rule-scope" title="${r.scope === "session" ? "Every prompt of a session where the agent used it" : "Prompts where the agent used it"}">${where}</span> <code>${esc(r.skill)}</code>`;
  }
  const where = r.scope === "session" ? '<span class="rule-scope" title="Every prompt of a session whose title or prompts contain them">sessions with</span> ' : "";
  if (r.keywords) return `${where}${r.keywords.map((k) => `<code>${esc(k)}</code>`).join(r.match === "any" ? " or " : " and ")}`;
  return `regex <code>${esc(r.pattern)}</code>`;
}

// ---- cost details --------------------------------------------------------------

/** Agent type, plus the persona it was briefed with: runs of one type with different personas are reported apart. */
function agentName(type, persona) {
  return `<strong>${esc(type)}</strong>${persona ? ` <span class="persona" title="Persona from the start of the subagent's prompt">as ${esc(persona)}</span>` : ""}`;
}

/** Where the shown cost comes from, with the other figure when there is one to compare. */
function costNote(r) {
  if (r.cost_source === "claude-code") return `official · price table ${fmtCost(r.estimated_cost_usd)}`;
  if (r.official_cost_usd != null) return `price table · official ${fmtCost(r.official_cost_usd)}`;
  return r.api_ms != null ? "price table (telemetry incomplete)" : "price table (no telemetry)";
}

/** The agent's own cost (OpenTelemetry), per caller and model. */
function telemetryTable(rows) {
  if (!rows.length) return "";
  return `<h2>Cost reported by the agent <span class="muted">(OpenTelemetry, includes internal calls)</span></h2>
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
    const title = t.from_rule ? "Your tag, added by one of your auto-tag rules" : `${tagNoun(cat)}: ${CATEGORY_INFO[cat].help}`;
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

// series colors of the comparison groups: the categorical set without orange, which marks this prompt
const COMPARE_COLORS = ["--rule-0", "--rule-2", "--rule-3", "--rule-4", "--rule-5", "--tag-ai"];

/** One metric over time for several groups of prompts (`groups`: {label, color, points}); `current` stands out. */
function compareChart(canvas, groups, current, metric, format, reference = null) {
  const surface = css("--surface-1");
  const toXY = (label) => (p) => ({ x: new Date(p.started_at).getTime(), y: metric(p), p, label });
  const line = (g) => {
    const pts = g.points.filter((p) => metric(p) != null).sort((a, b) => a.started_at.localeCompare(b.started_at));
    return {
      label: g.label, data: pts.map(toXY(g.label)), borderColor: css(g.color), backgroundColor: css(g.color), borderWidth: pts.length > 60 ? 1.25 : 2,
      pointRadius: pts.length > 60 ? 2 : 3.5, pointHoverRadius: 6, pointBorderColor: surface, pointBorderWidth: pts.length > 60 ? 0 : 1.5,
      tension: 0, order: 1 + pts.length, // bigger groups are drawn first, behind the smaller ones
    };
  };
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      datasets: [
        { label: "This prompt", data: !current || metric(current) == null ? [] : [toXY("This prompt")(current)], showLine: false, pointRadius: 7, pointHoverRadius: 9,
          pointBackgroundColor: css("--series-2"), pointBorderColor: surface, pointBorderWidth: 2, order: 0 },
        { label: "Reference", data: !reference || metric(reference) == null ? [] : [toXY("Reference")(reference)], showLine: false,
          pointStyle: "rectRot", pointRadius: 8, pointHoverRadius: 10, pointBackgroundColor: css("--text-primary"),
          pointBorderColor: surface, pointBorderWidth: 2, order: 0 },
        ...groups.map(line),
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "nearest", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => fmtDate(items[0].raw.p.started_at),
            label: (item) => {
              const text = item.raw.p.text || "";
              return `${item.raw.label}: ${format(item.raw.y)} — ${text.slice(0, 60)}${text.length > 60 ? "…" : ""}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: "linear", grid: { color: css("--grid") }, border: { color: css("--border") },
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
        y: { beginAtZero: true, grid: { color: css("--grid") }, border: { display: false },
          ticks: { color: css("--text-muted"), callback: (v) => format(v), maxTicksLimit: 5 } },
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

// ---- Prompts page ----------------------------------------------------------------

const RANGES = {
  all: ["All time", null],
  today: ["Today", () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; }],
  "7d": ["7 days", () => new Date(Date.now() - 7 * 864e5)],
  "30d": ["30 days", () => new Date(Date.now() - 30 * 864e5)],
};
const PROMPT_SORTS = { recent: "Newest", cost: "Most expensive", duration: "Longest", tokens: "Most tokens" };
// card order in the "By tag" layout, in two menu sections; a group is { info, list } with its prompts in `list`
const latestStart = (g) => g.list.reduce((m, p) => (p.started_at > m ? p.started_at : m), "");
const sumOf = (g, f) => g.list.reduce((sum, p) => sum + (f(p) || 0), 0);
const medianOf = (g, f) => median(g.list.map((p) => f(p) || 0));
const createdAt = (g) => listState.tags.find((t) => t.tag === g.info.tag)?.created_at || "";
const promptCost = (p) => p.total_cost_usd;
const GROUP_SORTS = {
  used: ["Tags", "Most used", (a, b) => b.list.length - a.list.length],
  recent: ["Tags", "Recently used", (a, b) => latestStart(b).localeCompare(latestStart(a))],
  created: ["Tags", "Newest tags", (a, b) => createdAt(b).localeCompare(createdAt(a))],
  name: ["Tags", "A to Z", () => 0],
  median: ["Timing, cost & usage", "Priciest per run", (a, b) => medianOf(b, promptCost) - medianOf(a, promptCost)],
  cost: ["Timing, cost & usage", "Most expensive in total", (a, b) => sumOf(b, promptCost) - sumOf(a, promptCost)],
  duration: ["Timing, cost & usage", "Longest per run", (a, b) => medianOf(b, duration) - medianOf(a, duration)],
  time: ["Timing, cost & usage", "Most time in total", (a, b) => sumOf(b, duration) - sumOf(a, duration)],
  tokens: ["Timing, cost & usage", "Most tokens per run", (a, b) => medianOf(b, (p) => p.total_tokens) - medianOf(a, (p) => p.total_tokens)],
};
const LIST_PAGE = 100;

function loadPromptPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem("prompts-prefs") || "{}");
    return { layout: p.layout === "grouped" ? "grouped" : "list" };
  } catch { return { layout: "list" }; }
}

function savePromptPrefs() {
  try {
    localStorage.setItem("prompts-prefs", JSON.stringify({ layout: listState.layout }));
  } catch { /* storage unavailable: preferences last for this visit */ }
}

// filters live in the URL (shareable); layout preferences in the browser
// on a tag page `lockTag` pins the tag filter
// `selecting` turns the list's rows into a selection (`selected`: prompt ids) to give them a tag
const listState = { q: "", tag: "", project: "", range: "all", sort: "recent", order: "used", lockTag: "", ...loadPromptPrefs(), items: [], summary: null, tags: [],
  selecting: false, selected: new Set(), split: "", reference: null };

// a tag page's charts: one line for all runs, or one per model (project, skill, tag of yours) among them
const SPLITS = { "": "Nothing", model: "Model", skill: "Skill", skillver: "Skill version", agentver: "Agent version", project: "Project", human: "Your tags" };

/** The runs grouped by their tags of category `cat` (a run with two models is in both); the biggest groups,
    the rest together, and the runs with none. */
function splitRuns(items, cat, exclude = listState.lockTag) {
  if (!cat) return [{ label: "All runs", points: items, color: COMPARE_COLORS[0] }];
  const groups = new Map(), none = [];
  for (const p of items) {
    // versions come from the run itself; the other splits from its tags
    const values = cat === "agentver" ? (p.harness_version ? [`${p.harness || "Agent"} ${p.harness_version}`.trim()] : [])
      : cat === "skillver" ? (p.skill_versions ? p.skill_versions.split("\x1f") : [])
      : (p.tags || []).filter((t) => tagCategory(t) === cat && t.tag !== exclude).map((t) => t.tag);
    if (!values.length) none.push(p);
    for (const v of values) {
      if (!groups.has(v)) groups.set(v, []);
      groups.get(v).push(p);
    }
  }
  const sorted = [...groups].sort((a, b) => b[1].length - a[1].length);
  const max = COMPARE_COLORS.length - (none.length ? 2 : 1);
  const out = sorted.slice(0, max).map(([label, points]) => ({ label, points }));
  if (sorted.length > max) out.push({ label: `${sorted.length - max} others`, points: [...new Set(sorted.slice(max).flatMap(([, ps]) => ps))] });
  if (none.length) out.push({ label: { model: "No model", skill: "No skill", skillver: "No skill", agentver: "Unknown version", project: "No project", human: "None of your tags" }[cat], points: none });
  return out.map((g, i) => ({ ...g, color: COMPARE_COLORS[i] }));
}

function splitSummaryHtml(groups) {
  if (groups.length < 2) return "";
  const cheapest = Math.min(...groups.map((g) => median(g.points.map((p) => p.total_cost_usd)) ?? Infinity));
  return `<div class="table-wrap split-summary"><table>
    <thead><tr><th>Group</th><th class="num">Runs</th><th class="num">Median cost</th><th class="num">Median time</th><th class="num">Median tokens</th><th class="num">Total cost</th></tr></thead>
    <tbody>${groups.map((g) => {
      const cost = median(g.points.map((p) => p.total_cost_usd));
      return `<tr><td><i class="series-swatch" style="--series-color:var(${g.color})"></i> ${esc(g.label)}${cost === cheapest ? ' <span class="trend down">cheapest</span>' : ""}</td>
        <td class="num">${g.points.length}</td><td class="num">${fmtCost(cost)}</td><td class="num">${fmtMs(median(g.points.map(duration)))}</td>
        <td class="num">${fmtTokens(median(g.points.map((p) => p.total_tokens)))}</td><td class="num">${fmtCost(g.points.reduce((t, p) => t + (p.total_cost_usd || 0), 0))}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

/** "By tag" sections on the Prompts page only: a tag page lists that tag's prompts, unsplit. */
const groupedLayout = () => listState.layout === "grouped" && !listState.lockTag;

function filtersFromQuery(query) {
  listState.q = query.get("q") || "";
  listState.tag = query.get("tag") || "";
  listState.project = query.get("project") || "";
  listState.range = RANGES[query.get("range")] ? query.get("range") : "all";
  listState.sort = PROMPT_SORTS[query.get("sort")] ? query.get("sort") : "recent";
  listState.order = GROUP_SORTS[query.get("order")] ? query.get("order") : "used";
}

function syncFiltersToUrl() {
  const q = new URLSearchParams();
  for (const key of listState.lockTag ? ["q", "project"] : ["q", "tag", "project"]) if (listState[key]) q.set(key, listState[key]);
  if (listState.range !== "all") q.set("range", listState.range);
  if (listState.sort !== "recent") q.set("sort", listState.sort);
  if (listState.lockTag && listState.split) q.set("split", listState.split);
  if (groupedLayout() && listState.order !== "used") q.set("order", listState.order);
  const base = listState.lockTag ? `#/tag/${encodeURIComponent(listState.lockTag)}` : "#/prompts";
  const hash = `${base}${q.toString() ? `?${q}` : ""}`;
  if (location.hash !== hash) history.replaceState(null, "", hash); // no hashchange: the page updates itself
}

function promptQuery(extra = {}) {
  const since = RANGES[listState.range][1];
  const q = { q: listState.q, tag: listState.tag, project: listState.project, sort: listState.sort, ...extra };
  if (since) q.since = since().toISOString();
  return new URLSearchParams(q);
}

function dayLabel(iso) {
  const d = new Date(iso), today = new Date();
  const diff = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 864e5);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

/** Tag chips inside a prompt row: buttons that filter the list by that tag (on a tag page: open that tag). */
function rowChips(tags) {
  const sorted = tags.filter((t) => t.tag !== listState.lockTag).sort((a, b) => CATEGORY_ORDER[tagCategory(a)] - CATEGORY_ORDER[tagCategory(b)] || a.tag.localeCompare(b.tag));
  return sorted.map((t) => `<button type="button" class="chip cat-${tagCategory(t)}" data-filter-tag="${esc(t.tag)}"
    title="${listState.lockTag ? "Open the tag page of" : "Show only prompts tagged"} ${esc(t.tag)}">${esc(t.tag)}</button>`).join("");
}

function promptRow(p, maxCost, { compact = false } = {}) {
  const pct = maxCost ? Math.max(2, Math.round(((p.total_cost_usd || 0) / maxCost) * 100)) : 0;
  const time = new Date(p.started_at);
  const when = listState.sort === "recent" && !compact
    ? time.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : fmtDate(p.started_at);
  const meta = [fmtTokens(p.total_tokens) + " tok", fmtMs(duration(p)), p.agent_count ? `${p.agent_count} subagent${p.agent_count > 1 ? "s" : ""}` : ""]
    .filter(Boolean).join(" · ");
  const selectable = listState.selecting && !groupedLayout() && !compact;
  const picked = selectable && listState.selected.has(p.id);
  const timeHtml = `<time datetime="${esc(p.started_at)}">${when}</time>`;
  // on a tag page: the reference run, and the button making a run the reference
  const ref = listState.lockTag && !compact ? listState.reference : null;
  const isRef = Boolean(ref && ref.id === p.id);
  const starHtml = listState.lockTag && !compact
    ? `<button type="button" class="pref" data-ref="${esc(p.id)}" aria-pressed="${isRef}" title="${isRef ? "The reference run: click to clear it" : "Make this run the reference every other run is compared with"}">${isRef ? "★" : "☆"}</button>` : "";
  return `<article class="prow ${compact ? "compact" : ""} ${picked ? "selected" : ""}" data-id="${esc(p.id)}">
    ${selectable ? `<div class="pwhen"><input type="checkbox" class="psel" data-select="${esc(p.id)}" ${picked ? "checked" : ""}
      aria-label="Select this prompt">${timeHtml}</div>` : timeHtml}
    <div class="pbody">
      <a class="plink" href="#/prompt/${encodeURIComponent(p.id)}">${p.command ? `<span class="pcmd">/${esc(p.command)}</span> ` : ""}${esc(p.command ? p.text.replace(/^\/\S+\s*/, "") : p.text)}</a>
      ${compact ? "" : `<div class="pchips">${p.project ? `<span class="pproject">${esc(p.project)}</span>` : ""}${rowChips((p.tags || []).filter((t) => tagCategory(t) !== "project"))}</div>`}
    </div>
    <div class="pmetrics">
      <span class="pcost-line">${starHtml}<span class="pcost">${fmtCost(p.total_cost_usd)}</span></span>
      <span class="cost-bar" title="Cost relative to the most expensive prompt shown"><i style="width:${pct}%"></i></span>
      <span class="pmeta">${meta}</span>
      ${ref && !isRef ? refDelta(p.total_cost_usd, ref.total_cost_usd) : isRef ? '<span class="pdelta ref-badge">◆ Reference</span>' : ""}
    </div>
  </article>`;
}

function listHtml(items, maxCost) {
  if (listState.sort !== "recent") return `<div class="prow-list">${items.map((p) => promptRow(p, maxCost)).join("")}</div>`;
  const days = new Map();
  for (const p of items) {
    const key = new Date(p.started_at).toDateString();
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(p);
  }
  return [...days.values()].map((list) => {
    const cost = list.reduce((s, p) => s + (p.total_cost_usd || 0), 0);
    return `<section class="day"><h2 class="day-head"><span>${dayLabel(list[0].started_at)}</span>
      <span class="sec-meta">${list.length} prompt${list.length === 1 ? "" : "s"} · ${fmtCost(cost)}</span></h2>
      <div class="prow-list">${list.map((p) => promptRow(p, maxCost)).join("")}</div></section>`;
  }).join("");
}

const FAMILY_LABELS = { project: "Projects", model: "Models", skill: "Skills", cmd: "Slash commands", agent: "Subagent types", persona: "Personas", rerun: "Reruns" };

// earlier runs of each group, rendered on first open: the sections can hold thousands of rows
let groupRuns = [];

/** One section per tag category, then per family (prefix), as on the Tags page; one card per tag. */
function groupedPromptsHtml(items, maxCost) {
  const groups = new Map();
  const loose = [];
  for (const p of items) {
    const tags = (p.tags || []).filter((t) => t.tag !== listState.lockTag);
    if (!tags.some((t) => t.kind === "human")) loose.push(p);
    for (const t of tags) {
      if (!groups.has(t.tag)) groups.set(t.tag, { info: t, list: [] });
      groups.get(t.tag).list.push(p);
    }
  }
  groupRuns = [];
  const byRecent = (a, b) => b.started_at.localeCompare(a.started_at);
  const card = (label, list, cat, tag) => {
    list.sort(byRecent);
    const [first, ...rest] = list;
    const costs = [...list].reverse().map((p) => p.total_cost_usd || 0);
    const total = costs.reduce((sum, c) => sum + c, 0);
    groupRuns.push(rest);
    const [prefix, ...name] = label.split(":");
    const title = tag && name.length ? `<span class="tag-prefix">${esc(prefix)}:</span>${esc(name.join(":"))}` : esc(label);
    return `<section class="pgroup cat-${cat}">
      <header class="pgroup-title"><span class="tag-dot"></span>${tag ? `<a href="#/tag/${encodeURIComponent(tag)}" title="Open the tag page">${title}</a>` : title}</header>
      <div class="tag-metric"><span class="big">${fmtCost(median(costs))}</span><span class="unit">median per run</span>${trendBadge(costs)}</div>
      ${sparkline(costs.slice(-24))}
      <div class="pgroup-meta"><span><strong>${list.length}</strong> run${list.length === 1 ? "" : "s"}</span><span>${fmtCost(total)} total</span></div>
      ${latestRun(first)}
      ${rest.length ? `<details class="more-runs" data-runs="${groupRuns.length - 1}"><summary><span class="closed">Show ${rest.length} more run${rest.length === 1 ? "" : "s"}</span><span class="opened">Hide earlier runs</span></summary>
        <div class="prow-list"></div></details>` : ""}
    </section>`;
  };
  // one section per family: the tag's prefix (`task:`, `project:`, `cmd:`…), in category order
  const families = new Map();
  for (const g of groups.values()) {
    const prefix = g.info.tag.includes(":") ? g.info.tag.split(":")[0] : "";
    const key = `${tagCategory(g.info)}|${prefix}`;
    if (!families.has(key)) families.set(key, { cat: tagCategory(g.info), prefix, groups: [] });
    families.get(key).groups.push(g);
  }
  const ordered = [...families.values()].sort((a, b) =>
    CATEGORY_ORDER[a.cat] - CATEGORY_ORDER[b.cat] || (a.prefix === "") - (b.prefix === "") || a.prefix.localeCompare(b.prefix));
  const section = (id, cat, title, meta, help, cards) => `<details class="tag-section psection cat-${cat}" id="${id}" open>
      <summary><h2><span class="tag-dot"></span>${title}<span class="sec-meta">${meta}</span></h2></summary>
      <p class="sec-help">${esc(help)}</p>
      <div class="pgroup-grid">${cards}</div>
      <button type="button" class="link-btn more-tags" hidden></button>
    </details>`;
  const sections = ordered.map((f, i) => {
    f.groups.sort((a, b) => GROUP_SORTS[listState.order][2](a, b) || a.info.tag.localeCompare(b.info.tag));
    const runs = new Set(f.groups.flatMap((g) => g.list)).size;
    const name = f.prefix ? FAMILY_LABELS[f.prefix] || `${f.prefix}:` : "Your tags";
    const help = `${f.cat === "human" ? "Tags you created" : `${CATEGORY_INFO[f.cat].label} tags`}${f.prefix ? `, written ${f.prefix}:…` : ""}`;
    return { id: `psec-${i}`, cat: f.cat, name, n: f.groups.length, html: section(`psec-${i}`, f.cat, esc(name),
      `${f.groups.length} tag${f.groups.length === 1 ? "" : "s"} · ${runs} run${runs === 1 ? "" : "s"}`, help,
      f.groups.map((g) => card(g.info.tag, g.list, f.cat, g.info.tag)).join("")) };
  });
  if (loose.length && loose.length < items.length) {
    const label = `Without ${listState.lockTag && sections.some((x) => x.cat === "human") ? "another" : "any"} tag of yours`;
    sections.push({ id: "psec-loose", cat: "ai", name: "Not tagged by you", n: 1, html: section("psec-loose", "ai", "Not tagged by you",
      `${loose.length} run${loose.length === 1 ? "" : "s"}`, "Prompts with none of your tags yet: tag them to follow them", card(label, loose, "ai", null)) });
  }
  return `<div class="group-actions">
      <nav class="jump" aria-label="Jump to a section">${sections.map((x) =>
        `<a class="chip cat-${x.cat}" href="#${x.id}" data-jump="${x.id}">${esc(x.name)} <span class="mi-count">${x.n}</span></a>`).join("")}</nav>
      <span class="toolbar-spacer"></span>
      <button class="btn" type="button" data-expand="1">Expand all</button><button class="btn" type="button" data-expand="0">Collapse all</button></div>
    <p class="muted group-note">One section per tag family, one card per tag with its latest run; a prompt shows in each of its tags.</p>
    ${sections.map((x) => x.html).join("")}`;
}

/** The latest run inside a tag card: its text and cost; the whole block opens the prompt. */
function latestRun(p) {
  return `<a class="latest-run" href="#/prompt/${encodeURIComponent(p.id)}" title="Open the latest run">
    <span class="latest-head"><span>Latest · <time datetime="${esc(p.started_at)}" title="${esc(fmtDate(p.started_at))}">${fmtAgo(p.started_at)}</time></span><strong>${fmtCost(p.total_cost_usd)}</strong></span>
    <span class="latest-text">${p.command ? `<span class="pcmd">/${esc(p.command)}</span> ` : ""}${esc(p.command ? p.text.replace(/^\/\S+\s*/, "") : p.text)}</span>
    <span class="pmeta">${fmtTokens(p.total_tokens)} tok · ${fmtMs(duration(p))}</span></a>`;
}

/** Each family shows one row of cards (as many as fit); its button shows the rest. */
function clampGrids(root = document) {
  root.querySelectorAll(".pgroup-grid").forEach((grid) => {
    const more = grid.nextElementSibling?.classList.contains("more-tags") ? grid.nextElementSibling : null;
    if (!more) return;
    const cols = getComputedStyle(grid).gridTemplateColumns.split(" ").length;
    const cards = [...grid.children];
    const open = grid.classList.contains("expanded");
    cards.forEach((c, i) => { c.hidden = !open && i >= cols; });
    const extra = cards.length - cols;
    more.hidden = extra <= 0;
    more.textContent = open ? "Show fewer" : `Show ${extra} more tag${extra === 1 ? "" : "s"}`;
    more.setAttribute("aria-expanded", String(open));
  });
}
addEventListener("resize", () => clampGrids());

function fillRuns(d, maxCost) {
  const list = d.querySelector(".prow-list");
  if (!list.childElementCount) list.innerHTML = groupRuns[d.dataset.runs].map((p) => promptRow(p, maxCost, { compact: true })).join("");
}

function promptSummaryHtml(s) {
  const fact = (label, value, sub = "") => `<div class="hero-fact"><span class="label">${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></div>`;
  return `<div class="tags-hero">
    ${fact("Prompts", s.count || 0, RANGES[listState.range][0].toLowerCase())}
    ${fact("Total cost", fmtCost(s.cost_usd || 0), `${fmtTokens(s.tokens || 0)} tokens`)}
    ${fact("Per prompt", fmtCost(s.count ? s.cost_usd / s.count : null), "average")}
    ${fact("Agent time", fmtMs(s.duration_ms || 0), "active time, summed")}
  </div>`;
}

function activeFiltersHtml() {
  const chips = [];
  if (listState.q) chips.push(["q", `“${listState.q}”`]);
  if (listState.project) chips.push(["project", `Project: ${listState.project}`]);
  if (listState.tag && !listState.lockTag) chips.push(["tag", `Tag: ${listState.tag}`]);
  if (listState.range !== "all") chips.push(["range", RANGES[listState.range][0]]);
  if (!chips.length) return "";
  return `<div class="active-filters" aria-label="Active filters">${chips.map(([key, label]) =>
    `<button type="button" class="filter-chip" data-clear="${key}" title="Remove this filter">${esc(label)}<span aria-hidden="true">✕</span></button>`).join("")}
    <button type="button" class="link-btn" data-clear="all">Clear all</button></div>`;
}

function pillMenu(id, label, value, body) {
  return `<details class="pop" id="${id}"><summary class="pill ${value ? "set" : ""}">${label}${value ? `: <strong>${esc(value)}</strong>` : ""}<span class="caret-d">▾</span></summary>
    <div class="pop-panel">${body}</div></details>`;
}

function promptToolbarHtml(projects) {
  const projectMenu = `<button type="button" class="menu-item ${!listState.project ? "on" : ""}" data-set-project="">All projects</button>
    ${projects.map((p) => `<button type="button" class="menu-item ${p === listState.project ? "on" : ""}" data-set-project="${esc(p)}">${esc(p)}</button>`).join("")}`;
  const tagMenu = `<input type="search" class="menu-search" placeholder="Find a tag…" aria-label="Find a tag">
    <div class="menu-list">${TAG_CATEGORIES.map(([cat, label]) => {
      const list = listState.tags.filter((t) => tagCategory(t) === cat);
      return list.length ? `<div class="menu-group" data-group="${cat}">${label}</div>${list.map((t) =>
        `<button type="button" class="menu-item ${t.tag === listState.tag ? "on" : ""}" data-set-tag="${esc(t.tag)}" data-cat="${cat}">
          <span class="tag-dot cat-dot-${cat}"></span><span class="mi-text">${esc(t.tag)}</span><span class="mi-count">${t.prompts}</span></button>`).join("")}` : "";
    }).join("")}</div>`;
  return `<search class="tags-toolbar prompts-toolbar">
    <label class="search-box"><span aria-hidden="true">⌕</span>
      <input type="search" id="prompt-search" placeholder="${listState.lockTag ? "Search these runs…" : "Search prompts…"}" value="${esc(listState.q)}" aria-label="Search prompts"></label>
    ${pillMenu("pop-project", "Project", listState.project, projectMenu)}
    ${listState.lockTag ? "" : pillMenu("pop-tag", "Tag", listState.tag, tagMenu)}
    <div class="segmented" role="group" aria-label="Period">${Object.entries(RANGES).map(([key, [label]]) =>
      `<button type="button" data-range="${key}" aria-pressed="${key === listState.range}">${label}</button>`).join("")}</div>
    <span class="toolbar-spacer"></span>
    ${groupedLayout()
      ? pillMenu("pop-order", "Order", GROUP_SORTS[listState.order][1], [...new Set(Object.values(GROUP_SORTS).map(([section]) => section))].map((section) =>
        `<div class="menu-group">${section}</div>${Object.entries(GROUP_SORTS).filter(([, [sec]]) => sec === section).map(([key, [, label]]) =>
          `<button type="button" class="menu-item ${key === listState.order ? "on" : ""}" data-set-order="${key}">${label}</button>`).join("")}`).join("")).replace("pill set", "pill") /* always set: not highlighted as a filter */
      : `<label class="sort-box">Sort <select id="prompt-sort">${Object.entries(PROMPT_SORTS).map(([key, label]) =>
        `<option value="${key}" ${key === listState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>`}
    ${groupedLayout() ? "" : `<button type="button" class="btn ${listState.selecting ? "primary" : ""}" data-toggle-select aria-pressed="${listState.selecting}"
      title="Pick prompts to group them under a tag">${listState.selecting ? "Selecting…" : "Select prompts"}</button>`}
    ${listState.lockTag ? "" : `<div class="segmented" role="group" aria-label="Layout">
      <button type="button" data-layout="list" aria-pressed="${listState.layout === "list"}">List</button>
      <button type="button" data-layout="grouped" aria-pressed="${listState.layout === "grouped"}">By tag</button>
    </div>`}
  </search>`;
}

async function loadPrompts() {
  // grouped layouts and tag pages (charts, medians) need every matching prompt
  const all = listState.layout === "grouped" || listState.lockTag;
  const filters = promptQuery();
  filters.delete("sort");
  const [data, overview] = await Promise.all([
    api(`/api/prompts?${promptQuery({ limit: all ? 100000 : LIST_PAGE, offset: 0 })}`),
    api(`/api/overview?${filters}`),
  ]);
  listState.items = data.items;
  listState.summary = data.summary;
  listState.overview = overview;
}

async function renderPrompts({ reload = true } = {}) {
  syncFiltersToUrl();
  if (reload) await loadPrompts();
  const { items, summary } = listState;
  const maxCost = summary.max_cost_usd || 0;
  document.getElementById("prompt-summary").innerHTML = listState.lockTag ? tagSummaryHtml(items, summary) : promptSummaryHtml(summary);
  // the same highlights as the home page, over the prompts the filters keep
  document.getElementById("prompt-highlights").innerHTML = items.length ? `<div class="highlights">${homeHighlights(listState.overview)}</div>` : "";
  if (listState.lockTag) {
    destroyCharts();
    const groups = splitRuns(items, listState.split);
    const metrics = [["cost", "Cost (USD, incl. subagents)", (x) => x.total_cost_usd, fmtCost], ["dur", "Duration (active time, else wall time)", duration, fmtMs],
      ["tok", "Tokens (incl. cache, incl. subagents)", (x) => x.total_tokens, fmtTokens]];
    document.getElementById("tag-charts").innerHTML = !items.length ? "" : `
      <div class="compare-picker split-picker" role="group" aria-label="Split by"><span class="muted">One line per</span>
        ${Object.entries(SPLITS).map(([key, label]) => `<button type="button" class="series-chip" data-split="${key}" aria-pressed="${key === listState.split}">${label}</button>`).join("")}</div>
      ${groups.length > 1 || listState.reference ? `<div class="legend">${listState.reference ? '<span><i class="ref-swatch"></i>Reference run</span>' : ""}${groups.length > 1 ? groups.map((g) => `<span><i style="background:var(${g.color})"></i>${esc(g.label)} <span class="mi-count">${g.points.length}</span></span>`).join("") : ""}</div>` : ""}
      <div class="charts">${metrics.map(([id, title]) => `<div class="card"><div class="chart-title">${title}</div><div class="chart-box"><canvas id="tagchart-${id}"></canvas></div></div>`).join("")}</div>
      ${splitSummaryHtml(groups)}`;
    if (items.length) metrics.forEach(([id, , metric, fmt]) => compareChart(document.getElementById(`tagchart-${id}`), groups, null, metric, fmt, listState.reference));
    document.getElementById("tag-reference").innerHTML = referenceCardHtml();
    document.querySelectorAll("[data-split]").forEach((b) => b.onclick = () => { listState.split = b.dataset.split; renderPrompts({ reload: false }); });
  }
  document.getElementById("active-filters").innerHTML = activeFiltersHtml();
  const results = document.getElementById("prompt-results");
  if (!items.length) {
    results.innerHTML = `<div class="empty-state"><div class="empty-icon">⌕</div><p>${activeFiltersHtml() || !listState.lockTag ? "No prompt matches these filters." : "No prompt carries this tag yet."}</p>
      ${activeFiltersHtml() ? '<button class="btn" type="button" data-clear="all">Clear all filters</button>' : '<p>Check the <a href="#/setup">Setup</a> page, then press Refresh.</p>'}</div>`;
  } else if (groupedLayout()) {
    results.innerHTML = groupedPromptsHtml(items, maxCost);
  } else {
    const more = summary.count - items.length;
    results.innerHTML = listHtml(items, maxCost) + (more > 0
      ? `<div class="load-more"><button class="btn" type="button" id="load-more">Show ${Math.min(more, LIST_PAGE)} more <span class="muted">(${more} left)</span></button></div>` : "");
  }
  bindPromptResults();
}

function selectBarHtml() {
  const n = listState.selected.size, matching = listState.summary?.count || 0;
  // every matching prompt already selected: by "select all" for these filters, or row by row with all loaded
  const allIn = (listState.allSelectedFor === promptQuery().toString() && n >= matching)
    || (matching === listState.items.length && listState.items.every((p) => listState.selected.has(p.id)));
  const filtered = listState.q || listState.project || listState.range !== "all" || (listState.tag && !listState.lockTag);
  return `<div class="select-bar" role="region" aria-label="Selection">
    <strong>${n} selected</strong>
    <button type="button" class="btn" data-select-all ${allIn || !matching ? "disabled" : ""}
      title="Every prompt matching the search and filters, not only the ones loaded">Select all ${matching}${filtered ? " matching" : ""}</button>
    ${n ? '<button type="button" class="link-btn" data-select-none>Clear</button>' : ""}
    <span class="toolbar-spacer"></span>
    <span class="muted select-hint">${n ? "" : "Click prompts to select them; search or filter first to narrow them down."}</span>
    <span class="select-note" id="select-note"></span>
    <button type="button" class="btn" data-queue-selected ${n ? "" : "disabled"} title="Rerun them later from the Reruns page, with the model and effort they ran with">↻ Queue ${n || ""} rerun${n === 1 ? "" : "s"}</button>
    ${listState.lockTag && listState.reference ? `<button type="button" class="btn" data-queue-selected="reference" ${n ? "" : "disabled"}
      title="Rerun them with the reference run's model, effort and Claude Code release">↻ … with the reference's settings</button>` : ""}
    <button type="button" class="btn primary" data-new-tag ${n ? "" : "disabled"}>Tag ${n || ""} selected…</button>
    <button type="button" class="btn" data-toggle-select>Done</button>
  </div>`;
}

/** Selection on a list page: the toggle, select all (fetching the matching prompts not loaded yet), clear,
    and the tag form giving the selection a tag. */
function bindSelection(page, { tags, exclude = [] }) {
  bindTagDialog(page, {
    tags, targets: () => [...listState.selected], exclude,
    title: (n) => `Tag ${n} selected prompt${n === 1 ? "" : "s"}`,
  });
  page.addEventListener("click", async (e) => {
    if (e.target.closest("[data-toggle-select]")) {
      listState.selecting = !listState.selecting;
      if (!listState.selecting) listState.selected.clear();
      rerenderToolbar(); renderPrompts({ reload: false });
    } else if (e.target.closest("[data-select-all]")) {
      let items = listState.items;
      if (items.length < listState.summary.count) {
        items = (await api(`/api/prompts?${promptQuery({ limit: 100000, offset: 0 })}`)).items;
      }
      items.forEach((p) => listState.selected.add(p.id));
      listState.allSelectedFor = promptQuery().toString();
      renderPrompts({ reload: false });
    } else if (e.target.closest("[data-select-none]")) {
      listState.selected.clear(); renderPrompts({ reload: false });
    } else if (e.target.closest("[data-queue-selected]")) {
      const ref = e.target.closest("[data-queue-selected]").dataset.queueSelected === "reference" ? listState.reference : null;
      const settings = ref ? { model: ref.requested_model || "", effort: ref.effort || "", version: ref.harness_version || "" } : {};
      const out = await api("/api/reruns/queue", { method: "POST", body: { prompt_ids: [...listState.selected], ...settings } });
      document.getElementById("select-note").innerHTML = `${plural(out.added, "rerun")} queued · <a href="#/reruns">${out.queued} on the Reruns page</a>`;
    }
  });
}

function renderSelectBar() {
  const bar = document.getElementById("select-bar");
  if (bar) bar.innerHTML = listState.selecting ? selectBarHtml() : "";
}

function bindPromptResults() {
  const results = document.getElementById("prompt-results");
  renderSelectBar();
  // while selecting, a click on a row (its checkbox included) selects it instead of opening it
  results.onclick = (e) => {
    if (e.target.closest("[data-ref]")) return; // the reference star: handled by the page
    if (!listState.selecting || e.target.closest("[data-filter-tag]")) return;
    const row = e.target.closest(".prow[data-id]");
    if (!row || !row.querySelector(".psel")) return;
    const box = row.querySelector(".psel");
    if (e.target !== box) { e.preventDefault(); box.checked = !box.checked; } // the checkbox toggles itself
    listState.selected[box.checked ? "add" : "delete"](row.dataset.id);
    row.classList.toggle("selected", box.checked);
    renderSelectBar();
  };
  results.querySelectorAll("[data-filter-tag]").forEach((b) => b.onclick = () => {
    if (listState.lockTag) { location.hash = `#/tag/${encodeURIComponent(b.dataset.filterTag)}`; return; }
    listState.tag = b.dataset.filterTag;
    rerenderToolbar();
    renderPrompts();
    scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  });
  const maxCost = listState.summary.max_cost_usd || 0;
  results.querySelectorAll("details.more-runs").forEach((d) => d.addEventListener("toggle", () => { if (d.open) fillRuns(d, maxCost); }));
  results.querySelectorAll("[data-expand]").forEach((b) => b.onclick = () => {
    const open = b.dataset.expand === "1";
    results.querySelectorAll("details.psection").forEach((d) => { d.open = true; });
    results.querySelectorAll(".pgroup-grid").forEach((g) => g.classList.toggle("expanded", open));
    results.querySelectorAll("details.more-runs").forEach((d) => { if (open) fillRuns(d, maxCost); d.open = open; });
    clampGrids(results);
  });
  results.querySelectorAll(".more-tags").forEach((b) => b.onclick = () => {
    b.previousElementSibling.classList.toggle("expanded");
    clampGrids(results);
  });
  clampGrids(results);
  // the hash routes pages: jump links scroll instead of navigating
  results.querySelectorAll("[data-jump]").forEach((a) => a.onclick = (e) => {
    e.preventDefault();
    const sec = document.getElementById(a.dataset.jump);
    sec.open = true;
    sec.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  });
  const more = document.getElementById("load-more");
  if (more) more.onclick = async () => {
    more.disabled = true;
    const data = await api(`/api/prompts?${promptQuery({ limit: LIST_PAGE, offset: listState.items.length })}`);
    listState.items.push(...data.items);
    renderPrompts({ reload: false });
  };
  app.querySelectorAll("[data-clear]").forEach((b) => b.onclick = () => {
    const key = b.dataset.clear;
    if (key === "all") Object.assign(listState, { q: "", tag: listState.lockTag, project: "", range: "all" });
    else listState[key] = key === "range" ? "all" : "";
    rerenderToolbar();
    renderPrompts();
  });
}

let promptProjects = [];

function rerenderToolbar() {
  document.getElementById("prompt-toolbar").innerHTML = promptToolbarHtml(promptProjects);
  bindPromptToolbar();
}

function bindPromptToolbar() {
  const bar = document.getElementById("prompt-toolbar");
  let timer;
  bar.querySelector("#prompt-search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { listState.q = e.target.value; renderPrompts(); }, 250);
  });
  bar.querySelectorAll("[data-set-project]").forEach((b) => b.onclick = () => { listState.project = b.dataset.setProject; rerenderToolbar(); renderPrompts(); });
  bar.querySelectorAll("[data-set-tag]").forEach((b) => b.onclick = () => {
    listState.tag = b.dataset.setTag === listState.tag ? "" : b.dataset.setTag; rerenderToolbar(); renderPrompts();
  });
  const find = bar.querySelector(".menu-search");
  if (find) find.addEventListener("input", () => {
    const q = find.value.toLowerCase();
    bar.querySelectorAll("#pop-tag [data-set-tag]").forEach((b) => { b.hidden = !b.dataset.setTag.toLowerCase().includes(q); });
    bar.querySelectorAll("#pop-tag [data-group]").forEach((g) => {
      g.hidden = !bar.querySelector(`#pop-tag [data-cat="${g.dataset.group}"]:not([hidden])`);
    });
  });
  bar.querySelector("#pop-tag")?.addEventListener("toggle", (e) => { if (e.target.open) find.focus(); });
  bar.querySelectorAll("[data-range]").forEach((b) => b.onclick = () => { listState.range = b.dataset.range; rerenderToolbar(); renderPrompts(); });
  bar.querySelectorAll("[data-layout]").forEach((b) => b.onclick = () => {
    listState.layout = b.dataset.layout; savePromptPrefs();
    if (groupedLayout()) Object.assign(listState, { selecting: false, selected: new Set() });
    rerenderToolbar(); renderPrompts();
  });
  const sort = bar.querySelector("#prompt-sort");
  if (sort) sort.onchange = (e) => { listState.sort = e.target.value; renderPrompts(); };
  bar.querySelectorAll("[data-set-order]").forEach((b) => b.onclick = () => {
    listState.order = b.dataset.setOrder; rerenderToolbar(); renderPrompts({ reload: false });
  });
}

// pill menus close when clicking elsewhere or pressing Escape, like native menus
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.pop[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll("details.pop[open]").forEach((d) => { d.open = false; d.querySelector("summary").focus(); });
});

async function viewPrompts(query = new URLSearchParams()) {
  filtersFromQuery(query);
  Object.assign(listState, { lockTag: "", selecting: false, selected: new Set() });
  [promptProjects, listState.tags] = await Promise.all([api("/api/projects"), api("/api/tags")]);
  ruleSkills = skillsFromTags(listState.tags);
  app.innerHTML = `
    <div class="prompts-page">
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Prompts</span>Your whole prompt history. <em>Pick one</em> to dig deeper.</h1>
          <p>Every prompt you gave your coding agent, with what it cost and how long it took. Search, filter or sort to find one, then open it:
            <strong>cost per agent</strong>, <strong>comparison with its reruns</strong> and similar prompts, and tags to follow the task.</p>
        </div>
      </header>
      <div id="prompt-summary"></div>
      <div id="prompt-highlights"></div>
      <div id="prompt-toolbar"></div>
      <div id="select-bar"></div>
      <div id="active-filters"></div>
      <div id="prompt-results"></div>
    </div>
    ${tagDialogHtml({ forPrompts: true })}`;
  bindSelection(app.querySelector(".prompts-page"), { tags: listState.tags });
  rerenderToolbar();
  await renderPrompts();
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
    ${human.map((t) => `<optgroup label="Your tag ${esc(t.tag)}">
      ${opt("tag", t.tag, `All prompts tagged ${t.tag}`)}
      ${opt("similar_tag", t.tag, `Similar prompts tagged ${t.tag}`)}
    </optgroup>`).join("")}
  </select>
  ${human.length ? "" : `<span class="muted" style="font-size:12px">Add one of your tags to compare with every prompt sharing it.</span>`}`;
}

// ---- run it again: what ran a prompt, and the command repeating it ----------------------

const shellQuote = (v) => `'${String(v).replace(/'/g, "'\\''")}'`;

/** Where a skill version comes from, in words. */
function skillOrigin(k) {
  const short = (k.git_commit || "").slice(0, 7);
  const plugin = k.plugin ? ` · plugin ${esc(k.plugin)}${k.plugin_version ? ` ${esc(k.plugin_version)}` : ""}` : "";
  switch (k.origin === "git" ? k.git_state : k.origin) {
    case "committed": return `commit <code>${esc(short)}</code> of <code>${esc(k.git_repo)}</code> (<code>${esc(k.git_path)}</code>)${plugin}`;
    case "uncommitted": return `uncommitted changes in <code>${esc(k.git_repo)}</code> when it ran: the snapshot is the only copy${plugin}`;
    case "changed": return `no commit of <code>${esc(k.git_repo)}</code> has this version: the snapshot is the only copy${plugin}`;
    case "bundled": return `built into Claude Code ${esc(k.bundled_version)}`;
    case "missing": return `its folder <code>${esc(k.base_dir)}</code> is gone: the snapshot is the only copy${plugin}`;
    default: return `folder <code>${esc(k.base_dir)}</code>, outside git: the snapshot is the only copy${plugin}`;
  }
}

/** Shell lines repeating the run with Claude Code: the same folder, skill versions, release and model. */
function rerunCommand(p) {
  const r = p.rerun, model = r.requested_model || Object.keys(p.models || {})[0] || "";
  const lines = [];
  if (r.cwd) lines.push(`cd ${shellQuote(r.cwd)}`);
  const undo = [];
  for (const k of r.skills) {
    if (k.origin === "git" && k.git_state === "committed") {
      lines.push(`# ${k.skill} as it ran (commit ${k.git_commit.slice(0, 7)}): this replaces the skill's files, commit or stash your changes first`,
        `git -C ${shellQuote(k.git_repo)} checkout ${k.git_commit} -- ${shellQuote(k.git_path)}`);
      undo.push(`git -C ${shellQuote(k.git_repo)} checkout HEAD -- ${shellQuote(k.git_path)}`);
    } else if (k.origin === "bundled") {
      lines.push(`# ${k.skill}: built into Claude Code ${k.bundled_version}, comes with the release below`);
    } else {
      lines.push(`# ${k.skill}: put the downloaded SKILL.md (as it ran) back in ${k.base_dir}`);
    }
  }
  const agent = r.harness_version ? `npx @anthropic-ai/claude-code@${r.harness_version}` : "claude";
  lines.push(`${agent}${model ? ` --model ${shellQuote(model)}` : ""}${r.effort ? ` --effort ${r.effort}` : ""} ${shellQuote(p.text)}`);
  if (undo.length) lines.push("# afterwards, back to the current skills:", ...undo);
  return lines.join("\n");
}

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

/** Replay it with Claude Code from here, and the replays launched so far. */
function replayHtml(p, models, defaultJudge = "") {
  const r = p.rerun;
  if (r.harness && r.harness !== "Claude Code") return "";
  const same = r.requested_model || Object.keys(p.models || {})[0] || "";
  const opt = (value, label, current) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
  const others = [...new Set([...models, ...p.references.map((x) => x.model).filter(Boolean)])].filter((m) => m !== same).sort();
  // releases to pin: the one this run used, and the reference runs' ones
  const releases = [];
  if (r.harness_version) releases.push([r.harness_version, "the release it ran with"]);
  for (const x of p.references) {
    if (x.harness_version && !releases.some(([v]) => v === x.harness_version)) releases.push([x.harness_version, `reference of ${x.tag}`]);
  }
  const restorable = r.skills.filter((k) => k.origin !== "bundled");
  // a budget cap well above what the run cost, so it only stops runaways
  const budget = Math.max(0.5, Math.ceil((p.total_cost_usd || 0) * 3 * 2) / 2);
  const replays = p.replays.map((x) => {
    const done = x.prompt;
    return `<li><span class="muted">${esc(fmtDate(x.launched_at))}</span> ${rerunStatusHtml(x, { details: false })}
      ${done ? `${judgeBadge(x.rating)} <a href="#/compare?a=${encodeURIComponent(p.id)}&b=${encodeURIComponent(done.id)}">${fmtCost(done.total_cost_usd)} · ${fmtMs(duration(done))} · compare</a> ${refDelta(done.total_cost_usd, p.total_cost_usd, { against: "original" })}`
        : x.status === "running" ? '<span class="muted">running in the background</span>'
        : '<span class="muted">not imported yet: press Refresh</span>'}</li>`;
  }).join("");
  return `<div class="replay">
    <div class="replay-head"><strong>Rerun with Claude</strong>
      <span class="muted">In <code>${esc(r.cwd || "your home folder")}</code>, with this prompt${restorable.length ? " and the skills as they ran" : ""}.
        <strong>Unattended</strong>: it runs in the background, anything that would need you is denied, and it stops to report what it needed.
        <strong>In a terminal</strong>: you watch it and approve its actions.
        ${p.references.length ? "<em>Settings from</em> copies a reference run's model, effort and release." : ""}</span></div>
    <form id="replay-form" class="replay-form">
      ${p.references.length ? `<label class="replay-from">Settings from <select id="replay-from">${opt("", "This run", "")}${p.references.map((x, i) =>
        opt(String(i), `Reference of ${x.tag} · ${fmtDate(x.started_at)} (${[x.model, x.effort && `effort ${x.effort}`, x.harness_version && `Claude Code ${x.harness_version}`].filter(Boolean).join(", ")})`, "")).join("")}</select></label>` : ""}
      <label>Model <select id="replay-model">${opt("", `Same (${same || "default"})`, "")}${others.map((m) => opt(m, m, "")).join("")}</select></label>
      <label>Effort <select id="replay-effort">${opt("", `Same (${r.effort || "default"})`, "")}${EFFORTS.map((e) => opt(e, e, "")).join("")}</select></label>
      <label title="Through npx for a release other than the installed one">Claude Code <select id="replay-release">${opt("", "Installed", "")}${releases.map(([v, why]) => opt(v, `${v} · ${why}`, "")).join("")}</select></label>
      <label>Run <select id="replay-mode">${opt("background", "Unattended, in the background", "background")}${opt("terminal", "In a terminal window", "")}</select></label>
      <label class="bg-only" title="Anything else it tries is denied, and reported">May <select id="replay-permission">${opt("ask", "only read", "ask")}${opt("edits", "read and edit files", "")}</select></label>
      <label class="bg-only" title="It stops when it has spent this much">Stop at $ <input id="replay-budget" type="number" min="0.1" step="0.5" value="${budget}" size="5"></label>
      ${restorable.length ? `<label class="replay-check" title="${esc(restorable.map((k) => k.skill).join(", "))}: rebuilt for this run only, your copies stay untouched">
        <input type="checkbox" id="replay-skills" checked> Restore ${restorable.length === 1 ? "the skill" : `the ${restorable.length} skills`} as ${restorable.length === 1 ? "it" : "they"} ran</label>` : ""}
      <label class="replay-check" title="Once imported, an LLM judge rates how much its output differs from this run's">
        <input type="checkbox" id="replay-rate" checked> Judge it with <select id="replay-judge">${opt("", `default (${defaultJudge || "Claude Code's"})`, "")}${[...new Set([same, ...models])].filter(Boolean).map((m) => opt(m, m, "")).join("")}</select></label>
      <span class="replay-buttons"><button type="button" class="btn" id="queue-replay" title="Keep it for later, to launch with others from the Reruns page">+ Add to queue</button>
        <button class="btn primary">↻ Rerun now</button></span>
    </form>
    <p id="replay-status" class="replay-status" hidden></p>
    ${replays ? `<div class="replays"><span class="muted">Reruns</span><ul>${replays}</ul></div>` : ""}
  </div>`;
}

function rerunSectionHtml(p, knownModels = [], defaultJudge = "") {
  const r = p.rerun;
  if (!r) return "";
  const models = Object.keys(p.models || {}).map((m) => m.replace("claude-", ""));
  const rows = [
    ["Agent", r.harness ? `${esc(r.harness)}${r.harness_version ? ` <code>${esc(r.harness_version)}</code>` : ""}` : '<span class="muted">unknown</span>'],
    ["Model", `${r.requested_model ? `<code>${esc(r.requested_model)}</code>` : ""}${models.length ? ` <span class="muted">answered by ${esc(models.join(", "))}</span>` : ""}${r.effort ? ` · effort ${esc(r.effort)}` : ""}`],
    ...r.skills.map((k) => [`Skill`, `<strong>${esc(k.skill)}</strong>${k.args ? ` <span class="muted">with “${esc(k.args.slice(0, 80))}${k.args.length > 80 ? "…" : ""}”</span>` : ""}
      <div class="skill-origin">${skillOrigin(k)}</div>
      <div class="skill-actions"><button type="button" class="link-btn" data-view-skill="${esc(k.hash)}">View it as it ran</button>
        <button type="button" class="link-btn" data-download-skill="${esc(k.hash)}">Download SKILL.md</button>
        <span class="muted" title="Hash of the instructions as loaded">#${esc(k.hash.slice(0, 7))}</span></div>`]),
  ];
  const command = r.harness === "Claude Code" ? rerunCommand(p) : "";
  return `<h2 class="page-sec">Run it again</h2>
    <div class="card rerun">
      <dl class="rerun-list">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${replayHtml(p, knownModels, defaultJudge)}
      ${command ? `<div class="rerun-cmd"><div class="rerun-cmd-head"><span>The same run, from a terminal</span>
          <button type="button" class="btn" id="copy-rerun">Copy</button></div><pre><code>${esc(command)}</code></pre></div>` : ""}
    </div>
    <dialog id="skill-dialog" class="tag-dialog skill-dialog"><div class="skill-dialog-body">
      <div class="rerun-cmd-head"><h2 id="skill-dialog-title"></h2><button type="button" class="btn" data-close-skill>Close</button></div>
      <p class="muted" id="skill-dialog-origin"></p><pre id="skill-dialog-text"></pre></div></dialog>`;
}

function bindRerun(p) {
  const form = document.getElementById("replay-form");
  const settings = () => ({
    model: document.getElementById("replay-model").value, effort: document.getElementById("replay-effort").value,
    version: document.getElementById("replay-release").value, mode: document.getElementById("replay-mode").value,
    permission: document.getElementById("replay-permission").value, budget_usd: document.getElementById("replay-budget").value,
    restore_skills: document.getElementById("replay-skills")?.checked ?? true,
    rate: document.getElementById("replay-rate").checked, judge_model: document.getElementById("replay-judge").value,
  });
  // permission and budget only apply to unattended reruns
  const mode = document.getElementById("replay-mode");
  if (mode) mode.onchange = () => form.querySelectorAll(".bg-only").forEach((el) => { el.hidden = mode.value !== "background"; });
  // a reference run's settings, copied into the menus (still editable)
  const from = document.getElementById("replay-from");
  if (from) from.onchange = () => {
    const x = p.references[Number(from.value)];
    const set = (id, value, sameValue) => {
      const select = document.getElementById(id);
      const v = !x || !value || value === sameValue ? "" : value;
      if (v && ![...select.options].some((o) => o.value === v)) select.add(new Option(v, v));
      select.value = v;
    };
    set("replay-model", x?.model, p.rerun.requested_model);
    set("replay-effort", x?.effort, p.rerun.effort);
    set("replay-release", x?.harness_version, null);
  };
  const queueButton = document.getElementById("queue-replay");
  if (queueButton) queueButton.onclick = async () => {
    const out = await api("/api/reruns/queue", { method: "POST", body: { prompt_ids: [p.id], ...settings() } });
    const status = document.getElementById("replay-status");
    status.innerHTML = `${out.added ? "Added to the queue" : "Already in the queue with these settings"}: <a href="#/reruns">${out.queued} queued on the Reruns page</a>.`;
    status.hidden = false;
  };
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    const status = document.getElementById("replay-status");
    const button = form.querySelector("button.primary");
    button.disabled = true;
    try {
      const out = await api(`/api/prompts/${encodeURIComponent(p.id)}/replay`, { method: "POST", body: settings() });
      const skills = out.skills.length ? ` With ${out.skills.map((k) => `${esc(k.skill)} @ ${esc(k.source)}`).join(", ")}.` : "";
      status.innerHTML = out.mode === "background"
        ? `Rerun started in the background.${skills} Follow it on the <a href="#/reruns">Reruns</a> page: it says when it is done, or what it needed.`
        : out.terminal
          ? `Claude Code opened in a new terminal window.${skills} Once it answers, the rerun shows up here (press Refresh).`
          : `No terminal window could be opened from here. Run this in a terminal: the rerun is linked to this prompt once imported.<pre><code>${esc(out.command)}</code></pre>`;
      status.hidden = false;
      if (out.mode === "background" || out.terminal) setTimeout(() => route(), 1500);
    } catch (err) {
      status.textContent = err.message; status.hidden = false;
    } finally { button.disabled = false; }
  };
  const copy = document.getElementById("copy-rerun");
  if (copy) copy.onclick = async () => {
    await navigator.clipboard.writeText(rerunCommand(p));
    copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  };
  const fetched = {};
  const skillText = async (hash) => (fetched[hash] ||= await api(`/api/skill-versions/${hash}`));
  app.querySelectorAll("[data-view-skill]").forEach((b) => b.onclick = async () => {
    const v = await skillText(b.dataset.viewSkill);
    document.getElementById("skill-dialog-title").textContent = v.skill;
    document.getElementById("skill-dialog-origin").innerHTML = skillOrigin(v);
    document.getElementById("skill-dialog-text").textContent = v.body;
    document.getElementById("skill-dialog").showModal();
  });
  app.querySelector("[data-close-skill]")?.addEventListener("click", () => document.getElementById("skill-dialog").close());
  app.querySelectorAll("[data-download-skill]").forEach((b) => b.onclick = async () => {
    const v = await skillText(b.dataset.downloadSkill);
    // the instructions as loaded: SKILL.md's frontmatter is not part of them, so a minimal one is put back
    const name = v.skill.split(":").pop();
    const file = new Blob([`---\nname: ${name}\n---\n${v.body}\n`], { type: "text/markdown" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(file), download: `${name}-${v.hash.slice(0, 7)}-SKILL.md` });
    a.click(); URL.revokeObjectURL(a.href);
  });
}

/** Line differences between two texts (longest common subsequence): ["=", line] kept, ["-", line] only in
    the first, ["+", line] only in the second. */
function lineDiff(a, b) {
  const x = a.split("\n").slice(0, 400), y = b.split("\n").slice(0, 400);
  const lcs = Array.from({ length: x.length + 1 }, () => new Array(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
    lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push(["=", x[i]]); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) out.push(["-", x[i++]]);
    else out.push(["+", y[j++]]);
  }
  while (i < x.length) out.push(["-", x[i++]]);
  while (j < y.length) out.push(["+", y[j++]]);
  return out;
}

/** The LLM judge's call on a run against another: a badge telling whether to look at it. */
function judgeBadge(r) {
  if (!r) return "";
  if (r.status === "rating") return '<span class="rerun-status status-running" title="The LLM judge is comparing the two outputs">⏳ Judging…</span>';
  if (r.status === "failed") return `<span class="rerun-status status-failed" title="${esc(r.error || "")}">✕ Judge failed</span>`;
  const [icon, label, cls] = r.look || r.verdict === "major" ? ["⚠", "Look at it", "status-failed"]
    : r.verdict === "minor" ? ["◐", "Minor differences", "status-needs"] : ["✓", "Good enough", "status-done"];
  const quality = { better: " · better", worse: " · worse" }[r.quality] || "";
  return `<span class="rerun-status ${cls}" title="${esc(r.summary || "")}">${icon} ${label} · ${r.score}${quality}</span>`;
}

/** The judge's rating in full, and a way to judge again with a model of your choice. */
function judgeCardHtml(r, models, defaultModel) {
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label)}</option>`;
  const current = r?.model || defaultModel || "";
  const list = [...new Set([current, ...models].filter(Boolean))];
  const body = !r ? '<p class="muted">Not judged yet.</p>'
    : r.status === "rating" ? `<p class="muted">${esc(r.model || "The judge")} is comparing the two outputs…</p>`
    : r.status === "failed" ? `<p class="status-warn">The judge failed: ${esc(r.error || "no reason given")}</p>`
    : `<div class="judge-score"><span class="big">${r.score}</span><span class="unit">/ 100 same substance</span> ${judgeBadge(r)}</div>
       <p class="judge-summary">${esc(r.summary)}</p>
       ${r.differences.length ? `<ul class="judge-diffs">${r.differences.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
       <p class="muted judge-meta">Judged by ${esc(r.model || "Claude Code's default model")} · ${esc(fmtDate(r.rated_at))}${r.cost_usd != null ? ` · ${fmtCost(r.cost_usd)}` : ""}</p>`;
  return `<div class="judge-card"><div class="judge-head"><strong>LLM judge</strong>
      <span class="muted">Says whether the second output keeps the first one's substance, so you know whether to read the differences.</span></div>
    ${body}
    <form class="judge-form" id="judge-form"><label>Judge with <select id="judge-model">${opt("", `Default (${defaultModel || "Claude Code's"})`, "")}${list.map((m) => opt(m, m, "")).join("")}</select></label>
      <button class="btn">${r && r.status !== "failed" ? "Judge again" : "Judge them"}</button></form></div>`;
}

/** Two runs side by side (`a` first): figures, files edited, and their final answers. */
function compareRunsHtml(a, b, labels = ["Original", "Rerun"]) {
  const rows = [
    ["Cost", a.total_cost_usd, b.total_cost_usd, fmtCost], ["Tokens", a.total_tokens, b.total_tokens, fmtTokens],
    ["Active time", a.active_ms ?? a.wall_ms, b.active_ms ?? b.wall_ms, fmtMs], ["API calls", a.api_calls, b.api_calls, String],
    ["Tool calls", a.tool_calls, b.tool_calls, String],
  ];
  const before = new Set(a.files || []), after = new Set(b.files || []);
  const files = [...new Set([...before, ...after])].sort().map((f) => {
    const where = before.has(f) && after.has(f) ? ["both", "both"] : before.has(f) ? ["before", `only the ${labels[0].toLowerCase()}`] : ["after", `only the ${labels[1].toLowerCase()}`];
    return `<li class="file-${where[0]}"><code>${esc(f)}</code> <span class="muted">${where[1]}</span></li>`;
  }).join("");
  const same = (a.answer || "").trim() === (b.answer || "").trim();
  return `<table class="compare-figures"><thead><tr><th></th><th class="num">${esc(labels[0])}</th><th class="num">${esc(labels[1])}</th><th class="num">Change</th></tr></thead>
      <tbody>${rows.map(([label, x, y, fmt]) => `<tr><td>${label}</td><td class="num">${x == null ? "–" : fmt(x)}</td><td class="num">${y == null ? "–" : fmt(y)}</td>
        <td class="num">${x && y != null ? refDelta(y, x, { against: "" }) : ""}</td></tr>`).join("")}</tbody></table>
    <div class="compare-files"><strong>Files edited</strong>${files ? `<ul>${files}</ul>` : '<p class="muted">Neither run edited a file.</p>'}</div>
    <div class="compare-answers">
      <div class="compare-answers-head"><strong>Final answers</strong>
        ${same ? '<span class="trend down">identical</span>' : '<button type="button" class="link-btn" id="toggle-diff">Show differences</button>'}</div>
      <div class="answers" id="answers-side">
        <div><span class="muted">${esc(labels[0])}</span><pre>${esc(a.answer || "(no answer recorded: re-import its transcript)")}</pre></div>
        <div><span class="muted">${esc(labels[1])}</span><pre>${esc(b.answer || "(no answer yet)")}</pre></div>
      </div>
      <pre class="answer-diff" id="answers-diff" hidden>${lineDiff(a.answer || "", b.answer || "").map(([op, line]) =>
        `<span class="diff-${op === "=" ? "same" : op === "-" ? "del" : "add"}">${op === "=" ? " " : op} ${esc(line) || " "}</span>`).join("")}</pre>
    </div>`;
}

/** Binds what compareRunsHtml and judgeCardHtml draw: the differences switch and the judge form. */
function bindCompare(base, other, after) {
  const toggleDiff = document.getElementById("toggle-diff");
  if (toggleDiff) toggleDiff.onclick = () => {
    const diff = document.getElementById("answers-diff"), side = document.getElementById("answers-side");
    diff.hidden = !diff.hidden; side.hidden = !diff.hidden;
    toggleDiff.textContent = diff.hidden ? "Show differences" : "Show side by side";
  };
  const form = document.getElementById("judge-form");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    form.querySelector("button").disabled = true;
    try {
      await api("/api/ratings", { method: "POST", body: { base, other, model: document.getElementById("judge-model").value, force: true } });
    } catch (err) { alert(err.message); }
    after();
  };
}

/** While the judge works, look again in a few seconds (only while the same page is shown). */
function pollJudge(rating, again) {
  if (rating?.status !== "rating") return;
  const here = location.hash;
  setTimeout(() => { if (location.hash === here) again(); }, 3000);
}

/** A rerun against its original: the judge, figures, files edited, and their final answers. */
function compareOriginalHtml(p, models, defaultJudge) {
  const o = p.replay_of;
  const rerun = { ...p, answer: p.answer || o.result || "" };
  return `<h2 class="page-sec" id="compare-original">Compared with the original <a class="link-btn" href="#/compare?a=${encodeURIComponent(o.id)}&b=${encodeURIComponent(p.id)}">Open in Compare</a></h2>
    <div class="card compare-original">
      ${judgeCardHtml(o.rating, models, defaultJudge)}
      ${compareRunsHtml(o, rerun)}
    </div>`;
}

/** On a rerun's page: what it reruns, against it, and the configuration chosen when launching it. */
function rerunOfHtml(p) {
  const o = p.replay_of;
  const original = o.id
    ? `<a href="#/prompt/${encodeURIComponent(o.id)}">the run of ${esc(fmtDate(o.started_at))}</a> (${fmtCost(o.total_cost_usd)})`
    : "a prompt no longer in the history";
  return `<div class="rerun-of">
    <span class="ref-label">↻ Rerun</span>
    ${rerunStatusHtml(o)}${o.first ? judgeBadge(o.rating) : ""}
    <span>${o.first ? `Rerun of ${original} ${o.id ? refDelta(p.total_cost_usd, o.total_cost_usd, { against: "original" }) : ""}` : `Follow-up in the rerun of ${original}`}</span>
    <span class="rerun-config"><span class="muted">launched ${esc(fmtDate(o.launched_at))} with</span> ${rerunSettings(o)}</span>
    <a class="link-btn" href="#/tag/rerun">All reruns</a>
  </div>`;
}

// how a prompt page splits its history ("" or a SPLITS key): kept while moving from prompt to prompt
let promptSplit = "";

async function viewPrompt(id, compare = {}) {
  let [p, allTags, settings] = await Promise.all([api(`/api/prompts/${encodeURIComponent(id)}`), api("/api/tags"), api("/api/settings")]);
  const knownModels = allTags.filter((t) => t.tag.startsWith("model:")).map((t) => `claude-${t.tag.slice("model:".length)}`);
  ruleSkills = skillsFromTags(allTags);
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
  // lower than the group is better for every compared figure
  const delta = (value, r) => {
    const text = deltaText(value, r);
    return text ? `<span class="${value <= r ? "delta-good" : "delta-bad"}">${value <= r ? "▼" : "▲"} ${text}</span>` : "";
  };
  // compared with a tag that has a reference run: this run against it (unless it is the reference)
  const refRun = p.compare.reference && p.compare.reference.id !== p.id ? p.compare.reference : null;
  const vsRef = (metric) => (refRun ? `<br>${refDelta(metric(p), metric(refRun), { long: true })}` : "");
  const facts = [
    ["Total cost", fmtCost(p.total_cost_usd), delta(p.total_cost_usd, ref.cost) + vsRef((x) => x.total_cost_usd), costNote(p)],
    ["Total tokens", fmtTokens(p.total_tokens), delta(p.total_tokens, ref.tokens) + vsRef((x) => x.total_tokens), "incl. cache and subagents"],
    ["Active time", fmtMs(p.active_ms), delta(duration(p), ref.dur) + vsRef(duration), `wall ${fmtMs(p.wall_ms)}`],
    ["API calls", p.api_calls, `${p.tool_calls} tool calls`, p.api_ms != null ? `API time ${fmtMs(p.api_ms)}` : ""],
    ["Subagents", p.agent_count, p.agent_count ? `${fmtCost(p.agent_cost_usd)} · ${fmtTokens(p.agent_tokens)} tok` : "none", ""],
  ];
  const tagLink = (prefix) => p.tags.filter((t) => t.tag.startsWith(`${prefix}:`));
  const [project] = tagLink("project"), models = tagLink("model");
  const linkCard = (label, title, text, href, attrs = "") => `<a class="page-card link-card" href="${href}" ${attrs}>
      <span class="hl-label">${label}</span><strong class="lc-title">${title}</strong><span class="pc-text">${text}</span></a>`;
  const links = [
    linkCard("Session", esc(p.session.title || p.session_id.slice(0, 8)),
      `${p.session.reported_cost_usd != null ? `${fmtCost(p.session.reported_cost_usd)} in total · ` : ""}every prompt of this conversation`,
      `#/session/${encodeURIComponent(p.session_id)}`),
    project ? linkCard("Project", esc(project.tag.slice("project:".length)), `${p.git_branch ? `branch ${esc(p.git_branch)} · ` : ""}every run in this repository`,
      `#/tag/${encodeURIComponent(project.tag)}`) : "",
    models.length ? linkCard(models.length > 1 ? "Models" : "Model", esc(models.map((t) => t.tag.slice("model:".length)).join(", ")),
      "cost and time of every prompt it answered", `#/tag/${encodeURIComponent(models[0].tag)}`) : "",
    linkCard("Compare", `${sim.length} ${esc(what)}`, sim.length ? "how this run evolves against them, below" : "rerun it, or tag it, to compare",
      "#", 'data-jump="compare-sec"'),
  ].join("");
  // the charts only use this prompt's history: it and the prompts the menu compares it with
  const history = [p, ...sim];
  const empty = p.compare.scope === "similar"
    ? `No similar prompt yet. Similarity uses word overlap (threshold on the Setup page) or the same slash command. To group prompts explicitly, give them the same tag of yours and compare with all prompts tagged with it.`
    : `No other prompt in ${esc(what)}.`;
  const unpriced = (p.unpriced_models || []).length
    ? `<div class="card status-warn" style="margin-top:8px">No price configured for ${esc(p.unpriced_models.join(", "))}: their cost is counted as 0. Add them on the <a href="#/setup">Setup</a> page.</div>` : "";
  app.innerHTML = `
    <div class="prompt-page">
    <a class="crumb" href="#/prompts">← All prompts</a>
    <header class="prompt-head">
      ${pageKicker("Prompt", "What this prompt cost, where the money went, and how it compares with its reruns.")}
      ${p.replay_of ? rerunOfHtml(p) : ""}
      <div class="prompt-meta"><time datetime="${esc(p.started_at)}">${fmtDate(p.started_at)}</time>${p.project ? `<span>${esc(p.project)}</span>` : ""}
        ${p.command ? `<span class="pcmd">/${esc(p.command)}</span>` : ""}<span class="prompt-cost">${fmtCost(p.total_cost_usd)}</span></div>
      <pre class="prompt-text">${esc(p.text)}</pre>
      <div class="tag-groups">
        <div><span class="tag-kind">Your tags</span><span id="tags">${tagChips(humanTags(p.tags), { removable: true })}</span>
          <button type="button" class="btn add-tag-btn" data-new-tag>+ Add a tag</button></div>
        <div><span class="tag-kind">AI tags</span>${tagChips(aiTags(p.tags)) || '<span class="muted">none</span>'}</div>
      </div>
    </header>
    <nav class="page-cards link-cards" aria-label="Related pages">${links}</nav>
    ${unpriced}
    <div class="tags-hero prompt-facts">${facts.map(([label, value, sub, note]) => `<div class="hero-fact"><span class="label">${label}</span>
      <span class="value">${value}</span><span class="sub">${sub || "&nbsp;"}</span>${note ? `<span class="sub">${note}</span>` : ""}</div>`).join("")}</div>
    ${p.replay_of?.first && p.replay_of.id ? compareOriginalHtml(p, knownModels, settings.judge_model) : ""}
    ${rerunSectionHtml(p, knownModels, settings.judge_model)}

    <h2 class="page-sec">Cost per agent</h2>
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

    <h2 class="page-sec" id="compare-sec">How it compares with ${compareSelect(p)} <span class="muted">(${sim.length} prompts)</span></h2>
    ${p.compare.tag ? `<p class="ref-hint">${p.compare.reference
      ? (p.compare.reference.id === p.id
        ? `◆ This run is the reference of <strong>${esc(p.compare.tag)}</strong>.`
        : `◆ Reference of <strong>${esc(p.compare.tag)}</strong>: <a href="#/prompt/${encodeURIComponent(p.compare.reference.id)}">${esc(fmtDate(p.compare.reference.started_at))} · ${fmtCost(p.compare.reference.total_cost_usd)}</a>.`)
      : `<span class="muted"><strong>${esc(p.compare.tag)}</strong> has no reference run yet.</span>`}
      ${p.compare.reference?.id === p.id ? "" : `<button type="button" class="link-btn" id="make-reference">Make this run the reference</button>`}</p>` : ""}
    ${sim.length ? `<div class="compare-picker split-picker" role="group" aria-label="Split by"><span class="muted">One line per</span>
        ${Object.entries(SPLITS).map(([key, label]) => `<button type="button" class="series-chip" data-split="${key}">${label}</button>`).join("")}</div>
      <div class="legend" id="cmp-legend"></div>
      <div class="charts">${[["cost", "Cost (USD, incl. subagents)"], ["dur", "Duration (active time, else wall time)"], ["tok", "Tokens (incl. cache, incl. subagents)"]]
        .map(([id, title]) => `<div class="card"><div class="chart-title">${title}</div><div class="chart-box"><canvas id="cmp-${id}"></canvas></div></div>`).join("")}</div>
      <div id="cmp-summary"></div>
      <h2 class="page-sec">The runs</h2>` : ""}
    ${sim.length ? promptTable([{ ...p, score: null }, ...sim], { currentId: p.id, showScore: true }) : `<div class="card muted">${empty}</div>`}
    </div>
    ${tagDialogHtml({ forPrompts: true })}`;
  document.getElementById("compare").onchange = (e) => { location.hash = promptHash(JSON.parse(e.target.value)); };
  // the hash routes pages: the compare card scrolls instead of navigating
  app.querySelector('[data-jump="compare-sec"]').onclick = (e) => {
    e.preventDefault();
    document.getElementById("compare-sec").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };
  // the charts: the history, as one line or one per model (skill, project, tag of yours) among it
  const drawCompare = () => {
    if (!sim.length) return;
    destroyCharts();
    const groups = splitRuns(history, promptSplit, p.compare.tag || "");
    app.querySelectorAll("[data-split]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.split === promptSplit)));
    document.getElementById("cmp-legend").innerHTML = `<span><i style="background:var(--series-2)"></i>This prompt</span>${refRun ? '<span><i class="ref-swatch"></i>Reference run</span>' : ""}${groups.map((g) =>
      `<span><i style="background:var(${g.color})"></i>${esc(g.label === "All runs" ? `This prompt and ${what}` : g.label)} <span class="mi-count">${g.points.length}</span></span>`).join("")}`;
    document.getElementById("cmp-summary").innerHTML = splitSummaryHtml(groups);
    [["cost", (x) => x.total_cost_usd, fmtCost], ["dur", duration, fmtMs], ["tok", (x) => x.total_tokens, fmtTokens]]
      .forEach(([id, metric, fmt]) => compareChart(document.getElementById(`cmp-${id}`), groups, p, metric, fmt, refRun));
  };
  app.querySelectorAll("[data-split]").forEach((b) => b.onclick = () => { promptSplit = b.dataset.split; drawCompare(); });
  bindRerun(p);
  if (p.replay_of?.first && p.replay_of.id) {
    bindCompare(p.replay_of.id, p.id, () => route());
    pollJudge(p.replay_of.rating, () => route());
  }
  const makeRef = document.getElementById("make-reference");
  if (makeRef) makeRef.onclick = async () => {
    await api(`/api/tags/${encodeURIComponent(p.compare.tag)}/reference`, { method: "PUT", body: { prompt_id: p.id } });
    route();
  };
  drawCompare();
  bindRowClicks();
  document.querySelectorAll("[data-remove-tag]").forEach((b) => b.onclick = async () => {
    const tag = b.dataset.removeTag;
    await api(`/api/prompts/${encodeURIComponent(p.id)}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" });
    // the comparison based on the removed tag no longer applies
    const c = p.compare.tag === tag ? {} : p.compare;
    if (location.hash === promptHash(c)) route(); else location.hash = promptHash(c);
  });
  bindTagDialog(app.querySelector(".prompt-page"), {
    tags: allTags, targets: () => [p.id], exclude: p.tags.map((t) => t.tag),
    title: () => "Add a tag to this prompt", done: () => route(), // the new tag adds comparison choices
  });
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
/** Median cost of the last runs (up to 3) against the ones before, in percent; null under 4 runs. */
function trendPct(spark) {
  if (spark.length < 4) return null;
  const half = Math.min(3, Math.floor(spark.length / 2));
  const recent = median(spark.slice(-half));
  const before = median(spark.slice(-2 * half, -half));
  return before ? Math.round(((recent - before) / before) * 100) : null;
}

function trendBadge(spark) {
  const pct = trendPct(spark);
  if (pct == null) return "";
  const half = Math.min(3, Math.floor(spark.length / 2));
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
    ${t.description ? `<p class="tag-desc">${esc(t.description)}</p>` : ""}
    ${t.prompts ? `<div class="tag-metric"><span class="big">${fmtCost(t.avg_cost_usd)}</span><span class="unit">per prompt</span>${trendBadge(t.spark)}</div>
    ${sparkline(t.spark)}` : `<div class="spark-empty">No prompt yet: give it keywords on its page, or add it to a prompt</div>`}
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
    if (!inCat.length && cat === "human" && !tagsState.q && (!tagsState.filter.size || tagsState.filter.has("human"))) {
      return `<section class="tag-section cat-human" aria-labelledby="sec-human">
        <h2 id="sec-human"><span class="tag-dot"></span>${label}<span class="sec-meta">no tag yet</span></h2>
        <div class="human-empty"><strong>Group the runs of one task under your own tag.</strong>
          <span>Open any prompt and add a tag such as <code>task:review-branch</code>. Give it keywords and every matching prompt, past and future, gets it too.
          Its page then shows whether each rerun got cheaper or faster.</span>
          <span class="human-actions"><button class="btn primary" type="button" data-new-tag>+ New tag</button><a class="btn" href="#/prompts">Pick a prompt →</a></span></div></section>`;
    }
    if (!inCat.length) return "";
    // a big category splits into subgroups; a small one stays a single grid
    const split = inCat.length > 8;
    const cost = inCat.reduce((sum, t) => sum + (t.cost_usd || 0), 0);
    const subtypes = [...new Set(inCat.map(tagSubtype))].sort((a, b) => a.localeCompare(b));
    return `<section class="tag-section cat-${cat}" aria-labelledby="sec-${cat}">
      <h2 id="sec-${cat}"><span class="tag-dot"></span>${label}<span class="sec-meta">${inCat.length} tag${inCat.length === 1 ? "" : "s"} · ${fmtCost(cost)}</span></h2>
      <p class="sec-help">${esc(help[0].toUpperCase() + help.slice(1))}</p>
      ${subtypes.map((sub) => {
        const inSub = inCat.filter((t) => !split || tagSubtype(t) === sub).sort(sort);
        if (!split && sub !== subtypes[0]) return "";
        return `${sub && subtypes.length > 1 && inCat.length > 8 ? `<h3 class="subtype">${esc(sub)} <span>${inSub.length}</span></h3>` : ""}${cardGrid(inSub)}`;
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
  const fact = (label, value, sub) => `<div class="hero-fact"><span class="label">${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></div>`;
  // the tags whose last runs moved the most, either way: did the prompt changes pay off?
  const trends = all.map((t) => ({ t, pct: trendPct(t.spark) })).filter((x) => x.pct != null && Math.abs(x.pct) >= 5);
  const cheaper = trends.filter((x) => x.pct < 0).sort((a, b) => a.pct - b.pct)[0];
  const pricier = trends.filter((x) => x.pct > 0).sort((a, b) => b.pct - a.pct)[0];
  const trendFact = (label, x, word) => x
    ? `<a class="hero-fact" href="#/tag/${encodeURIComponent(x.t.tag)}"><span class="label">${label}</span><span class="value ${x.pct < 0 ? "delta-good" : "delta-bad"}">${x.pct < 0 ? "▼" : "▲"} ${Math.abs(x.pct)}%</span>
        <span class="sub">${esc(x.t.tag)}</span></a>`
    : fact(label, "–", `no tag ${word} over its last runs`);
  const card = (label, t, value, unit) => highlightCard(label, value, unit,
    `<span class="hl-text"><span class="chip cat-${tagCategory(t)} hl-chip">${esc(t.tag)}</span></span>
     <span class="pmeta">${plural(t.prompts, "prompt")} · ${fmtCost(t.cost_usd)} total · ${fmtCost(t.avg_cost_usd)} each · last ${esc(fmtAgo(t.last_used))}</span>`,
    `#/tag/${encodeURIComponent(t.tag)}`, "Open the tag");
  // models tag nearly every prompt: leaving them out keeps "most" meaningful
  const ranked = (sort, min = 1) => [...all].filter((t) => tagCategory(t) !== "model" && t.prompts >= min).sort(sort)[0];
  const total = ranked(TAG_SORTS.total[1]), pricey = ranked(TAG_SORTS.avg[1], 2), busiest = ranked(TAG_SORTS.prompts[1]);
  return `<div class="tags-hero">
      ${fact("Tags", all.length, `${all.length - human.length} derived by the agent`)}
      ${fact("Yours", human.length, human.length ? `${human.reduce((n, t) => n + t.prompts, 0)} tagged prompts` : "add one on any prompt page")}
      ${trendFact("Getting cheaper", cheaper, "got cheaper")}
      ${trendFact("Getting pricier", pricier, "got pricier")}
    </div>
    <div class="highlights">
      ${total ? card("Most expensive in total", total, fmtCost(total.cost_usd), `over ${plural(total.prompts, "prompt")}`) : ""}
      ${pricey ? card("Priciest per prompt", pricey, fmtCost(pricey.avg_cost_usd), "per prompt") : ""}
      ${busiest ? card("Most used", busiest, busiest.prompts, "prompts") : ""}
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
  panel.innerHTML = `<div class="panel-head">${tagChips([t])}<span class="muted">${tagNoun(cat)}</span><span class="panel-spacer"></span>
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

/** The tag form: a new tag (Tags page), or one of your tags or a new one given to some prompts
    (`forPrompts`: a prompt page, or the prompts selected on a tag page). */
function tagDialogHtml({ forPrompts = false } = {}) {
  return `<dialog id="new-tag-dialog" class="tag-dialog" aria-labelledby="new-tag-title">
      <form id="new-tag-form" method="dialog">
        <h2 id="new-tag-title">New tag</h2>
        ${forPrompts ? '<div class="field" id="nt-pick-field"><span>Your tags</span><div class="nt-pick" id="nt-pick"></div></div>' : ""}
        <label class="field"><span>${forPrompts ? "Or a new tag" : "Name"}</span>
          <input id="nt-name" required placeholder="task:review-branch" autocomplete="off" list="nt-known">
          <datalist id="nt-known"></datalist>
          <small id="nt-name-help">A prefix such as <code>task:</code> groups related tags into a section.</small></label>
        <div class="nt-new-only">
          <label class="field"><span>Description <em>optional</em></span>
            <textarea id="nt-desc" rows="2" placeholder="What this task is, so you recognise it later"></textarea></label>
          <div class="field"><span>Rules <em>optional</em></span>
            <div class="rules-editor" id="nt-rules"></div>
            <button type="button" class="link-btn add-rule-row" id="nt-add-rule">+ Add a rule</button>
            <small>A rule is one keyword, or several together (“any of them” becomes one rule per keyword), or a <strong>skill</strong> the agent used.
              Every prompt, past and future, matching <strong>any</strong> rule gets the tag. “In a session” tags every prompt of a session whose title or prompts match.
              ${forPrompts ? "The chosen prompts get the tag either way." : "Without rules, add the tag to prompts by hand."}</small></div>
        </div>
        <p id="nt-error" class="status-warn" hidden></p>
        <div class="dialog-actions"><button class="btn" type="button" data-close-dialog>Cancel</button><button class="btn primary" id="nt-submit">Create tag</button></div>
      </form>
    </dialog>`;
}

/** Opens from any [data-new-tag] inside `page`. `tags`: every tag, to recognise yours. With `targets` (a function
    giving prompt ids), the chosen tag is given to those prompts; `exclude`: tags not offered; `title(n)`: the heading. */
function bindTagDialog(page, { tags, targets = null, exclude = [], title = () => "New tag", done = null }) {
  const dialog = document.getElementById("new-tag-dialog");
  const form = document.getElementById("new-tag-form");
  const name = document.getElementById("nt-name");
  const error = document.getElementById("nt-error");
  const submit = document.getElementById("nt-submit");
  const editor = bindRulesEditor(document.getElementById("nt-rules"), document.getElementById("nt-add-rule"));
  const yours = tags.filter((t) => t.kind === "human").map((t) => t.tag);
  const pickable = yours.filter((t) => !exclude.includes(t)).sort();
  document.getElementById("nt-known").innerHTML = (targets ? pickable : []).map((t) => `<option value="${esc(t)}">`).join("");
  // an existing tag of yours is only given to the prompts: its description and rules live on its page
  const sync = () => {
    const tag = name.value.trim();
    const existing = yours.includes(tag);
    form.querySelector(".nt-new-only").hidden = existing;
    document.getElementById("nt-name-help").innerHTML = existing
      ? (targets ? `Your tag <strong>${esc(tag)}</strong>: the prompts are added to it. Change its description and rules on its page.` : `A tag named <strong>${esc(tag)}</strong> already exists.`)
      : "A prefix such as <code>task:</code> groups related tags into a section.";
    submit.textContent = targets ? (existing ? "Add tag" : "Create and add") : "Create tag";
    submit.disabled = existing && !targets;
    form.querySelectorAll("[data-pick]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pick === tag)));
  };
  name.addEventListener("input", sync);
  if (targets) {
    const pick = document.getElementById("nt-pick");
    pick.innerHTML = pickable.length
      ? pickable.map((t) => `<button type="button" class="chip cat-human" data-pick="${esc(t)}" aria-pressed="false">${esc(t)}</button>`).join("")
      : `<span class="muted">${yours.length ? "These prompts already have all your tags." : "None yet: create your first one below."}</span>`;
    pick.onclick = (e) => {
      const b = e.target.closest("[data-pick]");
      if (b) { name.value = b.dataset.pick; sync(); }
    };
  }
  page.addEventListener("click", (e) => {
    if (!e.target.closest("[data-new-tag]")) return;
    form.reset(); error.hidden = true;
    editor.reset(); sync();
    document.getElementById("new-tag-title").textContent = title(targets ? targets().length : 0);
    dialog.showModal();
  });
  dialog.querySelector("[data-close-dialog]").onclick = () => dialog.close();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const tag = name.value.trim();
    if (!tag) return;
    try {
      if (!yours.includes(tag)) {
        await api("/api/tags", { method: "POST", body: { tag, description: document.getElementById("nt-desc").value, rules: editor.read() } });
      }
      if (targets) await api(`/api/tags/${encodeURIComponent(tag)}/prompts`, { method: "POST", body: { prompt_ids: targets() } });
    } catch (err) {
      // an AI tag's name is taken too
      error.textContent = err.message.startsWith("409") ? `A tag named ${tag} already exists.` : err.message;
      error.hidden = false;
      return;
    }
    dialog.close();
    if (done) done(tag);
    else location.hash = `#/tag/${encodeURIComponent(tag)}`;
  };
}

async function viewTags() {
  tagsState.all = await api("/api/tags");
  ruleSkills = skillsFromTags(tagsState.all);
  app.innerHTML = `
    <div class="tags-page">
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Tags · <span id="tags-count"></span></span>Follow a task across every rerun. <em>Did it get cheaper?</em></h1>
          <p>Tags group prompts by <strong>task</strong>, <strong>project</strong>, <strong>model</strong> or <strong>skill</strong>, each with its cost per prompt and trend.
            Yours group the runs of one task; the others are derived from what the agent did. Open one for its runs and how they evolve.</p>
        </div>
      </header>
      ${tagsHero(tagsState.all)}
      <search class="tags-toolbar">
        <label class="search-box"><span aria-hidden="true">⌕</span>
          <input type="search" id="tags-search" placeholder="Search tags…" value="${esc(tagsState.q)}" aria-label="Search tags"></label>
        <div class="tag-legend" role="group" aria-label="Categories">${TAG_CATEGORIES.map(([cat, label, help]) =>
          `<button type="button" class="chip cat-${cat}" data-cat="${cat}" title="${esc(help)}">${label}</button>`).join("")}</div>
        <span class="toolbar-spacer"></span>
        <button class="btn primary" type="button" data-new-tag>+ New tag</button>
        <label id="tags-sort" class="sort-box">Sort
          <select id="tags-sort-select">${Object.entries(TAG_SORTS).map(([key, [label]]) =>
            `<option value="${key}" ${key === tagsState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <div class="segmented" role="group" aria-label="Layout">
          <button type="button" data-view="grouped">Grouped</button>
          <button type="button" data-view="timeline">Timeline</button>
        </div>
      </search>
      <div id="tag-results"></div>
    </div>
    ${tagDialogHtml()}`;
  bindTagDialog(app.querySelector(".tags-page"), { tags: tagsState.all });
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
}

function tagHeadHtml(info, rules) {
  const cat = tagCategory(info);
  const [prefix, ...rest] = info.tag.split(":");
  const name = rest.length ? `<span class="tag-prefix">${esc(prefix)}:</span>${esc(rest.join(":"))}` : esc(info.tag);
  const help = CATEGORY_INFO[cat].help;
  const meta = info.prompts ? [
    `Created ${esc(fmtDate(info.created_at))}`,
    `last used <span title="${esc(fmtDate(info.last_used))}">${fmtAgo(info.last_used)}</span>`,
  ] : ["No prompt carries this tag yet"];
  const rulesHtml = cat !== "human" ? "" : `<section class="rules-card" aria-labelledby="rules-title">
      <div class="rules-head"><h2 id="rules-title">Auto-assign</h2>
        <span class="muted">${rules.length ? "Every prompt (or session), past and future, matching one of these rules gets the tag:" : "No rule yet: only prompts you tagged by hand carry it."}</span></div>
      ${rules.length ? `<div class="rule-chips">${rules.map((r, i) => `<span class="rule-chip" style="--rule-color:${ruleColor(i)}" data-rule-pos="${i}">
        <button type="button" class="rule-edit" data-edit-rule="${i}" title="Change this rule"><span class="rule-dot" aria-hidden="true"></span>${ruleText(r)}</button>
        <button type="button" class="rule-x" data-delete-rule="${r.index}" title="Stop assigning the tag with these keywords" aria-label="Remove this rule">✕</button></span>`).join("")}</div>` : ""}
      <form id="rule-edit-form" class="rule-edit-form" hidden><div class="rules-editor"></div>
        <div class="rule-edit-actions"><button class="btn primary">Save</button><button type="button" class="btn" data-cancel-rule>Cancel</button></div></form>
      <button type="button" class="btn add-rule-btn" data-add-keywords>+ Add a rule</button>
    </section>`;
  return `<header class="tag-head cat-${cat}">
      ${pageKicker("Tag", cat === "human"
        ? "Every run of this task: is it getting cheaper or faster? Select runs to regroup them under another tag."
        : "Every run where this applied: how its cost and time evolve. Select runs to group them under a tag of yours.")}
      <div class="tag-head-title"><span class="tag-dot"></span><h1>${name}</h1>
        ${cat === "human" ? '<span class="by-you">yours</span>' : `<span class="by-ai">${CATEGORY_INFO[cat].label}</span>`}</div>
      ${cat === "human"
        ? `<div class="tag-desc-edit"><p class="tag-head-desc" id="tag-desc">${info.description ? esc(info.description) : '<span class="muted">No description yet.</span>'}</p>
            <button type="button" class="link-btn" id="edit-desc">${info.description ? "Edit" : "Add a description"}</button></div>
          <form id="desc-form" class="inline-form desc-form" hidden><textarea id="desc-input" rows="2" placeholder="What this task is">${esc(info.description || "")}</textarea>
            <button class="btn primary">Save</button><button type="button" class="btn" id="desc-cancel">Cancel</button></form>`
        : `<p class="tag-head-sub">${esc(help[0].toUpperCase() + help.slice(1))}</p>`}
      <div class="tag-head-meta">${meta.map((m) => `<span>${m}</span>`).join("")}
        ${cat === "human" ? '<button type="button" class="link-btn danger-link" id="delete-tag" title="Remove this tag from every prompt, with its rules">Delete tag</button>' : ""}</div>
    </header>${rulesHtml}`;
}

function tagSummaryHtml(items, s) {
  const costs = [...items].reverse().map((p) => p.total_cost_usd || 0);
  const fact = (label, value, sub = "") => `<div class="hero-fact"><span class="label">${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></div>`;
  return `<div class="tags-hero">
    ${fact("Runs", s.count || 0, RANGES[listState.range][0].toLowerCase())}
    ${fact("Total cost", fmtCost(s.cost_usd || 0), `${fmtTokens(s.tokens || 0)} tokens`)}
    ${fact("Median cost", fmtCost(median(costs)), trendBadge(costs) || "per run")}
    ${fact("Median time", fmtMs(median(items.map(duration))), "per run, active time")}
    ${listState.reference ? fact("Median vs reference", refDelta(median(costs), listState.reference.total_cost_usd, { long: true }) || "–",
      `reference run ${fmtCost(listState.reference.total_cost_usd)}`) : ""}
  </div>`;
}

/** The tag's reference run, or how to pick one. */
function referenceCardHtml() {
  const r = listState.reference;
  if (!r) return `<p class="ref-hint muted">No reference run yet: click ☆ on a run below to compare every other run with it.</p>`;
  return `<div class="ref-card"><span class="ref-label">◆ Reference run</span>
    <a class="ref-text" href="#/prompt/${encodeURIComponent(r.id)}">${esc(r.text)}</a>
    <span class="pmeta">${fmtDate(r.started_at)} · ${fmtCost(r.total_cost_usd)} · ${fmtTokens(r.total_tokens)} tok · ${fmtMs(duration(r))}</span>
    <button type="button" class="link-btn" data-ref="${esc(r.id)}">Clear</button></div>`;
}

async function viewTag(tag, query = new URLSearchParams()) {
  filtersFromQuery(query);
  Object.assign(listState, { lockTag: tag, tag, split: query.get("split") in SPLITS ? query.get("split") : "" });
  const enc = encodeURIComponent(tag);
  const [projects, tags, rules, { reference }] = await Promise.all([api("/api/projects"), api("/api/tags"), api(`/api/tags/${enc}/rules`), api(`/api/tags/${enc}/reference`)]);
  listState.reference = reference;
  promptProjects = projects;
  listState.tags = tags;
  ruleSkills = skillsFromTags(tags);
  const info = tags.find((t) => t.tag === tag) || { tag, kind: "ai" };
  app.innerHTML = `
    <div class="prompts-page tag-page">
      <a class="crumb" href="#/tags">← All tags</a>
      ${tagHeadHtml(info, rules)}
      <div id="prompt-summary"></div>
      <div id="prompt-highlights"></div>
      <div id="tag-reference"></div>
      <section class="tag-evolution" aria-labelledby="evo-title"><h2 id="evo-title" class="page-sec">How it evolves</h2><div id="tag-charts"></div></section>
      <h2 class="page-sec">Runs</h2>
      <div id="prompt-toolbar"></div>
      <div id="select-bar"></div>
      <div id="active-filters"></div>
      <div id="prompt-results"></div>
    </div>
    ${tagDialogHtml({ forPrompts: true })}`;
  Object.assign(listState, { selecting: false, selected: new Set() });
  bindSelection(app.querySelector(".tag-page"), { tags, exclude: [tag] });
  // ☆ on a run makes it the reference, ★ (or Clear on the card) clears it
  app.querySelector(".tag-page").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ref]");
    if (!b) return;
    e.preventDefault();
    const id = listState.reference?.id === b.dataset.ref ? null : b.dataset.ref;
    listState.reference = (await api(`/api/tags/${enc}/reference`, { method: "PUT", body: { prompt_id: id } })).reference;
    renderPrompts({ reload: false });
  });
  rerenderToolbar();
  await renderPrompts();
  if (tagCategory(info) !== "human") return;
  document.getElementById("delete-tag").onclick = async () => {
    const n = info.prompts || 0;
    const rulesText = rules.length === 1 ? " and its rule" : rules.length ? ` and its ${rules.length} rules` : "";
    const promptsText = n ? `Your ${n} prompt${n === 1 ? " stays" : "s stay"}: only the tag${rulesText} ${rulesText ? "are" : "is"} removed.` : `No prompt carries it.`;
    if (!confirm(`Delete the tag ${tag}?\n\n${promptsText}`)) return;
    await api(`/api/tags/${enc}`, { method: "DELETE" });
    location.hash = "#/tags";
  };
  const descForm = document.getElementById("desc-form");
  const toggleDesc = (editing) => { descForm.hidden = !editing; app.querySelector(".tag-desc-edit").hidden = editing; if (editing) document.getElementById("desc-input").focus(); };
  document.getElementById("edit-desc").onclick = () => toggleDesc(true);
  document.getElementById("desc-cancel").onclick = () => toggleDesc(false);
  descForm.onsubmit = async (e) => {
    e.preventDefault();
    await api(`/api/tags/${enc}/description`, { method: "PUT", body: { description: document.getElementById("desc-input").value } });
    route();
  };
  // one editor row, for a new rule or in place of the chip being changed
  const editForm = document.getElementById("rule-edit-form");
  const editBox = editForm.querySelector(".rules-editor");
  let editing = null; // position of the rule being changed, or null for a new one
  const openEditor = (pos) => {
    editing = pos;
    editBox.innerHTML = ruleRowHtml(pos ?? rules.length, pos == null ? {} : rules[pos]);
    editBox.querySelector("[data-remove-row]").hidden = true;
    bindRuleRow(editBox.firstElementChild);
    app.querySelectorAll(".rule-chip").forEach((c) => c.classList.toggle("editing", Number(c.dataset.rulePos) === pos));
    editForm.hidden = false;
    editBox.querySelector(".rr-keywords").focus();
  };
  app.querySelectorAll("[data-edit-rule]").forEach((b) => b.onclick = () => openEditor(Number(b.dataset.editRule)));
  app.querySelector("[data-add-keywords]").onclick = () => openEditor(null);
  editForm.querySelector("[data-cancel-rule]").onclick = () => {
    editForm.hidden = true;
    app.querySelectorAll(".rule-chip.editing").forEach((c) => c.classList.remove("editing"));
  };
  editForm.onsubmit = async (e) => {
    e.preventDefault();
    const values = readRuleRow(editBox.firstElementChild);
    if (!values.skill && !values.keywords.length) return;
    await (editing == null
      ? api(`/api/tags/${enc}/rules`, { method: "POST", body: values })
      : api(`/api/tags/${enc}/rules/${rules[editing].index}`, { method: "PUT", body: values }));
    route();
  };
  app.querySelectorAll("[data-delete-rule]").forEach((b) => b.onclick = async () => {
    await api(`/api/tags/${enc}/rules/${b.dataset.deleteRule}`, { method: "DELETE" });
    route();
  });
}

// ---- sessions ----------------------------------------------------------------

const SESSION_SORTS = { recent: "Newest", cost: "Most expensive", prompts: "Most prompts", duration: "Longest", tokens: "Most tokens" };
// filters live in the URL (shareable), as on the Prompts page
const sessState = { q: "", project: "", range: "all", sort: "recent", items: [], summary: null, highlights: null };

function sessionFiltersFromQuery(query) {
  sessState.q = query.get("q") || "";
  sessState.project = query.get("project") || "";
  sessState.range = RANGES[query.get("range")] ? query.get("range") : "all";
  sessState.sort = SESSION_SORTS[query.get("sort")] ? query.get("sort") : "recent";
}

function sessionQuery(extra = {}) {
  const since = RANGES[sessState.range][1];
  const q = { q: sessState.q, project: sessState.project, sort: sessState.sort, ...extra };
  if (since) q.since = since().toISOString();
  return new URLSearchParams(q);
}

function syncSessionUrl() {
  const q = new URLSearchParams();
  for (const key of ["q", "project"]) if (sessState[key]) q.set(key, sessState[key]);
  if (sessState.range !== "all") q.set("range", sessState.range);
  if (sessState.sort !== "recent") q.set("sort", sessState.sort);
  const hash = `#/sessions${q.toString() ? `?${q}` : ""}`;
  if (location.hash !== hash) history.replaceState(null, "", hash); // no hashchange: the page updates itself
}

const sessionTitle = (x) => x.title || (x.first_prompt ? x.first_prompt.slice(0, 90) : `Session ${x.id.slice(0, 8)}`);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function sessionRow(x, maxCost) {
  const pct = maxCost ? Math.max(2, Math.round(((x.cost_usd || 0) / maxCost) * 100)) : 0;
  const when = sessState.sort === "recent"
    ? new Date(x.started_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : fmtDate(x.started_at);
  const meta = [plural(x.prompts, "prompt"), fmtTokens(x.total_tokens) + " tok", fmtMs(x.active_ms), x.agent_count ? plural(x.agent_count, "subagent") : ""]
    .filter(Boolean).join(" · ");
  return `<article class="prow">
    <time datetime="${esc(x.started_at)}">${when}</time>
    <div class="pbody">
      <a class="plink" href="#/session/${encodeURIComponent(x.id)}">${esc(sessionTitle(x))}</a>
      ${x.title && x.first_prompt ? `<div class="sfirst">${esc(x.first_prompt)}</div>` : ""}
      <div class="pchips">${x.project ? `<button type="button" class="pproject" data-set-project="${esc(x.project)}" title="Show only this project's sessions">${esc(x.project)}</button>` : ""}</div>
    </div>
    <div class="pmetrics">
      <span class="pcost">${fmtCost(x.cost_usd)}</span>
      <span class="cost-bar" title="Cost relative to the most expensive session shown"><i style="width:${pct}%"></i></span>
      <span class="pmeta">${meta}</span>
    </div>
  </article>`;
}

function sessionListHtml(items, maxCost) {
  if (sessState.sort !== "recent") return `<div class="prow-list">${items.map((x) => sessionRow(x, maxCost)).join("")}</div>`;
  const days = new Map();
  for (const x of items) {
    const key = new Date(x.started_at).toDateString();
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(x);
  }
  return [...days.values()].map((list) => `<section class="day"><h2 class="day-head"><span>${dayLabel(list[0].started_at)}</span>
      <span class="sec-meta">${plural(list.length, "session")} · ${fmtCost(list.reduce((sum, x) => sum + (x.cost_usd || 0), 0))}</span></h2>
      <div class="prow-list">${list.map((x) => sessionRow(x, maxCost)).join("")}</div></section>`).join("");
}

function sessionHighlights(h, total) {
  const share = (cost) => (total ? `${Math.round((cost / total) * 100)}% of the total` : "");
  const card = (label, x, value, unit) => highlightCard(label, value, unit,
    `<span class="hl-text">${esc(sessionTitle(x))}</span>
     <span class="pmeta">${[x.project, fmtDate(x.started_at), plural(x.prompts, "prompt"), fmtCost(x.cost_usd), fmtMs(x.active_ms)].filter(Boolean).map(esc).join(" · ")}</span>`,
    `#/session/${encodeURIComponent(x.id)}`, "Open the session");
  return `<div class="highlights">
    ${card("Most expensive session", h.cost, fmtCost(h.cost.cost_usd), share(h.cost.cost_usd))}
    ${card("Most prompts", h.prompts, h.prompts.prompts, "prompts")}
    ${card("Longest session", h.duration, fmtMs(h.duration.active_ms), "active time")}
  </div>`;
}

function sessionToolbarHtml(projects) {
  const projectMenu = `<button type="button" class="menu-item ${!sessState.project ? "on" : ""}" data-set-project="">All projects</button>
    ${projects.map((p) => `<button type="button" class="menu-item ${p === sessState.project ? "on" : ""}" data-set-project="${esc(p)}">${esc(p)}</button>`).join("")}`;
  return `<search class="tags-toolbar prompts-toolbar">
    <label class="search-box"><span aria-hidden="true">⌕</span>
      <input type="search" id="session-search" placeholder="Search titles and prompts…" value="${esc(sessState.q)}" aria-label="Search sessions"></label>
    ${pillMenu("pop-project", "Project", sessState.project, projectMenu)}
    <div class="segmented" role="group" aria-label="Period">${Object.entries(RANGES).map(([key, [label]]) =>
      `<button type="button" data-range="${key}" aria-pressed="${key === sessState.range}">${label}</button>`).join("")}</div>
    <span class="toolbar-spacer"></span>
    <label class="sort-box">Sort <select id="session-sort">${Object.entries(SESSION_SORTS).map(([key, label]) =>
      `<option value="${key}" ${key === sessState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>
  </search>`;
}

function sessionActiveFiltersHtml() {
  const chips = [];
  if (sessState.q) chips.push(["q", `“${sessState.q}”`]);
  if (sessState.project) chips.push(["project", `Project: ${sessState.project}`]);
  if (sessState.range !== "all") chips.push(["range", RANGES[sessState.range][0]]);
  if (!chips.length) return "";
  return `<div class="active-filters" aria-label="Active filters">${chips.map(([key, label]) =>
    `<button type="button" class="filter-chip" data-clear="${key}" title="Remove this filter">${esc(label)}<span aria-hidden="true">✕</span></button>`).join("")}
    <button type="button" class="link-btn" data-clear="all">Clear all</button></div>`;
}

let sessionProjects = [];

async function renderSessions({ reload = true } = {}) {
  syncSessionUrl();
  if (reload) {
    const data = await api(`/api/sessions?${sessionQuery({ limit: LIST_PAGE, offset: 0 })}`);
    Object.assign(sessState, { items: data.items, summary: data.summary, highlights: data.highlights || null });
  }
  const { items, summary: s } = sessState;
  const fact = (label, value, sub = "") => `<div class="hero-fact"><span class="label">${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></div>`;
  document.getElementById("session-summary").innerHTML = `<div class="tags-hero">
    ${fact("Sessions", s.count || 0, RANGES[sessState.range][0].toLowerCase())}
    ${fact("Total cost", fmtCost(s.cost_usd || 0), `${fmtTokens(s.tokens || 0)} tokens`)}
    ${fact("Per session", fmtCost(s.count ? s.cost_usd / s.count : null), `${s.count ? (s.prompts / s.count).toFixed(1) : 0} prompts on average`)}
    ${fact("Agent time", fmtMs(s.active_ms || 0), "active time, summed")}
  </div>`;
  document.getElementById("session-highlights").innerHTML = sessState.highlights ? sessionHighlights(sessState.highlights, s.cost_usd) : "";
  document.getElementById("active-filters").innerHTML = sessionActiveFiltersHtml();
  const results = document.getElementById("session-results");
  const more = (s.count || 0) - items.length;
  results.innerHTML = items.length
    ? sessionListHtml(items, s.max_cost_usd || 0) + (more > 0
      ? `<div class="load-more"><button class="btn" type="button" id="load-more">Show ${Math.min(more, LIST_PAGE)} more <span class="muted">(${more} left)</span></button></div>` : "")
      + `<p class="muted group-note">A session's cost sums its prompts. The total the agent stores in the transcript, with its internal calls, is on the session page.</p>`
    : `<div class="empty-state"><div class="empty-icon">⌕</div><p>No session matches these filters.</p>
        ${sessionActiveFiltersHtml() ? '<button class="btn" type="button" data-clear="all">Clear all filters</button>' : '<p>Check the <a href="#/setup">Setup</a> page, then press Refresh.</p>'}</div>`;
  bindSessionPage();
}

function rerenderSessionToolbar() {
  document.getElementById("session-toolbar").innerHTML = sessionToolbarHtml(sessionProjects);
}

function bindSessionPage() {
  const bar = document.getElementById("session-toolbar");
  let timer;
  bar.querySelector("#session-search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { sessState.q = e.target.value; renderSessions(); }, 250);
  };
  app.querySelectorAll("[data-set-project]").forEach((b) => b.onclick = () => {
    sessState.project = b.dataset.setProject; rerenderSessionToolbar(); renderSessions();
  });
  bar.querySelectorAll("[data-range]").forEach((b) => b.onclick = () => { sessState.range = b.dataset.range; rerenderSessionToolbar(); renderSessions(); });
  bar.querySelector("#session-sort").onchange = (e) => { sessState.sort = e.target.value; renderSessions(); };
  app.querySelectorAll("[data-clear]").forEach((b) => b.onclick = () => {
    const key = b.dataset.clear;
    if (key === "all") Object.assign(sessState, { q: "", project: "", range: "all" });
    else sessState[key] = key === "range" ? "all" : "";
    rerenderSessionToolbar(); renderSessions();
  });
  const more = document.getElementById("load-more");
  if (more) more.onclick = async () => {
    more.disabled = true;
    const data = await api(`/api/sessions?${sessionQuery({ limit: LIST_PAGE, offset: sessState.items.length })}`);
    sessState.items.push(...data.items);
    renderSessions({ reload: false });
  };
}

async function viewSessions(query = new URLSearchParams()) {
  sessionFiltersFromQuery(query);
  sessionProjects = await api("/api/projects");
  app.innerHTML = `
    <div class="prompts-page">
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Sessions</span>Every conversation with your agent, <em>from start to finish</em>.</h1>
          <p>A session gathers the prompts of one conversation. See what each one cost in total, how long it ran, and open it for
            <strong>cost per agent and subagent</strong> and <strong>how each prompt weighed</strong> along the way.</p>
        </div>
      </header>
      <div id="session-summary"></div>
      <div id="session-highlights"></div>
      <div id="session-toolbar"></div>
      <div id="active-filters"></div>
      <div id="session-results"></div>
    </div>`;
  rerenderSessionToolbar();
  await renderSessions();
}

async function viewSession(id) {
  const s = await api(`/api/sessions/${encodeURIComponent(id)}`);
  const block = chartsBlock(s.prompts, null);
  const priciest = [...s.prompts].sort((a, b) => (b.total_cost_usd || 0) - (a.total_cost_usd || 0))[0];
  const [first] = s.prompts;
  const maxCost = priciest?.total_cost_usd || 0;
  const wall = s.started_at && s.ended_at ? new Date(s.ended_at) - new Date(s.started_at) : null;
  const facts = [
    ["Cost", fmtCost(s.cost_usd), plural(s.prompts.length, "prompt"), s.official_cost_usd != null ? `${fmtCost(s.official_cost_usd)} from the agent's telemetry` : "price-table estimate"],
    ["Agent total", fmtCost(s.reported_cost_usd), "stored in the transcript", "includes internal calls"],
    ["Tokens", fmtTokens(s.total_tokens), "incl. cache and subagents", ""],
    ["Active time", fmtMs(s.active_ms), `wall ${fmtMs(wall)}`, s.api_ms != null ? `API time ${fmtMs(s.api_ms)}` : ""],
    ["API calls", s.api_calls, `${s.tool_calls} tool calls`, ""],
    ["Subagent runs", s.agent_count || 0, "", ""],
  ];
  const linkCard = (label, title, text, href, attrs = "") => `<a class="page-card link-card" href="${href}" ${attrs}>
      <span class="hl-label">${label}</span><strong class="lc-title">${title}</strong><span class="pc-text">${text}</span></a>`;
  const links = [
    s.project ? linkCard("Project", esc(s.project), "every run in this repository", `#/tag/${encodeURIComponent(`project:${s.project}`)}`) : "",
    first ? linkCard("First prompt", esc(first.text.slice(0, 80)), `${fmtDate(first.started_at)} · ${fmtCost(first.total_cost_usd)}`, `#/prompt/${encodeURIComponent(first.id)}`) : "",
    priciest ? linkCard("Most expensive prompt", fmtCost(priciest.total_cost_usd), esc(priciest.text.slice(0, 80)), `#/prompt/${encodeURIComponent(priciest.id)}`) : "",
    linkCard("Prompts", plural(s.prompts.length, "prompt"), "how each one weighed, below", "#", 'data-jump="session-prompts"'),
  ].join("");
  app.innerHTML = `
    <div class="prompt-page">
    <a class="crumb" href="#/sessions">← All sessions</a>
    <header class="prompt-head">
      ${pageKicker("Session", "One conversation from start to finish: what each agent cost and how each prompt weighed.")}
      <div class="prompt-meta"><time datetime="${esc(s.started_at)}">${fmtDate(s.started_at)}</time>${s.project ? `<span>${esc(s.project)}</span>` : ""}
        <code title="Session id">${esc(s.id.slice(0, 8))}</code><span class="prompt-cost">${fmtCost(s.cost_usd)}</span></div>
      <h1 class="session-title">${esc(sessionTitle({ ...s, first_prompt: first?.text }))}</h1>
      <div class="tag-head-meta"><button type="button" class="link-btn danger-link" id="exclude-this-session"
        title="Leave this session out of every page; bring it back from the Setup page">Exclude from history</button></div>
    </header>
    <nav class="page-cards link-cards" aria-label="Related pages">${links}</nav>
    <div class="tags-hero prompt-facts">${facts.map(([label, value, sub, note]) => `<div class="hero-fact"><span class="label">${label}</span>
      <span class="value">${value}</span><span class="sub">${sub || "&nbsp;"}</span>${note ? `<span class="sub">${note}</span>` : ""}</div>`).join("")}</div>
    <h2 class="page-sec">Cost per agent</h2>
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
    <h2 class="page-sec">Prompts over the session</h2>${block.html}
    <h2 class="page-sec" id="session-prompts">Prompts <span class="muted">in order</span></h2>
    <div class="prow-list">${s.prompts.map((p) => promptRow(p, maxCost, { compact: true })).join("")}</div>
    </div>`;
  block.mount();
  document.getElementById("exclude-this-session").onclick = async () => {
    const its = s.prompts.length === 1 ? "Its prompt disappears from every page but stays imported: bring it" : `Its ${s.prompts.length} prompts disappear from every page but stay imported: bring them`;
    if (!confirm(`Leave this session out of the history?\n\n${its} back from the Setup page.`)) return;
    const { excluded_sessions: list } = await api("/api/settings");
    await api("/api/settings", { method: "PUT", body: { excluded_sessions: [...new Set([...list, s.id])] } });
    location.hash = "#/sessions";
  };
  // the hash routes pages: the prompts card scrolls instead of navigating
  app.querySelector('[data-jump="session-prompts"]').onclick = (e) => {
    e.preventDefault();
    document.getElementById("session-prompts").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };
}

/** Projects to click in or out of the history, and the sessions left out with a way back. */
function excludedCardHtml(scope) {
  const sessionLabel = (x) => `${fmtDate(x.started_at)} · ${x.title || (x.first_prompt || "").slice(0, 60) || x.id.slice(0, 8)}${x.project ? ` · ${x.project}` : ""}`;
  const out = scope.sessions.filter((x) => x.excluded);
  const pickable = scope.sessions.filter((x) => !x.excluded && !scope.projects.some((pr) => pr.excluded && pr.project === x.project));
  const { hidden } = scope;
  const tagChips = (list, prefix) => list.map((x) => `<button type="button" class="exclude-chip" data-exclude-tag="${esc(x.tag)}" aria-pressed="${x.excluded}"
    title="${x.excluded ? "Left out: click to bring it back" : "Click to leave out every prompt where the agent used it"}">${esc(x.tag.slice(prefix.length))} <span class="mi-count">${x.prompts}</span></button>`).join("");
  return `<div class="card exclusions">
    <p class="muted">Leave a project, a skill, a kind of subagent or a session out of every page: totals, lists, tags, charts and comparisons. Its prompts stay imported,
      so bringing it back is instant.${hidden ? ` <strong>${plural(hidden, "prompt")} left out now.</strong>` : ""}</p>
    <div class="setup-row"><div><strong>Projects</strong><br><span class="muted">Click a project to leave it out; click it again to bring it back.</span>
      <div class="exclude-chips">${scope.projects.map((x) => `<button type="button" class="exclude-chip" data-exclude-project="${esc(x.project)}" aria-pressed="${x.excluded}"
        title="${x.excluded ? "Left out: click to bring it back" : "Click to leave it out of the history"}">${esc(x.project)} <span class="mi-count">${x.prompts}</span></button>`).join("") || '<span class="muted">No project yet.</span>'}</div></div></div>
    <div class="setup-row"><div><strong>Skills</strong><br><span class="muted">Leave out every prompt where the agent used a skill, such as a batch job that skews the figures.</span>
      <div class="exclude-chips">${tagChips(scope.skills, "skill:") || '<span class="muted">No skill used yet.</span>'}</div></div></div>
    ${scope.reruns.length ? `<div class="setup-row"><div><strong>Reruns</strong><br><span class="muted">Leave out every prompt of the reruns launched from here, to keep the history to your own work.</span>
      <div class="exclude-chips">${tagChips(scope.reruns, "")}</div></div></div>` : ""}
    <div class="setup-row"><div><strong>Subagent types</strong><br><span class="muted">Leave out every prompt that ran a kind of subagent.</span>
      <div class="exclude-chips">${tagChips(scope.agents, "agent:") || '<span class="muted">No subagent run yet.</span>'}</div></div></div>
    <div class="setup-row"><div><strong>Sessions</strong><br><span class="muted">Leave out one conversation, such as a test or an experiment.</span>
      ${out.length ? `<ul class="excluded-sessions">${out.map((x) => `<li><span>${esc(sessionLabel(x))} <span class="mi-count">${plural(x.prompts, "prompt")}</span></span>
        <button type="button" class="link-btn" data-include-session="${esc(x.id)}">Bring back</button></li>`).join("")}</ul>` : ""}
      <form id="exclude-session-form" class="inline-form exclude-form"><select id="exclude-session" aria-label="Session to leave out">
        <option value="">Pick a session…</option>${pickable.map((x) => `<option value="${esc(x.id)}">${esc(sessionLabel(x))}</option>`).join("")}</select>
        <button class="btn">Leave it out</button></form></div></div>
  </div>`;
}

async function viewSetup() {
  const [st, settings, scope] = await Promise.all([api("/api/setup/status"), api("/api/settings"), api("/api/setup/history")]);
  const ok = (cond) => (cond ? "status-ok" : "status-warn");
  const retentionOk = st.cleanup_period_days != null && st.cleanup_period_days >= 365;
  // the checks below, in order: counted in the banner
  const steps = [st.projects_dir_exists, st.prompts > 0, st.hook_installed, st.autostart.enabled, retentionOk,
    st.telemetry.configured && st.telemetry.matched_events > 0];
  app.innerHTML = `
    <header class="home-hero page-hero">
      <div class="hero-copy">
        <h1><span class="hero-name">Setup · ${steps.filter(Boolean).length} of ${steps.length} done</span>Connect your agent once. <em>Every prompt shows up on its own.</em></h1>
        <p>Rerunly reads what <strong>${esc(st.agent)}</strong> already writes on your machine: nothing leaves it. Import your history, then let the
          <strong>hook</strong> bring each new prompt in as soon as the agent answers. What to leave out of the history, prices and similarity are below; tags and their rules live on the <a href="#/tags">Tags</a> page.</p>
      </div>
    </header>
    <div class="card">
      <div class="setup-row"><div class="${ok(st.projects_dir_exists)}"><strong>Transcripts</strong><br>
        <span class="muted">The agent writes every session (and each subagent) to JSONL files. ${st.transcript_count} session files found in <code>${esc(st.projects_dir)}</code>.</span></div></div>
      <div class="setup-row"><div class="${ok(st.prompts > 0)}"><strong>Imported</strong><br>
        <span class="muted">${st.sessions} sessions · ${st.prompts} prompts · ${st.agents} subagent runs · last prompt ${fmtDate(st.last_prompt_at)}. Database <code>${esc(st.db_path)}</code></span></div>
        <div><button class="btn primary" id="ingest">Import now</button> <button class="btn" id="reingest" title="Re-parse every session (after a price or rule change)">Re-import all</button></div></div>
      <div class="setup-row"><div class="${ok(st.hook_installed)}"><strong>Live updates (agent hook)</strong><br>
        <span class="muted">Without the hook the dashboard re-scans every ${settings.auto_ingest_interval}s while it runs. The hook imports right after each answer of the agent.
        It adds this command to <code>${esc(st.claude_settings_path)}</code> (a <code>.bak</code> copy is kept):</span><br><code>${esc(st.hook_command)}</code></div>
        <div><button class="btn" id="hook">${st.hook_installed ? "Remove hook" : "Install hook"}</button></div></div>
      <div class="setup-row"><div class="${ok(st.autostart.enabled)}"><strong>Start at login</strong><br>
        <span class="muted">Starts the dashboard in the background when you log in, with an icon in the status bar to open it, import, or quit.
        It writes <code>${esc(st.autostart.file)}</code>. To start it now without a terminal: <code>uv run rerunly start</code>.</span></div>
        <div><button class="btn" id="autostart">${st.autostart.enabled ? "Don't start at login" : "Start at login"}</button></div></div>
      <div class="setup-row"><div class="${ok(retentionOk)}"><strong>Transcript retention</strong><br>
        <span class="muted">The agent deletes transcripts older than <code>cleanupPeriodDays</code> (default 30). Imported prompts stay in the dashboard database,
        but a longer retention lets you re-import with new prices or rules. Current: ${st.cleanup_period_days ?? "not set (30 days)"}.</span></div>
        <div><button class="btn" id="retention">Set to 3650 days</button></div></div>
      <div class="setup-row"><div class="${ok(st.telemetry.configured && st.telemetry.matched_events > 0)}"><strong>Cost reported by the agent (OpenTelemetry)</strong><br>
        <span class="muted">The agent can send its own cost for every API call, including internal calls the transcripts do not detail.
        The dashboard then shows that cost for every prompt fully covered by telemetry, and the price-table estimate otherwise.
        Enabling it adds these entries to <code>env</code> in <code>${esc(st.claude_settings_path)}</code>; they apply to agent sessions started afterwards,
        and calls made while the dashboard is not running are not recorded.</span><br>
        ${Object.entries(st.telemetry.env).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}<br>
        <span class="muted">${st.telemetry.events} API calls received · ${st.telemetry.matched_events} linked to an imported prompt · last ${fmtDate(st.telemetry.last_event_at)}</span>
        ${Object.keys(st.telemetry.conflicts).length ? `<br><span class="status-warn">Already set to other values: ${Object.entries(st.telemetry.conflicts).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</span>` : ""}</div>
        <div><button class="btn" id="telemetry">${st.telemetry.configured ? "Disable telemetry" : "Enable telemetry"}</button></div></div>
      ${st.unpriced_models.length ? `<div class="setup-row"><div class="status-warn"><strong>Models without a price</strong><br><span class="muted">${esc(st.unpriced_models.join(", "))} — add them to the price table below.</span></div></div>` : ""}
    </div>

    <h2 id="excluded">Excluded from the history</h2>
    ${excludedCardHtml(scope)}

    <h2>Settings</h2>
    <form id="settings" class="card">
      <div class="setup-row"><div><strong>Transcripts directory</strong></div><input name="claude_projects_dir" size="50" value="${esc(settings.claude_projects_dir)}"></div>
      <div class="setup-row"><div><strong>Cost shown</strong><br><span class="muted">The agent's own cost when telemetry covers the whole prompt, or always the price table.</span></div>
        <select name="cost_source">
          <option value="claude-code" ${settings.cost_source === "claude-code" ? "selected" : ""}>The agent's own cost when available</option>
          <option value="prices" ${settings.cost_source === "prices" ? "selected" : ""}>Price table only</option></select></div>
      <div class="setup-row"><div><strong>Similarity threshold</strong><br><span class="muted">0–1 word overlap above which two prompts are shown together.</span></div>
        <input name="similarity_threshold" type="number" min="0" max="1" step="0.05" value="${settings.similarity_threshold}"></div>
      <div class="setup-row"><div><strong>Default LLM judge</strong><br><span class="muted">The model rating how much a rerun's output differs from its original's.
        Empty uses Claude Code's default model. Each rerun, and the Compare page, can pick another.</span></div>
        <input name="judge_model" size="24" value="${esc(settings.judge_model || "")}" placeholder="claude-haiku-5-5"></div>
      <div class="setup-row"><div><strong>Auto-import interval (seconds)</strong><br><span class="muted">0 disables periodic scanning.</span></div>
        <input name="auto_ingest_interval" type="number" min="0" value="${settings.auto_ingest_interval}"></div>
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
  document.getElementById("autostart").onclick = (e) => busy(e.target, async () => {
    await api("/api/setup/autostart", { method: "POST", body: { enable: !st.autostart.enabled } }); viewSetup();
  });
  document.getElementById("retention").onclick = (e) => busy(e.target, async () => { await api("/api/setup/retention", { method: "POST", body: { days: 3650 } }); viewSetup(); });
  // exclusions: a click saves the new list and redraws the page
  const exclude = (key, list) => api("/api/settings", { method: "PUT", body: { [key]: list } }).then(() => viewSetup());
  const excludedProjects = scope.projects.filter((x) => x.excluded).map((x) => x.project);
  const excludedSessions = scope.sessions.filter((x) => x.excluded).map((x) => x.id);
  app.querySelectorAll("[data-exclude-project]").forEach((b) => b.onclick = () => {
    const name = b.dataset.excludeProject;
    exclude("excluded_projects", excludedProjects.includes(name) ? excludedProjects.filter((x) => x !== name) : [...excludedProjects, name]);
  });
  const excludedTags = [...scope.skills, ...scope.agents, ...scope.reruns].filter((x) => x.excluded).map((x) => x.tag);
  app.querySelectorAll("[data-exclude-tag]").forEach((b) => b.onclick = () => {
    const tag = b.dataset.excludeTag;
    exclude("excluded_tags", excludedTags.includes(tag) ? excludedTags.filter((x) => x !== tag) : [...excludedTags, tag]);
  });
  app.querySelectorAll("[data-include-session]").forEach((b) => b.onclick = () =>
    exclude("excluded_sessions", excludedSessions.filter((x) => x !== b.dataset.includeSession)));
  document.getElementById("exclude-session-form").onsubmit = (e) => {
    e.preventDefault();
    const id = document.getElementById("exclude-session").value;
    if (id) exclude("excluded_sessions", [...excludedSessions, id]);
  };
  document.getElementById("settings").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let values;
    try {
      values = {
        claude_projects_dir: f.get("claude_projects_dir"),
        cost_source: f.get("cost_source"),
        similarity_threshold: parseFloat(f.get("similarity_threshold")),
        judge_model: String(f.get("judge_model") || "").trim(),
        auto_ingest_interval: parseInt(f.get("auto_ingest_interval"), 10),
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

// ---- Home page: every page, and the highlights of the period ----------------------

const HOME_PAGES = [
  ["prompts", "Prompts", "Every prompt you gave your coding agent with its cost, tokens and time. Search it, filter by project, tag or period, sort by cost, or group by tag to follow a task.",
    (o) => `${o.totals.prompts || 0} prompt${o.totals.prompts === 1 ? "" : "s"}`],
  ["sessions", "Sessions", "One row per agent session: its prompts, the cost of the main agent and each subagent, and the total the agent reports.",
    (o) => `${o.totals.sessions || 0} session${o.totals.sessions === 1 ? "" : "s"}`],
  ["tags", "Tags", "Cost and time by task, project, model or skill. Create your own tags to group the runs of one task and see if your prompt changes paid off.",
    (o) => `${o.tags.length} tag${o.tags.length === 1 ? "" : "s"}`],
  ["reruns", "Reruns", "Queue prompts to run again, with the same model or another, then launch them together and see what each rerun cost against the original.",
    () => "Queue and launch"],
  ["setup", "Setup", "Import your transcripts, install the live-update hook, start at login, turn on the agent's own cost reporting, and edit prices and auto-tag rules.",
    () => "Import, hook, prices"],
];

function promptLabel(p) {
  return `${p.command ? `<span class="pcmd">/${esc(p.command)}</span> ` : ""}${esc(p.command ? p.text.replace(/^\/\S+\s*/, "") : p.text)}`;
}

/** Opens a detail page's header card: what the page is, and what it is for. */
function pageKicker(label, purpose) {
  return `<p class="page-kicker"><span class="hero-name">${label}</span><span>${purpose}</span></p>`;
}

function highlightCard(label, value, unit, body, href, linkLabel) {
  return `<a class="highlight" href="${href}" title="${esc(linkLabel)}">
    <span class="hl-label">${label}</span>
    <span class="hl-value"><span class="big">${value}</span><span class="unit">${unit}</span></span>
    ${body}
    <span class="hl-link">${esc(linkLabel)} →</span></a>`;
}

/** A prompt text typed more than once: `value`/`unit` say what made it stand out. */
function repeatedCard(label, r, value, unit, meta) {
  return r
    ? highlightCard(label, value, unit, `<span class="hl-text">${esc(r.text)}</span><span class="pmeta">${meta}</span>`,
      `#/prompt/${encodeURIComponent(r.id)}`, "Compare its runs")
    : `<div class="highlight empty"><span class="hl-label">${label}</span>
        <p class="muted">None of these prompts was typed twice. Rerun one to compare its runs, or give similar prompts the same tag.</p></div>`;
}

function homeHighlights(o) {
  const p = o.priciest, rc = o.priciest_repeated, ru = o.repeated;
  const share = (cost) => (o.totals.cost_usd ? `${Math.round((cost / o.totals.cost_usd) * 100)}% of the total` : "");
  const priciest = p
    ? highlightCard("Most expensive single run", fmtCost(p.total_cost_usd), share(p.total_cost_usd),
      `<span class="hl-text">${promptLabel(p)}</span>
       <span class="pmeta">${[p.project, fmtDate(p.started_at), `${fmtTokens(p.total_tokens)} tok`, fmtMs(duration(p))].filter(Boolean).map(esc).join(" · ")}</span>`,
      `#/prompt/${encodeURIComponent(p.id)}`, "Open the prompt")
    : "";
  return priciest
    + repeatedCard("Most expensive with its reruns", rc, fmtCost(rc?.cost_usd), rc ? `over ${rc.runs} runs · ${share(rc.cost_usd)}` : "",
      rc && `${fmtCost(rc.avg_cost_usd)} per run · last ${esc(fmtAgo(rc.last_at))}`)
    + repeatedCard("Most used prompt", ru, ru?.runs, "runs",
      ru && `${fmtCost(ru.avg_cost_usd)} per run · ${fmtCost(ru.cost_usd)} total · last ${esc(fmtAgo(ru.last_at))}`);
}

/** The most used tag of each category, with its two runners-up. */
function homeTopTags(o) {
  const total = o.totals.prompts || 0;
  return TAG_CATEGORIES.map(([cat, label, help]) => {
    const list = o.tags.filter((t) => tagCategory(t) === cat).slice(0, 3);
    if (!list.length) {
      return cat !== "human" ? "" : `<div class="top-tag cat-human empty"><span class="hl-label"><span class="tag-dot"></span>${label}</span>
        <p class="muted">No tag of yours yet: add one on a prompt page to follow a task across runs.</p></div>`;
    }
    const [top, ...rest] = list;
    const [prefix, ...name] = top.tag.split(":");
    const title = name.length ? `<span class="tag-prefix">${esc(prefix)}:</span>${esc(name.join(":"))}` : esc(top.tag);
    const share = (t) => (total ? Math.round((t.prompts / total) * 100) : 0);
    const shareText = (t) => (share(t) === 0 && t.prompts ? "<1" : share(t));
    return `<div class="top-tag cat-${cat}">
      <span class="hl-label" title="${esc(help)}"><span class="tag-dot"></span>${label}</span>
      <a class="top-name" href="#/tag/${encodeURIComponent(top.tag)}">${title}</a>
      <span class="top-share"><span class="share-bar" aria-hidden="true"><i style="width:${Math.max(2, share(top))}%"></i></span>${shareText(top)}% of prompts</span>
      <span class="pmeta">${top.prompts} prompt${top.prompts === 1 ? "" : "s"} · ${fmtCost(top.cost_usd)} total · ${fmtCost(top.avg_cost_usd)} each</span>
      ${rest.length ? `<ol class="runners-up" start="2">${rest.map((t) => `<li><a href="#/tag/${encodeURIComponent(t.tag)}">${esc(t.tag)}</a><span class="mi-count">${t.prompts}</span></li>`).join("")}</ol>` : ""}
    </div>`;
  }).join("");
}

async function viewHome(query = new URLSearchParams()) {
  const range = RANGES[query.get("range")] ? query.get("range") : "all";
  const since = RANGES[range][1];
  const o = await api(`/api/overview${since ? `?${new URLSearchParams({ since: since().toISOString() })}` : ""}`);
  const t = o.totals;
  const fact = (label, value, sub = "") => `<div class="hero-fact"><span class="label">${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></div>`;
  const rangeQuery = range === "all" ? "" : `?range=${range}`;
  app.innerHTML = `
    <div class="home-page">
      <header class="home-hero">
        <img class="hero-logo" src="/static/icon.svg" alt="" width="96" height="96">
        <div class="hero-copy">
          <h1><span class="hero-name">Rerunly</span>Rerun a task, see if your prompt change <em>paid off</em>.</h1>
          <p>Cost, tokens and time for every prompt you give your coding agent, so each rerun tells you if your last change made it
            <strong>cheaper</strong>, <strong>faster</strong>, or neither.</p>
        </div>
      </header>
      <div class="home-range segmented" role="group" aria-label="Period">${Object.entries(RANGES).map(([key, [label]]) =>
        `<button type="button" data-range="${key}" aria-pressed="${key === range}">${label}</button>`).join("")}</div>
      <div class="tags-hero">
        ${fact("Prompts", t.prompts || 0, `in ${t.sessions || 0} session${t.sessions === 1 ? "" : "s"}`)}
        ${fact("Total cost", fmtCost(t.cost_usd || 0), `${fmtTokens(t.tokens || 0)} tokens`)}
        ${fact("Per prompt", fmtCost(t.prompts ? t.cost_usd / t.prompts : null), "average")}
        ${fact("Agent time", fmtMs(t.duration_ms || 0), t.last_at ? `last prompt ${esc(fmtAgo(t.last_at))}` : "")}
      </div>
      ${t.prompts ? `
        <h2 class="page-sec">Highlights</h2>
        <div class="highlights">${homeHighlights(o)}</div>
        <h2 class="page-sec">Most used in each tag category</h2>
        <div class="top-tags">${homeTopTags(o)}</div>`
      : `<div class="empty-state"><div class="empty-icon">⌕</div><p>${range === "all" ? "Nothing imported yet." : "No prompt in this period."}</p>
          ${range === "all" ? '<p>Go to <a href="#/setup">Setup</a> and press <strong>Import now</strong>.</p>' : ""}</div>`}
      <h2 class="page-sec">Pages</h2>
      <nav class="page-cards" aria-label="Pages">${HOME_PAGES.map(([id, label, text, stat]) =>
        `<a class="page-card" href="#/${id}${id === "prompts" ? rangeQuery : ""}"><span class="pc-head"><strong>${label}</strong><span class="mi-count">${esc(stat(o))}</span></span>
          <span class="pc-text">${text}</span><span class="hl-link">Open ${label} →</span></a>`).join("")}</nav>
    </div>`;
  app.querySelectorAll("[data-range]").forEach((b) => b.onclick = () => {
    location.hash = b.dataset.range === "all" ? "#/" : `#/?range=${b.dataset.range}`;
  });
}

// ---- Reruns page: the queue, launched together, and what the reruns cost ---------------------

const RERUN_STATUS = {
  running: ["⏳", "Running", "status-running"],
  done: ["✓", "Done", "status-done"],
  needs_you: ["⚠", "Needs you", "status-needs"],
  failed: ["✕", "Failed", "status-failed"],
  interactive: ["▸", "In a terminal", "status-interactive"],
};

/** How a rerun ended: a badge, and what it needed when it stopped for you. */
function rerunStatusHtml(x, { details = true } = {}) {
  const [icon, label, cls] = RERUN_STATUS[x.status] || RERUN_STATUS.interactive;
  const d = x.denials || {};
  const denied = (d.denied || []).map((a) => `${esc(a.tool)}${a.input?.file_path || a.input?.command ? ` <code>${esc(String(a.input.file_path || a.input.command).slice(0, 80))}</code>` : ""}`);
  const needs = [d.blocked ? esc(d.blocked) : "", ...denied].filter(Boolean);
  return `<span class="rerun-status ${cls}" title="${esc(label)}">${icon} ${label}</span>${details && x.status === "needs_you" && needs.length
    ? `<span class="rerun-needs">It needed: ${needs.join(" · ")}</span>` : ""}${details && x.status === "failed" && x.result
    ? `<span class="rerun-needs">${esc(x.result.slice(0, 200))}</span>` : ""}`;
}

const rerunState = { picked: new Set() };

function rerunSettings(x) {
  const skills = Array.isArray(x.skills) ? x.skills : [];
  const parts = [x.mode === "terminal" ? "in a terminal" : "unattended", x.mode !== "terminal" ? (x.permission === "edits" ? "may edit files" : "read-only") : "",
    x.budget_usd ? `up to ${fmtCost(x.budget_usd)}` : "",
    ...skills.map((k) => `${k.skill.split(":").pop()} @ ${k.source}`),
    x.model || "same model", x.effort ? `effort ${x.effort}` : "same effort",
    x.harness_version ? `Claude Code ${x.harness_version}` // launched: the release it ran with
      : x.release === "same" || (x.release == null && x.same_version) ? "the release it ran with"
      : x.release ? `Claude Code ${x.release}` : "installed Claude Code"];
  return parts.filter(Boolean).map((t) => `<span class="rr-chip">${esc(t)}</span>`).join("");
}

function rerunPromptCell(p) {
  if (!p) return '<span class="muted">This prompt left the history (excluded on the Setup page).</span>';
  return `<a class="rr-text" href="#/prompt/${encodeURIComponent(p.id)}">${esc(p.text)}</a>
    <span class="pmeta">${[p.project, `ran ${fmtDate(p.started_at)}`, fmtCost(p.total_cost_usd)].filter(Boolean).map(esc).join(" · ")}</span>`;
}

async function viewReruns() {
  const data = await api("/api/reruns");
  rerunState.picked = new Set([...rerunState.picked].filter((id) => data.queue.some((q) => q.id === id)));
  const picked = data.queue.filter((q) => rerunState.picked.has(q.id));
  const estimate = picked.reduce((t, q) => t + (q.prompt?.total_cost_usd || 0), 0);
  const done = data.launched.filter((x) => x.replay);
  app.innerHTML = `
    <div class="prompts-page reruns-page">
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Reruns · ${data.queue.length} queued</span>Queue reruns, launch them together. <em>Did your change pay off?</em></h1>
          <p>Queue a prompt from its page (<strong>Add to queue</strong>), or many from the Prompts page or a tag's page (<strong>Select prompts</strong>, then
            <strong>Queue reruns</strong>). Launching opens one Claude Code window per rerun, interactively: you approve their actions.
            Once imported, each rerun is linked to its original, with what it cost against it.</p>
        </div>
      </header>

      <h2 class="page-sec">Queue <span class="muted">${data.queue.length}</span></h2>
      ${data.queue.length ? `
        <div class="select-bar rerun-bar">
          <strong>${picked.length} selected</strong>
          <button type="button" class="btn" data-rr-all ${picked.length === data.queue.length ? "disabled" : ""}>Select all ${data.queue.length}</button>
          ${picked.length ? '<button type="button" class="link-btn" data-rr-none>Clear</button>' : ""}
          <span class="toolbar-spacer"></span>
          ${picked.length ? `<span class="muted select-hint">their originals cost ${fmtCost(estimate)} together</span>` : ""}
          <button type="button" class="btn primary" data-rr-launch ${picked.length ? "" : "disabled"}>↻ Run ${picked.length || ""} selected</button>
        </div>
        <p id="rr-status" class="replay-status" hidden></p>
        <div class="rr-list">${data.queue.map((q) => `<article class="rr-row ${rerunState.picked.has(q.id) ? "selected" : ""}" data-rr="${q.id}">
          <input type="checkbox" class="psel" data-rr-pick="${q.id}" ${rerunState.picked.has(q.id) ? "checked" : ""} aria-label="Select this rerun">
          <div class="rr-body">${rerunPromptCell(q.prompt)}<div class="rr-settings">${rerunSettings(q)}</div></div>
          <span class="muted rr-added" title="${esc(fmtDate(q.added_at))}">queued ${esc(fmtAgo(q.added_at))}</span>
          <button type="button" class="rr-remove" data-rr-remove="${q.id}" title="Take it out of the queue" aria-label="Take it out of the queue">✕</button>
        </article>`).join("")}</div>`
      : `<div class="card muted rr-empty">Nothing queued. Open a prompt and choose <strong>Add to queue</strong> in <em>Run it again</em>, or select prompts on the
          <a href="#/prompts">Prompts</a> page or a tag's page and choose <strong>Queue reruns</strong>.</div>`}
      <p id="rr-status-empty" class="replay-status" hidden></p>

      <h2 class="page-sec">Launched <span class="muted">${data.launched.length}${data.launched.length ? ` · ${done.length} imported` : ""}</span></h2>
      ${data.launched.length ? `<div class="rr-list">${data.launched.map((x) => {
        const r = x.replay, o = x.original;
        return `<article class="rr-row launched">
          <span class="muted rr-added">${esc(fmtDate(x.launched_at))}</span>
          <div class="rr-body">${rerunPromptCell(o)}<div class="rr-settings">${rerunSettings(x)}</div></div>
          <div class="rr-result">${rerunStatusHtml(x)}${judgeBadge(x.rating)}${x.rating?.summary ? `<span class="rerun-needs">${esc(x.rating.summary)}</span>` : ""}${r
            ? `<a href="#/compare?a=${encodeURIComponent(x.prompt_id)}&b=${encodeURIComponent(r.id)}"><strong>${fmtCost(r.total_cost_usd)}</strong> · ${fmtMs(duration(r))} · compare</a>${o ? refDelta(r.total_cost_usd, o.total_cost_usd, { against: "original" }) : ""}`
            : x.status === "running" ? '<span class="muted">running in the background</span>'
            : `<span class="muted">${x.cost_usd != null ? `${fmtCost(x.cost_usd)} · ` : ""}not imported yet</span>`}</div>
        </article>`;
      }).join("")}</div><p class="muted group-note">${data.launched.some((x) => x.status === "running")
        ? "This page updates itself while reruns are running." : "A rerun's figures show once it was imported: press Refresh if needed."}</p>`
      : '<div class="card muted rr-empty">No rerun launched yet.</div>'}
    </div>`;

  const page = app.querySelector(".reruns-page");
  // while reruns run, check again every few seconds (only while this page is shown)
  clearTimeout(rerunState.timer);
  if (data.launched.some((x) => x.status === "running" || x.rating?.status === "rating")) {
    const here = location.hash;
    rerunState.timer = setTimeout(async () => {
      if (location.hash !== here) return;
      await api("/api/ingest", { method: "POST" }).catch(() => {});
      if (location.hash === here) viewReruns();
    }, 5000);
  }
  page.onclick = async (e) => {
    const pick = e.target.closest("[data-rr-pick]"), row = e.target.closest("article.rr-row:not(.launched)");
    if (e.target.closest("[data-rr-remove]")) {
      await api(`/api/reruns/queue/${e.target.closest("[data-rr-remove]").dataset.rrRemove}`, { method: "DELETE" });
      return viewReruns();
    }
    if (e.target.closest("[data-rr-all]")) { data.queue.forEach((q) => rerunState.picked.add(q.id)); return viewReruns(); }
    if (e.target.closest("[data-rr-none]")) { rerunState.picked.clear(); return viewReruns(); }
    if (e.target.closest("[data-rr-launch]")) return launchPicked(picked, estimate);
    // a click on a queued row (not its links) picks it
    if (row && (pick || !e.target.closest("a"))) {
      const id = Number(row.dataset.rr);
      rerunState.picked.has(id) ? rerunState.picked.delete(id) : rerunState.picked.add(id);
      return viewReruns();
    }
  };
}

async function launchPicked(picked, estimate) {
  const n = picked.length;
  if (!n || !confirm(`Open ${n} terminal window${n === 1 ? "" : "s"}, each running Claude Code on one prompt?\n\nThey run at the same time, ` +
      `and you approve each one's actions. Their originals cost ${fmtCost(estimate)} together.`)) return;
  const { results } = await api("/api/reruns/launch", { method: "POST", body: { ids: picked.map((q) => q.id) } });
  rerunState.picked.clear();
  await viewReruns();
  const opened = results.filter((r) => r.terminal), manual = results.filter((r) => !r.terminal && !r.error), failed = results.filter((r) => r.error);
  const status = document.getElementById("rr-status") || document.getElementById("rr-status-empty");
  status.innerHTML = [
    opened.length ? `${plural(opened.length, "Claude Code window")} opened.` : "",
    manual.length ? `No terminal window could be opened for ${manual.length}: run ${manual.length === 1 ? "this" : "these"} yourself (they are linked once imported):
      <pre><code>${esc(manual.map((r) => r.command).join("\n"))}</code></pre>` : "",
    failed.length ? `${plural(failed.length, "rerun")} could not start: ${failed.map((r) => esc(r.error)).join("; ")}.` : "",
  ].filter(Boolean).join(" ");
  status.hidden = false;
}

// ---- Compare page: any two runs of a prompt, and the LLM judge's call ------------------------

async function viewCompare(query) {
  const a = query.get("a"), b = query.get("b");
  if (!a || !b) { app.innerHTML = '<div class="card status-warn">Pick two runs to compare: open a rerun, then Compare.</div>'; return; }
  const [data, tags, settings] = await Promise.all([api(`/api/compare?${new URLSearchParams({ a, b })}`), api("/api/tags"), api("/api/settings")]);
  const models = tags.filter((t) => t.tag.startsWith("model:")).map((t) => `claude-${t.tag.slice("model:".length)}`);
  const kindLabel = { original: "Original", rerun: "Rerun", reference: "Reference run" };
  const label = (pid) => kindLabel[data.candidates.find((x) => x.id === pid)?.kind] || "Run";
  const picker = (id, current) => `<select id="${id}">${data.candidates.map((x) => `<option value="${esc(x.id)}" ${x.id === current ? "selected" : ""}>
    ${esc(kindLabel[x.kind])} · ${esc(fmtDate(x.started_at))} · ${fmtCost(x.total_cost_usd)}</option>`).join("")}</select>`;
  const labels = [label(a), label(b)];
  if (labels[0] === labels[1]) labels.splice(0, 2, `${labels[0]} 1`, `${labels[1]} 2`);
  app.innerHTML = `
    <div class="prompts-page compare-page">
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Compare</span>Two runs of one prompt. <em>Read the differences, or trust it.</em></h1>
          <p>${esc(data.a.text.slice(0, 220))}${data.a.text.length > 220 ? "…" : ""}</p>
        </div>
      </header>
      <div class="compare-pick card">
        <label>First ${picker("pick-a", a)}</label>
        <button type="button" class="btn" id="swap" title="Swap the two runs">⇄</button>
        <label>Second ${picker("pick-b", b)}</label>
        <span class="muted">The original, each rerun of it, and its tags' reference runs.</span>
      </div>
      <div class="card compare-original">
        ${judgeCardHtml(data.rating, models, settings.judge_model)}
        ${compareRunsHtml(data.a, data.b, labels)}
      </div>
      <p class="muted group-note"><a href="#/prompt/${encodeURIComponent(a)}">Open the first run</a> · <a href="#/prompt/${encodeURIComponent(b)}">Open the second run</a></p>
    </div>`;
  const go = (x, y) => { location.hash = `#/compare?${new URLSearchParams({ a: x, b: y })}`; };
  document.getElementById("pick-a").onchange = (e) => go(e.target.value, b);
  document.getElementById("pick-b").onchange = (e) => go(a, e.target.value);
  document.getElementById("swap").onclick = () => go(b, a);
  bindCompare(a, b, () => viewCompare(query));
  pollJudge(data.rating, () => viewCompare(query));
}

// ---- router ----------------------------------------------------------------

async function route() {
  navigation++;
  destroyCharts();
  const [hash, queryString] = (location.hash.slice(1) || "/").split("?");
  const query = new URLSearchParams(queryString || "");
  let [, view, arg] = hash.split("/");
  // links from before the home page put the prompt filters on #/
  if (!view && ["q", "tag", "project", "sort", "order"].some((k) => query.has(k))) view = "prompts";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active",
    a.dataset.nav === ({ tag: "tags", session: "sessions", prompt: "prompts", compare: "reruns" }[view] || view || "home")));
  try {
    if (view === "prompts") await viewPrompts(query);
    else if (view === "prompt") await viewPrompt(decodeURIComponent(arg), { scope: query.get("compare"), tag: query.get("tag") });
    else if (view === "tags") await viewTags();
    else if (view === "sessions") await viewSessions(query);
    else if (view === "session") await viewSession(decodeURIComponent(arg));
    else if (view === "tag") await viewTag(decodeURIComponent(hash.slice("/tag/".length)), query);
    else if (view === "setup") await viewSetup();
    else if (view === "reruns") await viewReruns();
    else if (view === "compare") await viewCompare(query);
    else await viewHome(query);
  } catch (e) {
    if (e instanceof StaleNavigation) return; // the newer page renders itself
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

// not through api(): it belongs to the top bar, not to a page, and must survive the first navigation
fetch("/api/settings").then((r) => r.json()).then((s) => showCostSource(s.cost_source));

document.getElementById("refresh").onclick = async (e) => {
  e.target.disabled = true;
  try { await api("/api/ingest", { method: "POST" }); await route(); } finally { e.target.disabled = false; }
};
window.addEventListener("hashchange", route);
route();
