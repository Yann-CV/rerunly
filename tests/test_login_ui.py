"""Exercise the actual login gate in a browser, including first use, logout and session expiry."""

import shutil
import threading
import time

import pytest
import uvicorn

from rerunly import auth, db
from rerunly.server import create_app

pytestmark = pytest.mark.require_login
PASSWORD = "an illustrative local passphrase"


@pytest.fixture
def dashboard(tmp_path):
    database = tmp_path / "dashboard.db"
    app = create_app(database, background=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, proxy_headers=False, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Login test server did not start")
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", database
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which("google-chrome"))
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            yield page
        finally:
            browser.close()


def test_first_visit_creates_one_account_and_logout_requires_login(dashboard, page):
    base, database = dashboard
    page.goto(base + "/")
    page.get_by_role("heading", name="Create your password").wait_for()
    assert page.request.get(base + "/api/projects").status == 401
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_label("Confirm password").fill("a different passphrase")
    page.get_by_role("button", name="Create password", exact=True).click()
    assert page.get_by_role("alert").inner_text() == "The passwords do not match."
    page.get_by_label("Confirm password").fill(PASSWORD)
    page.get_by_role("button", name="Create password", exact=True).click()
    page.get_by_role("heading", name="Save your recovery code").wait_for()
    code = page.get_by_label("Recovery code", exact=True).input_value()
    assert code.startswith("RR-") and len(code) >= 40
    assert code not in page.url
    assert code not in page.evaluate("JSON.stringify(localStorage) + JSON.stringify(sessionStorage)")
    for width in (360, 768, 1440):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="I've saved my code").click()
    page.wait_for_url(base + "/")
    page.get_by_role("button", name="Sign out").wait_for()
    assert page.request.get(base + "/api/projects").status == 200
    with db.connection_scope():
        assert db.connect(database).execute("SELECT COUNT(*) FROM login_account").fetchone()[0] == 1
    page.locator('[data-nav="prompts"]').click()
    page.wait_for_url(base + "/#/prompts")
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(base + "/login")
    page.get_by_role("heading", name="Welcome back").wait_for()
    assert not page.get_by_label("Confirm password").is_visible()
    assert page.request.get(base + "/api/projects").status == 401
    page.go_back()
    page.get_by_role("heading", name="Welcome back").wait_for()
    assert not page.locator(".topbar").is_visible()
    page.reload()
    page.get_by_role("heading", name="Welcome back").wait_for()


def test_login_errors_responsiveness_and_expiry(dashboard, page):
    base, database = dashboard
    auth.set_password(database, PASSWORD)
    page.goto(base + "/login")
    page.get_by_role("heading", name="Welcome back").wait_for()
    for theme in ("light", "dark"):
        page.emulate_media(color_scheme=theme)
        for width in (360, 768, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.get_by_role("button", name="Sign in").is_visible()
    page.get_by_label("Password", exact=True).fill("the wrong passphrase")
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("alert").wait_for()
    assert page.get_by_role("alert").inner_text() == "Incorrect password."
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(base + "/")
    page.get_by_role("button", name="Sign out").wait_for()
    with db.connection_scope():
        c = db.connect(database)
        with c:
            c.execute("UPDATE login_sessions SET expires_at=?", (time.time() - 1,))
    page.evaluate("api('/api/projects').catch(() => {})")
    page.wait_for_url(base + "/login")
    page.get_by_role("heading", name="Welcome back").wait_for()
    assert page.request.get(base + "/api/projects").status == 401
