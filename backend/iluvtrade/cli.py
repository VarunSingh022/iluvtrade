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

    reset = sub.add_parser(
        "issue-password-reset",
        help="Mint a password-reset token for one account and print it once.",
        description=(
            "For a deployment with no email delivery. The token is printed once, for "
            "an administrator to pass to the account holder through a channel they "
            "trust. It expires in one hour, works once, and supersedes any "
            "outstanding token for the same account. Running this is recorded in the "
            "audit trail."
        ),
    )
    reset.add_argument("email", help="The account's email address.")

    backup = sub.add_parser(
        "backup",
        help="Write a consistent copy of the database.",
        description=(
            "For SQLite, uses the online backup API: consistent even while the "
            "application is running and writing. For PostgreSQL, prints the pg_dump "
            "command to run rather than shelling out to a tool that may not be "
            "installed and embedding credentials in a process argument."
        ),
    )
    backup.add_argument("output", help="Where to write the backup file.")
    backup.add_argument(
        "--verify",
        action="store_true",
        help="After writing, open the copy and report its schema revision and row counts.",
    )

    config = sub.add_parser(
        "check-config",
        help="Report whether the current environment is safe to start in production.",
    )
    config.add_argument(
        "--environment",
        help="Check as if ILUVTRADE_ENVIRONMENT were this, without changing it.",
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

    if args.command == "issue-password-reset":
        from sqlalchemy import select

        from iluvtrade.db.models.platform import User
        from iluvtrade.db.session import session_scope
        from iluvtrade.platform import audit, passwords

        email = args.email.strip().lower()
        with session_scope() as session:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                # A CLI run by someone with shell access is not an enumeration
                # oracle, so this says plainly what happened. The API endpoint,
                # which anyone can reach, deliberately does not.
                print(f"No account exists for {email}.", file=sys.stderr)
                return 1
            issued = passwords.issue(session, user)
            organization_id = passwords._audit_organization(session, user.id)
            if organization_id:
                audit.record(
                    session,
                    organization_id=organization_id,
                    action="user.password_reset_issued_by_operator",
                    resource_type="user",
                    resource_id=user.id,
                    actor_user_id=None,
                    payload={"email": email, "channel": "cli"},
                )
            token, expires_at = issued.token, issued.expires_at

        print(f"Reset token for {email}:\n")
        print(f"  {token}\n")
        print(f"Valid until {expires_at.isoformat()}. Single use.")
        print(
            "Give this to the account holder over a channel you trust. They enter it "
            "on the sign-in screen under 'Reset password'. It is not stored anywhere "
            "in readable form and cannot be printed again."
        )
        return 0

    if args.command == "backup":
        import sqlite3
        from pathlib import Path

        from iluvtrade.config import get_settings

        settings = get_settings()
        if not settings.uses_sqlite:
            # Deliberately not shelling out. pg_dump may not be installed, its
            # version has to match the server's, and passing a URL containing a
            # password as a process argument puts it in every process listing on
            # the host.
            print(
                "This deployment does not use SQLite, so there is nothing for this "
                "command to copy safely.\n\nUse pg_dump, which is the supported path:\n\n"
                f'  pg_dump --format=custom --file={args.output} "$ILUVTRADE_DATABASE_URL"\n\n'
                "Restore with pg_restore --clean --if-exists. See docs/DEPLOYMENT.md.",
                file=sys.stderr,
            )
            return 1

        source_path = settings.database_url.split("///", 1)[-1]
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)

        # The online backup API, not a file copy. Copying the file of a live
        # SQLite database can capture a torn write or miss the write-ahead log;
        # this takes a consistent snapshot while the application keeps running.
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        size = destination.stat().st_size
        print(f"Wrote {destination} ({size:,} bytes) from {source_path}.")

        if args.verify:
            check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
            try:
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                revision = check.execute("SELECT version_num FROM alembic_version").fetchone()
                tables = [
                    row[0]
                    for row in check.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ]
                print(f"  integrity_check: {integrity}")
                print(f"  schema revision: {revision[0] if revision else 'UNKNOWN'}")
                print(f"  tables:          {len(tables)}")
                # Literal statements rather than an interpolated table name.
                # Nothing here is user input, but a formatted SQL string is the
                # shape the next person copies, and the structural check in
                # tests/security/test_code_execution_surface.py refuses it for
                # exactly that reason.
                counts = {
                    "users": "SELECT count(*) FROM users",
                    "organizations": "SELECT count(*) FROM organizations",
                    "audit_events": "SELECT count(*) FROM audit_events",
                    "trading_sessions": "SELECT count(*) FROM trading_sessions",
                }
                for name, statement in counts.items():
                    if name in tables:
                        print(f"    {name:20} {check.execute(statement).fetchone()[0]}")
            finally:
                check.close()
            if integrity != "ok":
                print("Integrity check FAILED — do not rely on this backup.", file=sys.stderr)
                return 1
        return 0

    if args.command == "check-config":
        import os

        from iluvtrade.config import Settings

        overrides = {"environment": args.environment} if args.environment else {}
        settings = Settings(**overrides)
        problems = settings.deployment_problems()
        print(f"environment:      {settings.environment}")
        print(f"database:         {settings.database_url.split('://')[0]}")
        key_origin = "generated per-process" if settings.secret_key_was_generated else "configured"
        schema_origin = "on" if settings.should_create_tables else "off (Alembic owns it)"
        print(f"secret key:       {key_origin}")
        print(f"schema creation:  {schema_origin}")
        print(f"rate limiting:    {'on' if settings.rate_limit_enabled else 'off'}")
        print(f"live trading:     {'enabled' if settings.live_trading_enabled else 'disabled'}")
        print(f"payments:         {settings.payment_provider}")
        if not problems:
            print("\nNo unsafe settings for this environment.")
            return 0
        print(f"\n{len(problems)} unsafe setting(s) for production:")
        for problem in problems:
            print(f"  - {problem}")
        _ = os
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
