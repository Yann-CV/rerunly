# Agent Task Dashboard

See what each prompt you give an AI coding agent costs, in tokens, dollars and time, and how
that changes when you rerun the same task after editing a skill, a prompt or your tooling.

Claude Code is the only supported agent for now. The parsing sits behind a small `Source`
interface (`agent_dashboard/sources/base.py`), so other agents can be added later.

## What you get

- **The Prompts page**:
  - a summary band follows your filters: number of prompts, total cost, cost per prompt, and
    agent time;
  - a toolbar that stays visible while scrolling offers search, pill menus for the project and
    the tag (searchable, grouped by category), a period (today, 7 days, 30 days, all time), and
    a sort (newest, most expensive, longest, most tokens). Clicking a tag on any prompt filters
    by it. Active filters appear as removable chips, and they are kept in the URL so a view can
    be bookmarked or shared;
  - **List** layout: prompts grouped by day with each day's total. Each row shows the cost with
    a bar relative to the most expensive prompt shown, plus tokens, time and subagents. The
    whole row opens the prompt;
  - **By tag** layout: choose human tags, project, skill, model or other AI tags. Each group
    shows its run count, total and median cost, trend and sparkline, and only its latest run.
    "Show N more runs" lists the others, and Expand all / Collapse all act on every group.
- **Sessions**: every session with its totals, and a page per session showing:
  - the cost from the price table next to the total Claude Code stores in the transcript;
  - tokens, active time, API calls and tool calls;
  - cost per agent type and persona;
  - Claude Code's own per-caller cost, when telemetry is on;
  - the session's prompts over time.
- **A page for each prompt**:
  - total cost, tokens, active time and wall time;
  - a per-agent table splitting the main agent from each subagent (models, input, output,
    cache read and cache write tokens, cost, duration, tool calls). It also shows each subagent's
    **persona** and the skill that spawned it. Runs of the same agent type with different
    personas are reported separately.
  - charts of cost, duration and tokens over time against a comparison group, plus the change
    against the group's median (for example "-23% vs group median"). Pick the group
    with the **Compare with** selector:
    - similar prompts (the default);
    - all prompts sharing one of this prompt's human tags;
    - similar prompts sharing that human tag.
- **Tags in five color-coded categories**, each tag with its own page of charts over time:
  - **Human** (blue, filled chip with a solid border): added by you on a prompt page, or by
    your regex auto-tag rules on the Setup page. Use them to group prompts trying to achieve
    the same task, for example `task:review-branch`.
  - **AI tags** (outlined chip with a dashed border), derived from what the agent did. They are
    recomputed on every import and cannot be removed:
    - **Project** (orange): `project:`;
    - **Model** (aqua): `model:`;
    - **Skill** (violet): `skill:`;
    - **Other AI** (grey): `cmd:` (slash command), `agent:` (subagent type) and `persona:`.

  Color is never the only cue: each chip also carries its prefix, and human chips have a
  different shape. The three AI hues pass a color-blindness check as a set in light and dark mode.
- **The Tags page**, a gallery of tag cards:
  - each card shows the average cost per prompt, a sparkline of the latest prompts' cost, a trend
    badge (for example "▼ 23% cheaper": the median of the last runs against the runs before),
    the number of prompts, the total cost and when the tag was last used;
  - a summary band at the top shows the number of tags, the priciest tag per prompt and the most
    used tag;
  - a toolbar that stays visible while scrolling offers search, category filters, sorting
    (recently used, most prompts, highest total cost, highest cost per prompt, name), and two
    layouts:
    - **Grouped**: by category, then by subtype (prefix for human tags, model family, skill
      plugin, slash commands, subagent types, personas);
    - **Timeline**: by month of creation. A human tag's creation date is when you created it;
      an AI tag's is the date of its first prompt.
  - clicking a card (or pressing Enter on it) opens a panel under its row with the tag's
    charts and latest prompts, plus a link to its full page; Escape closes it.
  - your layout, sort and filters are remembered in the browser.
- **A tag's page**: a header in the tag's color, its keyword rules (human tags) shown as
  removable chips with an "Add keywords" form, a summary band (runs, total cost, median cost
  with its trend, median time) and the cost, duration and token charts. Below, its runs with
  the same toolbar as the Prompts page (search, project, period, sort, List or By tag
  layout); the band and charts follow the filters, which are kept in the URL. Clicking another
  tag on a run opens that tag's page.
- **Keywords on human tags**: when you add a tag on a prompt page, or later from the tag's page,
  you can give keywords. Every prompt containing all (or any) of them gets the tag, past ones
  right away and new ones on import. A live count shows how many prompts would match.
- **A Setup page** to:
  - check that the transcripts are found;
  - install a Claude Code `Stop` hook for live updates;
  - extend transcript retention;
  - edit the price table, the similarity threshold and the auto-tag rules.

## Quick start

```bash
uv sync
uv run agent-dashboard            # http://127.0.0.1:8765
```

Then open **Setup**:

1. Press **Import now**. All existing transcripts in `~/.claude/projects` are imported.
2. Press **Install hook**. Claude Code will notify the dashboard after every answer. Without
   the hook, the server rescans every 60 s while it runs.
3. Optionally, press **Enable telemetry**. Claude Code then sends the dashboard its own cost for
   every API call (see "How the numbers are computed").
4. Optionally, set the retention to 3650 days. By default Claude Code deletes transcripts after
   30 days. Prompts already imported stay in the dashboard either way, but keeping the
   transcripts lets you re-import them after a price change.

To import once without starting the server: `uv run agent-dashboard ingest [--force]`.
The database lives in `~/.local/share/agent-dashboard/dashboard.db`. Change it with `--db` or
`AGENT_DASHBOARD_DB`.

## How the numbers are computed

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

To compare runs of the same task reliably, give them the same human tag, then choose "All prompts
tagged …" on a prompt page. Add the tag by hand on the prompt page, or with an auto-tag rule
such as `{"pattern": "review .*paper", "tag": "task:paper-review"}`. Choose "Similar prompts
tagged …" instead when one tag covers several kinds of prompts, such as a project-wide tag.

Switch between the two costs at any time with **Official / Price table** in the top bar.
- **Official** shows Claude Code's own cost for every prompt telemetry fully covers, and the
  price-table estimate for the rest.
- **Price table** always shows the estimate.

Each prompt's cost shows the other figure underneath for comparison.

As a sanity check, Claude Code stores a session-level `cost-state` total. Price-table estimates
land within a few percent of it. On a test prompt, telemetry and the price table agreed exactly.

Telemetry has two limits:
- It only applies to Claude Code sessions started after it is enabled.
- Calls made while the dashboard is not running are lost. A prompt missing some of its calls
  falls back to the estimate.

## Development

```bash
uv run pytest
```
