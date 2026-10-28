"""Refresh README screenshots exclusively from the isolated example app.

Always use the example app for documentation illustrations; never capture personal
history or a normal dashboard, including an empty normal dashboard.

Run from the repository root:
    uv run --with playwright python docs/capture_screenshots.py

Requires Chrome, Chromium, or `uv run --with playwright playwright install chromium`.
No transcripts are imported, agents launched, or agent settings changed.
"""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rerunly.server import create_app
from rerunly import db


def main():
    output = Path(__file__).resolve().parent / "screenshots"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rerunly-screenshots-") as temp:
        database = Path(temp) / "examples.db"
        app = FastAPI()
        app.mount("/examples", create_app(database, background=False, examples=True))
        app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "rerunly/static"))
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning",
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Screenshot server failed to start")
                time.sleep(0.05)
            port = server.servers[0].sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}/examples/"
            with sync_playwright() as p:
                executable = next((shutil.which(name) for name in
                    ("google-chrome", "chromium", "chromium-browser") if shutil.which(name)), None)
                browser = p.chromium.launch(executable_path=executable)
                page = browser.new_page(viewport={"width": 1440, "height": 1100},
                    device_scale_factor=1, color_scheme="light", locale="en-US", timezone_id="UTC")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def capture(name, route, *, focus=None, dark=False):
                    page.emulate_media(color_scheme="dark" if dark else "light", reduced_motion="reduce")
                    page.goto(base + "#" + route)
                    if not page.url.startswith(base):
                        raise RuntimeError("Illustrations must come from the example app")
                    page.locator(".examples-banner").wait_for()
                    page.wait_for_load_state("networkidle")
                    page.locator("#app h1").first.wait_for()
                    page.evaluate("document.fonts.ready")
                    # Wait for Chart.js animations before capturing, and fail on missing charts.
                    page.wait_for_function("typeof Chart !== 'undefined'")
                    page.wait_for_timeout(1200)
                    if focus:
                        page.locator(focus).first.evaluate(
                            "el => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 75)")
                    else:
                        page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(output / (name + ".png")), full_page=name == "reruns")
                    print(f"Captured {name}")

                capture("home", "/")
                # A pending illustrative item exposes the queue estimates without launching work.
                connection = db.connect(database)
                with connection:
                    connection.execute(
                        "INSERT INTO replay_queue(prompt_id, model, effort, added_at, mode, permission, release) "
                        "SELECT id, 'codex:gpt-6.1-sol', 'medium', started_at, 'terminal', 'ask', 'same' "
                        "FROM prompts WHERE id='rerunly-example:broad-prompt'")
                connection.close()
                capture("prompts", "/prompts")
                page.evaluate("localStorage.setItem('prompts-prefs', JSON.stringify({layout:'grouped'}))")
                capture("by-tag", "/prompts")
                capture("sessions", "/sessions")
                capture("session", "/session/rerunly-example:review-skill-v2:session")
                capture("prompt", "/prompt/rerunly-example:review-skill-v2", focus='h2:has-text("Cost per agent")')
                capture("tag", "/tag/example:skills")
                capture("tag-dark", "/tag/example:skills", dark=True)
                capture("tags", "/tags")
                capture("reruns", "/reruns")
                capture("compare", "/compare?a=rerunly-example:review-skill-v1&b=rerunly-example:cheap-but-incomplete")
                browser.close()
                if errors:
                    raise RuntimeError("Browser errors: " + "; ".join(errors))
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
