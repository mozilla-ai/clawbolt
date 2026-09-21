"""Reusable query and serialization utilities for route handlers.

These wrap the four SQLAlchemy 2.0 idioms that every list-shaped endpoint
repeats: fetch-one-or-404, count-with-filters, fetch-many, and timestamp
serialization. Calling them keeps a handler's body about the query it is
making rather than about the ``(await db.execute(...)).scalars().all()``
scaffolding around it.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, TypeVar

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy.sql import ColumnElement
    from sqlalchemy.sql.elements import SQLColumnExpression
    from sqlalchemy.sql.selectable import Select

T = TypeVar("T")


async def get_or_404_async(
    db: AsyncSession,
    model: type[T],
    detail: str = "Not found",
    **filters: object,
) -> T:
    """Async peer of :func:`get_or_404`.

    Usage::

        user = await get_or_404_async(db, User, detail="User not found", id=user_id)
    """
    row = (await db.execute(select(model).filter_by(**filters))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


async def count_rows(
    db: AsyncSession,
    column: SQLColumnExpression[object],
    *where: ColumnElement[bool],
) -> int:
    """Count rows matching *where*, as an ``int`` that is never ``None``.

    Usage::

        total = await count_rows(db, HeartbeatLog.id, HeartbeatLog.user_id == user_id)
    """
    stmt = select(func.count(column))
    if where:
        stmt = stmt.where(*where)
    return (await db.execute(stmt)).scalar_one() or 0


async def fetch_all(db: AsyncSession, stmt: Select[tuple[T]]) -> list[T]:
    """Run *stmt* and return its entities as a list.

    ``.scalars().all()`` returns a ``Sequence``; response models are typed
    for ``list``, so the conversion happens here instead of at each call.
    """
    return list((await db.execute(stmt)).scalars().all())


def iso(ts: datetime.datetime | None) -> str:
    """Serialize a timestamp, using ``""`` for a missing one."""
    return ts.isoformat() if ts else ""


def iso_or_none(ts: datetime.datetime | None) -> str | None:
    """Serialize a timestamp, preserving ``None`` as ``None``."""
    return ts.isoformat() if ts else None
