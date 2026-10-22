"""Authorise an email address to sign in to FabricBI.

Sign-in is Microsoft-only, so there is no password to set. This adds the row in
`app_users` that lets Entra sign-in through — without it, an authenticated
tenant user is still refused.

    python scripts/create_user.py <email> [role]

Use the address exactly as Entra presents it (the UPN), lowercased.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import ROLES, audit, create_app_user, init_db


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: python create_user.py <email> [role]")
        print(f"       role: {' | '.join(ROLES)}  (default: editor)")
        return 1

    email = sys.argv[1].lower().strip()
    role = sys.argv[2].lower().strip() if len(sys.argv) == 3 else "editor"

    if "@" not in email:
        print("Error: not a valid email.")
        return 1
    if role not in ROLES:
        print(f"Error: invalid role '{role}'. Choose from: {', '.join(ROLES)}")
        return 1

    init_db()
    try:
        # No password hash: the column is legacy and nothing reads it.
        create_app_user(email, "", role=role)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    audit("user.create", target=email, details=f"role={role} (sso)", user_email="cli")
    print(f"Authorised: {email}  (role: {role})")
    print("They sign in with Microsoft; no password is issued.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
