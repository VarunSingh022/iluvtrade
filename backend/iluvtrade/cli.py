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

    rotate = sub.add_parser(
        "rotate-credentials",
        help="Re-seal stored broker credentials under the current secret key.",
    )
    rotate.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be rotated without writing anything.",
    )

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

    if args.command == "rotate-credentials":
        from iluvtrade.brokers.service import rotate_credentials
        from iluvtrade.db.session import session_scope

        with session_scope() as session:
            report = rotate_credentials(session, dry_run=args.dry_run)

        prefix = "Would rotate" if args.dry_run else "Rotated"
        print(f"{prefix} {report.rotated} of {report.examined} stored credential(s).")
        print(f"  already under the current key: {report.already_current}")
        if report.failed:
            print(f"  FAILED: {len(report.failed)} — left untouched:")
            for connection_id, reason in report.failed:
                print(f"    {connection_id}: {reason}")
            print(
                "\n  The usual cause is a missing retired key. Add the previous "
                "ILUVTRADE_SECRET_KEY\n  to ILUVTRADE_RETIRED_SECRET_KEYS and run this again."
            )
            return 1
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
