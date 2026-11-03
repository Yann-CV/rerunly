# Contributing with coding agents

## Project overview

Rerunly is a local-first dashboard for inspecting coding agent history, rerunning
tasks, and comparing cost, tokens, time, and output quality.
Read `README.md` for supported behavior and setup instructions before making changes.

- `rerunly/`: Python backend, CLI, imports, pricing, and rerun logic.
- `rerunly/sources/`: agent transcript adapters and their shared interface.
- `rerunly/static/`: frontend HTML, JavaScript, and CSS.
- `tests/`: pytest tests.
- `docs/`: documentation artwork, screenshots, and screenshot capture tooling.

## Development and validation

- Use Python 3.11+ and `uv sync` to install dependencies.
- Run `uv run rerunly --reload` for local development.
- Run relevant tests with `uv run pytest`, optionally specifying a test file.
  Run `uv run playwright install chromium` once for the browser tests.
- Run `uv run ruff check .` and `uv run pyright`; CI enforces them with the tests.
- Keep changes focused and follow the surrounding code's conventions.
- Add or update tests when changing behavior, and report what was verified.
- Preserve unrelated changes already present in the working tree.
- Keep personal transcripts, credentials, and local history out of committed files.

## Frontend changes and README accuracy

For **every frontend change**, check `README.md` and verify that the elements you
updated are still documented accurately. Review the affected feature descriptions,
UI labels, navigation instructions, tour captions, and screenshots. Update any
outdated README content and affected documentation images as part of the same change.
If the README is already accurate, no documentation edit is necessary, but mention
that you checked it in your completion summary.

Use the isolated example app for documentation screenshots, as described in
`README.md` and `docs/capture_screenshots.py`. Keep illustrative data clearly
identified as examples.
