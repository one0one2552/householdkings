"""Pydantic schemas for validation and serialization."""

from datetime import date
from enum import Enum
from pydantic import BaseModel, Field


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"


# ---------- User Schemas ----------

class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4, max_length=128)
    role: UserRole = UserRole.USER
    daily_capacity_minutes: int = Field(default=480, ge=0, le=1440)


class UserUpdate(BaseModel):
    username: str | None = None
    role: UserRole | None = None
    daily_capacity_minutes: int | None = Field(default=None, ge=0, le=1440)
    password: str | None = Field(default=None, min_length=4, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    role: UserRole
    daily_capacity_minutes: int

    model_config = {"from_attributes": True}


# ---------- Task Schemas ----------

class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    base_duration_minutes: int = Field(default=30, ge=1, le=1440)
    is_recurring: bool = False
    recurrence_rule: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    base_duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    is_recurring: bool | None = None
    recurrence_rule: str | None = None


class TaskOut(BaseModel):
    id: str
    title: str
    base_duration_minutes: int
    is_recurring: bool
    recurrence_rule: str | None

    model_config = {"from_attributes": True}


# ---------- TaskInstance Schemas ----------

class TaskInstanceCreate(BaseModel):
    task_id: str
    date: date
    assigned_user_ids: list[str] = []


class TaskInstanceUpdate(BaseModel):
    status: TaskStatus | None = None
    assigned_user_ids: list[str] | None = None


class TaskInstanceOut(BaseModel):
    id: str
    task_id: str
    date: date
    status: TaskStatus
    assigned_users: list[UserOut] = []

    model_config = {"from_attributes": True}


# ---------- Statistics ----------

class UserDayStat(BaseModel):
    user_id: str
    username: str
    date: date
    total_minutes: float
    capacity_minutes: int
    over_capacity: bool


class UserPeriodStat(BaseModel):
    user_id: str
    username: str
    total_minutes: float
    capacity_minutes_per_day: int
    days_over_capacity: int
