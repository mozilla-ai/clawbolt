"""Reusable query utilities for FastAPI route handlers."""

from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Session

T = TypeVar("T")


def get_or_404(
    db: Session,
    model: type[T],
    detail: str = "Not found",
    **filters: object,
) -> T:
    """Query for a single row by filter or raise HTTP 404.

    Usage::

        user = get_or_404(db, User, detail="User not found", id=user_id)
    """
    row = db.query(model).filter_by(**filters).first()
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row
