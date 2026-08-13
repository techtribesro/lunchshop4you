from datetime import date, datetime

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class MenuItemOut(BaseModel):
    day: str
    category: str
    item_name: str
    description: str
    price_czk: int
    calories_kcal: int | None

    model_config = {"from_attributes": True}


class OrderLineIn(BaseModel):
    item_name: str
    quantity: int
    note: str = ""


class OrderSubmitRequest(BaseModel):
    items: list[OrderLineIn]
    order_date: date | None = None


class OrderLineOut(BaseModel):
    item_name: str
    quantity: int
    unit_price_czk: int
    note: str
    order_date: date
    submitted_at: datetime

    model_config = {"from_attributes": True}


class DashboardRow(BaseModel):
    user: str
    order_date: date
    items_ordered: int
    daily_total_czk: int
    week_total_czk: int
    month_total_czk: int
    daily_total_kcal: int
    week_total_kcal: int
    month_total_kcal: int


class DashboardResponse(BaseModel):
    rows: list[DashboardRow]
    week_aggregate_czk: int
    month_aggregate_czk: int
    week_aggregate_kcal: int
    month_aggregate_kcal: int


class AdminUserOut(BaseModel):
    username: str
    is_admin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TelegramSubscriberOut(BaseModel):
    chat_id: str
    display_name: str
    subscribed_at: datetime

    model_config = {"from_attributes": True}


class AdminAddTelegramSubscriberRequest(BaseModel):
    chat_id: str
    display_name: str = ""


class AdminCreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class AdminResetPasswordRequest(BaseModel):
    new_password: str


class AdminCaloriesEntry(BaseModel):
    day: str
    item_name: str
    calories_kcal: int


class AdminSetCaloriesRequest(BaseModel):
    items: list[AdminCaloriesEntry]


class AdminPriceEntry(BaseModel):
    day: str
    item_name: str
    price_czk: int


class AdminSetPricesRequest(BaseModel):
    items: list[AdminPriceEntry]


class AdminAssignOrderRequest(BaseModel):
    username: str
    order_date: date
    items: list[OrderLineIn]
