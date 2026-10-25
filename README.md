<p align="center">
  <img src="docs/hero.svg" alt="You change a prompt, you rerun the task, Rerunly shows whether it paid off: 66% cheaper" width="900">
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
    <img src="docs/logo-light.svg" alt="Rerunly" width="480">
  </picture>
</p>

<p align="center">
  <b>Cost, tokens and time for every prompt you give Claude Code,<br>so each rerun tells you if your last change made it cheaper, faster, or neither.</b>
</p>

<p align="center">
  <img alt="Claude Code" src="https://img.shields.io/badge/works%20with-Claude%20Code-6250d6">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-2a78d6">
  <img alt="Local first" src="https://img.shields.io/badge/data-stays%20on%20your%20machine-1baf7a">
  <img alt="Setup" src="https://img.shields.io/badge/setup-60%20seconds-eb6834">
  <img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-9a9993">
</p>

---

You tweak a skill, rewrite a `CLAUDE.md`, add a subagent… and then? Did the task get
**cheaper**? **Faster**? Or did that clever persona just double your token bill?

**Rerunly** reads the transcripts Claude Code already writes on your machine and turns them
into a living cost history: **per prompt, per subagent, per persona, per task**. Every rerun of
a task lands next to the previous ones, so after each change you see, at a glance, whether it
paid off.

## ✨ Why you'll love it

<table>
<tr>
<td width="50%" valign="top">

### 💸 Know the real cost
Dollars, tokens and time for every prompt, **including every subagent** it spawned. Use
Claude Code's **own official cost** (via OpenTelemetry) or your price table, with a switch to
compare them.

</td>
<td width="50%" valign="top">

### 📈 See the trend, not just the bill
Every task gets a sparkline and a verdict: **▼ 46% cheaper** or **▲ 200% pricier** than its
previous runs. Similar prompts are lined up automatically, so you compare like with like.

</td>
</tr>
<tr>
<td valign="top">

### 🏷️ Tags that write themselves
Project, model, skill, slash command, subagent type and persona tags are **created
automatically**. Add your own (`task:review-branch`) once, give it keywords, and every
future prompt that matches is tagged for you.

</td>
<td valign="top">

### 🔍 Find the expensive habits
Order your tags by **most used**, **priciest per run** or **most time in total**. Spot the
persona that burns tokens, the skill that drags, the project that quietly costs the most.

</td>
</tr>
<tr>
<td valign="top">

### ⚡ Always on, never in the way
Runs in the background with an **icon in your status bar** showing today's spend. Starts at
login on its own. Updates live after every Claude answer through a hook.

</td>
<td valign="top">

### 🔒 Yours, and only yours
No account, no cloud, no upload. A local SQLite file and a local web page. Your prompts
never leave your machine.

</td>
</tr>
</table>

## 🚀 Up and running in 60 seconds

```bash
git clone git@github.com:goldener-data/rerunly.git && cd rerunly
uv sync
uv run rerunly start   # runs in the background, with a status bar icon
```

Open <http://127.0.0.1:8765>, go to **Setup**, and press:

1. **Import now**: your whole Claude Code history appears.
2. **Install hook**: new prompts show up the moment Claude answers.
3. **Enable telemetry** *(optional)*: switch to Claude Code's own cost figures.
4. **Start at login** *(optional)*: never think about it again.

That's it. Go do some work, then come back and see what it cost.

## 🖼️ A tour

<table>
<tr>
<td width="50%" valign="top">
<img src="docs/screenshots/by-tag.png" alt="Prompts grouped by tag family">
<p><b>Prompts, by tag</b>: one card per task, with its median cost, trend and latest run, in
sections per tag family. Order them by most used or priciest per run, or switch to a plain list
by day. Filter by project, tag or period, and share any view by URL.</p>
</td>
<td width="50%" valign="top">
<img src="docs/screenshots/prompt.png" alt="A prompt's page with its evolution charts">
<p><b>A prompt, dissected</b>: the main agent and every subagent side by side (models, cache
reads and writes, cost, tools), and charts of how this task's cost, time and tokens evolved
across all its runs.</p>
</td>
</tr>
<tr>
<td valign="top">
<img src="docs/screenshots/tag-dark.png" alt="A tag's page in dark mode">
<p><b>A tag's story</b>: runs, total and median cost with its trend, the keyword rules that
assign it, and its full history. Dark mode included, of course.</p>
</td>
<td valign="top">
<img src="docs/screenshots/tags.png" alt="Tags gallery">
<p><b>The Tags gallery</b>: every tag as a card, clustered by type and family, or laid out on
a timeline by creation date. Click one to open its charts in place.</p>
</td>
</tr>
</table>

## 🧭 What's inside

<details>
<summary><b>Prompts page</b>: list or by tag, with filters you can share</summary>

- A summary band that follows your filters: prompts, total cost, cost per prompt, agent time.
- A toolbar that stays visible while you scroll: search, **Project** and **Tag** pill menus
  (searchable, grouped by category), period (today, 7 days, 30 days, all time) and sort
  (newest, most expensive, longest, most tokens). Click a tag on any prompt to filter by it.
  Active filters show as removable chips and live in the URL, so any view can be bookmarked.
- **List** layout: prompts grouped by day with each day's total; each row shows its cost with a
  bar relative to the most expensive prompt shown, plus tokens, time and subagents.
- **By tag** layout: one foldable section per tag family (the prefix before `:`, such as
  `task:`, `project:`, `model:`, `skill:`, `cmd:`, `agent:`), with jump links at the top. Each
  tag is a card with its median cost per run, trend, sparkline, totals and latest run. A section
  shows one row of cards (as many as fit), "Show N more tags" reveals the rest, and "Show N
  more runs" lists a tag's earlier runs. An **Order** menu sorts the cards in two sections:
  *Tags* (most used, recently used, newest tags, A to Z) and *Timing, cost & usage* (priciest
  per run, most expensive in total, longest per run, most time in total, most tokens per run).
  Prompts without a human tag get their own "Not tagged by you" card.
</details>

<details>
<summary><b>Prompt page</b>: every agent, every token, and how it evolves</summary>

- Total cost, tokens, active time and wall time; cost, tokens and active time are compared
  with the comparison group's median (for example "-23% vs group median").
- A per-agent table splitting the main agent from each subagent: models, input, output, cache
  read and cache write tokens, cost, duration, tool calls. Each subagent shows its **persona**
  and the skill that spawned it; runs of one agent type with different personas are reported
  apart.
- Cost, duration and token charts over time against a comparison group of your choice:
  similar prompts (default), all prompts sharing one of its human tags, or similar prompts
  sharing that tag.
- Add a human tag, with optional keywords, right from the page.
</details>

<details>
<summary><b>Tags</b>: five color-coded categories, automatic and your own</summary>

- **Human** (blue, filled chip): yours, added on a prompt page or by keyword and regex rules.
  Use them to group prompts aiming at the same task, such as `task:review-branch`.
- **AI tags** (dashed outline), derived from what the agent did and refreshed on every import:
  **Project** (orange) `project:`, **Model** (aqua) `model:`, **Skill** (violet) `skill:`,
  and **Other AI** (grey) `cmd:` for slash commands, `agent:` for subagent types, `persona:`.
- Color is never the only cue: chips carry their prefix and human chips have their own shape.
  The palette passes a color-blindness check in light and dark mode.
- **Keywords**: give a human tag keywords and every prompt containing all (or any) of them gets
  it, past ones right away and new ones on import, with a live count of matches.
- **Tags gallery**: cards with cost per prompt, sparkline and trend badge; a summary band with
  the priciest and most used tags; search, category filters, five sorts; **Grouped** (by
  category, then family) or **Timeline** (by month of creation) layouts. Click a card to open
  its charts in place; Escape closes them.
- **A page per tag**: a header in the tag's color, its keyword rules as removable chips, a
  summary band (runs, total cost, median cost with trend, median time), charts, and its runs
  with the same toolbar and layouts as the Prompts page.
</details>

<details>
<summary><b>Sessions, Setup and the status bar icon</b></summary>

- **Sessions**: every session with its totals, and a page each with the price-table cost next
  to the total Claude Code stores, tokens, active time, API and tool calls, cost per agent type
  and persona, Claude Code's own per-caller cost when telemetry is on, and its prompts over
  time.
- **Setup**: check that transcripts are found, install the live-update hook, enable telemetry,
  extend transcript retention, start at login, and edit the price table, similarity threshold
  and auto-tag rules.
- **Status bar icon**: today's cost and prompt count, *Open dashboard* (also a left click),
  *Import now*, *Start at login* and *Quit*.
</details>

## ⚙️ Running it your way

```bash
uv run rerunly                # in the foreground, http://127.0.0.1:8765
uv run rerunly start          # in the background, with a status bar icon
uv run rerunly stop
uv run rerunly autostart on   # start at every login, without any action (off to undo)
uv run rerunly ingest         # import once and exit (--force to re-parse everything)
uv run rerunly --reload       # while hacking on it: restarts on Python changes
```

- **Start at login** writes a standard desktop entry to `~/.config/autostart`, run by GNOME,
  KDE and other desktops. You can also switch it from the icon's menu or the Setup page.
- The icon uses Qt (PySide6, installed by `uv sync`) and your desktop's StatusNotifier tray.
  On Ubuntu GNOME it shows through the AppIndicator extension Ubuntu enables by default. With
  no tray, the dashboard still runs in the background. Its log is in
  `~/.local/share/rerunly/tray.log`.
- If a dashboard already serves the port (say, one started with `--reload`), the icon reuses
  it instead of starting a second server.
- Page files are never cached and `--reload` restarts the server on code changes, so a page
  refresh always shows the latest version.
- The database lives in `~/.local/share/rerunly/dashboard.db`. Change it with `--db` or
  `RERUNLY_DB`. Coming from the tool's former name, Agent Task Dashboard? Your data moves
  over by itself the first time you run `rerunly`.

## 🧮 How the numbers are computed

Trust, but verify: every figure comes from data Claude Code writes itself.

<details>
<summary>Show the details</summary>

| What | Source |
|---|---|
| Prompt | A user line in `<session>.jsonl` with `origin.kind == "human"` (typed text or a slash command). Everything until the next one, including background-task notifications, belongs to it. Local commands that never reach the model (`/plugin`, `/clear`) are dropped. |
| Tokens | `message.usage` of each assistant message, counted once per message id. Claude Code writes one line per content block, and each line repeats the usage. |
| Subagents | `<session>/subagents/agent-*.jsonl`, linked to their prompt through `promptId`. The type and description come from `agent-*.meta.json`. |
| Cost | **Claude Code's own cost** when telemetry is on and covers every API call of the prompt. Claude Code sends a `claude_code.api_request` OpenTelemetry event per API call, with `cost_usd` and a `prompt.id` equal to the transcript's `promptId`; the dashboard receives these events at `/v1/logs` (OTLP, `http/json`). These figures include internal calls the transcripts don't detail, such as WebSearch. **Otherwise**, the price-table estimate: token counts × the price table on the Setup page. Cache writes cost 1.25× the input price (5-minute TTL) or 2× (1-hour TTL), fast mode costs 2×, and web search costs $10 per 1,000 requests. Models missing from the table are flagged. Each prompt shows which source was used. |
| Active time | Sum of Claude Code's `turn_duration` events for the prompt. |
| Wall time | From the prompt to its last event, including subagents that finish later. |
| API time | Sum of `duration_ms` over the prompt's telemetry events (telemetry only). |
| Persona | Read from the start of the subagent's prompt: `<persona>…</persona>`, `Persona: …` / `Role: …`, or `You are a…` / `Act as…`. The skill is the last one invoked (or the slash command run) before the subagent was spawned. |
| Similar prompts | Word overlap (Jaccard) ≥ the threshold (default 0.6), or the same slash command with some overlap. |

Switch between the two costs at any time with **Official / Price table** in the top bar:
**Official** shows Claude Code's own cost for every prompt telemetry fully covers and the
estimate for the rest; **Price table** always shows the estimate. Each prompt's cost shows the
other figure underneath.

As a sanity check, Claude Code stores a session-level `cost-state` total: price-table estimates
land within a few percent of it, and on a test prompt telemetry and the price table agreed
exactly. Telemetry only applies to Claude Code sessions started after it is enabled, and calls
made while the dashboard is not running are lost; a prompt missing some of its calls falls back
to the estimate.

**Tip:** to compare runs of one task reliably, give them the same human tag and choose "All
prompts tagged …" on a prompt page. Add the tag by hand, with keywords, or with a regex rule
such as `{"pattern": "review .*paper", "tag": "task:paper-review"}`. Choose "Similar prompts
tagged …" when one tag covers several kinds of prompts, such as a project-wide tag.
</details>

## 🛣️ Beyond Claude Code

Claude Code is supported today. Parsing sits behind a small `Source` interface
(`rerunly/sources/base.py`), so other agents and harnesses can plug in next.

## 🛠️ Development

```bash
uv sync
uv run rerunly --reload   # edit, refresh the page, repeat
uv run pytest
```

<p align="center"><sub>Built for people who tune their agents like they tune their code.</sub></p>
