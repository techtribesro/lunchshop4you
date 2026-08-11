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
