"""Command-line entry points."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iluvtrade")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("init-db", help="Create every table.")
    sub.add_parser("demo", help="Run the end-to-end demonstration workflow.")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("iluvtrade.main:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "init-db":
        from iluvtrade.db.session import create_all

        create_all()
        print("Database initialised.")
        return 0

    if args.command == "demo":
        from iluvtrade.demo import run_demo

        return run_demo()

    return 1


if __name__ == "__main__":
    sys.exit(main())
