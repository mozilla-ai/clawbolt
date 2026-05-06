"""Reusable query utilities for FastAPI route handlers."""

from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def get_or_404(
    db: AsyncSession,
    model: type[T],
    detail: str = "Not found",
    **filters: object,
) -> T:
    """Query for a single row by filter or raise HTTP 404.

    Usage::

        user = await get_or_404(db, User, detail="User not found", id=user_id)
    """
    row = (await db.execute(select(model).filter_by(**filters))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row
