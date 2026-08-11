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

    model_config = {"from_attributes": True}


class OrderLineIn(BaseModel):
    item_name: str
    quantity: int
    note: str = ""


class OrderSubmitRequest(BaseModel):
    items: list[OrderLineIn]


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


class DashboardResponse(BaseModel):
    rows: list[DashboardRow]
    week_aggregate_czk: int
    month_aggregate_czk: int
