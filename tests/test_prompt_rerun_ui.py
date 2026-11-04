"""Browser validation must allow the prompt rerun form to submit."""
import shutil
from pathlib import Path

import pytest


def test_default_rerun_budget_allows_browser_submission():
    playwright = pytest.importorskip("playwright.sync_api")
    source = (Path(__file__).parents[1] / "rerunly/static/app.js").read_text()
    render = source[source.index("function sectionHelp("):source.index("let sectionHelpEvents")] + "function replayHtml(" + source.split("function replayHtml(", 1)[1].split("\nfunction rerunSectionHtml", 1)[0]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which("google-chrome"))
        try:
            page = browser.new_page()
            page.set_content("<main></main>")
            page.add_script_tag(content="const esc = s => String(s); const EFFORTS = []; " + render)
            for cost in [0, 0.2, 1]:
                page.evaluate("""cost => {
                    document.querySelector('main').innerHTML = replayHtml({
                        id: 'test', total_cost_usd: cost, references: [], replays: [],
                        rerun: {harness: 'Claude Code', skills: []}
                    }, []);
                    window.submitted = false;
                    document.querySelector('form').onsubmit = event => {
                        event.preventDefault(); window.submitted = true;
                    };
                }""", cost)
                page.get_by_role("button", name="Rerun now").click()
                assert page.evaluate("window.submitted")
        finally:
            browser.close()
