"""Notification responses (FR-08.2, FR-08.3)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: int
    kind: str
    message: str
    case_id: uuid.UUID | None
    created_at: datetime
    read: bool


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    total: int
    page: int
    page_size: int


class UnreadCount(BaseModel):
    count: int


class MarkedRead(BaseModel):
    marked: int
