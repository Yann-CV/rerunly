"use strict";

const app = document.getElementById("app");
let charts = [];

// ---- helpers ---------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const promptPreview = (text) => {
  const opening = String(text ?? "").replace(/\s+/g, " ").trim();
  return opening.length > 140 ? `${opening.slice(0, 140).trimEnd()}…` : opening;
};
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// bumped on every navigation: a page still loading when the user moves on must not draw over the next one
let navigation = 0;
class StaleNavigation extends Error {}
// requests started by a page's own handlers (typing, timers) can also outlive it: nothing to report
addEventListener("unhandledrejection", (e) => { if (e.reason instanceof StaleNavigation) e.preventDefault(); });

// the example app is the same page served under /examples/, over its own illustrative records
const EXAMPLES = location.pathname.startsWith("/examples");
const BASE = EXAMPLES ? "/examples" : "";

let queueBadgeRequest = 0;

function showQueueCount(count) {
  const badge = document.getElementById("rerun-queue-count");
  if (!badge || !Number.isInteger(count) || count < 0) return;
  badge.textContent = String(count);
  badge.hidden = count === 0;
  badge.closest("a").setAttribute("aria-label", `Reruns, ${count} queued`);
}

async function refreshQueueCount() {
  const request = ++queueBadgeRequest;
  try {
    const response = await fetch(BASE + "/api/reruns/queue/count");
    if (!response.ok) return;
    const data = await response.json();
    if (request === queueBadgeRequest) showQueueCount(data.queued);
  } catch { /* Keep the last known count when temporarily offline. */ }
}

async function api(path, opts = {}) {
  const started = navigation;
  const queueRequest = path.startsWith("/api/reruns") ? ++queueBadgeRequest : null;
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const data = await res.json();
  if (queueRequest === queueBadgeRequest) {
    if (Number.isInteger(data.queued)) showQueueCount(data.queued);
    else if (Array.isArray(data.queue)) showQueueCount(data.queue.length);
  }
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
let ruleTags = [];
let ruleTagKinds = new Map();
const RULE_TAG_CATEGORIES = [["human", "Your tags"], ["project", "Projects"], ["model", "Models"], ["skill", "Skills"], ["agent", "Agents"], ["persona", "Personas"], ["cmd", "Commands"], ["ai", "Other"]];
const ruleTagCategory = (tag) => ruleTagKinds.get(tag) === "human" ? "human"
  : RULE_TAG_CATEGORIES.some(([cat]) => cat === tag.split(":")[0]) ? tag.split(":")[0] : "ai";
const skillsFromTags = (tags) => tags.filter((t) => t.kind === "ai" && t.tag.startsWith("skill:")).map((t) => t.tag.slice("skill:".length)).sort();

/** One rule: match keywords, or include/exclude prompts carrying any chosen tag. */
function ruleRowHtml(i, r = {}) {
  const opt = (value, label, current) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
  const kind = r.preferred_tag ? "exclude" : r.include_tag || r.skill ? "include" : r.exclude ? "exclude_keywords" : "keywords";
  const selected = r.preferred_tag || r.include_tag || (r.skill ? `skill:${r.skill}` : "");
  const offeredTags = selected && !ruleTags.includes(selected) ? [selected, ...ruleTags] : ruleTags;
  return `<div class="rule-row" data-rule-row data-kind="${kind}" style="--rule-color:${ruleColor(i)}">
    <span class="rule-dot" aria-hidden="true"></span>
    <select class="rr-kind" aria-label="Rule type" title="Match keywords, or include/exclude prompts by tag">
      ${opt("keywords", "Include keywords", kind)}${opt("exclude_keywords", "Exclude keywords", kind)}${opt("include", "Include tag", kind)}${opt("exclude", "Exclude tag", kind)}</select>
    <div class="rr-priority">
      <input type="hidden" class="rr-priority-value" value="${esc(selected)}">
      <div class="rr-tag-categories" role="group" aria-label="Filter tags by category">
        <button type="button" class="chip" data-rule-category="all" aria-pressed="true">All tags</button>
        ${RULE_TAG_CATEGORIES.filter(([cat]) => ruleTags.some((tag) => ruleTagCategory(tag) === cat)).map(([cat, label]) => `<button type="button" class="chip" data-rule-category="${cat}" aria-pressed="false">${label}</button>`).join("")}
      </div>
      <input type="search" class="rr-priority-search" aria-label="Search tags taking priority" placeholder="Search tags…" autocomplete="off">
      <p class="rr-priority-selected" aria-live="polite">${selected ? `Selected: ${esc(selected)}` : "Choose a tag below"}</p>
      <div class="rr-priority-options" role="group" aria-label="Choose a tag taking priority">
        ${offeredTags.map((tag) => `<button type="button" class="chip cat-${esc(tagCategory({ tag, kind: ruleTagKinds.get(tag) }))}" data-priority-tag="${esc(tag)}" aria-pressed="${tag === selected}">${esc(tag)}</button>`).join("")}
      </div>
      <p class="rr-priority-empty muted" ${ruleTags.length ? "hidden" : ""}>No matching tags.</p>
    </div>
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
  const selected = row.querySelector(".rr-priority-value").value;
  if (row.dataset.kind === "exclude") return { preferred_tag: selected };
  if (row.dataset.kind === "include") return { include_tag: selected, scope: row.querySelector(".rr-scope").value };
  return {
    keywords: row.querySelector(".rr-keywords").value.split(",").map((k) => k.trim()).filter(Boolean),
    ...(row.dataset.kind === "exclude_keywords" ? { exclude: true } : {}),
    match: row.querySelector(".rr-match").value,
    scope: row.querySelector(".rr-scope").value,
  };
}

/** Live match count of a row. */
function bindRuleRow(row) {
  const search = row.querySelector(".rr-priority-search");
  const choices = [...row.querySelectorAll("[data-priority-tag]")];
  const categories = [...row.querySelectorAll("[data-rule-category]")];
  let category = "all";
  const filterChoices = () => {
    const query = search.value.trim().toLocaleLowerCase();
    choices.forEach((button) => {
      button.hidden = (category !== "all" && ruleTagCategory(button.dataset.priorityTag) !== category)
        || !button.dataset.priorityTag.toLocaleLowerCase().includes(query);
    });
    row.querySelector(".rr-priority-empty").hidden = choices.some((button) => !button.hidden);
    search.placeholder = "Search tags…";
    search.setAttribute("aria-label", "Search tags to include or exclude");
    row.querySelector(".rr-priority-options").setAttribute("aria-label", "Choose a tag to include or exclude");
  };
  search.addEventListener("input", filterChoices);
  row.querySelector(".rr-tag-categories").addEventListener("click", (e) => {
    const button = e.target.closest("[data-rule-category]");
    if (!button) return;
    category = button.dataset.ruleCategory;
    categories.forEach((choice) => choice.setAttribute("aria-pressed", String(choice === button)));
    filterChoices();
  });
  row.querySelector(".rr-priority-options").addEventListener("click", (e) => {
    const button = e.target.closest("[data-priority-tag]");
    if (!button) return;
    row.querySelector(".rr-priority-value").value = button.dataset.priorityTag;
    row.querySelector(".rr-priority-selected").textContent = `Selected: ${button.dataset.priorityTag}`;
    choices.forEach((choice) => choice.setAttribute("aria-pressed", String(choice === button)));
    row.dispatchEvent(new Event("change", { bubbles: true }));
  });
  let timer;
  const update = () => {
    row.dataset.kind = row.querySelector(".rr-kind").value;
    filterChoices();
    const { keywords = [], match, scope, skill } = readRuleRow(row);
    clearTimeout(timer);
    const out = row.querySelector(".rr-preview");
    if (["include", "exclude"].includes(row.dataset.kind)) {
      out.textContent = row.dataset.kind === "exclude" ? "Exclude prompts carrying the selected tag. Exclusions take priority over inclusion rules."
        : row.querySelector(".rr-scope").value === "session" ? "Include every prompt in a session carrying the selected tag." : "Include prompts carrying the selected tag.";
      return;
    }
    if (!keywords.length && !skill) { out.textContent = ""; return; }
    timer = setTimeout(async () => {
      const query = skill ? { skill, scope } : { keywords: keywords.join(","), match, scope };
      const r = await api(`/api/rules/preview?${new URLSearchParams(query)}`);
      // "any" of several keywords is saved as one rule per keyword (the server splits it)
      const split = match === "any" && keywords.length > 1 ? ` · saved as ${keywords.length} rules, one per keyword` : "";
      out.textContent = `${r.matches} ${scope}${r.matches === 1 ? "" : "s"} ${row.dataset.kind === "exclude_keywords" ? "matched for exclusion" : "matched for inclusion"}${split}`;
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
  });
  const add = (focus) => {
    box.insertAdjacentHTML("beforeend", ruleRowHtml(box.children.length));
    bindRuleRow(box.lastElementChild);
    recolor();
    if (focus) box.lastElementChild.querySelector(".rr-keywords").focus();
  };
  box.addEventListener("click", (e) => {
    const remove = e.target.closest("[data-remove-row]");
    if (remove) {
      const row = remove.closest("[data-rule-row]");
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      recolor();
      (next?.querySelector("[data-remove-row]") || addButton).focus();
    }
  });
  addButton.onclick = () => add(true);
  return {
    reset: () => { box.innerHTML = ""; add(false); },
    read: () => [...box.children].map(readRuleRow).filter((r) => r.include_tag || r.preferred_tag || r.skill || r.keywords?.length),
  };
}

function ruleText(r) {
  if (r.exclude) return `Exclude ${r.scope === "session" ? "sessions" : "prompts"} matching ${r.keywords.map((k) => `<code>${esc(k)}</code>`).join(r.match === "any" ? " or " : " and ")}`;
  if (r.include_tag) return `Include ${r.scope === "session" ? "sessions" : "prompts"} tagged <code>${esc(r.include_tag)}</code>`;
  if (r.preferred_tag?.startsWith("skill:")) return `Exclude skill <code>${esc(r.preferred_tag.slice(6))}</code>`;
  if (r.preferred_tag) return `Excluded when tagged <code>${esc(r.preferred_tag)}</code>`;
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
    const remove = removable && t.kind === "human"
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
    <thead><tr><th>When</th><th class="hide-sm">Project</th><th>Prompt</th>${showScore ? '<th class="num" title="Request text overlap; does not measure output quality">Prompt overlap</th>' : ""}
      <th class="num">Cost</th><th class="num">Tokens</th><th class="num">Duration</th><th class="num hide-sm">Subagents</th></tr></thead>
    <tbody>${rows.map((p) => `
      <tr class="clickable ${p.id === currentId ? "current" : ""}" data-id="${esc(p.id)}">
        <td class="num">${fmtDate(p.started_at)}</td>
        <td class="hide-sm">${esc(p.project)}</td>
        <td class="prompt-cell"><div class="text">${esc(p.text)}</div>${p.tags ? tagChips(p.tags) : ""}</td>
        ${showScore ? `<td class="num">${p.score != null ? `${Math.round(p.score * 100)}%` : "—"}</td>` : ""}
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
const listState = { categoryTags: {}, exclude_reruns: false, q: "", tag: "", project: "", range: "all", sort: "recent", order: "used", lockTag: "", ...loadPromptPrefs(), items: [], summary: null, tags: [],
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
  listState.tagCounts = null;
  listState.categoryTags = categoryTagsFromQuery(query);
  listState.exclude_reruns = query.get("exclude_reruns") === "true";
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
  Object.values(listState.categoryTags).flat().forEach((tag) => q.append("tags", tag));
  if (listState.lockTag && listState.tag && listState.tag !== listState.lockTag) q.set("tag", listState.tag);
  if (listState.exclude_reruns) q.set("exclude_reruns", "true");
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
  const q = { q: listState.q, tag: listState.lockTag || listState.tag, project: listState.project, sort: listState.sort, ...extra };
  if (listState.exclude_reruns) q.exclude_reruns = "true";
  if (since) q.since = since().toISOString();
  const params = new URLSearchParams(q);
  Object.values(listState.categoryTags).flat().forEach((tag) => params.append("tags", tag));
  if (listState.lockTag && listState.tag && listState.tag !== listState.lockTag) params.append("tags", listState.tag);
  return params;
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
  const actionsHtml = listState.lockTag && !compact ? `<details class="pop prompt-actions">
    <summary class="pill" aria-label="Actions for this prompt">Actions <span class="caret-d">▾</span></summary>
    <div class="pop-panel">
      <a class="menu-item" target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.id)}">Open prompt</a>
      <button type="button" class="menu-item" data-ref="${esc(p.id)}">${isRef ? "Clear reference" : "Set as reference"}</button>
      <button type="button" class="menu-item" data-queue-prompt="${esc(p.id)}">Queue rerun</button>
      ${listState.tags.some((t) => t.tag === listState.lockTag && t.kind === "human") ? `<button class="menu-item" type="button" data-exclude="${esc(p.id)}" ${isRef ? 'disabled title="Clear the reference before excluding this prompt"' : ""}>Exclude from tag</button>` : ""}
      <span class="prompt-action-status muted" role="status"></span>
    </div></details>` : "";
  return `<article class="prow ${compact ? "compact" : ""} ${picked ? "selected" : ""}" data-id="${esc(p.id)}">
    ${selectable ? `<div class="pwhen"><input type="checkbox" class="psel" data-select="${esc(p.id)}" ${picked ? "checked" : ""}
      aria-label="Select this prompt">${timeHtml}</div>` : timeHtml}
    <div class="pbody">
      <a class="plink" target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.id)}">${p.command ? `<span class="pcmd">/${esc(p.command)}</span> ` : ""}${esc(p.command ? p.text.replace(/^\/\S+\s*/, "") : p.text)}</a>
      ${compact ? "" : `<div class="pchips">${p.project ? `<span class="pproject">${esc(p.project)}</span>` : ""}${rowChips((p.tags || []).filter((t) => tagCategory(t) !== "project"))}</div>`}
    </div>
    <div class="pmetrics">
      <span class="pcost-line">${starHtml}<span class="pcost">${fmtCost(p.total_cost_usd)}</span></span>
      <span class="cost-bar" title="Cost relative to the most expensive prompt shown"><i style="width:${pct}%"></i></span>
      <span class="pmeta">${meta}</span>
      ${actionsHtml}
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
  return `<a class="latest-run" target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.id)}" title="Open the latest run">
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
  if (listState.tag && listState.tag !== listState.lockTag) chips.push(["tag", `Tag: ${listState.tag}`]);
  Object.entries(listState.categoryTags).forEach(([cat, tag]) => chips.push([`category:${cat}`, tag.join(" or ")]));
  if (listState.range !== "all") chips.push(["range", RANGES[listState.range][0]]);
  if (listState.exclude_reruns) chips.push(["exclude_reruns", "Exclude reruns"]);
  if (!chips.length) return "";
  return `<div class="active-filters" aria-label="Active filters">${chips.map(([key, label]) =>
    `<button type="button" class="filter-chip" data-clear="${key}" title="Remove this filter">${esc(label)}<span aria-hidden="true">✕</span></button>`).join("")}
    <button type="button" class="link-btn" data-clear="all">Clear all</button></div>`;
}

function pillMenu(id, label, value, body) {
  return `<details class="pop" id="${id}"><summary class="pill ${value ? "set" : ""}">${label}${value ? `: <strong>${esc(value)}</strong>` : ""}<span class="caret-d">▾</span></summary>
    <div class="pop-panel">${body}</div></details>`;
}

const promptTagCategory = (tag) => tag.includes(":") ? tag.split(":")[0] : "";

function categoryTagsFromQuery(query) {
  const result = {};
  for (const tag of new Set(query.getAll("tags"))) (result[promptTagCategory(tag)] ||= []).push(tag);
  return result;
}

function selectedCategoryTags(state, cat) {
  const selected = [...(state.categoryTags[cat] || [])];
  const legacy = cat === "project" && state.project ? `project:${state.project}`
    : state.tag && state.tag !== state.lockTag && promptTagCategory(state.tag) === cat ? state.tag : "";
  if (legacy && !selected.includes(legacy)) selected.push(legacy);
  return selected;
}

/** What a filter menu counts for `scope`: every active filter, the page's pinned tag apart from the others. */
function tagCountsQuery(state, scope) {
  const query = new URLSearchParams({ scope });
  const since = RANGES[state.range][1];
  if (state.q) query.set("q", state.q);
  if (since) query.set("since", since().toISOString());
  if (state.lockTag) query.set("tag", state.lockTag);
  if (state.project) query.set("project", state.project);
  if (state.exclude_reruns) query.set("exclude_reruns", "true");
  Object.values(state.categoryTags).flat().forEach((tag) => query.append("tags", tag));
  if (state.tag && state.tag !== state.lockTag) query.append("tags", state.tag);
  return query;
}

/** Blank until the counts for the current filters have loaded. */
const tagCount = (state, tag) => state.tagCounts ? state.tagCounts[tag] || 0 : "";

/** Updates the counts in place: an open menu keeps its search and scroll position. */
function showTagCounts(bar, state) {
  bar.querySelectorAll("[data-category-tag]:not([data-category-tag=''])").forEach((button) => {
    const count = tagCount(state, button.dataset.categoryTag);
    button.querySelector(".mi-count").textContent = count;
    button.classList.toggle("no-match", count === 0);
  });
}

function tagFilterMenusHtml(state) {
  const categories = [...new Set(["project", "model", "skill", "agent", "persona", "cmd", ...state.tags.map((t) => promptTagCategory(t.tag)), ...Object.keys(state.categoryTags)])].filter(Boolean);
  return [...categories, ""].map((cat) => {
    const label = { project: "Project", model: "Model", skill: "Skill", agent: "Agent", persona: "Persona", cmd: "Command", "": "Tag" }[cat] || cat.charAt(0).toUpperCase() + cat.slice(1);
    const selected = selectedCategoryTags(state, cat);
    const choices = state.tags.filter((t) => promptTagCategory(t.tag) === cat && t.tag !== state.lockTag);
    const body = `<input type="search" class="menu-search" placeholder="Find ${esc(label.toLowerCase())}…" aria-label="Find ${esc(label.toLowerCase())}">
      <button type="button" class="menu-item ${!selected.length ? "on" : ""}" data-set-category="${esc(cat)}" data-category-tag="">All</button>
      <div class="menu-list">${choices.map((t) => `<button type="button" role="checkbox" aria-checked="${selected.includes(t.tag)}" class="menu-item ${selected.includes(t.tag) ? "on" : ""} ${tagCount(state, t.tag) === 0 ? "no-match" : ""}" data-set-category="${esc(cat)}" data-category-tag="${esc(t.tag)}">
        <span class="filter-check" aria-hidden="true">${selected.includes(t.tag) ? "☑" : "☐"}</span><span class="mi-text">${esc(cat ? t.tag.slice(cat.length + 1) : t.tag)}</span><span class="mi-count">${tagCount(state, t.tag)}</span></button>`).join("") || '<p class="muted">No tags</p>'}</div>`;
    const value = selected.length > 1 ? `${selected.length} selected` : selected.length ? (cat ? selected[0].slice(cat.length + 1) : selected[0]) : "";
    return pillMenu(`pop-category-${cat || "tag"}`, label, value, body);
  }).join("");
}

function promptToolbarHtml(projects) {
  const categoryMenus = tagFilterMenusHtml(listState);
  return `<search class="tags-toolbar prompts-toolbar browse-toolbar">
    <label class="search-box"><span aria-hidden="true">⌕</span>
      <input type="search" id="prompt-search" placeholder="${listState.lockTag ? "Search these runs…" : "Search prompts…"}" value="${esc(listState.q)}" aria-label="Search prompts"></label>
    <div class="browse-filters" role="group" aria-label="Prompt filters">
    <div class="segmented" role="group" aria-label="Period">${Object.entries(RANGES).map(([key, [label]]) =>
      `<button type="button" data-range="${key}" aria-pressed="${key === listState.range}">${label}</button>`).join("")}</div>
    ${categoryMenus}
    </div>
    <div class="browse-actions">
    <div class="prompt-view-controls">
    ${listState.lockTag ? "" : `<div class="segmented" role="group" aria-label="Layout">
      <button type="button" data-layout="list" aria-pressed="${listState.layout === "list"}">List</button>
      <button type="button" data-layout="grouped" aria-pressed="${listState.layout === "grouped"}">By tag</button>
    </div>`}
    </div>
    <span class="toolbar-spacer"></span>
    ${groupedLayout()
      ? pillMenu("pop-order", "Order", GROUP_SORTS[listState.order][1], [...new Set(Object.values(GROUP_SORTS).map(([section]) => section))].map((section) =>
        `<div class="menu-group">${section}</div>${Object.entries(GROUP_SORTS).filter(([, [sec]]) => sec === section).map(([key, [, label]]) =>
          `<button type="button" class="menu-item ${key === listState.order ? "on" : ""}" data-set-order="${key}">${label}</button>`).join("")}`).join("")).replace("pill set", "pill") /* always set: not highlighted as a filter */
      : `<label class="sort-box">Sort <select id="prompt-sort">${Object.entries(PROMPT_SORTS).map(([key, label]) =>
        `<option value="${key}" ${key === listState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>`}
    ${groupedLayout() ? "" : `<button type="button" class="btn ${listState.selecting ? "primary" : ""}" data-toggle-select aria-pressed="${listState.selecting}"
      title="Pick prompts to group them under a tag">${listState.selecting ? "Selecting…" : "Select prompts"}</button>`}
    </div>
  </search>`;
}

async function loadPrompts() {
  // grouped layouts and tag pages (charts, medians) need every matching prompt
  const all = listState.layout === "grouped" || listState.lockTag;
  const filters = promptQuery();
  filters.delete("sort");
  const [data, overview, tagCounts] = await Promise.all([
    api(`/api/prompts?${promptQuery({ limit: all ? 100000 : LIST_PAGE, offset: 0 })}`),
    api(`/api/overview?${filters}`),
    api(`/api/tag-counts?${tagCountsQuery(listState, "prompts")}`),
  ]);
  listState.items = data.items;
  listState.summary = data.summary;
  listState.overview = overview;
  listState.tagCounts = tagCounts;
  showTagCounts(document.getElementById("prompt-toolbar"), listState);
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
  const filtered = Object.keys(listState.categoryTags).length || listState.exclude_reruns || listState.q || listState.project || listState.range !== "all" || (listState.tag && !listState.lockTag);
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
    ${listState.lockTag && listState.tags.some((t) => t.tag === listState.lockTag && t.kind === "human") ? `<button type="button" class="btn" data-exclude-selected ${n ? "" : "disabled"}>Exclude selected from tag</button>` : ""}
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
      const note = document.getElementById("select-note");
      try {
        const out = await api("/api/reruns/queue", { method: "POST", body: { prompt_ids: [...listState.selected], ...settings } });
        note.innerHTML = `${plural(out.added, "rerun")} queued · <a href="#/reruns">${out.queued} on the Reruns page</a>`;
      } catch (error) {
        if (!(error instanceof StaleNavigation)) note.textContent = error.message;
      }
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
    if (e.target.closest("[data-ref], .prompt-actions")) return;
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
    const tag = b.dataset.filterTag, cat = promptTagCategory(tag);
    if (cat === "project") listState.project = tag.slice("project:".length);
    else if (cat) listState.categoryTags[cat] = [tag];
    else listState.tag = tag;
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
    if (key === "all") Object.assign(listState, { q: "", tag: listState.lockTag, project: "", range: "all", exclude_reruns: false, categoryTags: {} });
    else if (key.startsWith("category:")) delete listState.categoryTags[key.slice("category:".length)];
    else listState[key] = key === "exclude_reruns" ? false : key === "range" ? "all" : "";
    rerenderToolbar();
    renderPrompts();
  });
}

let promptProjects = [];

function rerenderToolbar() {
  document.getElementById("prompt-toolbar").innerHTML = promptToolbarHtml(promptProjects);
  bindPromptToolbar();
}

function bindTagFilterMenus(bar, state, rerender, render) {
  bar.querySelectorAll("[data-set-category]").forEach((button) => button.onclick = (event) => {
    event.stopPropagation();
    const cat = button.dataset.setCategory, tag = button.dataset.categoryTag;
    const menuId = button.closest(".pop").id;
    const search = button.closest(".pop").querySelector(".menu-search").value;
    const selected = selectedCategoryTags(state, cat);
    const next = !tag ? [] : selected.includes(tag) ? selected.filter((value) => value !== tag) : [...selected, tag];
    if (next.length) state.categoryTags[cat] = next;
    else delete state.categoryTags[cat];
    if (cat === "project") state.project = "";
    if (state.tag !== state.lockTag && promptTagCategory(state.tag) === cat) state.tag = "";
    rerender();
    const menu = document.getElementById(menuId);
    menu.open = true;
    const find = menu.querySelector(".menu-search");
    find.value = search;
    find.dispatchEvent(new Event("input"));
    render();
  });
  bar.querySelectorAll(".pop .menu-search").forEach((find) => {
    const menu = find.closest(".pop");
    find.oninput = () => menu.querySelectorAll("[data-category-tag]").forEach((button) => {
      button.hidden = !!button.dataset.categoryTag && !button.textContent.toLowerCase().includes(find.value.toLowerCase());
    });
    menu.addEventListener("toggle", () => { if (menu.open) find.focus(); });
  });
}

function bindPromptToolbar() {
  const bar = document.getElementById("prompt-toolbar");
  let timer;
  bar.querySelector("#prompt-search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { listState.q = e.target.value; renderPrompts(); }, 250);
  });
  bar.querySelectorAll("[data-set-project]").forEach((b) => b.onclick = () => { listState.project = b.dataset.setProject; rerenderToolbar(); renderPrompts(); });
  bindTagFilterMenus(bar, listState, rerenderToolbar, renderPrompts);
  bar.querySelectorAll("[data-range]").forEach((b) => b.onclick = () => { listState.range = b.dataset.range; rerenderToolbar(); renderPrompts(); });
  bar.querySelectorAll("[data-layout]").forEach((b) => b.onclick = () => {
    listState.layout = b.dataset.layout;
    if (groupedLayout()) listState.exclude_reruns = false;
    savePromptPrefs();
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

// ---- run it again: what ran a prompt, and the command repeating it ----------------------

const shellQuote = (v) => `'${String(v).replace(/'/g, "'\\''")}'`;
// Transcript values reach a command the user pastes: quote them unless plainly harmless, and keep comments on one line.
const shellWord = (v) => (/^[\w.+-]+$/.test(String(v)) ? String(v) : shellQuote(v));
const shellComment = (v) => String(v).replace(/[\r\n]+/g, " ");

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
  if (r.cwd) lines.push(`cd ${shellQuote(r.cwd)} || exit 1`);
  const undo = [];
  for (const k of r.skills) {
    if (k.origin === "git" && k.git_state === "committed") {
      lines.push(`# ${shellComment(k.skill)} as it ran (commit ${shellComment(k.git_commit.slice(0, 7))}): this replaces the skill's files, commit or stash your changes first`,
        `git -C ${shellQuote(k.git_repo)} checkout ${shellWord(k.git_commit)} -- ${shellQuote(k.git_path)}`);
      undo.push(`git -C ${shellQuote(k.git_repo)} checkout HEAD -- ${shellQuote(k.git_path)}`);
    } else if (k.origin === "bundled") {
      lines.push(`# ${shellComment(k.skill)}: built into Claude Code ${shellComment(k.bundled_version)}, comes with the release below`);
    } else {
      lines.push(`# ${shellComment(k.skill)}: put the downloaded SKILL.md (as it ran) back in ${shellComment(k.base_dir)}`);
    }
  }
  const agent = r.harness === "Codex" ? (r.harness_version ? `npx @openai/codex@${shellWord(r.harness_version)}` : "codex") : (r.harness_version ? `npx @anthropic-ai/claude-code@${shellWord(r.harness_version)}` : "claude");
  const permissions = (r.permission_args || []).map(shellQuote).join(" ");
  lines.push(`${agent}${model ? ` --model ${shellQuote(model)}` : ""}${r.effort ? (r.harness === "Codex" ? ` -c ${shellQuote(`model_reasoning_effort="${r.effort}"`)}` : ` --effort ${shellWord(r.effort)}`) : ""}${permissions ? ` ${permissions}` : ""}${String(p.text).startsWith("-") ? " --" : ""} ${shellQuote(p.text)}`);
  if (undo.length) lines.push("# afterwards, back to the current skills:", ...undo);
  return lines.join("\n");
}

let modelCatalog = [];
async function loadModelCatalog() {
  modelCatalog = await api("/api/models");
  return modelCatalog;
}

function modelAccess(value, defaultModel = "") {
  const id = value || defaultModel;
  const model = modelCatalog.find((m) => m.id === id || m.id === `codex:${id}`)
    || (id === "" ? modelCatalog.find((m) => m.agent === "claude" && m.is_default) : null);
  return model?.access || "unknown";
}

/** Filters only the choices: it never changes the CLI login or silently replaces a saved model. */
function bindModelAccess(select, defaultModel = "") {
  if (!select) return null;
  const label = select.closest("label");
  const filterLabel = document.createElement("label");
  filterLabel.className = "model-access-filter";
  const caption = label.childNodes ? [...label.childNodes].filter((node) => node.nodeType === 3).map((node) => node.textContent).join(" ").trim() : label.firstChild.textContent.trim();
  filterLabel.innerHTML = `Model access <select aria-label="Model access for ${esc(caption)}">
    <option value="all">All models</option><option value="subscription">Subscription</option>
    <option value="api">API credits</option><option value="unknown">Unverified</option></select>`;
  label.before(filterLabel);
  const filter = filterLabel.querySelector("select");
  const note = document.createElement("span");
  note.className = "muted model-access-note";
  note.setAttribute("aria-live", "polite");
  label.append(note);
  const names = {subscription: "Subscription", api: "API credits", unknown: "Unverified"};
  const updateNote = () => {
    select.setCustomValidity(select.selectedIndex < 0 ? "Choose a model in this access filter." : "");
    const access = modelAccess(select.value, defaultModel);
    note.textContent = select.selectedIndex < 0 ? "Choose a model, or change the access filter."
      : access === "subscription" ? "Reported by the subscription account catalog. Limits and extra usage settings still apply."
      : access === "api" ? "This agent is signed in for API billing. Usage requires API credits."
      : "Subscription inclusion is unverified. A model listed in the CLI menu may still require separate usage credits.";
  };
  const apply = () => {
    const selected = select.selectedIndex >= 0 ? select.value : null;
    let visible = 0;
    for (const option of select.options) {
      const access = modelAccess(option.value, defaultModel);
      option.dataset.accessLabel ||= option.textContent;
      option.textContent = `${option.dataset.accessLabel} · ${names[access]}`;
      option.hidden = option.disabled = filter.value !== "all" && access !== filter.value;
      if (!option.hidden) visible++;
    }
    const current = [...select.options].find((o) => o.value === selected && !o.disabled);
    select.selectedIndex = current ? [...select.options].indexOf(current) : -1;
    updateNote();
    if (!visible) note.textContent = "No models verified in this category for the selected agent. Choose another filter.";
  };
  filter.addEventListener("change", () => {
    apply();
    select.dispatchEvent(new Event("change", {bubbles: true}));
  });
  select.addEventListener("change", updateNote);
  apply();
  return {apply};
}

function rerunModelAgent(model, fallback = "claude") {
  return model ? (model.startsWith("codex:") || (model.startsWith("gpt-") && !model.startsWith("gpt-oss")) ? "codex" : "claude") : fallback;
}

const EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

/** Section help stays within the viewport, including inside disclosures. */
function sectionHelp(id, label, explanation) {
  return `<span class="section-help" tabindex="0" role="button" aria-label="About ${esc(label)}" aria-describedby="help-${id}">ⓘ<span class="section-tooltip" id="help-${id}" role="tooltip" hidden>${esc(explanation)}</span></span>`;
}

let sectionHelpEvents;
function bindSectionHelp(root) {
  sectionHelpEvents?.abort();
  sectionHelpEvents = new AbortController();
  let active;
  const hide = () => {
    if (active) active.querySelector('[role="tooltip"]').hidden = true;
    active = null;
  };
  const show = (trigger) => {
    hide();
    active = trigger;
    const tip = trigger.querySelector('[role="tooltip"]');
    tip.hidden = false;
    const rect = trigger.getBoundingClientRect();
    const box = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - box.width - 12))}px`;
    tip.style.top = `${rect.bottom + box.height + 20 <= innerHeight ? rect.bottom + 8 : Math.max(12, rect.top - box.height - 8)}px`;
  };
  root.querySelectorAll('.section-help').forEach((trigger) => {
    trigger.onpointerenter = () => show(trigger);
    trigger.onfocus = () => show(trigger);
    trigger.onpointerleave = () => { if (document.activeElement !== trigger) hide(); };
    trigger.onblur = hide;
    trigger.onclick = (event) => { event.preventDefault(); event.stopPropagation(); show(trigger); };
    trigger.onkeydown = (event) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); hide(); }
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); show(trigger); }
    };
  });
  window.addEventListener('scroll', hide, {capture: true, passive: true, signal: sectionHelpEvents.signal});
  window.addEventListener('resize', hide, {signal: sectionHelpEvents.signal});
  document.addEventListener('pointerdown', (event) => { if (!event.target.closest('.section-help')) hide(); }, {signal: sectionHelpEvents.signal});
}

/** Plan a fresh run with current usage and a separate consumption forecast. */
function replayHtml(p, models, defaultJudge = "") {
  const r = p.rerun;
  const isExample = p.source === "rerunly-examples";
  if (r.harness && !["Claude Code", "Codex"].includes(r.harness)) return "";
  const isCodex = r.harness === "Codex";
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
  return `<div class="replay">
    <div class="replay-head"><span class="rerun-eyebrow">A fresh start</span><h3>Give this prompt another go ${sectionHelp("rerun-intro", "reruns", "Send this request to an agent in a fresh conversation. The recorded directory is reused; the previous conversation and repository state are not restored.")}</h3>
      <p class="muted">A rerun sends this prompt to an agent again, starting a fresh conversation in the recorded working directory. Choose how it runs below. Captured skills can be restored; conversation history and repository state are not restored.</p></div>
    <section class="replay-current" aria-label="Current account status"><div class="replay-impact-head"><h3>Current account status ${sectionHelp("current-usage", "current account status", "Reported usage windows and reset times for the selected agent. These are account readings, separate from estimated API costs, and refresh every minute.")}</h3><button class="btn" type="button" id="refresh-replay-impact">Refresh usage</button></div>
      <div id="replay-current-content" aria-live="polite"><p class="muted">Loading account usage…</p></div></section>
    ${isCodex ? '<p class="muted">Codex costs are API estimates. No USD budget cap is available. Read-only allows commands inside the read-only sandbox; read and edit allows workspace commands too. Restored skills are supplied by exact file path.</p>' : ""}
    ${isExample ? '<p class="muted">Illustrative example. Explore the settings; launching and queueing are disabled.</p>' : ""}
    <form id="replay-form" class="replay-form"><div class="rerun-step"><span>01</span><h3>Make it yours ${sectionHelp("rerun-settings", "rerun settings", "Choose the agent, model, reasoning effort, CLI release and permissions. Terminal runs let you respond to approvals; unattended runs deny actions requiring approval. Restored skills and optional judging also affect the run.")}</h3><p class="muted">Choose your agent, model and how much control to give it.</p></div>
      ${p.references.length ? `<label class="replay-from">Settings from <select id="replay-from">${opt("", "This run", "")}${p.references.map((x, i) =>
        opt(String(i), `Reference of ${x.tag} · ${fmtDate(x.started_at)} (${[x.model, x.effort && `effort ${x.effort}`, x.harness_version && `${agent} ${x.harness_version}`].filter(Boolean).join(", ")})`, "")).join("")}</select></label>` : ""}
      <label>Agent <select id="replay-agent">${opt("claude", "Claude Code", isCodex ? "codex" : "claude")}${opt("codex", "OpenAI Codex", isCodex ? "codex" : "claude")}</select></label>
      <label>Model <select id="replay-model">${opt("", `Same (${same || "default"})`, "")}${others.map((m) => opt(m, m, "")).join("")}</select></label>
      <label>Effort <select id="replay-effort">${opt("", `Same (${r.effort || "default"})`, "")}${EFFORTS.map((e) => opt(e, e, "")).join("")}</select></label>
      <label title="Through npx for a release other than the installed one">CLI release <select id="replay-release">${opt("", "Installed", "")}${releases.map(([v, why]) => opt(v, `${v} · ${why}`, "")).join("")}</select></label>
      <label>Run <select id="replay-mode">${opt("terminal", "In a terminal window", "terminal")}${opt("background", "Unattended, in the background", "terminal")}</select></label>
      <label class="terminal-only">After answer <select id="replay-terminal"><option value="false">Close terminal</option><option value="true">Keep open until Enter</option></select></label>
      <label title="Restore recorded permissions, or explicitly override them">Permissions <select id="replay-permission" required><option value="" disabled>Choose permissions</option>${opt("same", "Same as original", "same")}${opt("ask", "Read-only when unattended", "same")}${opt("edits", "Allow file edits", "same")}</select></label>
      <label class="bg-only" title="It stops when it has spent this much">Stop at $ <input id="replay-budget" type="number" min="0.1" step="any" value="${isCodex ? "" : budget}" size="5" ${isCodex ? "disabled" : ""}></label>
      ${restorable.length ? `<label class="replay-check" title="${esc(restorable.map((k) => k.skill).join(", "))}: rebuilt for this run only, your copies stay untouched">
        <input type="checkbox" id="replay-skills" checked> Restore ${restorable.length === 1 ? "the skill" : `the ${restorable.length} skills`} as ${restorable.length === 1 ? "it" : "they"} ran</label>` : ""}
      <label class="replay-check" title="Once imported, an LLM judge rates how much its output differs from this run's">
        <input type="checkbox" id="replay-rate" checked> Judge it with <select id="replay-judge" data-default-model="${esc(defaultJudge)}">${opt("", `default (${defaultJudge || "Claude Code's"})`, "")}${[...new Set([same, ...models])].filter(Boolean).map((m) => opt(m, m, "")).join("")}</select></label>
      <section class="replay-impact" aria-label="Selected agent usage impact">
        <div class="rerun-step"><span>02</span><h3>Estimated usage impact ${sectionHelp("usage-impact", "estimated usage impact", "Estimates reuse recorded token usage at the selected model’s prices. Time uses the recorded run as a baseline. Actual consumption can differ, judging adds usage, and account-limit forecasts appear when enough local observations are available.")}</h3><p class="muted">What this configuration could use.</p></div>
        <div id="replay-impact-content" aria-live="polite"><p class="muted">Loading the selected agent's usage…</p></div>
      </section>
      <span class="replay-buttons"><button type="button" class="btn" id="queue-replay" ${isExample ? "disabled" : ""} title="Keep it for later, to launch with others from the Reruns page">+ Add to queue</button>
        <button class="btn primary" ${isExample ? "disabled" : ""}>↻ Rerun now</button></span>
    </form>
    <p id="replay-status" class="replay-status" role="status" hidden></p>
  </div>`;
}

function rerunSectionHtml(p, knownModels = [], defaultJudge = "") {
  const r = p.rerun;
  if (!r) return "";
  const models = Object.keys(p.models || {}).map((m) => m.replace("claude-", ""));
  const rows = [
    ["Agent", r.harness ? `${esc(r.harness)}${r.harness_version ? ` <code>${esc(r.harness_version)}</code>` : ""}` : '<span class="muted">unknown</span>'],
    ["Directory", r.cwd ? `<code>${esc(r.cwd)}</code>` : '<span class="muted">not recorded</span>'],
    ["Permissions", Object.keys(r.permissions || {}).length ? `<code>${esc(JSON.stringify(r.permissions))}</code>` : '<span class="muted">not recorded; re-import history or choose explicit permissions</span>'],
    ["Model", `${r.requested_model ? `<code>${esc(r.requested_model)}</code>` : ""}${models.length ? ` <span class="muted">answered by ${esc(models.join(", "))}</span>` : ""}${r.effort ? ` · effort ${esc(r.effort)}` : ""}`],
    ...r.skills.map((k) => [`Skill`, `<strong>${esc(k.skill)}</strong>${k.args ? ` <span class="muted">with “${esc(k.args.slice(0, 80))}${k.args.length > 80 ? "…" : ""}”</span>` : ""}
      <div class="skill-origin">${skillOrigin(k)}</div>
      <div class="skill-actions"><button type="button" class="link-btn" data-view-skill="${esc(k.hash)}">View it as it ran</button>
        <button type="button" class="link-btn" data-download-skill="${esc(k.hash)}">Download SKILL.md</button>
        <span class="muted" title="Hash of the instructions as loaded">#${esc(k.hash.slice(0, 7))}</span></div>`]),
  ];
  const command = p.source !== "rerunly-examples" && r.cwd && r.permission_args && ["Claude Code", "Codex"].includes(r.harness) ? rerunCommand(p) : "";
  return `<details class="card rerun rerun-planner"><summary><span class="rerun-symbol" aria-hidden="true">↻</span><span><strong>Rerun this prompt</strong><span class="muted">A fresh conversation. Your choice of settings.</span></span><span class="rerun-expand" aria-hidden="true">＋</span></summary>
      ${replayHtml(p, knownModels, defaultJudge)}
      <details class="rerun-recorded"><summary>Recorded configuration &amp; skills ${sectionHelp("recorded-settings", "recorded configuration and skills", "Inspect the agent release, model, working directory, permissions and skill snapshots captured for this prompt. View or download the instructions as they were recorded.")}</summary><dl class="rerun-list">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></details>
      ${command ? `<div class="rerun-cmd"><div class="rerun-cmd-head"><span>The same run, from a terminal ${sectionHelp("terminal-command", "terminal command", "Copy a command based on the recorded configuration. Review any skill restoration commands before running them, since they may replace local skill files.")}</span>
          <button type="button" class="btn" id="copy-rerun">Copy</button></div><pre><code>${esc(command)}</code></pre></div>` : ""}
    </details>
    <dialog id="skill-dialog" class="tag-dialog skill-dialog"><div class="skill-dialog-body">
      <div class="rerun-cmd-head"><h2 id="skill-dialog-title"></h2><button type="button" class="btn" data-close-skill>Close</button></div>
      <p class="muted" id="skill-dialog-origin"></p><pre id="skill-dialog-text"></pre></div></dialog>`;
}

function bindRerun(p) {
  const form = document.getElementById("replay-form");
  const settings = (draft = false) => {
    for (const id of ["replay-model", ...(document.getElementById("replay-rate").checked ? ["replay-judge"] : [])]) {
      if (document.getElementById(id)?.selectedIndex === -1) throw new Error("Choose a model.");
    }
    if (!draft && !document.getElementById("replay-permission").value) throw new Error("Choose permissions for the selected agent.");
    return ({
    model: document.getElementById("replay-model").value, effort: document.getElementById("replay-effort").value,
    version: document.getElementById("replay-release").value, mode: document.getElementById("replay-mode").value,
    permission: document.getElementById("replay-permission").value, budget_usd: document.getElementById("replay-budget").value,
    keep_terminal_open: document.getElementById("replay-terminal")?.value === "true",
    restore_skills: document.getElementById("replay-skills")?.checked ?? true,
    rate: document.getElementById("replay-rate").checked, judge_model: document.getElementById("replay-judge").value,
  });
  };
  const modelSelect = document.getElementById("replay-model");
  const agentSelect = document.getElementById("replay-agent");
  const originalAgent = p.rerun.harness === "Codex" ? "codex" : "claude";
  const modelOptions = modelSelect ? [...modelSelect.options].map((o) => ({ value: o.value, label: o.textContent })) : [];
  const filterModels = () => {
    const current = modelSelect.value;
    const options = modelOptions.filter((o) => rerunModelAgent(o.value, originalAgent) === agentSelect.value);
    modelSelect.replaceChildren(...options.map((o) => new Option(o.label, o.value)));
    if (options.some((o) => o.value === current)) modelSelect.value = current;
  };
  if (agentSelect) filterModels();
  const impactBox = document.getElementById("replay-impact-content");
  let impactRequest = 0, impactTimer;
  const updateImpact = async () => {
    if (!impactBox?.isConnected || !modelSelect) return;
    const request = ++impactRequest;
    clearTimeout(impactTimer);
    if (modelSelect.selectedIndex < 0) {
      impactBox.innerHTML = '<p class="muted">Choose a model to see its usage estimate.</p>';
      return;
    }
    impactBox.innerHTML = '<p class="muted">Loading the selected agent’s usage…</p>';
    try {
      const data = await api(`/api/prompts/${encodeURIComponent(p.id)}/rerun-estimate?${new URLSearchParams({ model: modelSelect.value })}`);
      if (!impactBox.isConnected || request !== impactRequest) return;
      const estimate = data.estimate;
      const reached = data.limits?.windows.some((w) => w.used_percent >= 100);
      document.getElementById("replay-current-content").innerHTML = `<section class="replay-impact-current">
        ${reached ? '<p class="status-warn">A reported usage limit is reached. Check its reset time below.</p>' : ""}
        <div class="replay-impact-limits">${data.limits ? homeLimits([data.limits]) : `<p class="muted">No usage report for ${esc(data.agent)}. Check its connection in <a href="#/setup">Setup</a>.</p>`}</div>
        <p class="muted replay-impact-note">Latest reported usage and reset times. Refreshes every minute.</p></section>`;
      impactBox.innerHTML = `<section class="replay-impact-forecast" aria-label="Rerun estimate"><h3>Rerun estimate</h3>
        <div class="replay-impact-estimate">${estimate ? `<span>Estimated additional usage: <strong>≈ ${fmtTokens(estimate.total_tokens)} tokens</strong> · <strong>${estimate.cost_usd == null ? "Cost unavailable" : `≈ ${fmtCost(estimate.cost_usd)}`}</strong> · time baseline ${fmtMs(estimate.duration_ms)}</span>` : '<span>Consumption estimate unavailable.</span>'}</div>
        ${quotaForecast(data.forecast)}
        <p class="muted replay-impact-note">Token and API-cost estimates reuse original token and cache usage. Unchanged models use recorded costs as the baseline; changed models are repriced. Actual usage varies. Judge calls are extra. Limit forecasts learn from local model history; external account activity can affect them.</p></section>`;
    } catch (error) {
      if (!(error instanceof StaleNavigation) && impactBox.isConnected && request === impactRequest) {
        document.getElementById("replay-current-content").innerHTML = '<p class="muted">Account usage unavailable. Refresh usage to retry.</p>';
        impactBox.innerHTML = `<p class="muted">Could not load usage impact: ${esc(error.message)}. Use Refresh usage to retry.</p>`;
      }
    } finally {
      if (impactBox.isConnected && request === impactRequest) impactTimer = setTimeout(updateImpact, 60000);
    }
  };
  document.getElementById("refresh-replay-impact")?.addEventListener("click", updateImpact);
  const updateHarness = () => {
    const selected = modelSelect.value;
    const codex = (modelSelect.selectedIndex < 0 ? agentSelect.value : rerunModelAgent(selected, originalAgent)) === "codex";
    agentSelect.value = codex ? "codex" : "claude";
    for (const option of document.getElementById("replay-release").options) {
      option.disabled = Boolean(option.value) && agentSelect.value !== originalAgent;
    }
    const samePermission = document.getElementById("replay-permission").querySelector('option[value="same"]');
    samePermission.disabled = agentSelect.value !== originalAgent;
    if (samePermission.disabled && samePermission.selected) document.getElementById("replay-permission").value = "";
    const budget = document.getElementById("replay-budget");
    budget.disabled = codex || document.getElementById("replay-mode").value !== "background";
    if (codex) budget.value = "";
    updateImpact();
  };
  if (agentSelect) agentSelect.onchange = () => {
    filterModels();
    document.getElementById("replay-release").value = "";
    document.getElementById("replay-effort").value = "";
    updateHarness();
  };
  if (modelSelect) modelSelect.onchange = updateHarness;
  if (form) updateHarness();
  const rate = document.getElementById("replay-rate");
  if (rate) {
    rate.onchange = () => { document.getElementById("replay-judge").disabled = !rate.checked; };
    rate.onchange();
  }
  // permission and budget only apply to unattended reruns
  const mode = document.getElementById("replay-mode");
  if (mode) {
    mode.onchange = () => {
      form.querySelectorAll(".bg-only").forEach((el) => { el.hidden = mode.value !== "background"; });
      form.querySelectorAll(".terminal-only").forEach((el) => { el.hidden = mode.value !== "terminal"; });
      document.getElementById("replay-budget").disabled = mode.value !== "background" || agentSelect.value === "codex";
    };
    mode.onchange();
  }
  // a reference run's settings, copied into the menus (still editable)
  const from = document.getElementById("replay-from");
  if (from) from.onchange = () => {
    const x = from.value === "" ? null : p.references[Number(from.value)];
    agentSelect.value = rerunModelAgent(x?.model, originalAgent);
    filterModels();
    const set = (id, value, sameValue) => {
      const select = document.getElementById(id);
      const v = !x || !value || value === sameValue ? "" : value;
      if (v && ![...select.options].some((o) => o.value === v)) select.add(new Option(v, v));
      select.value = v;
    };
    set("replay-model", x?.model, p.rerun.requested_model);
    set("replay-effort", x?.effort, p.rerun.effort);
    set("replay-release", x?.harness_version, null);
    updateHarness();
  };
  const queueButton = document.getElementById("queue-replay");
  if (queueButton) queueButton.onclick = async () => {
    const status = document.getElementById("replay-status");
    queueButton.disabled = true;
    try {
      const out = await api("/api/reruns/queue", { method: "POST", body: { prompt_ids: [p.id], ...settings(true) } });
      status.innerHTML = `${out.added ? "Added to the queue" : "Already in the queue with these settings"}: <a href="#/reruns">${out.queued} queued on the Reruns page</a>.`;
      status.hidden = false;
    } catch (error) {
      if (!(error instanceof StaleNavigation)) {
        status.textContent = error.message;
        status.hidden = false;
      }
    } finally { queueButton.disabled = false; }
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
          ? `Agent opened in a new terminal window.${skills} Once it answers, find its progress on the Reruns page.`
          : `No terminal window could be opened from here. Run this in a terminal: the rerun is linked to this prompt once imported.<pre><code>${esc(out.command)}</code></pre>`;
      status.hidden = false;
      updateImpact();
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

function judgeActionHtml(r, models, defaultModel) {
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label)}</option>`;
  const current = r?.model || defaultModel || "";
  const list = [...new Set([current, ...models].filter(Boolean))];
  return `<form class="judge-form" id="judge-form">
      <div class="judge-action-intro"><span class="judge-action-icon" aria-hidden="true">↻</span><div><strong>${r && r.status !== "failed" ? "Take another look" : "Review these outputs"}</strong>
        <span>${EXAMPLES ? "Examples use an illustrative verdict." : "Choose a model to compare both prompts and answers. This adds usage and may incur cost."}</span></div></div>
      <div class="judge-action-controls"><label>Judge with <select id="judge-model" data-default-model="${esc(defaultModel || "")}">${opt("", `Default (${defaultModel || "Claude Code's"})`, "")}${list.map((m) => opt(m, m, "")).join("")}</select></label>
        <button class="btn" type="button" id="queue-judge" ${EXAMPLES ? "disabled" : ""}>＋ Add judgment to queue</button>
        <button class="btn primary" type="submit" ${r?.status === "rating" || EXAMPLES ? "disabled" : ""}><span aria-hidden="true">✦</span> ${r?.status === "rating" ? "Judging…" : r && r.status !== "failed" ? "Judge again" : "Judge them"}</button></div>
      <p class="muted" id="queue-judge-status" role="status" hidden></p>
      <div class="judge-impact" aria-live="polite"><div class="judge-impact-heading"><span aria-hidden="true">◷</span><div><strong>Estimated limit impact</strong><small>Before you run the judge</small></div></div><div id="judge-impact-content"><p class="muted">Estimating usage…</p></div></div>
    </form>`;
}

/** The judge's rating in full, and a way to judge again with a model of your choice. */
function judgeCardHtml(r, models, defaultModel, base, other) {
  const body = !r ? '<p class="muted">Not judged yet.</p>'
    : r.status === "rating" ? `<p class="muted">${esc(r.model || "The judge")} is comparing the two outputs…</p>`
    : r.status === "failed" ? `<p class="status-warn">The judge failed: ${esc(r.error || "no reason given")}</p>`
    : `<div class="judge-result"><div class="judge-score"><span class="judge-label">Substance match</span><div><span class="big">${esc(r.score)}</span><span class="unit"> / 100</span></div><span class="judge-score-caption">Same substance</span>${judgeBadge(r)}</div>
       <div class="judge-findings"><div><span class="judge-label">Conclusion</span><p class="judge-summary">${esc(r.summary)}</p></div>
       ${r.differences.length ? `<div class="judge-observations"><span class="judge-label">Observations</span><ul class="judge-diffs">${r.differences.map((d) => `<li>${esc(d)}</li>`).join("")}</ul></div>` : ""}</div></div>
       <p class="muted judge-meta">Judged by ${esc(r.model || "Claude Code's default model")} · ${esc(fmtDate(r.rated_at))}${r.cost_usd != null ? ` · ${fmtCost(r.cost_usd)}` : ""}</p>
       <form class="human-rating" id="human-rating"><div class="human-rating-heading"><span class="human-rating-icon" aria-hidden="true">✎</span><div><strong>Add your judgment</strong><p>How similar are these outputs in substance? Your score and reason provide context the next time this pair is judged.</p></div></div>
         <div class="human-rating-fields"><label>Your score <span><input name="score" type="number" min="0" max="100" step="1" required value="${esc(r.human_feedback?.human_score ?? r.score)}"> / 100</span></label>
         <label class="human-rating-note">Reason <span class="muted">(optional)</span><textarea name="note" rows="2" maxlength="2000" placeholder="What should the judge have considered?">${esc(r.human_feedback?.note || "")}</textarea></label></div>
         <div class="human-rating-actions"><button class="btn primary" type="submit">${r.human_feedback ? "Update my rating" : "Save my rating"}</button>${r.human_feedback ? '<button class="btn" type="button" data-remove-feedback>Remove rating</button>' : ""}<span class="muted" role="status" id="human-rating-status"></span></div>
         <p class="human-rating-calibration">Your feedback applies to these exact prompts and answers. Existing judge scores stay as recorded.</p></form>`;
  return `<div class="judge-card"><div class="judge-head"><span class="judge-icon" aria-hidden="true">✦</span><div><span class="judge-label">Output review</span><strong>LLM judge</strong>
      <span class="muted">Compares both prompts and whether the second output keeps the first one's substance.</span></div></div>
    ${body}
    ${r?.status === "done" && r.origin === "explicit" && base && other ? `<p class="judge-meta"><a href="${esc(judgeComparisonUrl(base, other))}">Compare judge runs for these prompts →</a></p>` : ""}
    ${judgeActionHtml(r, models, defaultModel)}</div>`;
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
  const samePrompt = (a.text || "").trim() === (b.text || "").trim();
  return `<div class="compare-requests"><div class="compare-answers-head"><strong>Prompts</strong>${samePrompt ? '<span class="trend down">identical</span>' : '<span class="muted">Compare the requests given to each run</span>'}</div>
      <div class="answers"><div><span class="muted">${esc(labels[0])}</span><pre>${esc(a.text || "(no prompt recorded)")}</pre></div>
      <div><span class="muted">${esc(labels[1])}</span><pre>${esc(b.text || "(no prompt recorded)")}</pre></div></div></div>
    <table class="compare-figures"><thead><tr><th></th><th class="num">${esc(labels[0])}</th><th class="num">${esc(labels[1])}</th><th class="num">Change</th></tr></thead>
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
  const humanForm = document.getElementById("human-rating");
  if (humanForm) {
    const status = humanForm.querySelector("#human-rating-status");
    const saveFeedback = async (method) => {
      const button = humanForm.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        const body = {base, other};
        if (method === "PUT") { body.score = Number(humanForm.elements.score.value); body.note = humanForm.elements.note.value; }
        await api("/api/ratings/feedback", {method, body});
        await after();
      } catch (error) { status.textContent = error.message; if (button.isConnected) button.disabled = false; }
    };
    humanForm.onsubmit = (event) => { event.preventDefault(); saveFeedback("PUT"); };
    humanForm.querySelector("[data-remove-feedback]")?.addEventListener("click", () => saveFeedback("DELETE"));
  }
  const form = document.getElementById("judge-form");
  bindModelAccess(document.getElementById("judge-model"), document.getElementById("judge-model")?.dataset.defaultModel || "");
  const impact = document.getElementById("judge-impact-content"), model = document.getElementById("judge-model");
  let impactRequest = 0;
  const updateJudgeImpact = async () => {
    const request = ++impactRequest;
    impact.innerHTML = '<p class="muted">Estimating usage…</p>';
    try {
      const data = await api(`/api/ratings/estimate?${new URLSearchParams({base, other, model: model.value})}`);
      if (!impact.isConnected || request !== impactRequest) return;
      impact.innerHTML = `<p class="judge-impact-tokens"><strong>≈ ${fmtTokens(data.input_tokens + data.output_tokens)} tokens</strong> <span>· ${esc(data.agent)}${data.model ? ` · ${esc(data.model)}` : ""}</span></p>${quotaForecast(data.forecast)}<p class="judge-impact-note">Rough estimate: input uses 1 token per 4 UTF-8 bytes, plus 512 output tokens. Actual tokenization, reasoning and CLI overhead vary; limit forecasts use local observations and may be incomplete.</p>`;
    } catch (error) {
      if (!(error instanceof StaleNavigation) && impact.isConnected && request === impactRequest) impact.innerHTML = `<p class="muted">Usage estimate unavailable: ${esc(error.message)}</p>`;
    }
  };
  if (impact && model) { model.addEventListener("change", updateJudgeImpact); updateJudgeImpact(); }
  const queueJudge = document.getElementById("queue-judge");
  if (queueJudge) queueJudge.onclick = async () => {
    if (!model.reportValidity()) return;
    queueJudge.disabled = true;
    const status = document.getElementById("queue-judge-status");
    try {
      const result = await api("/api/reruns/queue/judge", {method: "POST", body: {base, other, model: model.value}});
      status.innerHTML = result.added ? 'Judgment added to the <a href="#/reruns">queue</a>.' : 'This judgment is already in the <a href="#/reruns">queue</a>.';
    } catch (error) { status.textContent = error.message; }
    status.hidden = false;
    if (queueJudge.isConnected) queueJudge.disabled = false;
  };
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    form.querySelector('button[type="submit"]').disabled = true;
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
      ${judgeCardHtml(o.rating, models, defaultJudge, o.id, p.id)}
      ${compareRunsHtml(o, rerun)}
    </div>`;
}

/** On a rerun's page: what it reruns, against it, and the configuration chosen when launching it. */
function rerunOfHtml(p) {
  const o = p.replay_of;
  const original = o.id
    ? `<a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(o.id)}">the run of ${esc(fmtDate(o.started_at))}</a> (${fmtCost(o.total_cost_usd)})`
    : "a prompt no longer in the history";
  return `<div class="rerun-of">
    <span class="ref-label">↻ Rerun</span>
    ${rerunStatusHtml(o)}${o.first ? judgeBadge(o.rating) : ""}
    <span>${o.first ? `Rerun of ${original} ${o.id ? refDelta(p.total_cost_usd, o.total_cost_usd, { against: "original" }) : ""}` : `Follow-up in the rerun of ${original}`}</span>
    <a class="link-btn" href="#/prompt/${encodeURIComponent(p.id)}?jump=rerun-setup" data-jump="rerun-setup">Initial run and settings</a>
    <a class="link-btn" href="#/tag/rerun">All reruns</a>
  </div>`;
}

/** On a rerun's page: the initial run, the settings chosen to rerun it ("same" spelled out as the initial run's
    value), and what the rerun actually ran with when it differs from what was asked. */
function rerunSetupHtml(p) {
  const o = p.replay_of, ran = p.rerun || {};
  const dash = '<span class="muted">–</span>';
  const val = (v) => (v ? esc(v) : dash);
  const agent = (h, v) => (h ? esc(`${h}${v ? ` ${v}` : ""}`) : dash);
  const model = (v) => (v ? esc(v.replace(/^codex:/, "")) : "");
  const initialModel = o.original_model || (o.original_models || []).join(", ");
  const skillList = (list, part) => list.length ? list.map((k) => `<span class="rr-chip">${esc(`${k.skill.split(":").pop()} @ ${part(k)}`)}</span>`).join("") : dash;
  // the rerun's value: what was asked, then what ran when that says more ("same" resolved, a default made explicit)
  const both = (asked, actual) => (actual && actual !== asked.text ? `${asked.html}<span class="muted"> → ran ${esc(actual)}</span>` : asked.html);
  const same = (v, initial, fallback = "default") => (v ? { text: v, html: esc(v) } : { text: initial, html: `same <span class="muted">(${esc(initial || fallback)})</span>` });
  const release = o.harness_version ? { text: o.harness_version, html: esc(o.harness_version) } : { text: "", html: '<span class="muted">(installed release)</span>' };
  const rows = [
    ["When", o.id ? `<a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(o.id)}">${esc(fmtDate(o.started_at))}</a>` : dash,
      `launched ${esc(fmtDate(o.launched_at))}`],
    ["Agent", agent(o.original_harness, o.original_harness_version),
      both({ ...release, html: `${esc(ran.harness || o.original_harness || "")} ${release.html}` }, ran.harness_version)],
    ["Model", val(model(initialModel)), both(same(model(o.model), model(initialModel), "the agent's default"), model(ran.requested_model))],
    ["Effort", val(o.original_effort), both(same(o.effort, o.original_effort), ran.effort)],
    ["Skills", skillList(o.original_skills || [], (k) => k.hash.slice(0, 7)),
      (o.skills || []).length ? skillList(o.skills, (k) => k.source) : skillList(ran.skills || [], (k) => k.hash.slice(0, 7))],
    ["How it ran", "by you", o.mode === "terminal" ? "in a terminal" : "unattended"],
    ["Permissions", "as you granted them", o.permission === "edits" ? "may edit files" : o.permission === "ask" ? (o.mode === "terminal" ? "ask for approval" : "read-only")
      : "the initial run's"],
    ["Budget", dash, o.budget_usd ? `up to ${fmtCost(o.budget_usd)}` : "no cap"],
    ["Cost", o.id ? fmtCost(o.total_cost_usd) : dash, `${fmtCost(p.total_cost_usd)} ${o.id && o.first ? refDelta(p.total_cost_usd, o.total_cost_usd, { against: "original" }) : ""}`],
  ];
  return `<h2 class="page-sec" id="rerun-setup">Initial run and rerun settings</h2>
    <div class="card rerun-setup">
      ${o.id ? `<p class="muted">Same prompt as the initial run${o.original_project ? `, in ${esc(o.original_project)}` : ""}. “Same” settings repeat the initial run's value, shown in brackets.</p>`
        : '<p class="muted">The initial run is no longer in the history (excluded on the Setup page): only the rerun settings are known.</p>'}
      <div class="table-wrap"><table>
        <thead><tr><th scope="col">Setting</th><th scope="col">Initial run</th><th scope="col">This rerun</th></tr></thead>
        <tbody>${rows.map(([label, initial, rerun]) => `<tr><th scope="row">${label}</th><td>${initial}</td><td>${rerun}</td></tr>`).join("")}</tbody>
      </table></div>
    </div>`;
}

/** What the prompt produced, folded: its final answer and the files it edited. */
function answerSectionHtml(p) {
  const answer = p.answer || p.replay_of?.result || "";
  const files = p.files || [];
  const meta = [answer ? plural(answer.split("\n").length, "line") : "no answer recorded",
    files.length ? plural(files.length, "file") + " edited" : ""].filter(Boolean).join(" · ");
  return `<details class="tag-page-section prompt-output" id="prompt-output">
      <summary><h2>Output ${sectionHelp("prompt-output", "prompt output", "The final answer and edited file paths recorded for this prompt. Expand this section to read the answer; missing output may indicate an interrupted or incomplete transcript.")}</h2><span>Final answer and files edited · ${esc(meta)}</span></summary>
      <div class="tag-section-content">
        ${answer ? `<pre class="prompt-answer">${esc(answer)}</pre>`
          : '<p class="muted">No final answer recorded: the agent was interrupted, or the transcript was imported before it answered.</p>'}
        ${files.length ? `<div class="compare-files"><strong>Files edited</strong><ul>${files.map((f) => `<li><code>${esc(f)}</code></li>`).join("")}</ul></div>` : ""}
      </div>
    </details>`;
}

/** Scrolls to a section of the page (smoothly when asked and motion is welcome). */
function jumpTo(id, smooth = false) {
  document.getElementById(id)?.scrollIntoView({ behavior: smooth && !matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto" });
}

async function viewPrompt(id) {
  const [p, allTags, settings] = await Promise.all([api(`/api/prompts/${encodeURIComponent(id)}`), api("/api/tags"), api("/api/settings")]);
  const knownModels = (await loadModelCatalog()).map((m) => m.id);
  ruleSkills = skillsFromTags(allTags);
  const facts = [
    ["Total cost", fmtCost(p.total_cost_usd), "", costNote(p)],
    ["Total tokens", fmtTokens(p.total_tokens), "", "incl. cache and subagents"],
    ["Active time", fmtMs(p.active_ms), "", `wall ${fmtMs(p.wall_ms)}`],
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
  ].join("");
  const unpriced = (p.unpriced_models || []).length
    ? `<div class="card status-warn" style="margin-top:8px">No price configured for ${esc(p.unpriced_models.join(", "))}: their cost is counted as 0. Add them on the <a href="#/setup">Setup</a> page.</div>` : "";
  app.innerHTML = `
    <div class="prompt-page">
    <a class="crumb" href="#/prompts">← All prompts</a>
    <header class="prompt-head">
      ${pageKicker("Prompt", "The request, the answer, and every agent and token behind it.")}
      <div class="prompt-meta"><time datetime="${esc(p.started_at)}">${fmtDate(p.started_at)}</time>${p.project ? `<span>${esc(p.project)}</span>` : ""}
        ${p.command ? `<span class="pcmd">/${esc(p.command)}</span>` : ""}<span class="prompt-cost">${fmtCost(p.total_cost_usd)}</span>
        <button type="button" class="link-btn danger-link" id="delete-prompt" title="Remove this prompt from Rerunly's history; its transcript is kept">Delete prompt</button></div>
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
    ${answerSectionHtml(p)}
    ${rerunSectionHtml(p, knownModels, settings.judge_model)}

    <h2 class="page-sec">Cost per agent ${sectionHelp("agent-usage", "cost per agent", "Recorded token usage, estimated costs, elapsed time and tool calls for the main agent and each linked subagent. Scroll the table horizontally on small screens to see every column.")}</h2>
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

    </div>
    ${tagDialogHtml({ forPrompts: true })}`;
  bindRerun(p);
  bindSectionHelp(app);
  bindRowClicks();
  document.querySelectorAll("[data-remove-tag]").forEach((b) => b.onclick = async () => {
    const tag = b.dataset.removeTag;
    try { await api(`/api/prompts/${encodeURIComponent(p.id)}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" }); }
    catch (error) { if (!(error instanceof StaleNavigation)) alert(error.message); return; }
    route();
  });
  document.getElementById("delete-prompt").onclick = async (e) => {
    if (!confirm("Delete this prompt from the history?\n\nIts subagents, tags, ratings and queued reruns go with it, and it is not imported again. " +
      "Its transcript is kept.")) return;
    e.target.disabled = true;
    try { await api(`/api/prompts/${encodeURIComponent(p.id)}`, { method: "DELETE" }); }
    catch (error) { e.target.disabled = false; if (!(error instanceof StaleNavigation)) alert(error.message); return; }
    location.hash = "#/prompts";
  };
  bindTagDialog(app.querySelector(".prompt-page"), {
    tags: allTags, targets: () => [p.id], exclude: p.tags.map((t) => t.tag),
    title: () => "Add a tag to this prompt", done: () => route(),
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
    <ul class="recent-prompts">${recent.map((r) => `<li><a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(r.id)}">
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
            <small>Choose <strong>Include keywords</strong> or <strong>Exclude keywords</strong> to match text, or <strong>Include tag</strong> or <strong>Exclude tag</strong> to select any model, skill, project, agent, or tag of yours.
              Include adds matching prompts; Exclude keeps them out and takes priority. Rules apply to existing and future prompts. “In a session” includes every prompt of a matching session.
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
  ruleTags = tags.map((t) => t.tag).sort();
  ruleTagKinds = new Map(tags.map((t) => [t.tag, t.kind]));
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
      <search class="tags-toolbar browse-toolbar">
        <label class="search-box"><span aria-hidden="true">⌕</span>
          <input type="search" id="tags-search" placeholder="Search tags…" value="${esc(tagsState.q)}" aria-label="Search tags"></label>
        <div class="browse-filters" role="group" aria-label="Tag filters">
        <div class="tag-legend" role="group" aria-label="Categories">${TAG_CATEGORIES.map(([cat, label, help]) =>
          `<button type="button" class="chip cat-${cat}" data-cat="${cat}" title="${esc(help)}">${label}</button>`).join("")}</div>
        </div>
        <div class="browse-actions">
        <div class="segmented" role="group" aria-label="Layout">
          <button type="button" data-view="grouped">Grouped</button>
          <button type="button" data-view="timeline">Timeline</button>
        </div>
        <span class="toolbar-spacer"></span>
        <label id="tags-sort" class="sort-box">Sort
          <select id="tags-sort-select">${Object.entries(TAG_SORTS).map(([key, [label]]) =>
            `<option value="${key}" ${key === tagsState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <button class="btn primary" type="button" data-new-tag>+ New tag</button>
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

function tagRulesHtml(info, rules) {
  return tagCategory(info) !== "human" ? "" : `<section class="rules-card" aria-labelledby="rules-title">
      <div class="rules-head"><h2 id="rules-title">Tag rules</h2>
        <span class="muted">Match keywords or skills to assign this tag, or give another tag priority to prevent assignment. Rules apply to existing and future prompts.</span></div>
      ${rules.length ? `<div class="rule-chips">${rules.map((r, i) => `<span class="rule-chip" style="--rule-color:${ruleColor(i)}" data-rule-pos="${i}">
        <button type="button" class="rule-edit" data-edit-rule="${i}" title="Change this rule"><span class="rule-dot" aria-hidden="true"></span>${ruleText(r)}</button>
        <button type="button" class="rule-x" data-delete-rule="${r.index}" title="Remove this rule" aria-label="Remove this rule">✕</button></span>`).join("")}</div>` : ""}
      <form id="rule-edit-form" class="rule-edit-form" hidden><div class="rules-editor"></div>
        <div class="rule-edit-actions"><button class="btn primary">Save</button><button type="button" class="btn" data-cancel-rule>Cancel</button></div></form>
      <button type="button" class="btn add-rule-btn" data-add-keywords>+ Add a rule</button>
    </section>`;
}

function tagHeadHtml(info) {
  const cat = tagCategory(info);
  const [prefix, ...rest] = info.tag.split(":");
  const name = rest.length ? `<span class="tag-prefix">${esc(prefix)}:</span>${esc(rest.join(":"))}` : esc(info.tag);
  const help = CATEGORY_INFO[cat].help;
  const meta = info.prompts ? [
    `Created ${esc(fmtDate(info.created_at))}`,
    `last used <span title="${esc(fmtDate(info.last_used))}">${fmtAgo(info.last_used)}</span>`,
  ] : ["No prompt carries this tag yet"];
  return `<header class="tag-head cat-${cat}">
      ${pageKicker("Tag", cat === "human"
        ? "Every run of this task: is it getting cheaper or faster? Select runs to regroup them under another tag."
        : "Every run where this applied: how its cost and time evolve. Select runs to group them under a tag of yours.")}
      <div class="tag-head-title"><span class="tag-dot"></span><h1>${name}</h1>
        ${cat === "human" ? '<span class="by-you">yours</span><button type="button" class="link-btn" id="rename-tag">Rename</button>' : `<span class="by-ai">${CATEGORY_INFO[cat].label}</span>`}</div>
      ${cat === "human" ? `<form id="rename-tag-form" class="rename-tag-form" hidden><label for="rename-tag-input">Edit tag name</label><p>Prompt assignments, rules and history will follow the new name.</p><div class="rename-tag-fields"><input id="rename-tag-input" value="${esc(info.tag)}" required autocomplete="off"><button type="submit" class="btn primary">Save name</button><button type="button" class="btn" id="rename-tag-cancel">Cancel</button></div><p id="rename-tag-error" class="status-warn" role="alert" hidden></p></form>` : ""}
      ${cat === "human"
        ? `<div class="tag-desc-edit"><p class="tag-head-desc" id="tag-desc">${info.description ? esc(info.description) : '<span class="muted">No description yet.</span>'}</p>
            <button type="button" class="link-btn" id="edit-desc">${info.description ? "Edit" : "Add a description"}</button></div>
          <form id="desc-form" class="inline-form desc-form" hidden><textarea id="desc-input" rows="2" placeholder="What this task is">${esc(info.description || "")}</textarea>
            <button class="btn primary">Save</button><button type="button" class="btn" id="desc-cancel">Cancel</button></form>`
        : `<p class="tag-head-sub">${esc(help[0].toUpperCase() + help.slice(1))}</p>`}
      <div class="tag-head-meta">${meta.map((m) => `<span>${m}</span>`).join("")}
        ${cat === "human" ? '<button type="button" class="link-btn danger-link" id="delete-tag" title="Remove this tag from every prompt, with its rules">Delete tag</button>' : ""}</div>
      <div id="tag-reference"></div>
    </header>`;
}

function tagSummaryHtml(items, s) {
  const costs = [...items].reverse().map((p) => p.total_cost_usd || 0);
  const fact = (label, value, sub = "", tone = "var(--accent)") => `<div class="tag-stat" style="--stat-color:${tone}"><dt>${label}</dt><dd class="tag-stat-value">${value}</dd><dd class="tag-stat-note">${sub}</dd></div>`;
  return `<section class="tag-overview" aria-label="Tag summary">
    <div class="tag-overview-head"><h2>At a glance</h2><span class="tag-overview-range">${esc(RANGES[listState.range][0])}</span></div>
    <dl class="tag-stats">
    ${fact("Runs", s.count || 0, "in this selection")}
    ${fact("Total cost", fmtCost(s.cost_usd || 0), `${fmtTokens(s.tokens || 0)} tokens`, "var(--tag-model)")}
    ${fact("Median cost", fmtCost(median(costs)), trendBadge(costs) || "per run", "var(--tag-skill)")}
    ${fact("Median time", fmtMs(median(items.map(duration))), "per run, active time", "var(--tag-project)")}
    ${listState.reference ? fact("Median vs reference", refDelta(median(costs), listState.reference.total_cost_usd, { long: true }) || "–",
      `reference run ${fmtCost(listState.reference.total_cost_usd)}`) : ""}
    </dl>
  </section>`;
}

/** The tag's reference run, or how to pick one. */
function referenceCardHtml() {
  const r = listState.reference;
  if (!r) return `<p class="ref-hint muted">No reference run yet: click ☆ on a run below to compare every other run with it.</p>`;
  return `<div class="ref-card"><div class="ref-main"><span class="ref-label"><span class="ref-star" aria-hidden="true">★</span> Reference prompt</span>
    <a class="ref-text" target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(r.id)}" title="${esc(r.text)}">${esc(r.text)}</a>
    <span class="pmeta ref-metadata">${fmtDate(r.started_at)} · ${fmtTokens(r.total_tokens)} tokens</span></div>
    <dl class="ref-metrics">
      <div><dt>Cost</dt><dd>${fmtCost(r.total_cost_usd)}</dd></div>
      <div><dt>${r.active_ms != null ? "Active time" : "Duration"}</dt><dd>${fmtMs(duration(r))}</dd></div>
    </dl>
    <button type="button" class="link-btn" data-ref="${esc(r.id)}">Clear</button></div>`;
}

function bindTagRename(tag, enc) {
  const form = document.getElementById("rename-tag-form");
  const input = document.getElementById("rename-tag-input");
  const button = document.getElementById("rename-tag");
  const errorBox = document.getElementById("rename-tag-error");
  const toggle = (editing) => {
    form.hidden = !editing;
    button.hidden = editing;
    button.setAttribute("aria-expanded", String(editing));
    errorBox.hidden = true;
    if (editing) { input.value = tag; input.focus(); input.select(); }
    else button.focus();
  };
  button.setAttribute("aria-controls", "rename-tag-form");
  button.setAttribute("aria-expanded", "false");
  button.onclick = () => toggle(true);
  document.getElementById("rename-tag-cancel").onclick = () => toggle(false);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    errorBox.hidden = true;
    submit.disabled = true;
    try {
      const result = await api(`/api/tags/${enc}/name`, { method: "PUT", body: { name: input.value } });
      if (result.tag === tag) { toggle(false); return; }
      location.hash = `#/tag/${encodeURIComponent(result.tag)}`;
    } catch (error) {
      if (!(error instanceof StaleNavigation)) { errorBox.textContent = error.message; errorBox.hidden = false; }
    } finally { submit.disabled = false; }
  };
}

async function viewTag(tag, query = new URLSearchParams()) {
  filtersFromQuery(query);
  Object.assign(listState, { lockTag: tag, tag: listState.tag || tag, split: query.get("split") in SPLITS ? query.get("split") : "" });
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
      ${tagHeadHtml(info)}
      <details class="tag-page-section" open>
        <summary><h2>Global summary</h2><span>Costs, time and highlights</span></summary>
        <div class="tag-section-content"><div id="prompt-summary"></div><div id="prompt-highlights"></div></div>
      </details>
      <details class="tag-page-section tag-evolution" open>
        <summary><h2>Evolution</h2><span>Compare runs over time</span></summary>
        <div class="tag-section-content"><div id="tag-charts"></div></div>
      </details>
      <details class="tag-page-section" open>
        <summary><h2>Prompts</h2><span>Rules, exclusions and all runs</span></summary>
        <div class="tag-section-content">
          ${tagRulesHtml(info, rules)}
          <div id="tag-curation"></div>
          <div id="prompt-toolbar"></div>
          <div id="select-bar"></div>
          <div id="active-filters"></div>
          <div id="prompt-results"></div>
        </div>
      </details>
    </div>
    ${tagDialogHtml({ forPrompts: true })}`;
  if (tagCategory(info) === "human") bindTagRename(tag, enc);
  Object.assign(listState, { selecting: false, selected: new Set() });
  app.querySelectorAll(".tag-page-section").forEach((section) => {
    section.addEventListener("toggle", () => {
      if (section.open) requestAnimationFrame(() => charts.forEach((chart) => chart.resize()));
    });
  });
  bindSelection(app.querySelector(".tag-page"), { tags, exclude: [tag] });
  app.querySelector(".tag-page").addEventListener("click", async (e) => {
    const button = e.target.closest("[data-queue-prompt]");
    if (!button) return;
    e.preventDefault();
    button.disabled = true;
    const status = button.closest(".prompt-actions").querySelector(".prompt-action-status");
    try {
      const result = await api("/api/reruns/queue", { method: "POST", body: { prompt_ids: [button.dataset.queuePrompt] } });
      status.textContent = result.added ? "Rerun queued" : "Already queued";
    } catch (error) {
      if (!(error instanceof StaleNavigation)) { status.textContent = error.message; button.disabled = false; }
    }
  });
  // the curation card is filled once the page is usable (below, human tags only); the reference changes what it flags
  let refreshCuration = () => {};
  // ☆ on a run makes it the reference, ★ (or Clear on the card) clears it
  app.querySelector(".tag-page").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ref]");
    if (!b) return;
    e.preventDefault();
    const id = listState.reference?.id === b.dataset.ref ? null : b.dataset.ref;
    listState.reference = (await api(`/api/tags/${enc}/reference`, { method: "PUT", body: { prompt_id: id } })).reference;
    renderPrompts({ reload: false });
    refreshCuration();
  });
  rerenderToolbar();
  await renderPrompts();
  if (tagCategory(info) !== "human") return;
  // flagged prompts and excluded examples come after the page is usable: the check can take a while on a large tag
  let flagged = [], redundantExclusions = [];
  const selectedExclusions = new Set();
  const syncExcludedSelection = () => {
    const box = document.getElementById("tag-curation");
    box.querySelectorAll("[data-pick-exclusion]").forEach((input) => { input.checked = selectedExclusions.has(input.dataset.pickExclusion); });
    const count = box.querySelector("[data-exclusion-count]");
    if (count) count.textContent = `${selectedExclusions.size} selected`;
    box.querySelectorAll("[data-exclusions-action]").forEach((button) => { button.disabled = !selectedExclusions.size; });
  };
  app.querySelector(".tag-page").addEventListener("change", (e) => {
    const input = e.target.closest("[data-pick-exclusion]");
    if (!input) return;
    selectedExclusions[input.checked ? "add" : "delete"](input.dataset.pickExclusion);
    syncExcludedSelection();
  });
  const failed = (error) => { if (!(error instanceof StaleNavigation)) alert(error.message); };
  app.querySelector(".tag-page").addEventListener("click", async (e) => {
    const button = e.target.closest("[data-exclude], [data-exclude-selected], #exclude-flagged, [data-restore], [data-example], #forget-redundant, [data-exclusions-select], [data-exclusions-action]");
    if (!button) return;
    e.preventDefault();
    if (button.hasAttribute("data-exclusions-select")) {
      selectedExclusions.clear();
      if (button.dataset.exclusionsSelect === "all") document.querySelectorAll("[data-pick-exclusion]").forEach((input) => selectedExclusions.add(input.dataset.pickExclusion));
      syncExcludedSelection();
      return;
    }
    try {
      if (button.hasAttribute("data-exclusions-action")) {
        const ids = [...selectedExclusions];
        if (!ids.length) return;
        button.disabled = true;
        if (button.dataset.exclusionsAction === "restore") {
          await api(`/api/tags/${enc}/exclusions/restore`, { method: "POST", body: { prompt_ids: ids } });
        } else {
          await api(`/api/tags/${enc}/exclusions`, { method: "PUT", body: { prompt_ids: ids, example: false } });
        }
        selectedExclusions.clear();
        await renderPrompts();
        refreshCuration();
        return;
      }
      if (button.hasAttribute("data-restore")) {
        await api(`/api/tags/${enc}/exclusions/restore`, { method: "POST", body: { prompt_ids: [button.dataset.restore] } });
      } else if (button.hasAttribute("data-example") || button.id === "forget-redundant") {
        const ids = button.id === "forget-redundant" ? redundantExclusions.map((p) => p.id) : [button.dataset.example];
        await api(`/api/tags/${enc}/exclusions`, { method: "PUT", body: { prompt_ids: ids, example: button.dataset.use === "1" } });
      } else {
        const ids = button.id === "exclude-flagged" ? flagged.map((p) => p.id)
          : button.hasAttribute("data-exclude-selected") ? [...listState.selected] : [button.dataset.exclude];
        const out = await api(`/api/tags/${enc}/prompts`, { method: "DELETE", body: { prompt_ids: ids } });
        if (out.skipped_reference) alert("The reference run stays in the tag: clear it as the reference to exclude it.");
      }
      route();
    } catch (error) { syncExcludedSelection(); failed(error); }
  });
  const loadCuration = async () => {
    const box = document.getElementById("tag-curation");
    let curation;
    try { curation = await api(`/api/tags/${enc}/curation`); }
    catch (error) { if (box && !(error instanceof StaleNavigation)) box.innerHTML = `<p class="muted">Could not check this tag for irrelevant prompts: ${esc(error.message)}</p>`; return; }
    if (!box?.isConnected) return; // the page changed meanwhile
    const exclusionsOpen = box.querySelector(".tag-exclusions")?.open;
    flagged = curation.flagged;
    redundantExclusions = curation.redundant_exclusions;
    const examples = curation.excluded.filter((p) => p.example).length;
    box.innerHTML = `
      <section class="card" aria-label="Tag curation">
        ${flagged.length ? `<div role="status"><strong>⚠ ${flagged.length} possibly irrelevant prompt${flagged.length === 1 ? "" : "s"}</strong>
          <p>These prompts resemble examples you excluded as irrelevant (${Math.round(curation.threshold * 100)}% word overlap or more). Review their meaning before excluding them. Your reference run is never flagged.</p>
          <button class="btn" type="button" id="exclude-flagged">Exclude all ${flagged.length} flagged prompts</button>
          ${flagged.map((p) => `<div class="setup-row"><div><a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.id)}">${esc(promptPreview(p.text))}</a>
            <p class="muted">${Math.round(p.similarity * 100)}% overlap with excluded example: <a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.similar_to.id)}">${esc(promptPreview(p.similar_to.text))}</a></p></div>
            <button class="btn" type="button" data-exclude="${esc(p.id)}">Exclude from tag</button></div>`).join("")}</div>`
          : '<p>No prompts resemble your excluded examples. Existing and new prompts are checked when you open this page.</p>'}
        <details class="tag-exclusions" ${exclusionsOpen ? "open" : ""}>
          <summary><strong>${curation.excluded.length} prompt${curation.excluded.length === 1 ? "" : "s"} excluded as irrelevant</strong><span class="muted">${examples} used as example${examples === 1 ? "" : "s"}</span><span class="muted">Manage exclusions</span></summary>
          <div class="tag-exclusions-body">
        <p>These prompts stay out of this tag; ${examples === curation.excluded.length ? "all" : examples} of them serve as examples for relevance warnings. Restore puts a prompt back the way it had the tag: by hand, or through your rules if they still match. Older exclusions with no recorded assignment are restored by hand. A tag priority can still hold it back. Similarity alone never excludes a prompt.</p>
        ${curation.excluded.length ? `<div class="excluded-selection" role="group" aria-label="Selected excluded prompts">
          <strong data-exclusion-count aria-live="polite">0 selected</strong>
          <button class="link-btn" type="button" data-exclusions-select="all">Select all</button>
          <button class="link-btn" type="button" data-exclusions-select="none">Clear selection</button>
          <button class="btn" type="button" data-exclusions-action="ignore" disabled title="Keep these prompts excluded but stop using them for relevance warnings">Ignore selected as examples</button>
          <button class="btn" type="button" data-exclusions-action="restore" disabled>Restore selected to tag</button>
        </div>` : ""}
        ${redundantExclusions.length ? `<p role="status">${redundantExclusions.length} example${redundantExclusions.length === 1 ? " resembles" : "s resemble"} another excluded example. They can stop serving as examples and stay excluded.</p>
          <button class="btn" type="button" id="forget-redundant">Stop using ${redundantExclusions.length} redundant example${redundantExclusions.length === 1 ? "" : "s"}</button>` : ""}
        ${curation.excluded.map((p) => {
          const redundant = redundantExclusions.find((r) => r.id === p.id);
          const note = !p.example ? '<p class="muted">Excluded, not used as an example.</p>'
            : redundant ? `<p class="muted">Similar to excluded example: <a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(redundant.similar_to.id)}">${esc(promptPreview(redundant.similar_to.text))}</a></p>` : "";
          return `<div class="setup-row excluded-prompt-row"><input type="checkbox" data-pick-exclusion="${esc(p.id)}" aria-label="Select excluded prompt: ${esc(promptPreview(p.text))}"><div class="excluded-prompt-text"><a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.id)}">${esc(promptPreview(p.text))}</a>${note}</div>
            <div><button class="btn" type="button" data-example="${esc(p.id)}" data-use="${p.example ? 0 : 1}">${p.example ? "Stop using as example" : "Use as example"}</button>
            <button class="btn" type="button" data-restore="${esc(p.id)}">Restore to tag</button></div></div>`;
        }).join("")}
          </div>
        </details>
      </section>`;
    const available = new Set(curation.excluded.map((p) => p.id));
    for (const id of selectedExclusions) if (!available.has(id)) selectedExclusions.delete(id);
    syncExcludedSelection();
  };
  refreshCuration = loadCuration;
  loadCuration();
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
    editBox.querySelector(".rr-kind").focus();
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
    if (!values.include_tag && !values.preferred_tag && !values.skill && !values.keywords?.length) return;
    try { await (editing == null
      ? api(`/api/tags/${enc}/rules`, { method: "POST", body: values })
      : api(`/api/tags/${enc}/rules/${rules[editing].index}`, { method: "PUT", body: values })); }
    catch (error) { failed(error); return; }
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
const sessState = { tag: "", categoryTags: {}, tags: [], exclude_reruns: false, q: "", project: "", range: "all", sort: "recent", items: [], summary: null, highlights: null };

function sessionFiltersFromQuery(query) {
  sessState.tagCounts = null;
  sessState.tag = query.get("tag") || "";
  sessState.categoryTags = categoryTagsFromQuery(query);
  sessState.exclude_reruns = query.get("exclude_reruns") === "true";
  sessState.q = query.get("q") || "";
  sessState.project = query.get("project") || "";
  sessState.range = RANGES[query.get("range")] ? query.get("range") : "all";
  sessState.sort = SESSION_SORTS[query.get("sort")] ? query.get("sort") : "recent";
}

function sessionQuery(extra = {}) {
  const since = RANGES[sessState.range][1];
  const q = { q: sessState.q, tag: sessState.tag, project: sessState.project, sort: sessState.sort, ...extra };
  if (sessState.exclude_reruns) q.exclude_reruns = "true";
  if (since) q.since = since().toISOString();
  const params = new URLSearchParams(q);
  Object.values(sessState.categoryTags).flat().forEach((tag) => params.append("tags", tag));
  return params;
}

function syncSessionUrl() {
  const q = new URLSearchParams();
  for (const key of ["q", "project", "tag"]) if (sessState[key]) q.set(key, sessState[key]);
  Object.values(sessState.categoryTags).flat().forEach((tag) => q.append("tags", tag));
  if (sessState.exclude_reruns) q.set("exclude_reruns", "true");
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
  return `<search class="tags-toolbar prompts-toolbar browse-toolbar">
    <label class="search-box"><span aria-hidden="true">⌕</span>
      <input type="search" id="session-search" placeholder="Search titles and prompts…" value="${esc(sessState.q)}" aria-label="Search sessions"></label>
    <div class="browse-filters" role="group" aria-label="Session filters">
    <div class="segmented" role="group" aria-label="Period">${Object.entries(RANGES).map(([key, [label]]) =>
      `<button type="button" data-range="${key}" aria-pressed="${key === sessState.range}">${label}</button>`).join("")}</div>
    ${tagFilterMenusHtml(sessState)}
    </div>
    <div class="browse-actions">
    <label class="sort-box">Sort <select id="session-sort">${Object.entries(SESSION_SORTS).map(([key, label]) =>
      `<option value="${key}" ${key === sessState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label>
    </div>
  </search>`;
}

function sessionActiveFiltersHtml() {
  const chips = [];
  if (sessState.q) chips.push(["q", `“${sessState.q}”`]);
  if (sessState.project) chips.push(["project", `Project: ${sessState.project}`]);
  if (sessState.tag) chips.push(["tag", `Tag: ${sessState.tag}`]);
  Object.entries(sessState.categoryTags).forEach(([cat, tag]) => chips.push([`category:${cat}`, tag.join(" or ")]));
  if (sessState.range !== "all") chips.push(["range", RANGES[sessState.range][0]]);
  if (sessState.exclude_reruns) chips.push(["exclude_reruns", "Reruns hidden"]);
  if (!chips.length) return "";
  return `<div class="active-filters" aria-label="Active filters">${chips.map(([key, label]) =>
    `<button type="button" class="filter-chip" data-clear="${key}" title="Remove this filter">${esc(label)}<span aria-hidden="true">✕</span></button>`).join("")}
    <button type="button" class="link-btn" data-clear="all">Clear all</button></div>`;
}

let sessionProjects = [];

async function renderSessions({ reload = true } = {}) {
  syncSessionUrl();
  if (reload) {
    const [data, tagCounts] = await Promise.all([
      api(`/api/sessions?${sessionQuery({ limit: LIST_PAGE, offset: 0 })}`),
      api(`/api/tag-counts?${tagCountsQuery(sessState, "sessions")}`),
    ]);
    Object.assign(sessState, { items: data.items, summary: data.summary, highlights: data.highlights || null, tagCounts });
    showTagCounts(document.getElementById("session-toolbar"), sessState);
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
  bindTagFilterMenus(bar, sessState, rerenderSessionToolbar, renderSessions);
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
    if (key === "all") Object.assign(sessState, { q: "", project: "", tag: "", categoryTags: {}, range: "all", exclude_reruns: false });
    else if (key.startsWith("category:")) delete sessState.categoryTags[key.slice(9)];
    else sessState[key] = key === "exclude_reruns" ? false : key === "range" ? "all" : "";
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
  [sessionProjects, sessState.tags] = await Promise.all([api("/api/projects"), api("/api/tags")]);
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

/** The other tag categories of the exclusion card: title and help, by the key `/api/setup/history` gives them. */
const EXCLUDE_CATEGORIES = {
  your: ["Your tags", "Leave out every prompt carrying one of your tags, added by hand or by an auto-tag rule."],
  model: ["Models", "Leave out every prompt answered by a model, even in part."],
  persona: ["Subagent personas", "Leave out every prompt that ran a subagent with this persona."],
  cmd: ["Commands", "Leave out every prompt started with a slash command."],
  ai: ["Other AI tags", "Leave out every prompt carrying one of these tags."],
};

/** Projects to click in or out of the history, and the sessions left out with a way back. */
function excludedCardHtml(scope) {
  const sessionLabel = (x) => `${fmtDate(x.started_at)} · ${x.title || (x.first_prompt || "").slice(0, 60) || x.id.slice(0, 8)}${x.project ? ` · ${x.project}` : ""}`;
  const out = scope.sessions.filter((x) => x.excluded);
  const pickable = scope.sessions.filter((x) => !x.excluded && !scope.projects.some((pr) => pr.excluded && pr.project === x.project));
  const { hidden } = scope;
  const tagChips = (list, prefix, attr = "data-exclude-tag") => list.map((x) => `<button type="button" class="exclude-chip" ${attr}="${esc(x.tag)}" aria-pressed="${x.excluded}"
    title="${x.excluded ? "Left out: click to bring it back" : "Click to leave out every prompt where the agent used it"}">${esc(x.tag.slice(prefix.length))} <span class="mi-count">${x.prompts}</span></button>`).join("");
  return `<div class="card exclusions">
    <p class="muted">Leave a project, a skill, a kind of subagent, any other tag or a session out of every page: totals, lists, tags, charts and comparisons. Its prompts stay imported,
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
    ${scope.tag_categories.map((cat) => `<div class="setup-row"><div><strong>${EXCLUDE_CATEGORIES[cat.key][0]}</strong><br><span class="muted">${EXCLUDE_CATEGORIES[cat.key][1]}</span>
      <div class="exclude-chips">${cat.key === "your" ? tagChips(cat.tags, "", "data-exclude-your-tag") : tagChips(cat.tags, cat.key === "ai" ? "" : `${cat.key}:`)}</div></div></div>`).join("")}
    <div class="setup-row"><div><strong>Sessions</strong><br><span class="muted">Leave out one conversation, such as a test or an experiment.</span>
      ${out.length ? `<ul class="excluded-sessions">${out.map((x) => `<li><span>${esc(sessionLabel(x))} <span class="mi-count">${plural(x.prompts, "prompt")}</span></span>
        <button type="button" class="link-btn" data-include-session="${esc(x.id)}">Bring back</button></li>`).join("")}</ul>` : ""}
      <form id="exclude-session-form" class="inline-form exclude-form"><select id="exclude-session" aria-label="Session to leave out">
        <option value="">Pick a session…</option>${pickable.map((x) => `<option value="${esc(x.id)}">${esc(sessionLabel(x))}</option>`).join("")}</select>
        <button class="btn">Leave it out</button></form></div></div>
  </div>`;
}

function agentConnectionDetailsHtml(agent, settings, extra = "") {
  const status = !agent.enabled ? "Not linked" : agent.directory_exists ? "Linked" : "Waiting for history";
  const live = settings.auto_ingest_interval > 0 ? `Automatically scanned every ${settings.auto_ingest_interval}s while Rerunly runs.` : "Automatic scanning is off. Use Import now or set an auto-import interval below.";
  return `<div class="agent-connection-details">
    <div class="setup-row"><div><h3>${esc(agent.name)}</h3><span class="${agent.enabled && agent.directory_exists ? "status-ok" : "muted"}">${status}</span>
      <p class="muted">${agent.transcript_count} transcript files found · ${agent.sessions} imported sessions · ${agent.prompts} prompts.
        ${agent.installed ? "CLI installed; available for reruns." : "CLI not found; existing history can still be imported."}</p></div>
      </div>
    <form class="setup-row" data-agent-form="${esc(agent.id)}"><div><label for="agent-path-${esc(agent.id)}"><strong>Transcript directory</strong></label>
      <p class="muted">${agent.directory_exists ? "Directory found." : "Directory not found. Start a session with this agent or change the path."}</p></div>
      <input id="agent-path-${esc(agent.id)}" name="directory" size="50" required value="${esc(agent.directory)}">
      <button class="btn">Save path</button></form>
    <p class="muted">${esc(live)} Removing an agent stops future imports and keeps imported history.</p>
    ${agent.id === "codex" ? '<p class="muted">CLI and IDE history are supported. Costs are API estimates, not subscription charges. Codex uses periodic import; Claude’s hook, retention setting and cost telemetry do not apply.</p>' : extra}
  </div>`;
}

/** Choices for the default LLM judge: the models of the linked agents, grouped by agent, the saved one kept even when no
    linked agent lists it. */
function judgeModelOptions(models, linked, current) {
  const sel = (v) => (v === current ? "selected" : "");
  const groups = [["claude", "Claude Code"], ["codex", "Codex"]].filter(([agent]) => linked.has(agent)).map(([agent, name]) => {
    const list = models.filter((m) => m.agent === agent);
    const fallback = agent === "claude" ? `<option value="" ${sel("")}>Claude Code's default model</option>` : "";
    return `<optgroup label="${name}">${fallback}${list.map((m) => `<option value="${esc(m.id)}" ${sel(m.id)}>${esc(m.label)}</option>`).join("")}</optgroup>`;
  }).join("");
  const known = (current === "" && linked.has("claude")) || models.some((m) => m.id === current && linked.has(m.agent));
  return (known ? "" : `<option value="${esc(current)}" selected>${esc(current || "Claude Code's default model")} (saved; not from a linked agent)</option>`) + groups;
}

/** Effort levels the judge `model` accepts, "" being the model's own default. */
function judgeEffortOptions(models, model, current) {
  const m = models.find((x) => x.id === model) || (model === "" ? models.find((x) => x.agent === "claude") : null);
  const efforts = m?.efforts?.length ? m.efforts : EFFORTS;
  const pick = efforts.includes(current) ? current : "";
  return `<option value="" ${pick ? "" : "selected"}>${m?.default_effort ? `Model default (${esc(m.default_effort)})` : "Model default"}</option>`
    + efforts.map((e) => `<option value="${esc(e)}" ${e === pick ? "selected" : ""}>${esc(e)}</option>`).join("");
}

async function viewSetup() {
  const [st, settings, scope, models, checks] = await Promise.all([api("/api/setup/status"), api("/api/settings"), api("/api/setup/history"), loadModelCatalog(), api("/api/model-checks").catch((error) => { if (error instanceof StaleNavigation) throw error; return null; })]);
  const linked = new Set(st.connections.filter((agent) => agent.enabled).map((agent) => (agent.id === "claude-code" ? "claude" : agent.id)));
  const ok = (cond) => (cond ? "status-ok" : "status-warn");
  const retentionOk = st.cleanup_period_days != null && st.cleanup_period_days >= 365;
  const added = st.connections.filter((agent) => agent.enabled);
  const available = st.connections.filter((agent) => !agent.enabled);
  const claudeControls = `<details><summary>Claude Code live updates, retention and cost telemetry</summary>
      <div class="setup-row"><div class="${ok(st.hook_installed)}"><strong>Live updates (Claude Code hook)</strong><br>
        <span class="muted">Without the hook the dashboard re-scans every ${settings.auto_ingest_interval}s while it runs. The hook imports right after each answer of the agent.
        It adds this command to <code>${esc(st.claude_settings_path)}</code> (a <code>.bak</code> copy is kept):</span><br><code>${esc(st.hook_command)}</code></div>
        <div><button class="btn" id="hook">${st.hook_installed ? "Remove hook" : "Install hook"}</button></div></div>
      <div class="setup-row"><div class="${ok(st.statusline_installed)}"><strong>Usage limits (Claude Code statusline)</strong><br>
        <span class="muted">Claude Code reports how much of its 5-hour and weekly limits you used to its statusline only. This saves that statusline input in Rerunly's data folder,
        then runs your own statusline command, unchanged, so it looks the same. Removing it puts your statusline back.
        It sets this command in <code>${esc(st.claude_settings_path)}</code> (a <code>.bak</code> copy is kept):</span><br><code>${esc(st.statusline_command)}</code></div>
        <div><button class="btn" id="statusline">${st.statusline_installed ? "Remove" : "Capture usage"}</button></div></div>
      <div class="setup-row"><div class="${ok(retentionOk)}"><strong>Transcript retention</strong><br>
        <span class="muted">Claude Code deletes transcripts older than <code>cleanupPeriodDays</code> (default 30). Imported prompts stay in the dashboard database,
        but a longer retention lets you re-import with new prices or rules. Current: ${st.cleanup_period_days ?? "not set (30 days)"}.</span></div>
        <div><button class="btn" id="retention">Set to 3650 days</button></div></div>
      <div class="setup-row"><div class="${ok(st.telemetry.configured && st.telemetry.matched_events > 0)}"><strong>Claude Code cost telemetry</strong><br>
        <span class="muted">Claude Code can send its own cost for every API call, including internal calls the transcripts do not detail.
        The dashboard then shows that cost for every prompt fully covered by telemetry, and the price-table estimate otherwise.
        Enabling it adds these entries to <code>env</code> in <code>${esc(st.claude_settings_path)}</code>; they apply to agent sessions started afterwards,
        and calls made while the dashboard is not running are not recorded.</span><br>
        ${Object.entries(st.telemetry.env).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}<br>
        <span class="muted">${st.telemetry.events} API calls received · ${st.telemetry.matched_events} linked to an imported prompt · last ${fmtDate(st.telemetry.last_event_at)}</span>
        ${Object.keys(st.telemetry.conflicts).length ? `<br><span class="status-warn">Already set to other values: ${Object.entries(st.telemetry.conflicts).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</span>` : ""}</div>
        <div><button class="btn" id="telemetry">${st.telemetry.configured ? "Disable telemetry" : "Enable telemetry"}</button></div></div>
    </details>`;
  app.innerHTML = `
    <header class="home-hero page-hero"><div class="hero-copy">
      <h1><span class="hero-name">Setup · ${plural(added.length, "agent")} added</span>Connect your coding agents. <em>Keep every prompt in one history.</em></h1>
      <p>Link Claude Code, OpenAI Codex, or both. Rerunly reads local transcripts; nothing leaves your machine during import.
        Add agents using the selector, then manage their connections together below. Prices, automatic import and history settings are shared.</p>
    </div></header>
    <h2>Agents</h2>
    <div class="card">
      <form id="add-agent-form" class="inline-form">
        <label for="add-agent">Add an agent</label>
        <select id="add-agent" name="agent" required ${available.length ? "" : "disabled"}>
          <option value="">${available.length ? "Choose an agent…" : "All supported agents are added"}</option>
          ${available.map((agent) => `<option value="${esc(agent.id)}">${esc(agent.name)}</option>`).join("")}
        </select>
        <button class="btn primary" ${available.length ? "" : "disabled"}>Add agent</button>
      </form>
      ${added.length ? `<div class="table-scroll"><table class="agent-connections-table">
        <thead><tr><th>Agent</th><th>Status</th><th>Imported history</th><th>Updates</th><th>Manage</th></tr></thead>
        <tbody>${added.map((agent) => `<tr>
          <td><strong>${esc(agent.name)}</strong><br><span class="muted">${agent.installed ? "CLI installed" : "CLI not found"}</span></td>
          <td><span class="${agent.directory_exists ? "status-ok" : "status-warn"}">${agent.directory_exists ? "Connected" : "Waiting for history"}</span><br><span class="muted">${agent.transcript_count} transcript files</span></td>
          <td>${plural(agent.sessions, "session")} · ${plural(agent.prompts, "prompt")}<br><span class="muted">Last prompt ${fmtDate(agent.last_prompt_at)}</span></td>
          <td>${agent.id === "claude-code" && st.hook_installed ? "Hook + periodic import" : settings.auto_ingest_interval > 0 ? `Every ${settings.auto_ingest_interval}s` : "Manual import"}</td>
          <td><button type="button" class="btn" data-agent-details="${esc(agent.id)}" aria-expanded="false" aria-controls="agent-details-${esc(agent.id)}">Details</button>
            <button type="button" class="link-btn" data-remove-agent="${esc(agent.id)}">Remove</button></td>
        </tr><tr id="agent-details-${esc(agent.id)}" hidden><td colspan="5">${agentConnectionDetailsHtml(agent, settings, claudeControls)}</td></tr>`).join("")}</tbody>
      </table></div>` : '<p class="muted">No agents added yet. Choose an agent above to connect its local history.</p>'}
      <p class="muted">Removing an agent keeps its imported history. Add it again to resume imports.</p>
    </div>
    <section id="model-availability" aria-label="Model availability settings">${modelChecksHtml(checks)}</section>
    <h2>Examples</h2>
    <div class="card"><div class="setup-row"><div><strong>Explore Rerunly with simple examples</strong><p class="muted">Prompt tuning, Claude and Codex records, cache savings, skill versions, subagents, quality verdicts and a blocked rerun.
      They open in a separate example app holding only these illustrative records: no agents are run, and your own history never shows them.</p></div>
      <div><a class="btn" href="/examples/" target="_blank" rel="noopener">Open the example app ↗</a></div></div></div>
    <h2>Dashboard</h2>
    <div class="card">
      <div class="setup-row"><div class="${ok(st.prompts > 0)}"><strong>Imported history from all agents</strong><br>
        <span class="muted">${st.sessions} sessions · ${st.prompts} prompts · ${st.agents} subagent runs · last prompt ${fmtDate(st.last_prompt_at)}.
          Database <code>${esc(st.db_path)}</code></span></div>
        <div><button class="btn primary" id="ingest">Import now</button> <button class="btn" id="reingest">Re-import all</button></div></div>
      <div class="setup-row"><div class="${ok(st.autostart.enabled)}"><strong>Start at login</strong><br>
        <span class="muted">Starts the dashboard in the background when you log in, with an icon in the status bar to open it, import, or quit.
        It writes <code>${esc(st.autostart.file)}</code>. To start it now without a terminal: <code>uv run rerunly start</code>.</span></div>
        <div><button class="btn" id="autostart">${st.autostart.enabled ? "Don't start at login" : "Start at login"}</button></div></div>
      ${st.unpriced_models.length ? `<p class="status-warn">Models without a price: ${esc(st.unpriced_models.join(", "))}. Add them to the price table below.</p>` : ""}
    </div>

    <h2 id="excluded">Excluded from the history</h2>
    ${excludedCardHtml(scope)}

    <h2>Settings</h2>
    <form id="settings" class="card">
      <div class="setup-row"><div><strong>Cost shown</strong><br><span class="muted">Claude Code’s reported cost when telemetry covers a prompt, or the price table. Codex always uses price estimates.</span></div>
        <select name="cost_source">
          <option value="claude-code" ${settings.cost_source === "claude-code" ? "selected" : ""}>The agent's own cost when available</option>
          <option value="prices" ${settings.cost_source === "prices" ? "selected" : ""}>Price table only</option></select></div>
      <div class="setup-row"><div><strong>Similarity threshold</strong><br><span class="muted">0–1 word overlap above which two prompts are shown together.</span></div>
        <input name="similarity_threshold" type="number" min="0" max="1" step="0.05" value="${settings.similarity_threshold}"></div>
      <div class="setup-row"><div><strong>Default LLM judge</strong><br><span class="muted">The model, and its effort, rating how much a rerun's output differs from its original's.
        Any model of your linked agents; the efforts are the ones that model accepts. Each rerun and comparison can pick another model.</span></div>
        <div class="judge-pick">
          <label>Model <select name="judge_model" id="judge-model-setting">${judgeModelOptions(models, linked, settings.judge_model || "")}</select></label>
          <label>Effort <select name="judge_effort" id="judge-effort-setting">${judgeEffortOptions(models, settings.judge_model || "", settings.judge_effort || "")}</select></label>
        </div></div>
      <div class="setup-row"><div><strong>Auto-import interval (seconds)</strong><br><span class="muted">0 disables periodic scanning.</span></div>
        <input name="auto_ingest_interval" type="number" min="0" value="${settings.auto_ingest_interval}"></div>
      <div class="setup-row"><div><strong>Automatic model pricing</strong><br>
        <span class="muted">When a new model appears, look up standard short-context token prices on the official OpenAI or Claude pricing page. No prompts are sent.
          Published prices are cached for a day; failed lookups retry after an hour. Your price overrides take priority.</span>
        ${Object.entries(settings.price_catalogs || {}).map(([provider, state]) => `<p class="muted">${esc(provider)} · checked ${fmtDate(new Date(state.checked_at * 1000).toISOString())}
          ${state.error ? `<span class="status-warn">${esc(state.error)}</span>` : "· pricing page available"}</p>`).join("")}
        ${(settings.price_lookup_missing || []).length ? `<p class="status-warn">No published price found for ${esc(settings.price_lookup_missing.join(", "))}. Existing prefix estimates apply where configured; other models remain unpriced.</p>` : ""}
        ${Object.keys(settings.price_provenance || {}).length ? `<details><summary>Automatically found prices (${Object.keys(settings.price_provenance).length})</summary><ul>
          ${Object.entries(settings.price_provenance).map(([model, info]) => `<li>${esc(model)} · <a href="${esc(info.source)}" target="_blank" rel="noopener noreferrer">Official pricing</a> · ${fmtDate(new Date(info.checked_at * 1000).toISOString())}</li>`).join("")}</ul></details>` : ""}
        </div><label><input type="checkbox" name="auto_price_lookup" ${settings.auto_price_lookup ? "checked" : ""}> Find prices automatically</label></div>
      <div class="setup-row"><div style="flex-basis:100%"><strong>Prices (USD per million tokens)</strong><br><span class="muted">Longest model-id prefix wins. Cache writes cost 1.25× (5 min) or 2× (1 h) input; fast mode 2×; web search $10/1k.</span>
        <textarea name="prices" rows="12">${esc(JSON.stringify(settings.prices, null, 2))}</textarea></div></div>
      <div style="padding-top:10px"><button class="btn primary">Save &amp; re-import</button> <span id="saved" class="muted"></span></div>
    </form>`;

  bindModelChecks(app.querySelector("#model-availability"), checks);
  const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); } catch (e) { alert(e.message); } finally { btn.disabled = false; } };
  app.querySelectorAll("[data-agent-form]").forEach((form) => form.onsubmit = (event) => {
    event.preventDefault();
    busy(form.querySelector("button"), async () => {
      await api(`/api/setup/agents/${form.dataset.agentForm}`, { method: "POST", body: { directory: new FormData(form).get("directory"), enabled: true } });
      await api("/api/ingest", { method: "POST" });
      await viewSetup();
    });
  });
  document.getElementById("add-agent-form").onsubmit = (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const id = new FormData(form).get("agent");
    if (!id) return;
    busy(form.querySelector("button"), async () => {
      await api(`/api/setup/agents/${id}`, { method: "POST", body: { enabled: true } });
      await api("/api/ingest", { method: "POST" });
      await viewSetup();
    });
  };
  app.querySelectorAll("[data-remove-agent]").forEach((button) => button.onclick = () => busy(button, async () => {
    await api(`/api/setup/agents/${button.dataset.removeAgent}`, { method: "POST", body: { enabled: false } });
    await viewSetup();
  }));
  app.querySelectorAll("[data-agent-details]").forEach((button) => button.onclick = () => {
    const row = document.getElementById(`agent-details-${button.dataset.agentDetails}`);
    row.hidden = !row.hidden;
    button.setAttribute("aria-expanded", String(!row.hidden));
    button.textContent = row.hidden ? "Details" : "Hide details";
  });
  document.getElementById("ingest").onclick = (e) => busy(e.target, async () => { await api("/api/ingest", { method: "POST" }); viewSetup(); });
  document.getElementById("reingest").onclick = (e) => busy(e.target, async () => { await api("/api/ingest?force=true", { method: "POST" }); viewSetup(); });
  if (document.getElementById("statusline")) document.getElementById("statusline").onclick = (e) => busy(e.target, async () => { await api(`/api/setup/statusline?install=${!st.statusline_installed}`, { method: "POST" }); viewSetup(); });
  if (document.getElementById("hook")) document.getElementById("hook").onclick = (e) => busy(e.target, async () => { await api(`/api/setup/hook?install=${!st.hook_installed}`, { method: "POST" }); viewSetup(); });
  if (document.getElementById("telemetry")) document.getElementById("telemetry").onclick = (e) => busy(e.target, async () => {
    const conflicts = Object.entries(st.telemetry.conflicts);
    if (!st.telemetry.configured && conflicts.length &&
        !confirm(`This replaces your current values:\n${conflicts.map(([k, v]) => `${k}=${v}`).join("\n")}`)) return;
    await api("/api/setup/telemetry", { method: "POST", body: { enable: !st.telemetry.configured } });
    viewSetup();
  });
  document.getElementById("autostart").onclick = (e) => busy(e.target, async () => {
    await api("/api/setup/autostart", { method: "POST", body: { enable: !st.autostart.enabled } }); viewSetup();
  });
  if (document.getElementById("retention")) document.getElementById("retention").onclick = (e) => busy(e.target, async () => { await api("/api/setup/retention", { method: "POST", body: { days: 3650 } }); viewSetup(); });
  // exclusions: a click saves the new list and redraws the page
  const exclude = (key, list) => api("/api/settings", { method: "PUT", body: { [key]: list } }).then(() => viewSetup());
  const excludedProjects = scope.projects.filter((x) => x.excluded).map((x) => x.project);
  const excludedSessions = scope.sessions.filter((x) => x.excluded).map((x) => x.id);
  app.querySelectorAll("[data-exclude-project]").forEach((b) => b.onclick = () => {
    const name = b.dataset.excludeProject;
    exclude("excluded_projects", excludedProjects.includes(name) ? excludedProjects.filter((x) => x !== name) : [...excludedProjects, name]);
  });
  const yourCategory = scope.tag_categories.filter((cat) => cat.key === "your");
  const aiCategories = scope.tag_categories.filter((cat) => cat.key !== "your");
  const excludedTags = [...scope.skills, ...scope.agents, ...scope.reruns, ...aiCategories.flatMap((cat) => cat.tags)].filter((x) => x.excluded).map((x) => x.tag);
  const excludedYourTags = yourCategory.flatMap((cat) => cat.tags).filter((x) => x.excluded).map((x) => x.tag);
  const toggle = (list, tag) => list.includes(tag) ? list.filter((x) => x !== tag) : [...list, tag];
  app.querySelectorAll("[data-exclude-tag]").forEach((b) => b.onclick = () => exclude("excluded_tags", toggle(excludedTags, b.dataset.excludeTag)));
  app.querySelectorAll("[data-exclude-your-tag]").forEach((b) => b.onclick = () => exclude("excluded_your_tags", toggle(excludedYourTags, b.dataset.excludeYourTag)));
  app.querySelectorAll("[data-include-session]").forEach((b) => b.onclick = () =>
    exclude("excluded_sessions", excludedSessions.filter((x) => x !== b.dataset.includeSession)));
  document.getElementById("exclude-session-form").onsubmit = (e) => {
    e.preventDefault();
    const id = document.getElementById("exclude-session").value;
    if (id) exclude("excluded_sessions", [...excludedSessions, id]);
  };
  // another judge model accepts other efforts: keep the chosen one when it still can
  const judgeModel = document.getElementById("judge-model-setting");
  bindModelAccess(judgeModel);
  judgeModel.onchange = () => {
    const effort = document.getElementById("judge-effort-setting");
    effort.innerHTML = judgeEffortOptions(models, judgeModel.value, effort.value);
  };
  document.getElementById("settings").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let values;
    try {
      values = {
        cost_source: f.get("cost_source"),
        similarity_threshold: parseFloat(f.get("similarity_threshold")),
        judge_model: String(f.get("judge_model") || "").trim(),
        judge_effort: String(f.get("judge_effort") || ""),
        auto_ingest_interval: parseInt(f.get("auto_ingest_interval"), 10),
        prices: JSON.parse(f.get("prices")),
        auto_price_lookup: f.has("auto_price_lookup"),
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
  ["prompts", "Prompts", "Explore every prompt with its cost, tokens and time. Search, filter and group runs to follow a task.",
    (o) => `${o.totals.prompts || 0} prompt${o.totals.prompts === 1 ? "" : "s"}`],
  ["sessions", "Sessions", "Browse agent sessions, their prompts and the cost of the main agent and recorded subagents.",
    (o) => `${o.totals.sessions || 0} session${o.totals.sessions === 1 ? "" : "s"}`],
  ["tags", "Tags", "Compare cost and time by task, project, model or skill, and create tags to group related runs.",
    (o) => `${o.tags.length} tag${o.tags.length === 1 ? "" : "s"}`],
  ["reruns", "Reruns", "Queue and launch prompts again with different settings, then compare each attempt with the original.",
    () => "Queue and launch"],
  ["setup", "Setup", "Import transcripts and manage live updates, pricing, auto-tag rules and other settings.",
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
/** The top tags of each category, by number of prompts or, with `by: "cost"`, by total cost (shown first, in large). */
function homeTopTags(o, { by = "prompts" } = {}) {
  const byCost = by === "cost";
  const total = (byCost ? o.totals.cost_usd : o.totals.prompts) || 0;
  const value = (t) => (byCost ? t.cost_usd || 0 : t.prompts);
  return TAG_CATEGORIES.map(([cat, label, help]) => {
    // the server lists tags by prompts, then cost: the stable sort keeps that order among equal costs
    const list = o.tags.filter((t) => tagCategory(t) === cat && value(t) > 0).sort((a, b) => value(b) - value(a)).slice(0, 3);
    if (!list.length) {
      return cat !== "human" || byCost ? "" : `<div class="top-tag cat-human empty"><span class="hl-label"><span class="tag-dot"></span>${label}</span>
        <p class="muted">No tag of yours yet: add one on a prompt page to follow a task across runs.</p></div>`;
    }
    const [top, ...rest] = list;
    const [prefix, ...name] = top.tag.split(":");
    const title = name.length ? `<span class="tag-prefix">${esc(prefix)}:</span>${esc(name.join(":"))}` : esc(top.tag);
    const share = (t) => (total ? Math.round((value(t) / total) * 100) : 0);
    const shareText = (t) => (share(t) === 0 && value(t) ? "<1" : share(t));
    const prompts = `${top.prompts} prompt${top.prompts === 1 ? "" : "s"}`;
    return `<div class="top-tag cat-${cat}">
      <span class="hl-label" title="${esc(help)}"><span class="tag-dot"></span>${label}</span>
      ${byCost ? `<span class="hl-value top-cost"><span class="big">${fmtCost(top.cost_usd)}</span><span class="unit">total</span></span>` : ""}
      <a class="top-name" href="#/tag/${encodeURIComponent(top.tag)}">${title}</a>
      <span class="top-share"><span class="share-bar" aria-hidden="true"><i style="width:${Math.max(2, share(top))}%"></i></span>${shareText(top)}% of ${byCost ? "cost" : "prompts"}</span>
      <span class="pmeta">${byCost ? `${prompts} · ${fmtCost(top.avg_cost_usd)} each` : `${prompts} · ${fmtCost(top.cost_usd)} total · ${fmtCost(top.avg_cost_usd)} each`}</span>
      ${rest.length ? `<ol class="runners-up" start="2">${rest.map((t) => `<li><a href="#/tag/${encodeURIComponent(t.tag)}">${esc(t.tag)}</a><span class="mi-count">${byCost ? fmtCost(t.cost_usd) : t.prompts}</span></li>`).join("")}</ol>` : ""}
    </div>`;
  }).join("");
}

/** "2 d 4 h", "3 h 12 min", "8 min": how long until `date`. */
function fmtUntil(date) {
  const min = Math.max(0, Math.round((date - Date.now()) / 60000));
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  return h < 24 ? `${h} h ${min % 60} min` : `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** "5 h", "Week"…: the length of a usage window, short. */
function windowLabel(minutes) {
  if (minutes === 10080) return "Week";
  if (minutes === 1440) return "Day";
  if (!minutes) return "Usage";
  return minutes % 60 ? `${minutes} min` : `${minutes / 60} h`;
}

/** Empirical percentage-point impact, kept separate from reported account state. */
function quotaForecast(forecast) {
  if (!forecast?.windows?.length) return '<p class="muted">Limit impact unavailable: waiting for account usage readings.</p>';
  const pct = (value) => Number(value).toFixed(1);
  return `<div class="quota-forecast">${forecast.windows.map((w) => {
    const samples = (w.models || []).map((m) => `${esc(m.model)}: ${m.samples} sample${m.samples === 1 ? "" : "s"}${m.tokens_per_percentage_point ? ` · ≈ ${fmtTokens(m.tokens_per_percentage_point)} tokens/percentage point` : ""}`).join(" · ");
    if (w.status !== "estimated") return `<div class="quota-window"><span class="quota-window-label">${windowLabel(w.window_minutes)} window</span><strong>Learning</strong><p class="muted">${samples || "Model unknown"}. Needs at least 3 usable observations per model and 3 percentage points of observed usage.</p></div>`;
    const projected = w.projected_percent == null ? "" : ` · projected ${pct(w.projected_percent_low)}–${pct(w.projected_percent_high)}% used`;
    return `<div class="quota-window"><span class="quota-window-label">${windowLabel(w.window_minutes)} window</span><strong>+${pct(w.additional_percent_low)}–${pct(w.additional_percent_high)} percentage points</strong><p>${projected.replace(/^ · /, "")}</p><p class="muted">Low confidence · observed range · ${samples}</p>${w.projected_percent_high >= 100 ? '<p class="status-warn">May exhaust the remaining allowance.</p>' : ""}${w.may_cross_reset ? '<p class="status-warn">Run may cross the reported reset; projection assumes the current window.</p>' : ""}</div>`;
  }).join("")}</div>`;
}

function queueQuotaForecast(items) {
  const groups = new Map();
  for (const q of items) {
    const agent = q.agent || "Agent";
    if (!groups.has(agent)) groups.set(agent, []);
    groups.get(agent).push(q);
  }
  return [...groups].map(([agent, runs]) => {
    const minutes = [...new Set(runs.flatMap((q) => (q.forecast?.windows || []).map((w) => w.window_minutes)))];
    const windows = minutes.map((window_minutes) => {
      const rows = runs.map((q) => q.forecast?.windows?.find((w) => w.window_minutes === window_minutes));
      const models = new Map();
      rows.filter(Boolean).forEach((w) => (w.models || []).forEach((m) => models.set(m.model, m)));
      const result = {window_minutes, models: [...models.values()], status: "learning"};
      if (rows.every((w) => w?.status === "estimated")) {
        result.status = "estimated";
        for (const key of ["additional_percent", "additional_percent_low", "additional_percent_high"]) result[key] = rows.reduce((sum, w) => sum + w[key], 0);
        if (rows.every((w) => w.projected_percent != null)) {
          result.projected_percent_low = rows[0].projected_percent_low - rows[0].additional_percent_low + result.additional_percent_low;
          result.projected_percent_high = rows[0].projected_percent_high - rows[0].additional_percent_high + result.additional_percent_high;
          result.projected_percent = rows[0].projected_percent - rows[0].additional_percent + result.additional_percent;
        }
        result.may_cross_reset = rows.some((w) => w.may_cross_reset);
      }
      return result;
    });
    return `<strong>${esc(agent)}</strong>${quotaForecast({windows})}`;
  }).join("");
}

/** Subscription usage of each connected agent, beside the home title: share of each window used, and when it resets. */
function homeLimits(limits) {
  return limits.map((a) => {
    const rows = a.windows.map((w) => {
      const pct = Math.max(0, Math.min(100, Math.round(w.used_percent)));
      const resets = w.resets_at ? new Date(w.resets_at) : null;
      const at = resets && resets.toLocaleString(undefined,
        w.window_minutes >= 1440 ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
      const label = `${a.agent} ${windowLabel(w.window_minutes).toLowerCase()} limit used`;
      return `<div class="limit-row">
        <span class="limit-name">${windowLabel(w.window_minutes)}</span>
        <span class="limit-bar ${pct >= 80 ? "high" : ""}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${esc(label)}"><i style="width:${Math.max(1, pct)}%"></i></span>
        <strong class="limit-pct">${pct}%</strong>
        <span class="limit-reset">${w.reset ? "reset since last seen" : resets ? `resets in ${fmtUntil(resets)} · ${at}` : ""}</span>
      </div>`;
    }).join("");
    const empty = !a.capture ? `<a href="#/setup">Capture its usage in Setup</a>` : "No usage reported yet";
    return `<div class="limit-agent" ${a.observed_at ? `title="${esc(a.live ? `Asked from ${a.agent}'s account ${fmtAgo(a.observed_at)}` : `As of the last ${a.agent} activity, ${fmtAgo(a.observed_at)}`)}"` : ""}>
      <span class="limit-head"><strong>${esc(a.agent)}</strong>${a.plan ? ` <span class="muted">${esc(a.plan)}</span>` : ""}${a.observed_at ? `<span class="muted">${esc(fmtAgo(a.observed_at))}</span>` : ""}</span>
      ${rows || `<span class="limit-reset">${empty}</span>`}
    </div>`;
  }).join("");
}

async function viewHome(query = new URLSearchParams()) {
  const range = RANGES[query.get("range")] ? query.get("range") : "all";
  const since = RANGES[range][1];
  const [o, reruns] = await Promise.all([
    api(`/api/overview${since ? `?${new URLSearchParams({ since: since().toISOString() })}` : ""}`),
    api("/api/reruns").catch((error) => { if (error instanceof StaleNavigation) throw error; return null; }),
  ]);
  const originals = [...new Map((reruns?.launched || []).filter((run) => run.original).map((run) => [run.original.id, run.original])).values()];
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
        <div class="top-tags">${homeTopTags(o)}</div>
        ${t.cost_usd ? `<h2 class="page-sec">Most expensive in each tag category</h2>
        <div class="top-tags">${homeTopTags(o, { by: "cost" })}</div>` : ""}`
      : `<div class="empty-state"><div class="empty-icon">⌕</div><p>${range === "all" ? "Nothing imported yet." : "No prompt in this period."}</p>
          ${range === "all" ? '<p>Go to <a href="#/setup">Setup</a> and press <strong>Import now</strong>.</p>' : ""}</div>`}
      <section class="home-reruns" aria-labelledby="home-reruns-title">
        <div class="home-reruns-intro"><span class="rerun-eyebrow">Run it again. Learn what works.</span>
          <h2 id="home-reruns-title">Turn changes into better results</h2>
          <p>Rerun the same prompt with different settings, then compare cost, time, tokens and output quality to see what paid off.</p>
          <a href="#/reruns">Explore reruns →</a></div>
        <div class="home-reruns-history"><div class="home-history-heading"><div><span class="home-history-kicker">Your experiments</span><h3>Rerun history</h3></div>${reruns === null ? "" : `<span class="home-history-count">${originals.length} ${originals.length === 1 ? "prompt" : "prompts"}</span>`}</div>
          ${reruns === null ? '<p class="muted">Could not load rerun history. Visit <a href="#/reruns">Reruns</a> to try again.</p>' : originals.length
            ? `<ul>${originals.map((p, i) => `<li><a href="#/rerun-comparison/${encodeURIComponent(p.id)}"><span class="home-history-index" aria-hidden="true">${String(i + 1).padStart(2, "0")}</span><span class="home-history-prompt">${esc(p.text)}</span><span class="home-history-arrow" aria-hidden="true">↗</span></a></li>`).join("")}</ul>`
            : '<p class="muted">No reruns yet. Open a prompt to try your first rerun.</p>'}</div>
      </section>
      <h2 class="page-sec">Explore Rerunly</h2>
      <nav class="page-cards" aria-label="Explore Rerunly">${HOME_PAGES.filter(([id]) => !(EXAMPLES && id === "setup")).map(([id, label, text, stat]) =>
        `<a class="page-card" href="#/${id}${id === "prompts" ? rangeQuery : ""}"><span class="pc-main"><span class="pc-head"><strong>${label}</strong><span class="mi-count">${esc(stat(o))}</span></span>
          <span class="pc-text">${text}</span></span><span class="pc-arrow" aria-hidden="true">→</span></a>`).join("")}</nav>
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

const rerunState = { picked: new Set(), historyOpen: true, historyGroups: new Map(), judgeHistoryOpen: false, judgeHistoryGroups: new Map() };

function rerunSettings(x) {
  const skills = Array.isArray(x.skills) ? x.skills : [];
  const agent = x.model ? (x.model.startsWith("codex:") ? "Codex" : "Claude Code")
    : (x.replay || x.original || x.prompt)?.harness || "Claude Code";
  const parts = [x.mode === "terminal" ? "in a terminal" : "unattended", x.permission === "" ? "permissions needed" : x.permission === "same" || x.permission == null ? "same permissions as original" : x.permission === "edits" ? "may edit files" : x.mode !== "terminal" ? "read-only" : "ask for approval",
    x.mode === "terminal" ? (x.keep_terminal_open ? "terminal stays open until Enter" : "terminal closes after answer") : "",
    x.budget_usd ? `up to ${fmtCost(x.budget_usd)}` : "",
    ...skills.map((k) => `${k.skill.split(":").pop()} @ ${k.source}`),
    x.model || "same model", x.effort ? `effort ${x.effort}` : "same effort",
    x.harness_version ? `${agent} ${x.harness_version}` // launched: the release it ran with
      : x.release === "same" || (x.release == null && x.same_version) ? "the release it ran with"
      : x.release ? `${agent} ${x.release}` : `installed ${agent}`];
  return parts.filter(Boolean).map((t) => `<span class="rr-chip">${esc(t)}</span>`).join("");
}

function rerunPromptCell(p, { sessionId } = {}) {
  if (sessionId) return `<a class="rr-text" target="_blank" rel="noopener noreferrer" href="#/rerun/${encodeURIComponent(sessionId)}">${esc(p?.text || "View rerun details")}</a>`;
  if (!p) return '<span class="muted">This prompt left the history (excluded on the Setup page, or deleted).</span>';
  return `<a class="rr-text" target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(p.id)}">${esc(p.text)}</a>
    <span class="pmeta">${[p.project, `ran ${fmtDate(p.started_at)}`, fmtCost(p.total_cost_usd)].filter(Boolean).map(esc).join(" · ")}</span>`;
}

async function viewRerun(sessionId) {
  const x = await api(`/api/reruns/launched/${encodeURIComponent(sessionId)}`);
  const settings = [
    ["Model", x.model || "Same as original"],
    ["Reasoning effort", x.effort || "Same as original"],
    ["CLI release", x.harness_version || "Not recorded"],
    ["Mode", x.mode === "terminal" ? "Terminal" : x.mode === "background" ? "Background" : "Not recorded"],
    ["Permissions", x.permission === "same" ? "Same as original" : x.permission || "Not recorded"],
    ["Budget cap", x.budget_usd == null ? "None" : fmtCost(x.budget_usd)],
    ["After answer", x.mode === "terminal" ? (x.keep_terminal_open ? "Keep open until Enter" : "Close terminal") : "Not applicable"],
    ["Launched", fmtDate(x.launched_at)], ["Ended", fmtDate(x.ended_at)],
    ["Reported cost", fmtCost(x.cost_usd)],
    ["Automatic judging", x.auto_rate ? "Enabled" : "Disabled"],
    ["Judge model", x.judge_model || "Default at judging time"],
  ];
  app.innerHTML = `<div class="prompts-page rerun-detail-page">
    <p><a href="#/reruns">← Reruns</a></p>
    <h1>Rerun · ${esc(fmtDate(x.launched_at))}</h1>
    <section class="card"><h2>Status and result</h2>${rerunStatusHtml(x, { details: false })}
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere">${esc(x.result || "No result recorded yet.")}</pre>
      ${x.denials?.blocked ? `<p>${esc(x.denials.blocked)}</p>` : ""}
      ${x.denials?.denied?.length ? `<h3>Denied actions</h3><pre style="white-space: pre-wrap; overflow-wrap: anywhere">${esc(JSON.stringify(x.denials.denied, null, 2))}</pre>` : ""}
    </section>
    <section class="card rerun-diagnostics"><div class="rerun-diagnostics-head"><div><h2>Process and logs</h2><p class="muted">Check whether the rerun command is still running and inspect recent output.</p></div><button type="button" class="btn" id="rerun-check-process">Check process and logs</button></div><div id="rerun-diagnostics-result" aria-live="polite"><p class="muted">Check for the latest status.</p></div></section>
    <section class="card"><h2>Launch settings</h2><dl>${settings.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>
      <h3>Restored skills</h3>${x.skills.length ? `<ul>${x.skills.map(s => `<li>${esc(s.skill)} · ${esc(s.source || s.hash || "Version not recorded")}</li>`).join("")}</ul>` : '<p>No restored skills recorded.</p>'}
    </section>
    <section class="card"><h2>Prompt</h2><pre style="white-space: pre-wrap; overflow-wrap: anywhere">${esc(x.original?.text || "Original prompt is no longer in the history.")}</pre>
      ${x.original ? `<a href="#/prompt/${encodeURIComponent(x.original.id)}">Initial prompt →</a>` : ""}
      ${x.replay ? `<p><a href="#/prompt/${encodeURIComponent(x.replay.id)}">Imported rerun →</a></p>` : '<p class="muted">No imported transcript is available for this rerun.</p>'}
    </section></div>`;
  document.getElementById("rerun-check-process").onclick = async (event) => {
    const button = event.currentTarget, box = document.getElementById("rerun-diagnostics-result");
    button.disabled = true;
    box.innerHTML = '<p class="muted">Checking…</p>';
    try {
      const data = await api(`/api/reruns/launched/${encodeURIComponent(sessionId)}/diagnostics`);
      if (!box.isConnected) return;
      box.innerHTML = `<p><strong>Recorded status: ${esc(data.status || "unknown")}</strong> · Process: ${esc(data.process)} · Checked ${esc(fmtDate(data.checked_at))}</p><p class="muted">${esc(data.message)}</p>${data.log ? `<details><summary>Recent background log</summary><pre>${esc(data.log)}</pre></details>` : '<p class="muted">No background log is available.</p>'}${data.status !== x.status ? '<p><button class="btn" type="button" id="rerun-refresh-status">Refresh displayed status</button></p>' : ''}`;
      document.getElementById("rerun-refresh-status")?.addEventListener("click", () => viewRerun(sessionId));
    } catch (error) { if (!(error instanceof StaleNavigation) && box.isConnected) box.innerHTML = `<p class="status-warn">Could not check the rerun: ${esc(error.message)}</p>`; }
    finally { if (button.isConnected) button.disabled = false; }
  };
}

function groupRerunHistory(items) {
  const groups = new Map();
  // Group by identity, never by truncated titles: unrelated requests can look alike.
  for (const run of [...items].sort((a, b) => String(b.launched_at).localeCompare(String(a.launched_at)))) {
    const id = run.prompt_id || run.original?.id || run.session_id;
    if (!groups.has(id)) groups.set(id, { id, original: run.original, runs: [] });
    const group = groups.get(id);
    group.original ||= run.original;
    group.runs.push(run);
  }
  return [...groups.values()];
}

function rerunHistorySettings(x) {
  const observed = x.replay || x.original || {};
  const model = x.model || observed.requested_model;
  const agent = model?.startsWith("codex:") ? "Codex" : model?.startsWith("claude-") ? "Claude Code" : observed.harness || "Agent unrecorded";
  const effort = x.effort || observed.effort;
  const release = x.harness_version || x.replay?.harness_version
    || (x.release && x.release !== "same" ? x.release : (x.same_version || x.release === "same") ? x.original?.harness_version : "");
  return [agent, model?.replace(/^codex:/, "") || "Original model", effort ? `${effort} effort` : "Original effort",
    release ? `CLI ${release}` : x.same_version || x.release === "same" ? "Original CLI release" : "Installed CLI",
    x.mode === "terminal" ? "Terminal" : "Background"]
    .map((label) => `<span class="rr-chip">${esc(label)}</span>`).join("");
}

function rerunHistoryAttempt(x, number) {
  const r = x.replay, o = x.original;
  return `<article class="rh-attempt">
    <div class="rh-attempt-main"><div class="rh-attempt-heading"><a href="#/rerun/${encodeURIComponent(x.session_id)}">Attempt ${number} <span aria-hidden="true">↗</span></a>
      <time class="muted">${esc(fmtDate(x.launched_at))}</time>${rerunStatusHtml(x, { details: false })}</div>
      <a class="rh-prompt-link" href="#/${r ? "prompt/" + encodeURIComponent(r.id) : "rerun/" + encodeURIComponent(x.session_id)}">${esc(r?.text || o?.text || "View rerun details")}</a>
      <div class="rr-settings">${rerunHistorySettings(x)}</div>
      <details class="rh-settings"><summary>All launch settings</summary><div class="rr-settings">${rerunSettings(x)}</div></details>
      ${x.status === "failed" && x.result ? `<p class="rerun-needs">${esc(x.result.slice(0, 200))}</p>` : ""}
      ${x.status === "needs_you" ? rerunStatusHtml(x) : ""}
    </div>
    <div class="rh-attempt-result">${r ? `<strong class="rh-cost">${fmtCost(r.total_cost_usd)}</strong><span class="muted">${fmtMs(duration(r))}</span>
      ${o ? refDelta(r.total_cost_usd, o.total_cost_usd, { against: "original" }) : ""}
      <a href="#/compare?a=${encodeURIComponent(x.prompt_id)}&b=${encodeURIComponent(r.id)}">Compare with original →</a>`
      : `<span class="muted">${x.cost_usd != null ? fmtCost(x.cost_usd) : "Cost pending"}</span><span class="muted">${x.status === "running" ? "Running in background" : "No imported transcript"}</span>`}
      ${["running", "interactive"].includes(x.status) ? `<a href="#/rerun/${encodeURIComponent(x.session_id)}">Check process and logs →</a>` : ""}
      ${judgeBadge(x.rating)}${x.rating?.summary ? `<span class="rerun-needs">${esc(x.rating.summary)}</span>` : ""}
    </div></article>`;
}

function rerunHistoryHtml(items) {
  const groups = groupRerunHistory(items);
  const imported = items.filter((x) => x.replay).length;
  return `<details class="rh-section" id="rerun-history" ${rerunState.historyOpen ? "open" : ""}>
    <summary class="rh-section-heading"><span class="rh-section-icon" aria-hidden="true">↺</span><span><span class="rh-eyebrow">Your experiments</span><strong>Rerun history</strong><span class="muted">Every attempt, organized by its original prompt</span></span>
      <span class="rh-totals">${plural(items.length, "attempt")} · ${plural(groups.length, "prompt")}<small>${imported} imported</small></span><span class="rh-chevron" aria-hidden="true">⌄</span></summary>
    <div class="rh-groups"><div id="history-toolbar"></div><div id="history-results">${rerunHistoryGroupsHtml(items)}</div></div>
  </details>`;
}

function judgeHistoryHtml(groups) {
  const count = groups.reduce((total, group) => total + group.ratings.length, 0);
  return `<details class="rh-section judge-history" id="judge-history" ${rerunState.judgeHistoryOpen ? "open" : ""}>
    <summary class="rh-section-heading"><span class="rh-section-icon" aria-hidden="true">✦</span><span><span class="rh-eyebrow">Output reviews</span><strong>Judge run history</strong><span class="muted">Requested judgments, organized by prompt pair</span></span>
      <span class="rh-totals">${plural(count, "judge run")} · ${plural(groups.length, "pair")}</span><span class="rh-chevron" aria-hidden="true">⌄</span></summary>
    <div class="rh-groups">${groups.length ? groups.map((group, index) => {
      const key = JSON.stringify([group.base, group.other]);
      const open = rerunState.judgeHistoryGroups.get(key) || false;
      return `<details class="rh-group judge-history-group" data-judge-history-group="${esc(key)}" ${open ? "open" : ""}>
        <summary><span class="rh-group-number" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span><span class="rh-group-title"><strong>${esc(promptPreview(group.base_text))}</strong>
          <span class="muted">Compared with: ${esc(promptPreview(group.other_text))}</span></span><span class="rh-group-count">${plural(group.ratings.length, "judge run")}</span><span class="rh-chevron" aria-hidden="true">⌄</span></summary>
        <div class="rh-group-content"><a class="rh-compare-all" href="${esc(judgeComparisonUrl(group.base, group.other))}"><span class="rh-compare-icon" aria-hidden="true">✦</span><span class="rh-compare-copy"><span class="rh-compare-kicker">The same two prompts</span><strong>Compare all judge runs</strong><span>See their scores, verdicts and your human rating together.</span></span><span class="rh-compare-arrow" aria-hidden="true">→</span></a>
          ${group.human_feedback ? `<p class="judge-human-score">✎ Your rating <strong>${group.human_feedback.human_score} / 100</strong></p>` : ""}
          <div class="judge-history-runs">${group.ratings.map((rating) => `<article class="judge-history-run"><div><strong>${esc(rating.model || "Default model")}</strong><span class="muted">${esc(fmtDate(rating.rated_at))}</span></div><div>${judgeBadge(rating)}<p>${esc(rating.summary || "No summary recorded.")}</p></div></article>`).join("")}</div>
        </div></details>`;
    }).join("") : '<p class="muted rh-empty">No completed judge runs yet. Open a prompt comparison to judge its outputs.</p>'}</div></details>`;
}

function rerunHistoryGroupsHtml(items, sort = "recent") {
  const groups = groupRerunHistory(items);
  if (sort !== "recent") {
    const value = (group) => Math.max(0, ...group.runs.map((run) => {
      const p = run.replay;
      return sort === "cost" ? (p?.total_cost_usd ?? run.cost_usd ?? 0)
        : sort === "tokens" ? (p?.total_tokens ?? 0) : (p ? duration(p) || 0 : 0);
    }));
    groups.sort((a, b) => value(b) - value(a));
  }
  return groups.length ? groups.map((group, index) => {
      const o = group.original;
      const open = rerunState.historyGroups.has(group.id) ? rerunState.historyGroups.get(group.id) : index === 0;
      return `<details class="rh-group" data-history-group="${esc(group.id)}" ${open ? "open" : ""}>
        <summary><span class="rh-group-number" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span><span class="rh-group-title"><strong>${esc(o?.text || "Original prompt unavailable")}</strong>
          <span class="muted">${esc(o?.project || "Unknown project")} · Latest ${esc(fmtAgo(group.runs[0].launched_at))}</span></span>
          <span class="rh-group-count">${plural(group.runs.length, "attempt")}</span><span class="rh-chevron" aria-hidden="true">⌄</span></summary>
        <div class="rh-group-content">${o ? `<a class="rh-compare-all" href="#/rerun-comparison/${encodeURIComponent(o.id)}"><span class="rh-compare-icon" aria-hidden="true">↗</span><span class="rh-compare-copy"><span class="rh-compare-kicker">Your experiment, together</span><strong>Compare all reruns</strong><span>See every attempt side by side and choose a reference run.</span></span><span class="rh-compare-arrow" aria-hidden="true">→</span></a>` : ""}${o ? `<div class="rh-original"><span class="rh-eyebrow">Starting point</span><a href="#/prompt/${encodeURIComponent(o.id)}">Original prompt →</a><strong>${fmtCost(o.total_cost_usd)}</strong>
          <span class="muted">${esc([o.harness, o.requested_model, o.effort ? `${o.effort} effort` : "", o.harness_version ? `CLI ${o.harness_version}` : ""].filter(Boolean).join(" · "))}</span></div>` : '<p class="muted">The original prompt is outside the current history. Launch details are still available.</p>'}
          <div class="rh-attempts">${group.runs.map((x, i) => rerunHistoryAttempt(x, group.runs.length - i)).join("")}</div>
        </div></details>`;
    }).join("") : '<p class="muted rh-empty">Your next experiment starts above. Launched reruns will appear here, grouped by prompt.</p>';
}

const historyFilterState = { q: "", range: "all", sort: "recent", categoryTags: {}, tags: [], tag: "", project: "", lockTag: "" };

function historyToolbarHtml() {
  return `<search class="tags-toolbar browse-toolbar rh-filter-bar"><label class="search-box"><span aria-hidden="true">⌕</span><input type="search" id="history-search" placeholder="Search rerun history…" aria-label="Search rerun history" value="${esc(historyFilterState.q)}"></label>
    <div class="browse-filters" role="group" aria-label="History filters"><div class="segmented" role="group" aria-label="Period">${Object.entries(RANGES).map(([key, [label]]) => `<button type="button" data-history-range="${key}" aria-pressed="${key === historyFilterState.range}">${label}</button>`).join("")}</div>${tagFilterMenusHtml(historyFilterState)}</div>
    <div class="browse-actions"><span class="muted">Matches the original prompt or an imported rerun.</span><span class="toolbar-spacer"></span><label class="sort-box">Sort <select id="history-sort">${Object.entries(PROMPT_SORTS).map(([key, label]) => `<option value="${key}" ${key === historyFilterState.sort ? "selected" : ""}>${label}</option>`).join("")}</select></label><button class="link-btn" type="button" data-history-clear>Clear filters</button></div></search>`;
}

function historyPageUrl() {
  const query = new URLSearchParams();
  if (historyFilterState.q) query.set("q", historyFilterState.q);
  if (historyFilterState.range !== "all") query.set("range", historyFilterState.range);
  if (historyFilterState.sort !== "recent") query.set("sort", historyFilterState.sort);
  Object.values(historyFilterState.categoryTags).flat().forEach((tag) => query.append("tags", tag));
  return `#/reruns${query.size ? `?${query}` : ""}`;
}

function historyFilterQuery() {
  const query = new URLSearchParams({limit: "100000"});
  if (historyFilterState.q) query.set("q", historyFilterState.q);
  const since = RANGES[historyFilterState.range][1];
  if (since) query.set("since", since().toISOString());
  Object.values(historyFilterState.categoryTags).flat().forEach((tag) => query.append("tags", tag));
  return query;
}

function bindHistoryFilters(page, history) {
  const toolbar = page.querySelector("#history-toolbar"), results = page.querySelector("#history-results");
  let generation = 0, timer;
  historyFilterState.tagCounts = null;
  const renderResults = async () => {
    const current = ++generation;
    const url = historyPageUrl();
    if (location.hash !== url) window.history.replaceState(null, "", url);
    api(`/api/tag-counts?${tagCountsQuery(historyFilterState, "reruns")}`).then((tagCounts) => {
      if (current !== generation || !toolbar.isConnected) return;
      historyFilterState.tagCounts = tagCounts;
      showTagCounts(toolbar, historyFilterState);
    }).catch(() => { /* keep the last counts: the results report any failure */ });
    const filters = historyFilterQuery();
    const filtering = [...filters.keys()].some((k) => k !== "limit");
    let filtered = history;
    if (filtering) {
      try {
        const matches = await api(`/api/prompts?${filters}`);
        if (current !== generation || !results.isConnected) return;
        const ids = new Set(matches.items.map((p) => p.id));
        filtered = history.filter((x) => ids.has(x.prompt_id) || (x.replay && ids.has(x.replay.id)));
      } catch (error) {
        if (error instanceof StaleNavigation || current !== generation || !results.isConnected) return;
        results.innerHTML = `<p class="status-warn">Could not filter history: ${esc(error.message)}</p>`;
        return;
      }
    }
    results.innerHTML = filtered.length ? rerunHistoryGroupsHtml(filtered, historyFilterState.sort) : `<p class="muted rh-empty">${filtering ? "No reruns match these filters." : "Your next experiment starts above. Launched reruns will appear here, grouped by prompt."}</p>`;
    bindHistoryGroups(results);
  };
  const renderToolbar = () => {
    toolbar.innerHTML = historyToolbarHtml();
    toolbar.querySelector("#history-search").addEventListener("input", (event) => {
      clearTimeout(timer);
      const value = event.target.value;
      timer = setTimeout(() => { historyFilterState.q = value; renderResults(); }, 250);
    });
    toolbar.querySelectorAll("[data-history-range]").forEach((button) => button.onclick = () => {
      historyFilterState.range = button.dataset.historyRange; renderToolbar(); renderResults();
    });
    toolbar.querySelector("#history-sort").onchange = (event) => { historyFilterState.sort = event.target.value; renderResults(); };
    toolbar.querySelector("[data-history-clear]").onclick = () => {
      clearTimeout(timer);
      Object.assign(historyFilterState, {q: "", range: "all", categoryTags: {}}); renderToolbar(); renderResults();
    };
    bindTagFilterMenus(toolbar, historyFilterState, renderToolbar, renderResults);
  };
  renderToolbar();
  renderResults();
}

function bindHistoryGroups(container) {
  container.querySelectorAll("[data-history-group]").forEach((group) => group.addEventListener("toggle", () => {
    rerunState.historyGroups.set(group.dataset.historyGroup, group.open);
  }));
}

async function viewRerunComparison(promptId) {
  const [original, referenceData, settings, models] = await Promise.all([
    api(`/api/prompts/${encodeURIComponent(promptId)}`),
    api(`/api/reruns/reference/${encodeURIComponent(promptId)}`),
    api("/api/settings"), loadModelCatalog(),
  ]);
  const runs = original.replays || [], referenceId = referenceData.reference_id;
  const entries = [{prompt: original, run: null, label: "Original"}, ...runs.map((run, i) => ({prompt: run.prompt, run, label: `Attempt ${runs.length - i}`}))];
  const reference = entries.find((entry) => entry.prompt?.id === referenceId) || entries[0];
  const baseline = reference.prompt;
  const metric = (value, format) => value == null ? "—" : format(value);
  const delta = (value, base) => value != null && base != null ? refDelta(value, base, {against: "reference"}) : "";
  const rows = entries.map(({prompt: p, run, label}) => {
    const isReference = p?.id === referenceId;
    const rating = p ? referenceData.ratings[p.id] : null;
    const agent = run ? (run.model ? (run.model.startsWith("codex:") ? "Codex" : "Claude Code") : original.harness || "Original agent") : original.harness;
    const model = run?.model?.replace(/^codex:/, "") || original.requested_model || "Original model";
    const effort = run?.effort || original.effort;
    const settingsLabel = [agent, model, effort ? `${effort} effort` : "Original effort", run?.harness_version ? `CLI ${run.harness_version}` : original.harness_version && !run ? `CLI ${original.harness_version}` : "", run ? (run.mode === "terminal" ? "Terminal" : "Background") : ""].filter(Boolean).join(" · ");
    const cost = p?.total_cost_usd ?? run?.cost_usd;
    const time = p ? duration(p) : null;
    const canJudge = p && !isReference && referenceData.answers[referenceId] && referenceData.answers[p.id];
    return `<tr class="${isReference ? "rc-base" : ""}"><td>${run ? `<a href="#/rerun/${encodeURIComponent(run.session_id)}"><strong>${label}</strong></a>` : `<strong>Original</strong>`}
      <br><span class="muted">${esc(fmtDate(run?.launched_at || original.started_at))}</span>${run ? `<br>${rerunStatusHtml(run, {details:false})}` : ""}
      ${isReference ? '<br><span class="rc-reference">★ Reference run</span>' : p ? `<br><button type="button" class="rc-set-reference" data-set-rerun-reference="${esc(p.id)}">☆ Set as reference</button>` : ""}
      ${p ? `<details class="rc-prompt"><summary>Full initial prompt</summary><pre>${esc(p.text)}</pre><a href="#/prompt/${encodeURIComponent(p.id)}">Open prompt →</a></details>` : ""}</td>
      <td>${esc(settingsLabel)}${run ? `<br><details class="rc-settings"><summary>All settings</summary><div class="rr-settings">${rerunSettings({...run, original})}</div></details>` : ""}</td>
      <td>${metric(cost, fmtCost)}${!isReference && p ? delta(cost, baseline.total_cost_usd) : ""}</td>
      <td>${p ? metric(p.total_tokens, fmtTokens) : "—"}</td><td>${metric(time, fmtMs)}${!isReference && p ? delta(time, duration(baseline)) : ""}</td>
      <td>${isReference ? '<span class="muted">Reference output</span>' : p ? `${judgeBadge(rating)}${rating?.summary ? `<div class="muted">${esc(rating.summary)}</div>` : !rating ? '<span class="muted">Not judged against this reference</span>' : ""}
        ${rating?.status === "failed" ? `<div class="status-warn">${esc(rating.error || "Judge failed")}</div>` : ""}
        <p><a href="#/compare?${new URLSearchParams({a: referenceId, b: p.id, rerun_comparison: promptId})}">Compare outputs →</a></p>
        <button type="button" class="btn rc-judge" data-judge-run="${esc(p.id)}" ${!canJudge || rating?.status === "rating" || EXAMPLES ? "disabled" : ""}>${rating && rating.status !== "failed" ? "Judge again" : "Judge output"}</button>
        ${canJudge ? `<div class="rc-judge-impact" data-judge-impact="${esc(p.id)}" aria-live="polite">Estimating judge usage…</div>` : ""}
        ${!canJudge ? '<div class="muted">Both runs need a final answer to judge.</div>' : ""}` : '<span class="muted">No imported transcript</span>'}</td></tr>`;
  }).join("");
  const modelOptions = [...new Set(models.map((m) => m.id).filter(Boolean))];
  const chartPoints = entries.filter(({prompt}) => prompt && prompt.started_at).map(({prompt, label}) => ({...prompt, text: label}));
  const chartReference = chartPoints.find((point) => point.id === referenceId);
  const chartSpecs = [
    ["cost", "Cost (USD, including subagents)", (p) => p.total_cost_usd, fmtCost],
    ["time", "Duration (active time, else wall time)", duration, fmtMs],
    ["tokens", "Tokens (including cache and subagents)", (p) => p.total_tokens, fmtTokens],
  ];
  destroyCharts();
  app.innerHTML = `<div class="prompts-page rerun-comparison-page"><p><a href="${esc(historyPageUrl())}">← Rerun history</a></p>
    <header class="home-hero page-hero"><div class="hero-copy"><h1><span class="hero-name">Rerun comparison</span>One prompt. <em>Every attempt.</em></h1><p>${esc(promptPreview(original.text))}</p>
      <details class="rc-original-prompt"><summary>Read the full original prompt</summary><pre>${esc(original.text)}</pre></details></div></header>
    <section class="card rc-reference-feature" aria-label="Selected reference run"><span class="rc-reference-star" aria-hidden="true">★</span><div><span class="rc-reference-kicker">Your comparison baseline</span><h2>Reference run · ${esc(reference.label)}</h2><p>Cost, time and output verdicts are measured against this run.</p><a href="#/prompt/${encodeURIComponent(baseline.id)}">Open reference prompt →</a></div><div class="rc-reference-metrics"><span><small>Cost</small><strong>${metric(baseline.total_cost_usd, fmtCost)}</strong></span><span><small>Tokens</small><strong>${metric(baseline.total_tokens, fmtTokens)}</strong></span><span><small>Time</small><strong>${metric(duration(baseline), fmtMs)}</strong></span></div></section>
    <section class="rc-trends" aria-labelledby="rc-trends-title"><h2 id="rc-trends-title">Cost and usage over time</h2>
      <p class="muted">Original and imported reruns, in time order. ◆ marks ${esc(reference.label)}, the selected reference. Select a point to open its prompt; runs without imported usage are shown in the table.</p>
      <div class="charts">${chartSpecs.map(([id, title]) => `<div class="card"><div class="chart-title">${esc(title)}</div><div class="chart-box"><canvas id="rerun-chart-${id}" role="img" aria-label="${esc(title)} over time; values are also listed in the table below"></canvas></div></div>`).join("")}</div>
    </section>
    <div class="rc-intro"><h2>Compare with ${esc(reference.label)}</h2><p class="muted">Costs, time and output verdicts are compared with the selected reference. Set another imported run as the reference when it produces a better result.</p>
      <label>Judge model <select id="rc-judge-model" data-default-model="${esc(settings.judge_model || "")}"><option value="">Default (${esc(settings.judge_model || "agent default")})</option>${modelOptions.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("")}</select></label>
      ${EXAMPLES ? '<p class="muted">Illustrative examples do not run judges.</p>' : '<p class="muted">Judge calls use the selected agent and may add cost and usage.</p>'}<p id="rc-error" class="status-warn" role="alert" hidden></p></div>
    <div class="table-wrap card rc-table"><table><thead><tr><th>Run</th><th>Settings</th><th>Cost</th><th>Tokens</th><th>Time</th><th>Output vs reference</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${runs.length ? "" : '<p class="muted">No reruns have been launched for this prompt.</p>'}</div>`;
  const page = app.querySelector(".rerun-comparison-page");
  chartSpecs.forEach(([id, , value, format]) => compareChart(page.querySelector(`#rerun-chart-${id}`),
    [{label: "Run", color: "--series-1", points: chartPoints}], null, value, format, chartReference));
  const modelSelect = page.querySelector("#rc-judge-model");
  bindModelAccess(modelSelect, settings.judge_model || "");
  let judgeEstimateGeneration = 0;
  const updateJudgeEstimates = () => {
    const generation = ++judgeEstimateGeneration;
    page.querySelectorAll("[data-judge-impact]").forEach(async (box) => {
      box.textContent = "Estimating judge usage…";
      try {
        const data = await api(`/api/ratings/estimate?${new URLSearchParams({base: referenceId, other: box.dataset.judgeImpact, model: modelSelect.value})}`);
        if (!box.isConnected || generation !== judgeEstimateGeneration) return;
        box.innerHTML = `<strong>Judge: ≈ ${fmtTokens(data.input_tokens + data.output_tokens)} tokens</strong>${quotaForecast(data.forecast)}<small>Rough input estimate + 512 output tokens; actual usage varies.</small>`;
      } catch (error) { if (!(error instanceof StaleNavigation) && box.isConnected && generation === judgeEstimateGeneration) box.textContent = "Judge usage estimate unavailable."; }
    });
  };
  modelSelect.addEventListener("change", updateJudgeEstimates);
  updateJudgeEstimates();
  const showError = (error) => { if (!(error instanceof StaleNavigation) && page.isConnected) { const box = page.querySelector("#rc-error"); box.textContent = error.message; box.hidden = false; } };
  page.querySelectorAll("[data-set-rerun-reference]").forEach((button) => button.onclick = async () => {
    button.disabled = true;
    try {
      await api(`/api/reruns/reference/${encodeURIComponent(promptId)}`, {method:"PUT", body:{reference_id:button.dataset.setRerunReference}});
      await viewRerunComparison(promptId);
    } catch (error) { showError(error); if (button.isConnected) button.disabled = false; }
  });
  page.querySelectorAll("[data-judge-run]").forEach((button) => button.onclick = async () => {
    button.disabled = true;
    try {
      await api("/api/ratings", {method:"POST", body:{base:referenceId, other:button.dataset.judgeRun, model:modelSelect.value, force:true}});
      await viewRerunComparison(promptId);
    } catch (error) { showError(error); if (button.isConnected) button.disabled = false; }
  });
  if (Object.values(referenceData.ratings).some((rating) => rating?.status === "rating")) {
    const here = location.hash;
    setTimeout(() => { if (page.isConnected && location.hash === here) viewRerunComparison(promptId).catch(showError); }, 3000);
  }
}

function rerunBudgetWarning(q) {
  const budget = Number(q.budget_usd);
  if (!(budget > 0)) return "";
  if (q.mode === "terminal" || q.agent === "Codex" || q.model?.startsWith("codex:")) {
    return `The saved ${fmtCost(budget)} budget is not enforced in ${q.mode === "terminal" ? "terminal mode" : "Codex"}. This run can exceed it.`;
  }
  if (q.estimate?.cost_usd != null && q.estimate.cost_usd > budget) {
    return `Estimated cost ${fmtCost(q.estimate.cost_usd)} exceeds the ${fmtCost(budget)} budget cap. The agent may stop before completing the task. Increase the budget or choose a cheaper model in Configure.`;
  }
  return "";
}

function rerunEstimateTotal(items) {
  items = items.filter((q) => q.kind !== "judge");
  const estimates = items.map((q) => q.estimate).filter(Boolean);
  const priced = estimates.filter((e) => e.cost_usd != null);
  const durations = estimates.map((e) => e.duration_ms).filter((ms) => ms != null);
  return { count: items.length, missing: items.length - estimates.length, unpriced: items.length - priced.length,
    cost: priced.reduce((sum, e) => sum + e.cost_usd, 0),
    tokens: estimates.reduce((sum, e) => sum + e.total_tokens, 0),
    parallel_ms: durations.length ? Math.max(...durations) : null,
    work_ms: durations.length ? durations.reduce((sum, ms) => sum + ms, 0) : null,
    missing_time: items.length - durations.length };
}

function rerunEstimateCost(total) {
  return total.unpriced === total.count && total.count ? "Cost unavailable"
    : `≈ ${fmtCost(total.cost)}${total.unpriced ? ` + ${total.unpriced} unpriced` : ""}`;
}

function rerunEstimateSummary(title, total) {
  const tokens = total.missing === total.count ? "Unavailable" : `≈ ${fmtTokens(total.tokens)}`;
  return `<section class="rr-estimate-summary" aria-label="${title} consumption estimate">
    <div class="rr-estimate-header"><div><span class="rr-estimate-kicker">Consumption estimate</span><h3>${title}</h3></div><span class="rr-estimate-count">${total.count} run${total.count === 1 ? "" : "s"}</span></div>
    <div class="rr-estimate-metrics">
      <div class="rr-estimate-metric"><span class="rr-metric-icon" aria-hidden="true">◈</span><span class="rr-metric-label">Tokens</span><strong>${tokens}</strong><span class="rr-metric-detail">${total.missing ? `${total.missing} run${total.missing === 1 ? "" : "s"} unknown` : "Estimated total"}</span></div>
      <div class="rr-estimate-metric"><span class="rr-metric-icon" aria-hidden="true">◷</span><span class="rr-metric-label">Concurrent time</span><strong>${fmtMs(total.parallel_ms)}</strong><span class="rr-metric-detail">${total.missing_time ? `${total.missing_time} unknown · ` : ""}Combined run time: ${fmtMs(total.work_ms)}</span></div>
      <div class="rr-estimate-metric rr-metric-cost"><span class="rr-metric-icon" aria-hidden="true">↗</span><span class="rr-metric-label">Estimated API cost</span><strong>${rerunEstimateCost(total)}</strong><span class="rr-metric-detail">Based on recorded usage</span></div>
    </div></section>`;
}

function judgeQueueSummary(title, items) {
  const judges = items.filter((q) => q.kind === "judge");
  if (!judges.length) return "";
  const known = judges.filter((q) => q.estimate);
  const tokens = known.reduce((sum, q) => sum + q.estimate.total_tokens, 0);
  return `<section class="rr-estimate-summary judge-queue-summary"><div class="rr-estimate-header"><div><span class="rr-estimate-kicker">Judgment estimate</span><h3>${title}</h3></div><span class="rr-estimate-count">${plural(judges.length, "judgment")}</span></div><div class="judge-queue-summary-body"><span class="rr-metric-icon" aria-hidden="true">✦</span><div><span class="rr-metric-label">Approximate tokens</span><strong>${known.length ? `≈ ${fmtTokens(tokens)}` : "Unavailable"}</strong><span class="rr-metric-detail">${judges.length - known.length ? `${plural(judges.length - known.length, "judgment")} unavailable · ` : ""}Cost and time unavailable</span></div></div><p class="rr-estimate-note muted">Rough input estimate plus 512 output tokens per judgment. Actual usage, reasoning and CLI overhead can differ.</p></section>`;
}

function showQueueConfigIssues(dialog, issues, focus = true) {
  const list = dialog.querySelector("#queue-config-issues");
  list.innerHTML = issues.map((issue) => `<li>${esc(issue.message)}</li>`).join("");
  list.hidden = !issues.length;
  const warning = dialog.querySelector("#queue-config-warning");
  if (warning) warning.hidden = !issues.length;
  dialog.querySelectorAll("[aria-invalid]").forEach((field) => field.removeAttribute("aria-invalid"));
  for (const issue of issues) dialog.querySelector(`[name="${issue.field}"]`)?.setAttribute("aria-invalid", "true");
  const first = issues.find((issue) => dialog.querySelector(`[name="${issue.field}"]`));
  if (focus && first) dialog.querySelector(`[name="${first.field}"]`).focus();
}

function editQueuedRun(q, resumeIds = null) {
  document.getElementById("queue-config-dialog")?.remove();
  const option = (value, label, selected) => `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`;
  const permission = q.permission ?? "same";
  app.insertAdjacentHTML("beforeend", `<dialog id="queue-config-dialog" class="tag-dialog queue-config-dialog" aria-labelledby="queue-config-title">
    <form id="queue-config-form">
      <h2 id="queue-config-title">${resumeIds ? "Complete configuration before launching" : "Configure queued rerun"}</h2>
      <p>${esc(q.prompt?.text || "Queued prompt unavailable")}</p>
      <section id="queue-config-warning" class="queue-config-warning" role="alert" hidden><strong>Complete these settings to enable Save</strong><ul id="queue-config-issues"></ul></section>
      ${resumeIds ? '<p class="muted">Complete each selected run that needs input. You will review the launch confirmation after saving.</p>' : ""}
      <div class="queue-config-fields">
        <label>Model <select name="model">${option("", "Same as original", q.model || "")}${[...new Set([q.model, ...modelCatalog.map((m) => m.id)])].filter(Boolean).map((id) => option(id, id, q.model)).join("")}</select></label>
        <label>Effort <select name="effort">${option("", "Same as original", q.effort || "")}${EFFORTS.map((e) => option(e, e, q.effort)).join("")}</select></label>
        <label>Run <select name="mode">${option("terminal", "In a terminal window", q.mode || "background")}${option("background", "Unattended, in the background", q.mode || "background")}</select></label>
        <label class="queue-terminal-option">After answer <select name="keep_terminal_open">${option("false", "Close terminal", q.keep_terminal_open ? "true" : "false")}${option("true", "Keep open until Enter", q.keep_terminal_open ? "true" : "false")}</select></label>
        <label>Permissions <select name="permission" required aria-describedby="queue-config-warning">${option("", "Choose permissions", permission)}${option("same", "Same as original", permission)}${option("ask", "Read-only when unattended", permission)}${option("edits", "Allow file edits", permission)}</select></label>
        <label>CLI release <input name="version" value="${esc(q.release ?? (q.same_version ? "same" : ""))}" placeholder="Installed (or same / release number)"></label>
        <label>Budget in USD <input name="budget_usd" type="number" min="0.01" max="1000" step="any" value="${q.budget_usd ?? ""}" placeholder="No cap"></label>
        <label class="queue-config-directory">Working directory <input name="cwd" value="${esc(q.cwd || q.prompt?.cwd || "")}" placeholder="Absolute path to the workspace" required></label>
      </div>
      <p class="muted">Codex background runs require an empty USD budget. Changing agents requires explicit permissions.</p>
      <p id="queue-config-status" role="status" class="status-warn" hidden></p>
      <div class="replay-buttons"><button type="button" class="btn" data-cancel-config>Cancel</button><button type="submit" class="btn primary">${resumeIds ? "Save and continue" : "Save settings"}</button></div>
    </form></dialog>`);
  const dialog = document.getElementById("queue-config-dialog");
  dialog.querySelector("[data-cancel-config]").onclick = () => dialog.close();
  dialog.addEventListener("close", () => dialog.remove());
  const form = dialog.querySelector("form");
  const queueMode = form.elements.namedItem("mode");
  const showTerminalOption = () => { dialog.querySelector(".queue-terminal-option").hidden = queueMode.value !== "terminal"; };
  queueMode.addEventListener("change", showTerminalOption);
  showTerminalOption();
  const saveButton = dialog.querySelector('[type="submit"]');
  let validationReady = false, saving = false, validationRequest = 0, validationTimer, validatedValues = "";
  const validate = async () => {
    const request = ++validationRequest;
    validationReady = false;
    saveButton.disabled = true;
    const issues = [...form.elements].filter((field) => field.name && !field.validity.valid)
      .map((field) => ({field: field.name, message: field.name === "permission" ? "Choose permissions for this run." : `${field.closest("label").firstChild.textContent.trim()}: ${field.validationMessage}`}));
    if (issues.length) {
      showQueueConfigIssues(dialog, issues, false);
      dialog.querySelector("#queue-config-status").hidden = true;
      return;
    }
    try {
      const proposed = Object.fromEntries(new FormData(form));
      const result = await api(`/api/reruns/queue/${q.id}/validate`, {method: "POST", body: proposed});
      if (!dialog.isConnected || request !== validationRequest) return;
      validationReady = !result.needs_input;
      validatedValues = JSON.stringify(proposed);
      showQueueConfigIssues(dialog, result.issues, false);
      dialog.querySelector("#queue-config-status").hidden = true;
    } catch (error) {
      if (error instanceof StaleNavigation || !dialog.isConnected || request !== validationRequest) return;
      const status = dialog.querySelector("#queue-config-status");
      status.textContent = `Could not validate settings: ${error.message}. Edit a setting to retry.`;
      status.hidden = false;
    }
    if (request === validationRequest) saveButton.disabled = saving || !validationReady;
  };
  const scheduleValidation = () => {
    // A text field also emits change on blur; do not cancel a Save click when
    // its current value already passed validation after the input event.
    if (validationReady && JSON.stringify(Object.fromEntries(new FormData(form))) === validatedValues) return;
    clearTimeout(validationTimer);
    validationRequest++;
    validationReady = false;
    saveButton.disabled = true;
    validationTimer = setTimeout(validate, 200);
  };
  form.addEventListener("input", scheduleValidation);
  form.addEventListener("change", scheduleValidation);
  dialog.addEventListener("close", () => { clearTimeout(validationTimer); validationRequest++; });
  dialog.querySelector("form").onsubmit = async (event) => {
    event.preventDefault();
    if (!validationReady || saving) return;
    saving = true;
    const button = dialog.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      const saved = await api(`/api/reruns/queue/${q.id}`, {method: "PATCH", body: Object.fromEntries(new FormData(event.currentTarget))});
      if (saved.needs_input) {
        validationReady = false;
        showQueueConfigIssues(dialog, saved.issues);
        const status = dialog.querySelector("#queue-config-status");
        status.textContent = "Settings saved. Complete the remaining required choices before continuing.";
        status.hidden = false;
        return;
      }
      dialog.close();
      await viewReruns();
      if (resumeIds) {
        const latest = await api("/api/reruns");
        const selected = latest.queue.filter((run) => resumeIds.includes(run.id));
        await launchPicked(selected, rerunEstimateTotal(selected));
      }
    } catch (error) {
      if (!(error instanceof StaleNavigation) && dialog.isConnected) {
        const status = dialog.querySelector("#queue-config-status");
        status.textContent = error.message;
        status.hidden = false;
      }
    } finally { saving = false; button.disabled = !validationReady; }
  };
  dialog.showModal();
  showQueueConfigIssues(dialog, q.issues || []);
  validate();
}

function rerunQueueCard(q, index) {
  if (q.kind === "judge") return `<article class="rr-row rq-card judge-queue-card ${rerunState.picked.has(q.id) ? "selected" : ""}" data-rr="${q.id}">
    <header class="rq-head"><label class="rq-select"><input type="checkbox" class="psel" data-rr-pick="${q.id}" ${rerunState.picked.has(q.id) ? "checked" : ""} aria-label="Select judge run ${index + 1}"><span class="rq-index">${String(index + 1).padStart(2, "0")}</span></label>
      <span class="run-kind run-kind-judge">✦ Judge run</span><span class="rq-status">Ready to run</span><time class="muted rr-added">queued ${esc(fmtAgo(q.added_at))}</time>
      <button type="button" class="rr-remove" data-rr-remove="${q.id}" aria-label="Take it out of the queue">✕</button></header>
    <div class="judge-queue-body"><div><span class="judge-label">Reference prompt</span><p>${esc(q.prompt?.text || "Prompt unavailable")}</p><span class="judge-label">Compared prompt</span><p>${esc(q.other_prompt?.text || "Prompt unavailable")}</p></div>
      <div><span class="rq-label">Judge model</span><strong>${esc(q.model || "Default judge at launch")}</strong><p class="muted">${q.estimate ? `≈ ${fmtTokens(q.estimate.total_tokens)} tokens · cost and time unavailable` : "Usage estimate unavailable"}</p><div data-quota-run="${q.id}">${quotaForecast(q.forecast)}</div></div></div>
    <footer class="rq-footer"><a href="#/compare?${new URLSearchParams({a:q.prompt_id,b:q.other_prompt_id})}">Open comparison →</a><button type="button" class="btn primary" data-rr-launch-one="${q.id}">✦ Run judgment</button></footer></article>`;
  const p = q.prompt, e = q.estimate;
  const selected = rerunState.picked.has(q.id);
  const agent = q.agent || (q.model?.startsWith("codex:") ? "Codex" : p?.harness) || "Original agent";
  const model = (q.model || e?.model || p?.requested_model || "Original model").replace(/^codex:/, "");
  const effort = q.effort || p?.effort || "Original";
  return `<article class="rr-row rq-card ${q.needs_input ? "rr-needs-input" : ""} ${selected ? "selected" : ""}" data-rr="${q.id}">
    <header class="rq-head"><label class="rq-select"><input type="checkbox" class="psel" data-rr-pick="${q.id}" ${selected ? "checked" : ""} aria-label="Select rerun ${index + 1}"><span class="rq-index">${String(index + 1).padStart(2, "0")}</span></label>
      <span class="run-kind run-kind-prompt">↻ Prompt run</span><span class="rq-project">${esc(p?.project || "Original project")}</span>
      <span class="rq-status ${q.needs_input ? "rq-status-needs" : ""}">${q.needs_input ? "Needs input" : "Ready to run"}</span>
      <time class="muted rr-added" title="${esc(fmtDate(q.added_at))}">queued ${esc(fmtAgo(q.added_at))}</time>
      <button type="button" class="rr-remove" data-rr-remove="${q.id}" title="Take it out of the queue" aria-label="Take it out of the queue">✕</button>
    </header>
    <div class="rq-content"><div class="rq-main">
      ${p ? `<a class="rq-title" href="#/prompt/${encodeURIComponent(p.id)}">${esc(p.text)}</a>` : '<p class="muted">Original prompt is outside the current history.</p>'}
      <div class="rq-config"><div><span class="rq-label">${esc(agent)}</span><strong>${esc(model)}</strong></div>
        <div><span class="rq-label">Reasoning</span><strong>${esc(effort)}${effort === "Original" ? " effort" : ""}</strong></div>
        <div><span class="rq-label">Execution</span><strong>${q.mode === "terminal" ? "Terminal" : "Background"}</strong></div></div>
      ${q.needs_input ? `<section class="rr-input-needed rq-issues" aria-label="Configuration issues"><strong class="status-warn">Complete your setup</strong><ul>${(q.issues || []).map((issue) => `<li>${esc(issue.message)}</li>`).join("")}</ul></section>` : ""}
      ${rerunBudgetWarning(q) ? `<p class="rq-budget-warning status-warn" role="alert">${esc(rerunBudgetWarning(q))}</p>` : ""}
    </div>
    <aside class="rq-estimate" aria-label="Rerun estimate"><span class="rq-label">Estimated cost</span><strong class="rq-cost">${rerunEstimateCost(rerunEstimateTotal([q]))}</strong>
      ${e ? `<dl><div><dt>Tokens</dt><dd>≈ ${fmtTokens(e.total_tokens)}</dd></div><div><dt>Time baseline</dt><dd>${fmtMs(e.duration_ms)}</dd></div></dl>` : '<p class="muted">Original usage unavailable</p>'}
      ${p?.total_cost_usd != null ? `<span class="rq-original-cost">Original ${fmtCost(p.total_cost_usd)}</span>` : ""}
    </aside></div>
    <footer class="rq-footer"><details class="rq-details"><summary>Settings & usage details</summary><div class="rq-details-content"><div class="rr-settings">${rerunSettings(q)}</div>
      <p class="muted">Directory: <code>${esc(p?.cwd || "Unavailable")}</code></p>
      ${e ? `<p class="muted">${fmtTokens(e.input_tokens)} input · ${fmtTokens(e.output_tokens)} output tokens</p>` : ""}
      <div data-quota-run="${q.id}">${quotaForecast(q.forecast)}</div></div></details>
      <div class="rq-actions"><button type="button" class="btn ${q.needs_input ? "primary" : ""}" data-rr-config="${q.id}">${q.needs_input ? "Complete configuration" : "Configure"}</button>
        <button type="button" class="btn ${q.needs_input ? "" : "primary"}" data-rr-launch-one="${q.id}">↻ Run this rerun</button></div>
    </footer></article>`;
}

function modelAvailabilitySummaryHtml(data) {
  const link = `${EXAMPLES ? "/" : ""}#/setup?jump=model-availability`;
  const results = data?.scan.results || [];
  const listed = results.filter((r) => r.status === "listed" && r.model);
  const date = (value) => `<time datetime="${esc(value)}">${esc(fmtDate(value))}</time>`;
  const agents = [...new Set(results.map((r) => r.agent))];
  const agentNames = {claude: "Claude Code", codex: "OpenAI Codex"};
  const information = !data ? 'Model availability information is unavailable.'
    : listed.length ? `${plural(listed.length, "model")} listed by your agents.`
    : 'No model information available yet.';
  return `<div class="card model-availability-summary"><div class="model-availability-top"><details class="model-availability-details"><summary><span class="model-availability-icon" aria-hidden="true">✦</span><span><strong>Model availability</strong><span class="model-availability-description">${information}${data?.scan.status === "running" ? ' Refresh in progress.' : ""}</span></span></summary>
      ${agents.length ? `<ul>${agents.map((agent) => {
        const rows = results.filter((r) => r.agent === agent);
        const count = rows.filter((r) => r.status === "listed" && r.model).length;
        const checked = rows.map((r) => r.checked_at).filter(Boolean).sort().at(-1);
        return `<li><strong>${esc(agentNames[agent] || agent)}</strong> · ${count ? plural(count, "model") + ' listed' : 'No catalog information'}${checked ? ` · ${count ? "Updated" : "Last attempted"} ${date(checked)}` : ""}</li>`;
      }).join("")}</ul>` : data?.scan.finished_at ? `<p class="muted">Last attempted ${date(data.scan.finished_at)}</p>` : ""}
    </details><a class="btn" href="${link}">Recompute or schedule in Setup →</a></div></div>`;
}

function modelChecksHtml(data) {
  if (!data) return '<div class="card"><strong>Model availability</strong><p class="muted">Could not load model-check settings. Reload the page to retry.</p></div>';
  const {config, scan, next_at: nextAt} = data;
  const running = scan.status === "running";
  const names = {claude: "Claude Code", codex: "OpenAI Codex"};
  const labels = {listed: "Listed by agent", available: "Previous test succeeded", unavailable: "Access rejected", fallback: "Different model",
    rate_limited: "Limit reached", auth_required: "Sign-in needed", credits_required: "Billing issue",
    timeout: "Timed out", inconclusive: "Inconclusive", discovery_failed: "Catalog unavailable", error: "Could not start"};
  return `<details class="card model-checks" open><summary><span class="model-check-icon" aria-hidden="true">✓</span><span class="model-check-title"><span class="model-check-kicker">Setup</span><strong>Model availability</strong></span><span class="model-check-badge">${running ? "Checking…" : scan.finished_at ? `Last check ${esc(fmtDate(scan.finished_at))}` : "Ready to check"}</span></summary>
    <div class="model-checks-body">
      <div class="model-check-quick"><p>See the models listed by your signed-in agents.</p><button class="btn primary" type="button" data-check-models ${running || EXAMPLES ? "disabled" : ""}>${running ? "Checking…" : "Check now"}</button></div>
      <p>Refresh each installed agent’s model menu. This check sends no test prompts and uses no inference tokens or API credits.</p>
      <p class="muted">The agent may contact its provider to refresh the catalog. A listed model is not proof of subscription inclusion or a successful request; runtime limits and access can still apply.</p>
      ${EXAMPLES ? '<p class="muted">Illustrative example. Model checks never launch agents in the example app.</p>' : ""}
      <form id="model-check-config">
        <fieldset ${EXAMPLES ? "disabled" : ""}><legend>Agents to check</legend>
          ${Object.entries(names).map(([id, name]) => `<label><input type="checkbox" name="agents" value="${id}" ${config.agents.includes(id) ? "checked" : ""}> ${name}</label>`).join("")}
        </fieldset>
        <div class="model-check-schedule"><label><input type="checkbox" name="enabled" ${config.enabled ? "checked" : ""} ${EXAMPLES ? "disabled" : ""}> Check periodically while Rerunly is running</label>
          <label>Frequency <select name="interval_hours" ${EXAMPLES ? "disabled" : ""}>${[[6, "Every 6 hours"], [24, "Daily"], [168, "Weekly"]].map(([value, label]) => `<option value="${value}" ${config.interval_hours === value ? "selected" : ""}>${label}</option>`).join("")}</select></label></div>
        <div class="model-check-actions"><button class="btn" type="submit" ${EXAMPLES ? "disabled" : ""}>Save schedule</button>
          ${running ? '<button class="btn" type="button" data-cancel-model-check>Cancel check</button>' : ""}</div>
      </form>
      <p class="muted" data-model-check-state aria-live="polite">${running ? esc(scan.current || "Starting checks…") : `Status: ${esc(scan.status)}.${config.enabled ? ` Next check: ${nextAt ? esc(fmtDate(nextAt)) : "within 30 seconds"}.` : " Periodic checks are off."}`}</p>
      <p class="status-warn" data-model-check-error role="status" hidden></p>
      ${scan.results.length ? `<div class="table-wrap"><table><thead><tr><th>Agent / model</th><th>Result</th><th>Checked</th></tr></thead><tbody>
        ${scan.results.map((r) => `<tr><td>${esc(names[r.agent] || r.agent)}<br><code>${esc(r.model || "Model catalog")}</code></td><td><strong class="${r.status === "listed" ? "status-ok" : ""}">${esc(labels[r.status] || r.status)}</strong><div class="muted">${esc(r.message)}</div></td>
          <td>${esc(fmtDate(r.checked_at))}</td></tr>`).join("")}</tbody></table></div>` : '<p class="muted">No model menus refreshed yet.</p>'}
    </div></details>`;
}

function bindModelChecks(container, initial) {
  if (!initial) return;
  let data = initial, timer;
  const render = (updated) => {
    const open = container.querySelector('details')?.open;
    container.innerHTML = modelChecksHtml(updated);
    if (open != null) container.querySelector('details').open = open;
    data = updated;
    bind();
  };
  const fail = (error) => {
    if (error instanceof StaleNavigation || !container.isConnected) return;
    const box = container.querySelector('[data-model-check-error]');
    box.textContent = error.message;
    box.hidden = false;
  };
  const schedulePoll = () => {
    clearTimeout(timer);
    if (data.scan.status !== 'running' && !data.config.enabled) return;
    timer = setTimeout(async () => {
      if (!container.isConnected) return;
      try {
        const updated = await api('/api/model-checks');
        if (!container.isConnected) return;
        // Keep unsaved schedule edits and focused controls intact while polling.
        if (JSON.stringify(updated.scan) !== JSON.stringify(data.scan)) {
          const form = container.querySelector('form');
          const draft = values(form);
          render({...updated, config: draft});
          data = updated;
        } else data = updated;
        schedulePoll();
      } catch (error) { fail(error); if (container.isConnected) schedulePoll(); }
    }, data.scan.status === 'running' ? 2000 : 30000);
  };
  const values = (form) => ({enabled: form.elements.enabled.checked,
    interval_hours: Number(form.elements.interval_hours.value),
    agents: [...form.querySelectorAll('[name="agents"]:checked')].map((el) => el.value)});
  const bind = () => {
    const form = container.querySelector('form');
    const save = async () => {
      const config = values(form);
      if (!config.agents.length) throw new Error('Choose at least one agent.');
      return api('/api/model-checks', {method: 'PUT', body: config});
    };
    form.onsubmit = async (event) => {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try { render(await save()); schedulePoll(); } catch (error) { fail(error); }
      finally { if (button.isConnected) button.disabled = false; }
    };
    container.querySelector('[data-check-models]').onclick = async (event) => {
      event.target.disabled = true;
      try {
        await save();
        render(await api('/api/model-checks/start', {method: 'POST'}));
        container.querySelector('details').open = true;
        schedulePoll();
      } catch (error) { fail(error); if (event.target.isConnected) event.target.disabled = false; }
    };
    container.querySelector('[data-cancel-model-check]')?.addEventListener('click', async (event) => {
      event.target.disabled = true;
      try { await api('/api/model-checks/cancel', {method: 'POST'}); } catch (error) { fail(error); }
    });
  };
  bind();
  schedulePoll();
}

async function viewReruns(query = null) {
  if (query?.get("judge_history") === "1") rerunState.judgeHistoryOpen = true;
  if (query) Object.assign(historyFilterState, {
    q: query.get("q") || "", range: RANGES[query.get("range")] ? query.get("range") : "all",
    sort: PROMPT_SORTS[query.get("sort")] ? query.get("sort") : "recent", categoryTags: categoryTagsFromQuery(query),
  });
  clearTimeout(rerunState.limitsTimer);
  const [data, checks, , historyTags, judgeHistory] = await Promise.all([
    api("/api/reruns"),
    api("/api/model-checks").catch((error) => { if (error instanceof StaleNavigation) throw error; return null; }),
    loadModelCatalog(),
    api("/api/tags"),
    api("/api/ratings/history"),
  ]);
  historyFilterState.tags = historyTags;
  rerunState.picked = new Set([...rerunState.picked].filter((id) => data.queue.some((q) => q.id === id)));
  const picked = data.queue.filter((q) => rerunState.picked.has(q.id));
  const estimate = rerunEstimateTotal(picked);
  const fullEstimate = rerunEstimateTotal(data.queue);
  const active = data.launched.filter((x) => !x.replay && ["running", "interactive"].includes(x.status));
  const history = data.launched.filter((x) => !active.includes(x));
  app.innerHTML = `
    <div class="prompts-page reruns-page">
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Reruns</span>Rerun your prompts. <em>Did your change pay off?</em></h1>
          <p>Change what you want to test (a model, an effort level, a skill version, the agent's release) and run the same prompt again.
            Once imported, each rerun is linked to its original and compared with it: cost, time and tokens side by side, the files edited,
            the final answers and a judge's rating of how they differ.</p>
          <p class="muted">To rerun many prompts at once, queue them from a prompt's page (<strong>Add to queue</strong>), the Prompts page or a tag's page
            (<strong>Select prompts</strong>, then <strong>Queue reruns</strong>), then launch one, a selection or the whole queue with each rerun's saved settings.</p>
        </div>
      </header>

      <div class="run-histories" aria-label="Run histories"><button type="button" class="btn" data-prompt-history>↻ Prompt run history</button><button type="button" class="btn" data-judge-history>✦ Judge run history</button></div>
      <section id="model-check-tools" aria-label="Rerun tools">${modelAvailabilitySummaryHtml(checks)}</section>

      <h2 class="page-sec">Queue <span class="muted">${data.queue.length}</span>${data.queue.some((q) => q.needs_input) ? ` <span class="status-warn">· ${data.queue.filter((q) => q.needs_input).length} need input</span>` : ""}</h2>
      ${data.queue.length ? `
        <div class="rr-estimate-grid">${fullEstimate.count ? rerunEstimateSummary("Prompt runs in queue", fullEstimate) : ""}${judgeQueueSummary("Judgments in queue", data.queue)}${estimate.count && picked.length ? rerunEstimateSummary("Selected prompt runs", estimate) : ""}${picked.length ? judgeQueueSummary("Selected judgments", picked) : ""}</div>
        <section class="card queue-impact" aria-label="Estimated queue limit impact"><div class="queue-impact-heading"><span class="queue-impact-icon" aria-hidden="true">↗</span><div><span class="queue-impact-kicker">Before you run</span><h3>Estimated queue limit impact</h3><p>How this queue could affect your reported usage limits.</p></div></div><div id="queue-quota-forecast">${queueQuotaForecast(data.queue)}</div>${picked.length ? `<h3 class="queue-impact-selected">Selected runs</h3><div id="selected-quota-forecast">${queueQuotaForecast(picked)}</div>` : ""}</section>
        <p class="muted rr-estimate-note">Rerun estimates reuse original token volume at the chosen model's configured prices, preserving recorded cache usage and subagent models. Duration uses original run times; concurrent duration assumes all runs start together. Queued judgments use a rough token estimate; automatic judgments after reruns are extra. API estimates are not subscription charges. Limit forecasts learn from local observations; actual usage and external activity can differ.</p>
        <div class="select-bar rerun-bar">
          <strong>${picked.length} selected</strong>
          <button type="button" class="btn" data-rr-all ${picked.length === data.queue.length ? "disabled" : ""}>Select all ${data.queue.length}</button>
          ${picked.length ? '<button type="button" class="link-btn" data-rr-none>Clear</button>' : ""}
          <span class="toolbar-spacer"></span>
          <button type="button" class="btn primary" data-rr-launch ${picked.length ? "" : "disabled"}>↻ Run ${picked.length || ""} selected</button>
          <button type="button" class="btn" data-rr-launch-all>↻ Run all ${data.queue.length}</button>
        </div>
        <p id="rr-status" class="replay-status" role="status" hidden></p>
        <div class="rr-list rq-list">${data.queue.map(rerunQueueCard).join("")}</div>`
      : `<div class="card muted rr-empty">Nothing queued. Open a prompt and choose <strong>Add to queue</strong> in <em>Run it again</em>, or select prompts on the
          <a href="#/prompts">Prompts</a> page or a tag's page and choose <strong>Queue reruns</strong>.</div>`}
      <p id="rr-status-empty" class="replay-status" hidden></p>

      ${active.length ? `<section class="rh-active" aria-label="Active reruns"><h2 class="page-sec">In progress <span class="muted">${active.length}</span></h2>
        <div class="rh-attempts">${active.map((x, i) => rerunHistoryAttempt(x, active.length - i)).join("")}</div></section>` : ""}
      ${rerunHistoryHtml(history)}
      ${judgeHistoryHtml(judgeHistory)}
    </div>`;

  const page = app.querySelector(".reruns-page");
  page.querySelector("#rerun-history").addEventListener("toggle", (event) => {
    rerunState.historyOpen = event.currentTarget.open;
  });
  page.querySelector("#judge-history").addEventListener("toggle", (event) => { rerunState.judgeHistoryOpen = event.currentTarget.open; });
  page.querySelectorAll("[data-judge-history-group]").forEach((group) => group.addEventListener("toggle", () => {
    rerunState.judgeHistoryGroups.set(group.dataset.judgeHistoryGroup, group.open);
  }));
  bindHistoryFilters(page, history);
  const refreshLimits = async () => {
    if (!page.isConnected) return;
    clearTimeout(rerunState.limitsTimer);
    try {
      const [, updated, modelInfo] = await Promise.all([refreshUsagePanel(), api("/api/reruns"), api("/api/model-checks").catch((error) => { if (error instanceof StaleNavigation) throw error; return null; })]);
      if (!page.isConnected) return;
      page.querySelector("#model-check-tools").innerHTML = modelAvailabilitySummaryHtml(modelInfo);
      const byId = new Map(updated.queue.map((q) => [q.id, q]));
      data.queue.forEach((q) => { if (byId.has(q.id)) Object.assign(q, byId.get(q.id)); });
      const fullForecast = page.querySelector("#queue-quota-forecast");
      const selectedForecast = page.querySelector("#selected-quota-forecast");
      if (fullForecast) fullForecast.innerHTML = queueQuotaForecast(data.queue);
      if (selectedForecast) selectedForecast.innerHTML = queueQuotaForecast(data.queue.filter((q) => rerunState.picked.has(q.id)));
      page.querySelectorAll("[data-quota-run]").forEach((el) => {
        const q = byId.get(Number(el.dataset.quotaRun));
        if (q) el.innerHTML = quotaForecast(q.forecast);
      });
    } catch (error) {
      if (error instanceof StaleNavigation || !page.isConnected) return;
      // Keep the last successful forecasts until the next refresh.
    } finally {
      if (page.isConnected) {
        rerunState.limitsTimer = setTimeout(refreshLimits, 60000);
      }
    }
  };
  rerunState.limitsTimer = setTimeout(refreshLimits, 60000);
  // while reruns run, check again every few seconds (only while this page is shown)
  clearTimeout(rerunState.timer);
  if (data.launched.some((x) => x.status === "running" || x.rating?.status === "rating")) {
    const here = location.hash;
    const poll = async () => {
      if (location.hash !== here) return;
      if (document.getElementById("queue-config-dialog")?.open) {
        rerunState.timer = setTimeout(poll, 5000);
        return;
      }
      await api("/api/ingest", { method: "POST" }).catch(() => {});
      if (location.hash === here) viewReruns();
    };
    rerunState.timer = setTimeout(poll, 5000);
  }
  page.onclick = async (e) => {
    if (e.target.closest("[data-prompt-history], [data-judge-history]")) {
      const history = page.querySelector(e.target.closest("[data-judge-history]") ? "#judge-history" : "#rerun-history");
      history.open = true; history.scrollIntoView({behavior:"smooth"}); return;
    }
    const pick = e.target.closest("[data-rr-pick]"), row = e.target.closest("article.rr-row:not(.launched)");
    if (e.target.closest("[data-rr-remove]")) {
      await api(`/api/reruns/queue/${e.target.closest("[data-rr-remove]").dataset.rrRemove}`, { method: "DELETE" });
      return viewReruns();
    }
    const configure = e.target.closest("[data-rr-config]");
    if (configure) return editQueuedRun(data.queue.find((q) => q.id === Number(configure.dataset.rrConfig)));
    if (e.target.closest("[data-rr-all]")) { data.queue.forEach((q) => rerunState.picked.add(q.id)); return viewReruns(); }
    if (e.target.closest("[data-rr-none]")) { rerunState.picked.clear(); return viewReruns(); }
    if (e.target.closest("[data-rr-launch]")) return launchPicked(picked, estimate);
    if (e.target.closest("[data-rr-launch-all]")) return launchPicked(data.queue, fullEstimate);
    const single = e.target.closest("[data-rr-launch-one]");
    if (single) {
      const items = data.queue.filter((q) => q.id === Number(single.dataset.rrLaunchOne));
      return launchPicked(items, rerunEstimateTotal(items));
    }
    // a click on a queued row (not its links) picks it
    if (row && (pick || !e.target.closest("a, button, input, select, textarea, label, details"))) {
      const id = Number(row.dataset.rr);
      rerunState.picked.has(id) ? rerunState.picked.delete(id) : rerunState.picked.add(id);
      return viewReruns();
    }
  };
}

async function launchPicked(picked, estimate) {
  const n = picked.length;
  if (!n) return;
  const ids = picked.map((q) => q.id);
  const preflight = await api("/api/reruns/preflight", {method: "POST", body: {ids}});
  if (preflight.needs_input.length) {
    const latest = await api("/api/reruns");
    const incomplete = latest.queue.find((q) => q.id === preflight.needs_input[0].id);
    if (incomplete) editQueuedRun(incomplete, ids);
    return;
  }
  const budgetWarnings = picked.filter((q) => q.kind !== "judge").map(rerunBudgetWarning).filter(Boolean);
  const terminals = picked.filter((q) => q.mode === "terminal").length;
  const queuedJudges = picked.filter((q) => q.kind === "judge"), knownJudges = queuedJudges.filter((q) => q.estimate);
  if (!confirm(`Launch ${plural(n, "item")} together?\n\n${n - terminals} in the background · ${terminals} in terminal windows.\n` +
      `${estimate.count ? `Reruns: ${rerunEstimateCost(estimate)}, ≈ ${fmtTokens(estimate.tokens)} tokens${estimate.missing ? ` + ${estimate.missing} unknown` : ""}.\nConcurrent duration baseline: ${fmtMs(estimate.parallel_ms)}${estimate.missing_time ? ` (${estimate.missing_time} unknown)` : ""}.\n` : ""}${queuedJudges.length ? `Judgments: ${knownJudges.length ? `≈ ${fmtTokens(knownJudges.reduce((sum, q) => sum + q.estimate.total_tokens, 0))} tokens` : "estimate unavailable"}${knownJudges.length < queuedJudges.length ? " · some estimates unavailable" : ""}; cost and time unavailable.\n` : ""}Actual consumption may differ; automatic judgments after reruns are extra.${budgetWarnings.length ? "\n\nBudget warnings:\n" + budgetWarnings.join("\n") : ""}`)) return;
  const response = await api("/api/reruns/launch", { method: "POST", body: {ids} });
  if (response.needs_input?.length) {
    const latest = await api("/api/reruns");
    const incomplete = latest.queue.find((q) => q.id === response.needs_input[0].id);
    if (incomplete) editQueuedRun(incomplete, ids);
    return;
  }
  const {results} = response;
  rerunState.picked.clear();
  await viewReruns();
  const judges = results.filter((r) => r.kind === "judge" && !r.error);
  const opened = results.filter((r) => r.terminal), running = results.filter((r) => r.kind !== "judge" && r.mode === "background" && !r.error),
    manual = results.filter((r) => r.mode !== "background" && !r.terminal && !r.error), failed = results.filter((r) => r.error);
  const status = document.getElementById("rr-status") || document.getElementById("rr-status-empty");
  status.innerHTML = [
    opened.length ? `${plural(opened.length, "terminal window")} opened.` : "",
    running.length ? `${plural(running.length, "prompt run")} started in the background.` : "",
    judges.length ? `${plural(judges.length, "judge run")} started in the background.` : "",
    manual.length ? `No terminal window could be opened for ${manual.length}: run ${manual.length === 1 ? "this" : "these"} yourself (they are linked once imported):
      <pre><code>${esc(manual.map((r) => r.command).join("\n"))}</code></pre>` : "",
    failed.length ? `${plural(failed.length, "rerun")} could not start and stayed queued: ${failed.map((r) => esc(r.error)).join("; ")}.` : "",
  ].filter(Boolean).join(" ");
  status.hidden = false;
}

function judgeComparisonUrl(base, other) {
  return `#/judge-comparison?${new URLSearchParams({a: base, b: other})}`;
}

async function viewJudgeComparisons() {
  const groups = await api("/api/ratings/history");
  app.innerHTML = `<div class="prompts-page judge-comparisons"><p><a href="#/reruns">← Reruns</a></p>
    <header class="home-hero page-hero"><div class="hero-copy"><h1><span class="hero-name">Judge comparisons</span>One comparison. <em>Different perspectives.</em></h1>
    <p>Choose a pair to see all its judge runs together. Judge the pair again to add another perspective.</p></div></header>
    ${groups.length ? `<div class="judge-pair-list">${groups.map((group) => `<a class="card judge-pair" href="${esc(judgeComparisonUrl(group.base, group.other))}">
      <span class="judge-label">Compared prompts</span><div class="judge-pair-prompts"><p><strong>Reference</strong> ${esc(group.base_text)}</p><p><strong>Compared</strong> ${esc(group.other_text)}</p></div>
      <div class="judge-pair-footer"><span>${plural(group.ratings.length, "judge run")}${group.human_feedback ? ` · Your score ${group.human_feedback.human_score}/100` : ""}</span><strong>Compare judge runs →</strong></div></a>`).join("")}</div>` : '<div class="card muted">No completed judge runs yet. Open a comparison and choose Judge them to get started.</div>'}</div>`;
}

function judgeMetricChart(ratings, title, key, format) {
  const values = ratings.map((rating) => Number.isFinite(rating[key]) && rating[key] >= 0 ? rating[key] : null);
  const maximum = Math.max(0, ...values.filter((value) => value !== null));
  return `<section class="card judge-metric-chart"><h3>${esc(title)}</h3><div class="judge-metric-rows">${ratings.map((rating, i) => {
    const value = values[i], width = value == null || maximum === 0 ? 0 : (value / maximum) * 100;
    return `<div class="judge-metric-row"><span class="judge-metric-name" title="${esc(rating.model || "Default model")}">${esc(rating.model || "Default model")} <small>· run ${i + 1}</small></span><span class="judge-metric-track" aria-hidden="true"><span style="width:${width}%"></span></span><strong>${value == null ? "Unavailable" : esc(format(value))}</strong></div>`;
  }).join("")}</div></section>`;
}

function judgeOutputComparison(ratings) {
  if (ratings.length < 2) return `<section class="card judge-output-comparison"><h2>Compare judge outputs</h2><p class="muted">Run another judge to compare two outputs side by side.</p></section>`;
  const options = ratings.map((rating, i) => `<option value="${i}">${esc(rating.model || "Default model")} · run ${i + 1} · ${esc(fmtDate(rating.rated_at))}</option>`).join("");
  return `<section class="card judge-output-comparison" aria-labelledby="judge-output-title"><h2 id="judge-output-title">Compare judge outputs</h2><p class="muted">Choose two judge runs to compare their conclusions and observations.</p>
    <div class="judge-output-grid">${[0, 1].map((i) => `<div class="judge-output-column"><label for="judge-output-select-${i}">Judge ${i + 1}</label><select id="judge-output-select-${i}">${options}</select><div id="judge-output-result-${i}"></div></div>`).join("")}</div></section>`;
}

function bindJudgeOutputComparison(ratings) {
  if (ratings.length < 2) return;
  [0, 1].forEach((i) => {
    const select = document.getElementById(`judge-output-select-${i}`);
    const result = document.getElementById(`judge-output-result-${i}`);
    select.value = String(i);
    const render = () => {
      const rating = ratings[Number(select.value)];
      result.innerHTML = `<p><strong>${esc(rating.score)} / 100</strong> ${judgeBadge(rating)}</p><span class="judge-label">Conclusion</span><p>${esc(rating.summary || "No summary recorded.")}</p><span class="judge-label">Observations</span>${rating.differences.length ? `<ul>${rating.differences.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : '<p class="muted">No observations recorded.</p>'}`;
    };
    select.addEventListener("change", render);
    render();
  });
}

async function viewJudgeComparison(query) {
  const a = query.get("a"), b = query.get("b");
  if (!a || !b) { app.innerHTML = '<div class="card status-warn">Choose a pair from judge comparisons.</div>'; return; }
  const [groups, comparison, settings, catalog] = await Promise.all([api("/api/ratings/history"), api(`/api/compare?${new URLSearchParams({a, b})}`), api("/api/settings"), loadModelCatalog()]);
  const group = groups.find((item) => item.base === a && item.other === b);
  if (!group) { app.innerHTML = `<div class="card status-warn">No completed judge runs for this pair. <a href="#/reruns?judge_history=1">← Judge run history</a></div>`; return; }
  const scores = group.ratings.map((rating) => rating.score).filter((score) => Number.isFinite(score));
  const average = scores.length ? Math.round(scores.reduce((sum, score) => sum + score, 0) / scores.length) : null;
  const human = group.human_feedback;
  const verdictCount = (verdict) => group.ratings.filter((rating) => rating.verdict === verdict).length;
  const compareUrl = `#/compare?${new URLSearchParams({a, b})}`;
  app.innerHTML = `<div class="prompts-page judge-comparison-page"><p><a href="#/reruns?judge_history=1">← Judge run history</a></p>
    <header class="home-hero page-hero"><div class="hero-copy"><h1><span class="hero-name">Judge comparison</span>Two prompts. <em>Every judge run.</em></h1>
      <p>See how judges rated the same pair, and compare their scores with your own assessment.</p></div></header>
    ${judgeOutputComparison(group.ratings)}
    <section class="card judge-pair-detail"><span class="judge-label">The prompts being judged</span><div class="judge-pair-prompts"><p><strong>Reference</strong> ${esc(group.base_text)}</p><p><strong>Compared</strong> ${esc(group.other_text)}</p></div>
      <a class="btn" href="${esc(compareUrl)}">Open prompt comparison →</a></section>
    <section class="card judge-comparison-action" aria-label="Run another judgment">${judgeActionHtml(comparison.rating, catalog.map((m) => m.id), settings.judge_model)}</section>
    <section aria-labelledby="judge-summary-title"><h2 class="page-sec" id="judge-summary-title">At a glance</h2><div class="judge-summary-grid">
      <div class="card"><span>Judge runs</span><strong>${group.ratings.length}</strong><small>${plural(new Set(group.ratings.map((r) => r.model || "Default model")).size, "model configuration")}</small></div>
      <div class="card"><span>Average score</span><strong>${average ?? "—"}<small> / 100</small></strong><small>Arithmetic mean of judge scores</small></div>
      <div class="card"><span>Score range</span><strong>${scores.length ? `${Math.min(...scores)}–${Math.max(...scores)}` : "—"}</strong><small>Lowest to highest</small></div>
      <div class="card"><span>Your score</span><strong>${human ? human.human_score : "—"}<small> / 100</small></strong><small>${human ? "Human assessment" : "Add it in the prompt comparison"}</small></div>
    </div><p class="muted judge-verdict-counts">Verdicts: ${verdictCount("equivalent")} equivalent · ${verdictCount("minor")} minor · ${verdictCount("major")} major</p>
    ${human?.note ? `<p class="judge-human-score">Your note: ${esc(human.note)}</p>` : ""}</section>
    <section aria-labelledby="judge-metrics-title"><h2 class="page-sec" id="judge-metrics-title">Judge usage comparison</h2>
      <p class="muted judge-metric-note">Each row is a judge run, in the same order as the table below. Time is elapsed wall time; tokens and costs are reported by the agent. Older runs or agents may not provide every value.</p>
      <div class="judge-metric-grid">${judgeMetricChart(group.ratings, "Time", "duration_ms", fmtMs)}${judgeMetricChart(group.ratings, "Tokens", "total_tokens", fmtTokens)}${judgeMetricChart(group.ratings, "Cost", "cost_usd", fmtCost)}</div></section>
    <section aria-labelledby="judge-runs-title"><h2 class="page-sec" id="judge-runs-title">All judge runs <span class="muted">${group.ratings.length}</span></h2>
    <div class="table-wrap card judge-runs-table"><table><thead><tr><th>Judge</th><th>Score</th><th>Verdict</th><th>Conclusion</th><th>Time</th><th>Tokens</th><th>Cost</th><th>Run</th></tr></thead><tbody>${group.ratings.map((rating) => `<tr class="judge-comparison-run"><td><strong>${esc(rating.model || "Default model")}</strong></td><td><strong>${esc(rating.score)} / 100</strong>${human ? `<br><span class="muted">${rating.score - human.human_score > 0 ? "+" : ""}${rating.score - human.human_score} vs yours</span>` : ""}</td>
      <td>${judgeBadge(rating)}</td><td>${esc(rating.summary || "No summary recorded.")}${rating.differences.length ? `<details><summary>Observations</summary><ul>${rating.differences.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}</td>
      <td>${rating.duration_ms == null ? "—" : fmtMs(rating.duration_ms)}</td><td>${rating.total_tokens == null ? "—" : fmtTokens(rating.total_tokens)}</td><td>${rating.cost_usd == null ? "—" : fmtCost(rating.cost_usd)}</td>
      <td>${esc(fmtDate(rating.rated_at))}</td></tr>`).join("")}</tbody></table></div></section></div>`;
  bindJudgeOutputComparison(group.ratings);
  bindCompare(a, b, () => viewJudgeComparison(query));
  pollJudge(comparison.rating, () => viewJudgeComparison(query));
}

// ---- Compare page: any two runs of a prompt, and the LLM judge's call ------------------------

async function viewCompare(query) {
  const a = query.get("a"), b = query.get("b");
  const comparisonContext = query.get("rerun_comparison");
  if (!a || !b) { app.innerHTML = '<div class="card status-warn">Pick two runs to compare: open a rerun, then Compare.</div>'; return; }
  const [data, tags, settings] = await Promise.all([api(`/api/compare?${new URLSearchParams({ a, b })}`), api("/api/tags"), api("/api/settings")]);
  const rerunComparison = comparisonContext || data.a.rerun_of?.prompt_id || data.b.rerun_of?.prompt_id;
  const models = (await loadModelCatalog()).map((m) => m.id);
  const kindLabel = { original: "Original", rerun: "Rerun", reference: "Reference run" };
  const label = (pid) => { const candidate = data.candidates.find((x) => x.id === pid); return kindLabel[candidate?.kind] || "Run"; };
  const picker = (id, current) => `<select id="${id}">${data.candidates.map((x) => `<option value="${esc(x.id)}" ${x.id === current ? "selected" : ""}>
    ${x.is_reference ? "★ Reference · " : ""}${esc(kindLabel[x.kind])} · ${esc(fmtDate(x.started_at))} · ${fmtCost(x.total_cost_usd)} · ${esc((x.text || "").slice(0, 80))}</option>`).join("")}</select>`;
  const labels = [`Reference · ${label(a)}`, `Compared run · ${label(b)}`];
  app.innerHTML = `
    <div class="prompts-page compare-page">
      ${rerunComparison ? `<p><a href="#/rerun-comparison/${encodeURIComponent(rerunComparison)}">← All reruns comparison</a></p>` : ""}
      <header class="home-hero page-hero">
        <div class="hero-copy">
          <h1><span class="hero-name">Compare</span>Two runs of one prompt. <em>Read the differences, or trust it.</em></h1>
          <p>${esc(data.a.text.slice(0, 220))}${data.a.text.length > 220 ? "…" : ""}</p>
        </div>
      </header>
      <div class="compare-pick card">
        <label class="compare-role compare-role-reference"><span class="compare-role-kicker">01 · Baseline</span><strong>Reference run</strong><span>The starting point for changes and the judge.</span>${picker("pick-a", a)}</label>
        <button type="button" class="btn compare-swap" id="swap" title="Swap the two runs" aria-label="Swap reference and compared run">⇄</button>
        <label class="compare-role compare-role-other"><span class="compare-role-kicker">02 · Comparison</span><strong>Compared run</strong><span>Measured against the reference run.</span>${picker("pick-b", b)}</label>
        <span class="muted compare-pick-note">Choose either run. ★ marks a saved reference; changing these selections does not change it.</span>
      </div>
      <div class="card compare-original">
        <p><a class="btn" href="${esc(judgeComparisonUrl(a, b))}">Compare all judge runs →</a></p>
        ${judgeCardHtml(data.rating, models, settings.judge_model, a, b)}
        ${compareRunsHtml(data.a, data.b, labels)}
      </div>
      <p class="muted group-note"><a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(a)}">Open reference run</a> · <a target="_blank" rel="noopener noreferrer" href="#/prompt/${encodeURIComponent(b)}">Open compared run</a></p>
    </div>`;
  const go = (x, y) => { const params = new URLSearchParams({ a: x, b: y }); if (rerunComparison) params.set("rerun_comparison", rerunComparison); location.hash = `#/compare?${params}`; };
  document.getElementById("pick-a").onchange = (e) => go(e.target.value, b);
  document.getElementById("pick-b").onchange = (e) => go(a, e.target.value);
  document.getElementById("swap").onclick = () => go(b, a);
  bindCompare(a, b, () => viewCompare(query));
  pollJudge(data.rating, () => viewCompare(query));
}

// Keep the floating panel's visibility and position across reloads and navigation.
const usagePanel = document.getElementById("usage-panel");
const usageReopen = document.getElementById("usage-reopen");
const usageDrag = document.getElementById("usage-drag");
let usageAvailable = false;
let usagePrefs = {};
try { usagePrefs = JSON.parse(localStorage.getItem("usage-panel-prefs") || "{}"); } catch { /* Storage is optional. */ }
if (!usagePrefs || typeof usagePrefs !== "object") usagePrefs = {};
function saveUsagePrefs() {
  try { localStorage.setItem("usage-panel-prefs", JSON.stringify(usagePrefs)); } catch { /* Storage is optional. */ }
}
function showUsagePanel() {
  usagePanel.hidden = !usageAvailable || !!usagePrefs.closed;
  usageReopen.hidden = !usageAvailable || !usagePrefs.closed;
  if (!usagePanel.hidden && Number.isFinite(usagePrefs.x) && Number.isFinite(usagePrefs.y)) positionUsagePanel(usagePrefs.x, usagePrefs.y);
}
function positionUsagePanel(x, y) {
  const rect = usagePanel.getBoundingClientRect();
  x = Math.max(0, Math.min(x, innerWidth - rect.width));
  y = Math.max(0, Math.min(y, innerHeight - rect.height));
  usagePanel.style.left = `${x}px`;
  usagePanel.style.top = `${y}px`;
  usagePanel.style.right = "auto";
  usagePanel.style.bottom = "auto";
  usagePrefs.x = x; usagePrefs.y = y;
}
document.getElementById("usage-close").onclick = () => { usagePrefs.closed = true; saveUsagePrefs(); showUsagePanel(); };
usageReopen.onclick = () => { usagePrefs.closed = false; saveUsagePrefs(); showUsagePanel(); };
usageDrag.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  const rect = usagePanel.getBoundingClientRect();
  const offsetX = event.clientX - rect.left, offsetY = event.clientY - rect.top;
  usageDrag.setPointerCapture(event.pointerId);
  const move = (e) => positionUsagePanel(e.clientX - offsetX, e.clientY - offsetY);
  const end = () => {
    usageDrag.removeEventListener("pointermove", move);
    usageDrag.removeEventListener("pointerup", end);
    usageDrag.removeEventListener("pointercancel", end);
    saveUsagePrefs();
  };
  usageDrag.addEventListener("pointermove", move);
  usageDrag.addEventListener("pointerup", end, { once: true });
  usageDrag.addEventListener("pointercancel", end, { once: true });
});
usageDrag.addEventListener("keydown", (event) => {
  const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
  if (!direction) return;
  event.preventDefault();
  const rect = usagePanel.getBoundingClientRect(), step = event.shiftKey ? 50 : 10;
  positionUsagePanel(rect.left + direction[0] * step, rect.top + direction[1] * step);
  saveUsagePrefs();
});
addEventListener("resize", () => {
  if (!usagePanel.hidden && Number.isFinite(usagePrefs.x) && Number.isFinite(usagePrefs.y)) {
    positionUsagePanel(usagePrefs.x, usagePrefs.y); saveUsagePrefs();
  }
});

// The panel lives outside the routed page so its expanded state survives navigation.
let usagePanelRequest = 0;
async function refreshUsagePanel() {
  const request = ++usagePanelRequest;
  try {
    const response = await fetch(BASE + "/api/limits");
    if (!response.ok) return;
    const limits = await response.json();
    if (request !== usagePanelRequest) return;
    usageAvailable = limits.length > 0;
    usagePanel.querySelector(".hero-limits-content").innerHTML = homeLimits(limits);
    showUsagePanel();
  } catch { /* Keep the last known usage when temporarily offline. */ }
}

// ---- router ----------------------------------------------------------------

async function route() {
  navigation++;
  refreshQueueCount();
  refreshUsagePanel();
  destroyCharts();
  const [hash, queryString] = (location.hash.slice(1) || "/").split("?");
  const query = new URLSearchParams(queryString || "");
  let [, view, arg] = hash.split("/");
  // links from before the home page put the prompt filters on #/
  if (!view && ["q", "tag", "project", "sort", "order"].some((k) => query.has(k))) view = "prompts";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active",
    a.dataset.nav === ({ tag: "tags", session: "sessions", prompt: "prompts", compare: "reruns", rerun: "reruns", "rerun-comparison": "reruns", "judge-comparisons": "reruns", "judge-comparison": "reruns" }[view] || view || "home")));
  try {
    if (view === "prompts") await viewPrompts(query);
    else if (view === "prompt") await viewPrompt(decodeURIComponent(arg), { scope: query.get("compare"), tag: query.get("tag"), exclude_reruns: query.get("exclude_reruns") });
    else if (view === "tags") await viewTags();
    else if (view === "sessions") await viewSessions(query);
    else if (view === "session") await viewSession(decodeURIComponent(arg));
    else if (view === "tag") await viewTag(decodeURIComponent(hash.slice("/tag/".length)), query);
    else if (view === "setup" && !EXAMPLES) await viewSetup();
    else if (view === "reruns") await viewReruns(query);
    else if (view === "judge-comparisons") await viewJudgeComparisons();
    else if (view === "judge-comparison") await viewJudgeComparison(query);
    else if (view === "rerun") await viewRerun(decodeURIComponent(arg));
    else if (view === "rerun-comparison") await viewRerunComparison(decodeURIComponent(arg));
    else if (view === "compare") await viewCompare(query);
    else await viewHome(query);
  } catch (e) {
    if (e instanceof StaleNavigation) return; // the newer page renders itself
    app.innerHTML = `<div class="card status-warn">${esc(e.message)}</div>`;
  }
  window.scrollTo(0, 0);
  // a jump link opened in a new tab or shared: the page, then its section
  if (query.get("jump")) jumpTo(query.get("jump"));
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

// the example app has nothing to import nor set up: a banner says what it is and leads back
if (EXAMPLES) {
  document.title = "Rerunly examples";
  document.querySelector('[data-nav="setup"]').remove();
  document.getElementById("refresh").remove();
  document.querySelector(".topbar").insertAdjacentHTML("afterend", `<div class="examples-banner" role="note">
    <strong>Example app</strong><span>Illustrative records only: no agent was run, and your own history is not shown here.</span>
    <a href="/">Back to your Rerunly →</a></div>`);
}

// Selection actions sit below navigation even when its controls wrap on small screens.
const topbar = document.querySelector(".topbar");
new ResizeObserver(() => {
  document.documentElement.style.setProperty("--topbar-height", `${topbar.getBoundingClientRect().height}px`);
}).observe(topbar);

// not through api(): it belongs to the top bar, not to a page, and must survive the first navigation
fetch(BASE + "/api/settings").then((r) => r.json()).then((s) => showCostSource(s.cost_source));

if (!EXAMPLES) document.getElementById("refresh").onclick = async (e) => {
  e.target.disabled = true;
  try { await api("/api/ingest", { method: "POST" }); await route(); } finally { e.target.disabled = false; }
};
setInterval(() => { if (!document.hidden) refreshQueueCount(); }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshQueueCount(); });
window.addEventListener("hashchange", route);
route();
