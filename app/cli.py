"""Manual user administration for a 6-7 person office -- no self-service
password reset flow, per REQUIREMENTS.md ("Manual password reset only").

Usage:
    python -m app.cli create-user <username> <password>
    python -m app.cli reset-password <username> <new-password>
"""

import sys

from app.auth import hash_password
from app.db import SessionLocal, init_db
from app.models import User


def create_user(username: str, password: str) -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            print(f"User '{username}' already exists")
            return
        db.add(User(username=username, password_hash=hash_password(password)))
        db.commit()
        print(f"Created user '{username}'")
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


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in ("create-user", "reset-password"):
        print(__doc__)
        sys.exit(1)

    command, username, password = sys.argv[1], sys.argv[2], sys.argv[3]
    if command == "create-user":
        create_user(username, password)
    else:
        reset_password(username, password)


if __name__ == "__main__":
    main()
