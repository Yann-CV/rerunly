"""The usage panel remains available while navigating and scrolling."""
import shutil
import threading
import time

import pytest
import uvicorn

from rerunly.server import create_app


def test_usage_panel_floats_and_collapses(tmp_path):
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
                for width in (1440, 375):
                    page = browser.new_page(viewport={"width": width, "height": 700})
                    page.route("**/api/limits", lambda route: route.fulfill(json=[{
                        "agent": "Example agent", "capture": True, "windows": [{
                            "window_minutes": 300, "used_percent": 42, "resets_at": "2099-01-01T00:00:00Z"
                        }]
                    }]))
                    page.goto(base)
                    panel = page.locator(".hero-limits")
                    panel.get_by_text("42%").wait_for()
                    assert "resets in" in panel.inner_text()
                    before = panel.bounding_box()
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    after = panel.bounding_box()
                    assert abs(before["y"] - after["y"]) < 1
                    assert after["x"] >= 0 and after["x"] + after["width"] <= width
                    panel.locator("summary").click()
                    assert not panel.get_by_text("42%").is_visible()
                    panel.locator("summary").click()
                    assert panel.get_by_text("42%").is_visible()
                    panel.locator("summary").click()
                    page.locator('[data-nav="prompts"]').click()
                    page.locator("#prompt-search").wait_for()
                    assert panel.is_visible()
                    assert not panel.get_by_text("42%").is_visible()
                    panel.locator("summary").click()
                    assert panel.get_by_text("42%").is_visible()
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    assert panel.is_visible()
                    page.locator('[data-nav="sessions"]').click()
                    page.locator("#session-search").wait_for()
                    assert panel.get_by_text("42%").is_visible()
                    handle = page.locator("#usage-drag")
                    box = handle.bounding_box()
                    initial = panel.bounding_box()
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.mouse.down()
                    page.mouse.move(box["x"] - 60, box["y"] - 60, steps=5)
                    page.mouse.up()
                    moved = panel.bounding_box()
                    assert moved["x"] < initial["x"] and moved["y"] < initial["y"]
                    handle.focus()
                    page.keyboard.press("ArrowDown")
                    assert panel.bounding_box()["y"] == moved["y"] + 10
                    page.locator("#usage-close").click()
                    assert not panel.is_visible()
                    assert page.locator("#usage-reopen").is_visible()
                    page.reload()
                    page.locator("#usage-reopen").wait_for()
                    assert not panel.is_visible()
                    page.locator("#usage-reopen").click()
                    assert panel.is_visible()
                    restored = panel.bounding_box()
                    assert abs(restored["x"] - moved["x"]) < 1
                    assert abs(restored["y"] - moved["y"] - 10) < 1
                    page.close()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
