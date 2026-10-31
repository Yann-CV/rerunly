"""Refresh README screenshots exclusively from the isolated example app.

Always use the example app for documentation illustrations; never capture personal
history or a normal dashboard, including an empty normal dashboard.

Run from the repository root:
    uv run --with playwright python docs/capture_screenshots.py

Refresh only the prompt page and its expanded rerun panel with --prompt-only.
Use --model-checks-only for the Reruns page and illustrative availability results.
Use --compare-only for the comparison page and judge result.
Use --rerun-comparison-only for the all-reruns comparison.
Use --rerun-detail-only for the rerun details page.

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
                    page.locator("#app h1, #app .page-kicker").first.wait_for()
                    page.evaluate("document.fonts.ready")
                    # Wait for Chart.js animations before capturing, and fail on missing charts.
                    page.wait_for_function("typeof Chart !== 'undefined'")
                    page.wait_for_timeout(1200)
                    if focus:
                        page.locator(focus).first.evaluate(
                            "el => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 75)")
                    else:
                        page.evaluate("window.scrollTo(0, 0)")
                    if name == "reruns":
                        page.locator("#rerun-history").wait_for()
                        page.locator("[data-history-group]").evaluate_all("els => els.forEach(el => el.open = true)")
                        page.wait_for_timeout(100)
                    page.screenshot(path=str(output / (name + ".png")), full_page=name == "reruns")
                    print(f"Captured {name}")

                def capture_model_checks():
                    # Explicitly illustrative outcomes, never real agent calls.
                    import json
                    from datetime import datetime, timezone
                    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    state = {"status": "completed", "started_at": checked, "finished_at": checked, "current": None,
                        "results": [
                            {"agent": "codex", "model": "gpt-6.1-sol", "status": "listed", "checked_at": checked,
                             "message": "Illustrative result: listed in the agent’s menu. No test prompt was sent."},
                            {"agent": "claude", "model": "claude-sonnet-5-5", "status": "discovery_failed", "checked_at": checked,
                             "message": "Illustrative result: the agent’s model catalog could not be refreshed."}]}
                    connection = db.connect(database)
                    with connection:
                        connection.execute("INSERT INTO model_check_state(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value", (json.dumps(state),))
                    connection.close()
                    page.evaluate("viewReruns()")
                    summary = page.locator("#model-check-tools")
                    assert summary.locator("button, form, input, select").count() == 0
                    summary.screenshot(path=str(output / "model-availability-summary.png"), style=".topbar { visibility: hidden !important; }")
                    # Preview the real Setup component only inside the isolated example app.
                    # Its controls stay disabled; no personal Setup endpoints are accessed.
                    page.evaluate("""async () => {
                        const data = await api('/api/model-checks');
                        app.innerHTML = '<section id="model-availability" aria-label="Model availability settings">' + modelChecksHtml(data) + '</section>';
                    }""")
                    panel = page.locator(".model-checks")
                    panel.get_by_text("Listed by agent", exact=True).wait_for()
                    assert panel.get_by_role("button", name="Check now", exact=True).is_disabled()
                    panel.screenshot(path=str(output / "model-checks.png"), style=".topbar { visibility: hidden !important; }")
                    for width in (360, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Model checks overflow viewport"
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    page.emulate_media(color_scheme="dark")
                    panel.screenshot(path="/tmp/rerunly-model-checks-dark.png", style=".topbar { visibility: hidden !important; }")
                    page.emulate_media(color_scheme="light")
                    page.evaluate("viewReruns()")
                    page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(output / "reruns.png"), full_page=True)

                if "--rerun-detail-only" in sys.argv:
                    capture("rerun-detail", "/rerun/rerunly-example:blocked-rerun:session")
                    page.get_by_role("button", name="Check process and logs").click()
                    page.get_by_text("Recorded status:", exact=False).wait_for()
                    for width in (360, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.locator(".rerun-diagnostics").evaluate("el => el.getBoundingClientRect().right <= innerWidth + 1")
                    if errors:
                        raise RuntimeError("Browser errors: " + "; ".join(errors))
                    browser.close()
                    return

                if "--rerun-comparison-only" in sys.argv:
                    capture("rerun-comparison", "/rerun-comparison/rerunly-example:review-skill-v1")
                    reference = page.locator(".rc-reference-feature")
                    assert "Original" in reference.inner_text()
                    page.locator(".rc-original-prompt summary").click()
                    assert page.locator(".rc-original-prompt pre").is_visible()
                    attempt = page.locator(".rc-table tbody tr").nth(1)
                    attempt.locator(".rc-prompt summary").click()
                    assert attempt.locator(".rc-prompt pre").inner_text().strip()
                    page.locator('[data-set-rerun-reference="rerunly-example:review-skill-v2"]').click()
                    page.get_by_role("heading", name="Reference run · Attempt 1").wait_for()
                    page.locator('.rc-table a[href^="#/compare?"]').first.click()
                    page.get_by_role("link", name="All reruns comparison").wait_for()
                    page.locator("#swap").click()
                    page.get_by_role("link", name="All reruns comparison").click()
                    page.get_by_role("heading", name="Reference run · Attempt 1").wait_for()
                    for width in (390, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Comparison overflows viewport"
                    if errors:
                        raise RuntimeError("Browser errors: " + "; ".join(errors))
                    browser.close()
                    return

                if "--compare-only" in sys.argv:
                    capture("compare", "/compare?a=rerunly-example:review-skill-v1&b=rerunly-example:cheap-but-incomplete")
                    assert page.locator(".compare-role-reference select").input_value() == "rerunly-example:review-skill-v1"
                    assert page.locator(".compare-role-other select").input_value() == "rerunly-example:cheap-but-incomplete"
                    assert "Reference ·" in page.locator(".compare-figures th").nth(1).inner_text()
                    assert "Compared run ·" in page.locator(".compare-figures th").nth(2).inner_text()
                    back = page.get_by_role("link", name="All reruns comparison")
                    assert back.get_attribute("href") == "#/rerun-comparison/rerunly-example%3Areview-skill-v1"
                    judge = page.locator(".judge-card")
                    assert judge.locator(".judge-score .big").is_visible()
                    assert judge.locator(".judge-summary").is_visible()
                    assert judge.locator(".judge-diffs li").count() > 0
                    assert judge.get_by_text("Take another look").is_visible()
                    assert judge.get_by_role("button", name="Judge again").is_disabled()
                    judge.get_by_text("Estimated limit impact").wait_for()
                    judge.get_by_text("Rough estimate:", exact=False).wait_for()
                    assert "unavailable" in judge.locator("#judge-impact-content").inner_text().lower()
                    for width in (360, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert judge.evaluate("el => el.getBoundingClientRect().right <= innerWidth + 1"), "Judge card overflows viewport"
                    page.locator("#swap").click()
                    page.wait_for_function("document.querySelector('#pick-a')?.value === 'rerunly-example:cheap-but-incomplete'")
                    assert page.locator("#pick-b").input_value() == "rerunly-example:review-skill-v1"
                    # Add a second illustrative judge run for this pair, without calling an agent.
                    connection = db.connect(database)
                    with connection:
                        connection.execute("INSERT INTO judge_rating_history(base_id,other_id,score,verdict,quality,look,summary,differences,model,rated_at,origin,duration_ms,total_tokens,cost_usd) "
                            "SELECT base_id,other_id,65,'minor','worse',0,'The output omits a required check.',differences,'Example judge B',rated_at,'explicit',7300,3200,0.023 "
                            "FROM run_ratings WHERE base_id=? AND other_id=?",
                            ("rerunly-example:review-skill-v1", "rerunly-example:cheap-but-incomplete"))
                    connection.close()
                    page.goto(base + "#/reruns?judge_history=1")
                    pair = page.locator(".judge-history-group").filter(has_text="2 judge runs")
                    pair.locator("summary").click()
                    pair.get_by_role("link", name="Compare all judge runs").click()
                    page.locator(".judge-comparison-run").first.wait_for()
                    assert page.locator(".judge-comparison-run").count() == 2
                    outputs = page.locator(".judge-output-column")
                    assert outputs.count() == 2
                    second_run = outputs.nth(0).locator("option").filter(has_text="Example judge B").get_attribute("value")
                    outputs.nth(0).locator("select").select_option(second_run)
                    assert outputs.nth(0).get_by_text("The output omits a required check.").is_visible()
                    outputs.nth(0).locator("select").select_option("0")
                    assert page.get_by_text("Average score").is_visible()
                    assert page.locator(".judge-metric-chart").count() == 3
                    assert page.locator(".judge-metric-row").count() == 6
                    assert page.get_by_role("link", name="Judge run history").get_attribute("href") == "#/reruns?judge_history=1"
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    page.screenshot(path=str(output / "judge-comparison.png"), full_page=True)
                    for width in (360, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Judge comparison overflows viewport"
                    action = page.locator(".judge-comparison-action")
                    action.get_by_text("Estimated limit impact").wait_for()
                    assert action.get_by_role("button", name="Judge again").is_disabled()
                    calls = []
                    def mock_judge(route):
                        calls.append(route.request.post_data_json)
                        route.fulfill(json={"added": 1} if route.request.url.endswith("/queue/judge") else {"status": "rating"})
                    page.route("**/api/reruns/queue/judge", mock_judge)
                    page.route("**/api/ratings", mock_judge)
                    action.locator("#queue-judge, #judge-form button[type=submit]").evaluate_all("buttons => buttons.forEach(button => button.disabled = false)")
                    action.get_by_role("button", name="Add judgment to queue").click()
                    action.get_by_text("Judgment added to the", exact=False).wait_for()
                    action.get_by_role("button", name="Judge again").click()
                    assert len(calls) == 2 and all(call["base"] == "rerunly-example:review-skill-v1" and call["other"] == "rerunly-example:cheap-but-incomplete" for call in calls)
                    if errors:
                        raise RuntimeError("Browser errors: " + "; ".join(errors))
                    browser.close()
                    return

                if "--model-checks-only" in sys.argv:
                    connection = db.connect(database)
                    with connection:
                        connection.execute("INSERT INTO replay_queue(prompt_id,model,effort,added_at,mode,permission) SELECT id,'codex:gpt-6.1-sol','medium',started_at,'terminal','' FROM prompts WHERE id='rerunly-example:broad-prompt'")
                        connection.execute("INSERT INTO replay_queue(prompt_id,other_prompt_id,kind,model,added_at) SELECT id,'rerunly-example:cheap-but-incomplete','judge','codex:gpt-6.1-sol',started_at FROM prompts WHERE id='rerunly-example:review-skill-v1'")
                    connection.close()
                    capture("reruns", "/reruns")
                    assert page.locator(".judge-queue-card .run-kind").is_visible()
                    assert page.locator(".run-kind-prompt").is_visible()
                    assert "tokens" in page.locator(".judge-queue-card").inner_text()
                    page.get_by_role("checkbox", name="Select judge run 2").check()
                    page.get_by_role("heading", name="Selected judgments").wait_for()
                    assert page.locator("#selected-quota-forecast").is_visible()
                    assert page.get_by_role("button", name="Prompt run history").is_visible()
                    assert page.get_by_role("button", name="Judge run history").is_visible()
                    assert page.locator(".judge-history-link").count() == 0
                    page.get_by_role("button", name="Judge run history").click()
                    page.locator("#judge-history[open]").wait_for()
                    page.locator(".judge-history-group summary").first.click()
                    page.locator(".judge-history-run").first.wait_for()
                    assert page.get_by_role("link", name="Compare all judge runs").count() == 1
                    page.get_by_role("button", name="Prompt run history").click()
                    page.locator("#rerun-history[open]").wait_for()
                    for width in (360, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mixed queue overflows viewport"
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    capture_model_checks()
                    if errors:
                        raise RuntimeError("Browser errors: " + "; ".join(errors))
                    browser.close()
                    return

                if "--prompt-only" in sys.argv:
                    capture("prompt", "/prompt/rerunly-example:review-skill-v2", focus='h2:has-text("Cost per agent")')
                    assert page.locator("#compare, #compare-sec, #cmp-cost").count() == 0
                    planner = page.locator(".rerun-planner")
                    assert not planner.evaluate("el => el.open")
                    planner.locator(":scope > summary").click()
                    page.locator(".replay-impact-estimate").wait_for()
                    assert page.locator("#replay-current-content").inner_text().strip()
                    assert planner.locator(".model-access-filter, .model-access-note").count() == 0
                    assert page.locator("#queue-replay").is_disabled()
                    assert page.get_by_role("button", name="Rerun now").is_disabled()
                    assert page.evaluate("""() => {
                        const y = s => document.querySelector(s).getBoundingClientRect().top;
                        return y('.replay-head') < y('.replay-current') &&
                            y('.replay-current') < y('#replay-agent') &&
                            y('#replay-agent') < y('.replay-impact');
                    }""")
                    planner.evaluate("el => window.scrollTo(0, el.getBoundingClientRect().top + scrollY - 75)")
                    planner.screenshot(path=str(output / "prompt-rerun.png"), style=".topbar { visibility: hidden !important; }")
                    for width in (390, 768):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Prompt overflows viewport"
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    page.emulate_media(color_scheme="dark")
                    page.screenshot(path="/tmp/rerunly-prompt-dark.png", full_page=True)
                    if errors:
                        raise RuntimeError("Browser errors: " + "; ".join(errors))
                    browser.close()
                    return

                capture("home", "/", focus=".home-reruns")
                # An incomplete illustrative item exposes queue estimates and configuration needs without launching work.
                connection = db.connect(database)
                with connection:
                    connection.execute(
                        "INSERT INTO replay_queue(prompt_id, model, effort, added_at, mode, permission, release, budget_usd) "
                        "SELECT id, 'codex:gpt-6.1-sol', 'medium', started_at, 'terminal', '', 'same', 3 "
                        "FROM prompts WHERE id='rerunly-example:broad-prompt'")
                connection.close()
                capture("prompts", "/prompts")
                page.get_by_role("button", name="Select prompts", exact=True).click()
                page.locator(".psel").first.click()
                page.locator("#select-bar").evaluate("el => window.scrollTo(0, el.getBoundingClientRect().top + scrollY - parseFloat(getComputedStyle(el).top))")
                page.screenshot(path=str(output / "prompt-selection.png"))
                page.get_by_role("region", name="Selection", exact=True).get_by_role("button", name="Done", exact=True).click()
                page.evaluate("localStorage.setItem('prompts-prefs', JSON.stringify({layout:'grouped'}))")
                capture("by-tag", "/prompts")
                capture("sessions", "/sessions")
                capture("session", "/session/rerunly-example:review-skill-v2:session")
                capture("prompt", "/prompt/rerunly-example:review-skill-v2", focus='h2:has-text("Cost per agent")')
                planner = page.locator(".rerun-planner")
                planner.locator(":scope > summary").click()
                page.locator(".replay-impact-estimate").wait_for()
                planner.screenshot(path=str(output / "prompt-rerun.png"), style=".topbar { visibility: hidden !important; }")
                capture("tag", "/tag/example:skills")
                capture("tag-dark", "/tag/example:skills", dark=True)
                capture("tags", "/tags")
                capture("reruns", "/reruns")
                capture_model_checks()
                assert page.locator("[data-history-group]").count() == 2
                search = page.locator("#history-search")
                search.fill("divide(a, b)")
                page.wait_for_function("document.querySelectorAll('[data-history-group]').length === 1")
                assert page.locator(".rh-attempt").count() == 2
                assert "q=" in page.url, "History search should be bookmarkable"
                search.fill("not-a-real-example")
                page.get_by_text("No reruns match these filters.").wait_for()
                page.locator("[data-history-clear]").click()
                page.wait_for_function("document.querySelectorAll('[data-history-group]').length === 2")
                page.locator("#pop-category-example > summary").click()
                page.locator('[data-category-tag="example:skills"]').click()
                page.wait_for_function("document.querySelectorAll('[data-history-group]').length === 1")
                assert "tags=example%3Askills" in page.url
                page.locator("[data-history-clear]").click()
                page.wait_for_function("document.querySelectorAll('[data-history-group]').length === 2")
                review = page.locator("[data-history-group]").filter(has_text="Review divide")
                if not review.evaluate("el => el.open"):
                    review.locator(":scope > summary").click()
                review.get_by_role("link", name="Compare all reruns").click()
                page.locator(".rerun-comparison-page").wait_for()
                assert page.locator(".rc-table tbody tr").count() == 3
                assert page.locator(".rc-trends canvas").count() == 3
                assert page.evaluate("Chart.getChart(document.querySelector('#rerun-chart-cost')).data.datasets[1].data[0].p.id") == "rerunly-example:review-skill-v1"
                assert page.locator(".rc-table a[href^='#/compare?']").count() == 2
                page.locator('[data-set-rerun-reference="rerunly-example:review-skill-v2"]').click()
                page.get_by_role("heading", name="Compare with Attempt 1").wait_for()
                assert page.evaluate("Chart.getChart(document.querySelector('#rerun-chart-cost')).data.datasets[1].data[0].p.id") == "rerunly-example:review-skill-v2"
                assert page.locator(".rc-reference").count() == 1
                assert page.get_by_text("Not judged against this reference").count() == 2
                assert page.locator("[data-judge-run]:disabled").count() == 2
                page.locator('[data-set-rerun-reference="rerunly-example:review-skill-v1"]').click()
                page.get_by_role("heading", name="Compare with Original").wait_for()
                for width in (390, 768):
                    page.set_viewport_size({"width": width, "height": 900})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Comparison overflows viewport"
                page.set_viewport_size({"width": 1440, "height": 1100})
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(output / "rerun-comparison.png"), full_page=True)
                print("Captured rerun-comparison")
                page.goto(base + "#/reruns")
                page.locator(".reruns-page").wait_for()
                assert page.locator(".reruns-page .hero-name").text_content().strip() == "Reruns"
                card = page.locator(".rq-card").first
                assert card.locator(".rq-title").inner_text().strip()
                assert card.locator(".rq-estimate").inner_text().strip()
                card.screenshot(path=str(output / "rerun-queue.png"), style=".topbar { visibility: hidden !important; }")
                card.locator(".rq-details > summary").click()
                assert card.locator(".rq-details").evaluate("el => el.open")
                assert page.locator(".rq-card.selected").count() == 0, "Opening settings selected the card"
                card.locator("[data-rr-pick]").click()
                page.locator(".rq-card.selected").wait_for()
                assert page.locator(".rq-card.selected").count() == 1
                page.locator(".rq-card [data-rr-pick]").first.click()
                page.locator(".rq-card.selected").wait_for(state="detached")
                assert page.locator(".rq-card.selected").count() == 0
                history = page.locator("#rerun-history")
                assert page.locator("[data-history-group]").count() == 2
                assert page.locator(".rh-attempt").count() == 3
                assert page.locator('.rh-attempt a[href^="#/compare"]').count() == 2
                history.locator(":scope > summary").click()
                page.wait_for_timeout(50)
                assert not history.evaluate("el => el.open")
                page.evaluate("viewReruns()")
                assert not page.locator("#rerun-history").evaluate("el => el.open"), "History collapsed state was lost"
                page.locator("#rerun-history > summary").click()
                page.wait_for_timeout(50)
                for width in (390, 768):
                    page.set_viewport_size({"width": width, "height": 900})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Reruns overflow viewport"
                page.set_viewport_size({"width": 1440, "height": 1100})
                page.emulate_media(color_scheme="dark")
                page.locator("#rerun-history").screenshot(path="/tmp/rerunly-history-dark.png")
                page.locator(".rq-card").first.screenshot(path="/tmp/rerunly-queue-dark.png", style=".topbar { visibility: hidden !important; }")
                page.emulate_media(color_scheme="light")

                page.locator("[data-rr-config]").first.click()
                page.locator("#queue-config-warning").wait_for(state="visible")
                assert page.locator("#queue-config-form .model-access-filter, #queue-config-form .model-access-note").count() == 0
                page.wait_for_function("document.querySelector('#queue-config-form [type=submit]').disabled")
                page.screenshot(path=str(output / "queue-settings.png"))
                print("Captured queue-settings")
                page.locator("[data-cancel-config]").click()
                capture("compare", "/compare?a=rerunly-example:review-skill-v1&b=rerunly-example:cheap-but-incomplete")
                capture("rerun-detail", "/rerun/rerunly-example:blocked-rerun:session")
                page.get_by_role("heading", name="Launch settings", exact=True).wait_for()
                page.get_by_text("No imported transcript is available for this rerun.", exact=True).wait_for()
                # A missing optional model-check service must not hide the queue or history.
                page.route("**/api/model-checks", lambda route: route.fulfill(status=404, body="unavailable"))
                page.goto(base + "#/reruns")
                page.locator(".rq-card").first.wait_for()
                assert page.locator("#rerun-history").count() == 1
                assert "unavailable" in page.locator("#model-check-tools").inner_text().lower()
                browser.close()
                if errors:
                    raise RuntimeError("Browser errors: " + "; ".join(errors))
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
