"""Serialization keys for AppFolio write tools.

The agent runs every approved tool call from one model turn concurrently.
Each AppFolio write targets a single work order, and several of them can be
emitted together in one turn against the *same* work order:

* ``appfolio_update_work_order_status`` racing
  ``appfolio_undo_work_order_status`` leaves the status at whichever request
  AppFolio happened to serve last.
* ``appfolio_update_note`` racing the ``appfolio_add_note`` whose id it is
  editing reaches AppFolio before that note exists.
* ``appfolio_create_invoice`` racing ``appfolio_upload_invoice_pdf`` bills
  the same work order twice.

Keying on ``work_order_id`` serializes writes to one work order while
leaving writes to *different* work orders parallel, which is the common
case when the agent works through a batch of jobs.
"""

from __future__ import annotations

from typing import Any

_WORK_ORDER_GROUP_PREFIX = "appfolio_work_order"


def work_order_concurrency_key(args: dict[str, Any]) -> str | None:
    """Resolve the serialization key for a work-order-scoped AppFolio write.

    Returns ``None`` when the call carries no ``work_order_id``, which
    leaves that call unserialized rather than lumping every id-less write
    into one bucket. Every write tool declares ``work_order_id`` as a
    required parameter, so this is a defensive fallback rather than an
    expected path.
    """
    work_order_id = args.get("work_order_id")
    if work_order_id is None or str(work_order_id).strip() == "":
        return None
    return f"{_WORK_ORDER_GROUP_PREFIX}:{str(work_order_id).strip()}"
