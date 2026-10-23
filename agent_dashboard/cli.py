import argparse
from pathlib import Path

from . import db
from .config import DEFAULT_DB_PATH, DEFAULT_PORT
from .ingest import ingest


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-dashboard", description="Per-prompt cost and time dashboard for AI agents")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database (default: {DEFAULT_DB_PATH})")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="run the dashboard (default)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    p_ingest = sub.add_parser("ingest", help="import transcripts once and exit")
    p_ingest.add_argument("--force", action="store_true", help="re-parse every session (e.g. after a price change)")

    args = parser.parse_args()
    if args.cmd == "ingest":
        conn = db.connect(args.db)
        print(ingest(conn, force=args.force))
        return

    from .server import serve

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", DEFAULT_PORT)
    print(f"Agent dashboard on http://{host}:{port}  (db: {args.db})")
    serve(args.db, host, port)


if __name__ == "__main__":
    main()
