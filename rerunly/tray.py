"""Status bar icon: keeps the server running in the background and opens the dashboard."""

import json
import signal
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from . import background

REFRESH_MS = 60_000


def _get(url: str, method: str = "GET", timeout: float = 2) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def _start_server(db_path: Path, host: str, port: int):
    import uvicorn

    from .server import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(db_path, port), host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, name="dashboard-server", daemon=True).start()
    return server


def _icon():
    from PySide6.QtGui import QIcon

    return QIcon(str(background.ICON))  # the app logo; Qt renders the SVG at the size the tray asks for


def run_tray(db_path: Path, port: int, host: str = "127.0.0.1") -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    base = f"http://{host}:{port}"
    # a dashboard already serving this port (e.g. `rerunly --reload`) is reused
    server = None if _get(f"{base}/api/settings") is not None else _start_server(db_path, host, port)
    background.write_pid()

    app = QApplication([])
    app.setApplicationName("Rerunly")
    app.setQuitOnLastWindowClosed(False)  # there is no window: the app lives in the status bar

    tray = QSystemTrayIcon(_icon())
    menu = QMenu()
    today = menu.addAction("Today: …")
    today.setEnabled(False)
    menu.addSeparator()
    open_action = menu.addAction("Open dashboard")
    import_action = menu.addAction("Import now")
    menu.addSeparator()
    autostart = QAction("Start at login", menu, checkable=True)
    menu.addAction(autostart)
    menu.addSeparator()
    quit_action = menu.addAction("Quit")

    def refresh():
        """Today's spend in the menu and tooltip; also keeps the checkbox in step with the Setup page."""
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        data = _get(f"{base}/api/prompts?limit=1&since={urllib.parse.quote(midnight.isoformat())}")
        if data is None:
            text = "Dashboard not reachable"
        else:
            s = data["summary"]
            text = f"Today: ${s['cost_usd'] or 0:.2f} · {s['count']} prompt{'' if s['count'] == 1 else 's'}"
        today.setText(text)
        tray.setToolTip(f"Rerunly\n{text}")
        autostart.setChecked(background.autostart_enabled())

    def run_import():
        import_action.setEnabled(False)
        import_action.setText("Importing…")

        def work():
            _get(f"{base}/api/ingest", method="POST", timeout=600)
            QTimer.singleShot(0, app, done)  # back on the Qt thread

        def done():
            import_action.setText("Import now")
            import_action.setEnabled(True)
            refresh()

        threading.Thread(target=work, daemon=True).start()

    def quit_app():
        if server:
            server.should_exit = True
        app.quit()

    open_action.triggered.connect(lambda: webbrowser.open(f"{base}/"))
    import_action.triggered.connect(run_import)
    def toggle_autostart(on: bool):
        if on != background.autostart_enabled():
            background.set_autostart(on, db_path, port)

    autostart.toggled.connect(toggle_autostart)
    quit_action.triggered.connect(quit_app)
    tray.activated.connect(lambda reason: reason == QSystemTrayIcon.Trigger and webbrowser.open(f"{base}/"))
    tray.setContextMenu(menu)
    tray.show()

    # `rerunly stop` sends SIGTERM; Python only runs handlers when the Qt loop hands back control
    signal.signal(signal.SIGTERM, lambda *_: quit_app())
    signal.signal(signal.SIGINT, lambda *_: quit_app())
    tick = QTimer(app, interval=500)
    tick.timeout.connect(lambda: None)
    tick.start()

    timer = QTimer(app, interval=REFRESH_MS)
    timer.timeout.connect(refresh)
    timer.start()
    QTimer.singleShot(1500, refresh)  # once the server is up
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No status bar tray found: the dashboard still runs in the background on", base, flush=True)
    print(f"Rerunly in the status bar, serving {base}", flush=True)
    try:
        app.exec()
    finally:
        background.clear_pid()
        if server:
            server.should_exit = True
