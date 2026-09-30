"""Command line helpers, e.g. to make an existing user of a shared user table an administrator.

    python -m app.cli grant-admin <username>
    python -m app.cli list-users
"""

import argparse
import sys

from .config import Settings
from .db import Database
from .main import create_user_store
from .userstore import ADMIN_ROLE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant-admin", help="Rolle 'Administrator' an einen Benutzer vergeben")
    grant.add_argument("username")
    sub.add_parser("list-users", help="Benutzer mit ihren Rollen anzeigen")
    args = parser.parse_args(argv)

    settings = Settings()
    users = create_user_store(settings, Database(settings.db_path))
    roles = {role["id"]: role["name"] for role in users.list_roles()}

    if args.command == "list-users":
        for user in users.list_users():
            names = ", ".join(roles[r] for r in user["role_ids"]) or "-"
            print(f"{user['id']:>5}  {user['username']:<30}  {names}")
        return 0

    user = users.get_user_by_name(args.username)
    if user is None:
        print(f"Benutzer {args.username!r} nicht gefunden", file=sys.stderr)
        return 1
    admin_id = next((rid for rid, name in roles.items() if name == ADMIN_ROLE), None)
    if admin_id is None:
        admin_id = users.save_role(None, ADMIN_ROLE, "Vollzugriff auf alle Anwendungen", ["*"])
    current = next(u for u in users.list_users() if u["id"] == user["id"])["role_ids"]
    users.set_user_roles(user["id"], current + [admin_id])
    print(f"{args.username} ist jetzt Administrator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
