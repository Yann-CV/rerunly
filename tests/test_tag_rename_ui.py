"""The tag name editor is visible only while editing and can save changes."""
import shutil
import threading
import time

import pytest
import uvicorn

from rerunly.server import create_app


def test_tag_name_editor(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    app = create_app(tmp_path / "examples.db", background=False, examples=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.05)
        base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/"
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which("google-chrome"))
            try:
                page = browser.new_page(viewport={"width": 375, "height": 700})
                page.goto(base + "#/tag/example:skills")
                rename = page.locator("#rename-tag")
                rename.wait_for()
                form = page.locator("#rename-tag-form")
                assert not form.is_visible()
                rename.click()
                assert form.is_visible() and page.locator("#rename-tag-input").evaluate("el => el === document.activeElement")
                page.locator("#rename-tag-cancel").click()
                assert not form.is_visible() and rename.evaluate("el => el === document.activeElement")
                rename.click()
                page.locator("#rename-tag-input").fill("example:quality")
                page.get_by_role("button", name="Save name").click()
                page.locator("#rename-tag-error").wait_for(state="visible")
                assert "already exists" in page.locator("#rename-tag-error").inner_text()
                page.locator("#rename-tag-input").fill("example:renamed")
                page.get_by_role("button", name="Save name").click()
                page.wait_for_url("**/#/tag/example%3Arenamed")
                page.get_by_role("heading", name="example:renamed").wait_for()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
