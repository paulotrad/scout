"""Manage Scout admin roles locally; never expose this command over HTTP."""

from __future__ import annotations

import argparse

from marketplace_alerts import grant_admin, list_admin_emails, revoke_admin


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Scout admin emails")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("add", "remove"):
        commands.add_parser(action).add_argument("email")
    commands.add_parser("list")
    args = parser.parse_args()
    if args.action == "add":
        grant_admin(args.email)
        print("Admin added.")
    elif args.action == "remove":
        print("Admin removed." if revoke_admin(args.email) else "Email was not an admin.")
    else:
        for email in list_admin_emails():
            print(email)


if __name__ == "__main__":
    main()
