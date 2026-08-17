from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.auth import create_session, destroy_session, get_current_user, verify_password
from app.db import get_db
from app.models import User
from app.schemas import LoginRequest

router = APIRouter(tags=["auth"])


@router.get("/users", response_model=list[str])
def list_usernames(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Just usernames -- backs the "Objednávám za" pill row, visible to
    every logged-in user (not admin-gated like /admin/users, which also
    exposes role/created_at for account management)."""
    return [u.username for u in db.query(User).order_by(User.username).all()]


@router.post("/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    create_session(db, user, response)
    return {"username": user.username}


@router.post("/logout")
def logout(response: Response, lunch_session: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    if lunch_session:
        destroy_session(db, lunch_session, response)
    return {"ok": True}
