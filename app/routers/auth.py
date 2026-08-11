from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.auth import create_session, destroy_session, verify_password
from app.db import get_db
from app.models import User
from app.schemas import LoginRequest

router = APIRouter(tags=["auth"])


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
