from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timezone import now_local_naive


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local_naive)

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local_naive)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_local_naive)

    user: Mapped["User"] = relationship(back_populates="sessions")


class MenuItem(Base):
    __tablename__ = "menu"
    __table_args__ = (
        UniqueConstraint("week_start", "day", "category", "item_name", name="uq_menu_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    day: Mapped[str] = mapped_column(String(16))  # Monday..Friday
    category: Mapped[str] = mapped_column(String(32))  # Polevka, Hlavni jidlo 1, ...
    item_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(1024), default="")
    price_czk: Mapped[int] = mapped_column(Integer)
    calories_kcal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_at: Mapped[datetime] = mapped_column(DateTime, default=now_local_naive)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("user_id", "order_date", "item_name", name="uq_order_line"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_date: Mapped[date] = mapped_column(Date, index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    item_name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_czk: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(255), default="")
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=now_local_naive)

    user: Mapped["User"] = relationship(back_populates="orders")


class EarlyOrderingWindow(Base):
    """A date an admin has manually opened for ordering ahead of time,
    bypassing the normal "must be today, before cutoff" rule -- e.g.
    opening tomorrow's ordering the evening before."""

    __tablename__ = "early_ordering_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    opened_by: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=now_local_naive)
