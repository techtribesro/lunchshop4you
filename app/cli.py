"""Manual user administration -- bootstraps the first admin (who can then
use the in-app admin panel for everything else). No self-service signup,
per REQUIREMENTS.md ("Manual password reset only").

Usage:
    python -m app.cli create-user <username> <password> [--admin]
    python -m app.cli reset-password <username> <new-password>
    python -m app.cli make-admin <username>
"""

import argparse

from app.auth import hash_password
from app.db import SessionLocal, init_db
from app.models import User


def create_user(username: str, password: str, is_admin: bool = False) -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            print(f"User '{username}' already exists")
            return
        db.add(User(username=username, password_hash=hash_password(password), is_admin=is_admin))
        db.commit()
        print(f"Created user '{username}'" + (" (admin)" if is_admin else ""))
    finally:
        db.close()


def reset_password(username: str, new_password: str) -> None:
    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            print(f"User '{username}' not found")
            return
        user.password_hash = hash_password(new_password)
        db.commit()
        print(f"Password reset for '{username}'")
    finally:
        db.close()


def make_admin(username: str) -> None:
    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            print(f"User '{username}' not found")
            return
        user.is_admin = True
        db.commit()
        print(f"'{username}' is now an admin")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-user")
    create_parser.add_argument("username")
    create_parser.add_argument("password")
    create_parser.add_argument("--admin", action="store_true")

    reset_parser = subparsers.add_parser("reset-password")
    reset_parser.add_argument("username")
    reset_parser.add_argument("new_password")

    admin_parser = subparsers.add_parser("make-admin")
    admin_parser.add_argument("username")

    args = parser.parse_args()

    if args.command == "create-user":
        create_user(args.username, args.password, is_admin=args.admin)
    elif args.command == "reset-password":
        reset_password(args.username, args.new_password)
    elif args.command == "make-admin":
        make_admin(args.username)


if __name__ == "__main__":
    main()
