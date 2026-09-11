from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import sync_env_admin
from app.db import SessionLocal, init_db
from app.routers import admin, auth, dashboard, menu, orders, pages, telegram
from app.routers.pages import POST_LOGIN_PATH
from app.services.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        sync_env_admin(db)
    finally:
        db.close()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Lunch Ordering", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(menu.router)
app.include_router(orders.router)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(telegram.router)
app.include_router(pages.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    # Bare RedirectResponse -> 307, which is what this route has always
    # returned; only the destination changes here. The page routes in
    # app.routers.pages use an explicit 303, so the two are not the same.
    return RedirectResponse(POST_LOGIN_PATH)
