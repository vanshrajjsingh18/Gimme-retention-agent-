"""Shared response schemas."""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class Message(BaseModel):
    message: str


class OperationResult(BaseModel):
    success: bool = True
    message: str = ""
    detail: dict = Field(default_factory=dict)
