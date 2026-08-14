"""Management CLI: ``python -m backend.app.cli <command>``."""

import argparse
import asyncio
import os
import sys

import uvicorn
from sqlalchemy import select

from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.models import AdminAuditLog, Subscription, User
from backend.app.services.admin_audit import AdminAction
from backend.app.services.inactive_cleanup import (
    cleanup_inactive_accounts,
    warn_inactive_users,
)


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the server.

    Worker count is read from ``--workers`` (CLI) which defaults to the
    ``WEB_CONCURRENCY`` env var, which defaults to 2. ``reload`` mode
    forces a single worker (uvicorn requires it) regardless of the
    workers setting.
    """
    workers = 1 if args.reload else max(1, args.workers)
    uvicorn.run(
        "backend.app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=workers,
    )


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Run inactive account cleanup.

    Drives the now-async cleanup helpers from a sync entry point. We create
    a dedicated event loop and close it explicitly rather than calling
    ``asyncio.run``, because ``asyncio.run`` calls ``set_event_loop(None)``
    on exit, which interferes with sync test code that later calls
    ``asyncio.get_event_loop()``. CLIs run in their own process so the
    distinction is invisible at the command line; the test suite catches
    the difference because it calls ``main()`` in-process.
    """

    async def _run() -> None:
        async with db_session_async() as db:
            if args.warn_only:
                count = await warn_inactive_users(db)
                print(f"Warned {count} inactive users")
            else:
                warned = await warn_inactive_users(db)
                deleted = await cleanup_inactive_accounts(db)
                print(f"Warned {warned} users, deleted {deleted} accounts")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


def cmd_promote_env_admins(args: argparse.Namespace) -> None:
    """Promote users listed in ADMIN_USER_IDS to ``Subscription.role='admin'``.

    Migration tool for retiring the env-var fallback. Reads the legacy
    ``ADMIN_USER_IDS`` setting, looks up each user by ``user_id``, and
    sets their subscription role to ``admin``. Idempotent: re-running on
    an already-promoted user is a no-op.

    Each successful promotion writes one ``AdminAuditLog`` row with
    ``action='promote_env_admin'`` and ``admin_user_id=NULL`` (no
    authenticated admin is behind a one-shot operator command). Future
    audits of "who became admin and how" will see env-var promotions
    alongside UI-driven promotions instead of a silent gap.
    """
    admin_ids = settings.admin_user_ids
    if not admin_ids:
        print("ADMIN_USER_IDS is empty, nothing to promote.")
        return

    promoted: list[str] = []
    already_admin: list[str] = []
    no_subscription: list[str] = []
    not_found: list[str] = []

    async def _run() -> None:
        async with db_session_async() as db:
            for user_id in sorted(admin_ids):
                user = (
                    await db.execute(select(User).where(User.user_id == user_id))
                ).scalar_one_or_none()
                if user is None:
                    not_found.append(user_id)
                    continue
                sub = (
                    await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                ).scalar_one_or_none()
                if sub is None:
                    no_subscription.append(user_id)
                    continue
                if sub.role == "admin":
                    already_admin.append(user_id)
                    continue
                sub.role = "admin"
                db.add(
                    AdminAuditLog(
                        admin_user_id=None,
                        admin_email="cli:promote-env-admins",
                        target_user_id=user.id,
                        endpoint="cli promote-env-admins",
                        action=str(AdminAction.PROMOTE_ENV_ADMIN),
                        resource_type="subscription",
                        resource_id=user.id,
                        detail={"source": "env_var_migration_cli", "user_id_external": user_id},
                    )
                )
                promoted.append(user_id)
            await db.commit()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()

    print(f"Promoted to admin: {len(promoted)}")
    for uid in promoted:
        print(f"  + {uid}")
    if already_admin:
        print(f"Already admin: {len(already_admin)}")
        for uid in already_admin:
            print(f"  = {uid}")
    if no_subscription:
        print(f"User exists but has no subscription (skipped): {len(no_subscription)}")
        for uid in no_subscription:
            print(f"  ? {uid}")
    if not_found:
        print(f"Not found in users table (skipped): {len(not_found)}")
        for uid in not_found:
            print(f"  ! {uid}")
    print(
        "\nOnce verified, remove ADMIN_USER_IDS from your environment: "
        "the env-var fallback is no longer consulted at request time."
    )


def main() -> None:
    """Parse CLI arguments and dispatch to subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli",
        description="Clawbolt management commands",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the server")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("WEB_CONCURRENCY", "2")),
        help="Number of uvicorn workers (env: WEB_CONCURRENCY, default 2).",
    )

    # cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up inactive accounts")
    cleanup_parser.add_argument("--warn-only", action="store_true", help="Only warn, do not delete")

    # promote-env-admins
    subparsers.add_parser(
        "promote-env-admins",
        help="One-shot migration: promote ADMIN_USER_IDS users to Subscription.role='admin'",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "serve": cmd_serve,
        "cleanup": cmd_cleanup,
        "promote-env-admins": cmd_promote_env_admins,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
