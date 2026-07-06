from typing import Optional, List

import typer
from rich.table import Table

from app.db import GetDB, crud
from app.db.models import User
from app.utils.system import readable_size
from app.xray.credential_isolation import (
    credential_fingerprint,
    find_duplicate_credentials,
    repair_duplicate_credentials,
)

from . import utils

app = typer.Typer(no_args_is_help=True)


@app.command(name="list")
def list_users(
    offset: Optional[int] = typer.Option(None, *utils.FLAGS["offset"]),
    limit: Optional[int] = typer.Option(None, *utils.FLAGS["limit"]),
    username: Optional[List[str]] = typer.Option(None, *utils.FLAGS["username"], help="Search by username(s)"),
    search: Optional[str] = typer.Option(None, *utils.FLAGS["search"], help="Search by username/note"),
    status: Optional[crud.UserStatus] = typer.Option(None, *utils.FLAGS["status"]),
    admins: Optional[List[str]] = typer.Option(None, *utils.FLAGS["admin"], help="Search by owner admin's username(s)")
):
    """
    Displays a table of users

    NOTE: Sorting is not currently available.
    """
    with GetDB() as db:
        users: list[User] = crud.get_users(
            db=db, offset=offset, limit=limit,
            usernames=username, search=search, status=status,
            admins=admins
        )

        utils.print_table(
            table=Table(
                "ID", "Username", "Status", "Used traffic",
                "Data limit", "Reset strategy", "Expires at", "Owner",
            ),
            rows=[
                (
                    str(user.id),
                    user.username,
                    user.status.value,
                    readable_size(user.used_traffic),
                    readable_size(user.data_limit) if user.data_limit else "Unlimited",
                    user.data_limit_reset_strategy.value,
                    utils.readable_datetime(user.expire, include_time=False),
                    user.admin.username if user.admin else ''
                )
                for user in users
            ]
        )


@app.command(name="set-owner")
def set_owner(
    username: str = typer.Option(None, *utils.FLAGS["username"], prompt=True),
    admin: str = typer.Option(None, "--admin", "--owner", prompt=True, help="Admin's username"),
    yes_to_all: bool = typer.Option(False, *utils.FLAGS["yes_to_all"], help="Skips confirmations")
):
    """
    Transfers user's ownership

    NOTE: This command needs additional confirmation for users who already have an owner.
    """
    with GetDB() as db:
        user: User = utils.raise_if_falsy(
            crud.get_user(db, username=username), f'User "{username}" not found.')

        dbadmin = utils.raise_if_falsy(
            crud.get_admin(db, username=admin), f'Admin "{admin}" not found.')

        # Ask for confirmation if user already has an owner
        if user.admin and not yes_to_all and not typer.confirm(
            f'{username}\'s current owner is "{user.admin.username}".'
            f' Are you sure about transferring its ownership to "{admin}"?'
        ):
            utils.error("Aborted.")

        crud.set_owner(db, user, dbadmin)

        utils.success(f'{username}\'s owner successfully set to "{admin}".')


@app.command(name="audit-credentials")
def audit_credentials():
    """Lists duplicate runnable proxy credentials by protocol and inbound."""
    with GetDB() as db:
        users = crud.get_users(db=db)
        duplicates = find_duplicate_credentials(users)
        if not duplicates:
            utils.success("No duplicate runnable proxy credentials found.")

        utils.print_table(
            table=Table(
                "Protocol", "Inbound", "Credential Fingerprint", "Users"
            ),
            rows=[
                (
                    duplicate.key.protocol,
                    duplicate.key.inbound_tag,
                    credential_fingerprint(duplicate.key.credential),
                    ", ".join(duplicate.users),
                )
                for duplicate in duplicates
            ],
        )


@app.command(name="repair-credentials")
def repair_credentials(
    yes_to_all: bool = typer.Option(
        False,
        *utils.FLAGS["yes_to_all"],
        help="Skips confirmations",
    ),
):
    """Rotates duplicate runnable proxy credentials. Repaired users must re-pull subscriptions."""
    with GetDB() as db:
        users = crud.get_users(db=db)
        duplicates = find_duplicate_credentials(users)
        if not duplicates:
            utils.success("No duplicate runnable proxy credentials found.")

        if not yes_to_all and not typer.confirm(
            "Rotate duplicate credentials for all but one user in each group?"
        ):
            utils.error("Aborted.")

        repaired = repair_duplicate_credentials(users)
        db.commit()
        utils.success(
            "Rotated credentials for: " + ", ".join(sorted(set(repaired)))
        )
