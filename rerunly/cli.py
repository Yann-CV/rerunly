import argparse
from pathlib import Path

from . import db
from .config import DEFAULT_DB_PATH, DEFAULT_PORT, DATA_DIR, migrate_legacy_data
from .ingest import ingest
from .security import is_loopback_host


def main() -> None:
    if migrate_legacy_data():
        print(f"Moved your data to {DATA_DIR} (Agent Task Dashboard is now Rerunly)")
    parser = argparse.ArgumentParser(prog="rerunly", description="Rerun a task, see if your prompt change paid off: cost, tokens and time per prompt for AI agents")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--reload", action="store_true", help="restart the server when its Python code changes (UI changes only need a page refresh)")
    sub = parser.add_subparsers(dest="cmd")
    p_password = sub.add_parser("password", help="create or change the local password (requires the current password for an existing account)")
    p_password.add_argument("--recover", action="store_true", help="recover using a previously saved recovery code instead of the current password")
    sub.add_parser("recovery-code", help="generate a replacement recovery code (requires the current password)")

    p_serve = sub.add_parser("serve", help="run the dashboard (default)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--reload", action="store_true", default=argparse.SUPPRESS, help="same as the top-level --reload")

    p_ingest = sub.add_parser("ingest", help="import transcripts once and exit")
    p_ingest.add_argument("--force", action="store_true", help="re-parse every session (e.g. after a price change)")

    p_tray = sub.add_parser("tray", help="run the dashboard with an icon in the status bar (stays in the foreground)")
    p_start = sub.add_parser("start", help="run the dashboard and its status bar icon in the background")
    sub.add_parser("stop", help="stop the background dashboard")
    p_auto = sub.add_parser("autostart", help="start the background dashboard at login, without any action")
    p_auto.add_argument("state", choices=["on", "off"])
    for p in (p_tray, p_start, p_auto):
        p.add_argument("--port", type=int, default=DEFAULT_PORT)

    args = parser.parse_args()
    if args.cmd in ("password", "recovery-code"):
        from getpass import getpass
        from .auth import has_account, issue_recovery_code, set_password
        try:
            if args.cmd == "recovery-code":
                code = issue_recovery_code(args.db, getpass("Current password: "))
            else:
                proof = {}
                if args.recover:
                    proof["recovery_code"] = getpass("Saved recovery code: ")
                elif has_account(args.db):
                    proof["current_password"] = getpass("Current password: ")
                password = getpass("New local password (at least 12 characters): ")
                if password != getpass("Confirm password: "):
                    parser.error("The passwords do not match.")
                code = set_password(args.db, password, **proof).recovery_code
        except ValueError as error:
            parser.error(str(error))
        print("Account security updated. All browser sessions have been signed out.")
        print("Save this recovery code in a password manager or somewhere outside this machine. It is shown only once:")
        print(code)
        print("Previous recovery codes no longer work.")
        return
    port = getattr(args, "port", DEFAULT_PORT)
    from . import background

    if background.autostart_file(background.LEGACY_AUTOSTART).is_file() and args.cmd != "autostart":
        background.set_autostart(True, args.db, port)  # its command predates the rename to Rerunly
    if args.cmd in ("start", "stop", "autostart"):
        if args.cmd == "start":
            print(f"Rerunly running in the background (pid {background.start(args.db, port)}): http://127.0.0.1:{port}")
            print(f"Log: {background.LOG_FILE}")
        elif args.cmd == "stop":
            pid = background.stop()
            print(f"Stopped the background dashboard (pid {pid})" if pid else "No background dashboard running")
        else:
            path = background.set_autostart(args.state == "on", args.db, port)
            print(f"Starts at login: {path}" if args.state == "on" else f"No longer starts at login (removed {path})")
        return
    if args.cmd == "tray":
        from .tray import run_tray

        run_tray(args.db, port)
        return
    if args.cmd == "ingest":
        conn = db.connect(args.db)
        print(ingest(conn, force=args.force))
        return

    from .server import serve

    host = getattr(args, "host", "127.0.0.1")
    print(f"Rerunly on http://{host}:{port}  (db: {args.db}){'  [reloading on code changes]' if args.reload else ''}")
    if not is_loopback_host(host):
        print(f"Reachable from the network, over unencrypted HTTP. Sign in at http://<this-machine>:{port}/login using your local password.")
        print("Create the password locally first. Use an encrypted tunnel or HTTPS for network access.")
    serve(args.db, host, port, reload=args.reload)


if __name__ == "__main__":
    main()
