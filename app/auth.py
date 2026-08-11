import secrets
from datetime import timedelta

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User, UserSession
from app.timezone import now_local_naive

SESSION_COOKIE_NAME = "lunch_session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_session(db: Session, user: User, response: Response) -> str:
    token = secrets.token_urlsafe(32)
    session = UserSession(token=token, user_id=user.id)
    db.add(session)
    db.commit()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_idle_timeout_hours * 3600,
    )
    return token


def destroy_session(db: Session, token: str, response: Response) -> None:
    db.query(UserSession).filter(UserSession.token == token).delete()
    db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME)


def get_current_user_optional(
    lunch_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """Same lookup as get_current_user, but returns None instead of raising
    -- used by page routes so an anonymous visit renders a redirect to the
    login page rather than a bare 401 JSON body."""
    if not lunch_session:
        return None

    session = db.query(UserSession).filter(UserSession.token == lunch_session).first()
    if session is None:
        return None

    idle_limit = timedelta(hours=settings.session_idle_timeout_hours)
    if now_local_naive() - session.last_seen_at > idle_limit:
        db.delete(session)
        db.commit()
        return None

    session.last_seen_at = now_local_naive()
    db.commit()

    return db.query(User).filter(User.id == session.user_id).first()


def get_current_user(
    user: User | None = Depends(get_current_user_optional),
) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return user


def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


def sync_env_admin(db: Session) -> None:
    """Ensures ADMIN_USERNAME/ADMIN_PASSWORD (if set) always exists as an
    admin, re-synced on every startup -- so the admin account is entirely
    env-config-driven, no CLI bootstrap step required."""
    if not settings.admin_username or not settings.admin_password:
        return

    user = db.query(User).filter(User.username == settings.admin_username).first()
    if user is None:
        user = User(username=settings.admin_username, password_hash=hash_password(settings.admin_password), is_admin=True)
        db.add(user)
    else:
        user.password_hash = hash_password(settings.admin_password)
        user.is_admin = True
    db.commit()
