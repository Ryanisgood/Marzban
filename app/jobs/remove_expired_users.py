import logging

from app import logger, scheduler, xray
from app.db import GetDB, crud
from app.models.admin import Admin
from app.utils import report
from app.xray.credential_isolation import build_user_removal_plan
from config import USER_AUTODELETE_INCLUDE_LIMITED_ACCOUNTS

SYSTEM_ADMIN = Admin(username='system', is_sudo=True, telegram_id=None, discord_webhook=None)


def remove_expired_users():
    with GetDB() as db:
        deleted_users = crud.get_autodeletable_expired_users(
            db, USER_AUTODELETE_INCLUDE_LIMITED_ACCOUNTS
        )
        removal_plan = build_user_removal_plan(deleted_users)
        if deleted_users:
            crud.remove_users(db, deleted_users)
            xray.operations.remove_users_from_runtime(removal_plan)

        for user in deleted_users:
            report.user_deleted(user.username, SYSTEM_ADMIN,
                                user_admin=Admin.model_validate(user.admin) if user.admin else None
                                )
            logger.log(logging.INFO, "Expired user %s deleted." % user.username)


scheduler.add_job(remove_expired_users, 'interval', coalesce=True, hours=6, max_instances=1)
