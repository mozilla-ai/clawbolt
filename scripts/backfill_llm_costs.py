#!/usr/bin/env python3
"""Backfill cost on ``llm_usage_logs`` rows that were logged as $0.

When ``genai-prices`` does not know a (provider, model) pair,
``services.llm_pricing.compute_cost`` falls through to
``Decimal('0.000000')`` and the row is persisted with ``cost=0`` while
the token counts are recorded correctly. This happened in production for
``anthropic/claude-opus-4-8``: the pinned ``genai-prices`` predated the
model, so every usage row for it showed $0 in the admin panel (see the
regression added to ``tests/test_llm_pricing.py``).

Bumping ``genai-prices`` fixes rows written *after* the deploy. This
script repairs the historical rows: it recomputes ``cost`` from the
token counts already stored on each row using the current (bumped)
pricing data and updates the rows in place.

Safety model:

- **Dry-run by default.** It reports what it would change and writes
  nothing. Pass ``--apply`` to commit.
- **Only touches ``cost=0`` rows** with at least one non-zero token
  count. Rows that already carry a cost are never re-priced, so a
  library rate change since the original write cannot retroactively
  rewrite settled costs.
- **Only writes when the recomputed cost is > 0.** A row whose model is
  still unknown to ``genai-prices`` stays at 0, so the run is a safe
  no-op against stale pricing data.
- **Idempotent.** Re-running finds nothing to do once applied.

Usage::

    # Preview every $0 row that is now priceable (no writes):
    DATABASE_URL=postgresql://user:pass@host:5432/db \\
      uv run python scripts/backfill_llm_costs.py --model claude-opus-4-8

    # Apply:
    DATABASE_URL=... uv run python scripts/backfill_llm_costs.py \\
      --model claude-opus-4-8 --apply

Omit ``--model`` to sweep every $0 row regardless of model. The
``--model`` guard refuses to run if the model is still unpriced, which
catches "I forgot to bump genai-prices first".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select

from backend.app.database import db_session_async
from backend.app.models import LLMUsageLog
from backend.app.services.llm_pricing import compute_cost, is_known_model


@dataclass
class BackfillResult:
    """Outcome of a backfill sweep."""

    scanned: int
    updated: int
    total_added: Decimal
    applied: bool


async def backfill_costs(
    *,
    model: str | None = None,
    provider: str | None = None,
    batch_size: int = 500,
    apply: bool = False,
) -> BackfillResult:
    """Recompute cost for ``cost=0`` usage rows and optionally persist.

    Scans ``llm_usage_logs`` in ascending-id keyset batches so the
    working set stays bounded on tables with millions of rows. Within a
    batch every row is re-priced via ``compute_cost`` using its stored
    token counts (including the cache buckets); rows whose recomputed
    cost is still 0 are left untouched.

    Returns counts and the total dollar amount that was (or, in dry-run,
    would be) added. ``apply=False`` performs no writes.
    """
    scanned = 0
    updated = 0
    total_added = Decimal("0.000000")
    after_id = 0

    while True:
        async with db_session_async() as session:
            stmt = (
                select(LLMUsageLog)
                .where(
                    LLMUsageLog.cost == 0,
                    or_(LLMUsageLog.input_tokens > 0, LLMUsageLog.output_tokens > 0),
                    LLMUsageLog.id > after_id,
                )
                .order_by(LLMUsageLog.id)
                .limit(batch_size)
            )
            if model is not None:
                stmt = stmt.where(LLMUsageLog.model == model)
            if provider is not None:
                stmt = stmt.where(LLMUsageLog.provider == provider)

            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                break
            after_id = rows[-1].id

            batch_dirty = False
            for row in rows:
                scanned += 1
                new_cost = compute_cost(
                    row.model,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    provider=row.provider,
                    cache_creation_input_tokens=row.cache_creation_input_tokens,
                    cache_read_input_tokens=row.cache_read_input_tokens,
                )
                if new_cost <= 0:
                    continue
                updated += 1
                total_added += new_cost
                if apply:
                    row.cost = new_cost
                    batch_dirty = True

            if apply and batch_dirty:
                await session.commit()

    return BackfillResult(
        scanned=scanned,
        updated=updated,
        total_added=total_added,
        applied=apply,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--model",
        default=None,
        help="Only backfill this model (e.g. claude-opus-4-8). Default: all models.",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Only backfill this provider (e.g. anthropic). Default: all providers.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows scanned per keyset batch (default 500).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the updates. Without this flag the run is a dry-run.",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    # Guard: if a specific model was requested and pricing still does not
    # know it, the run would be a silent no-op. Fail loudly so the
    # operator bumps genai-prices first instead of thinking it worked.
    if args.model is not None and not is_known_model(args.model, provider=args.provider or ""):
        print(
            f"error: genai-prices does not know model={args.model!r} "
            f"(provider={args.provider or 'auto'!r}). Bump genai-prices before backfilling.",
            file=sys.stderr,
        )
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    target = args.model or "<all models>"
    print(f"[{mode}] backfilling $0 rows for {target} (provider={args.provider or 'all'})")

    result = await backfill_costs(
        model=args.model,
        provider=args.provider,
        batch_size=args.batch_size,
        apply=args.apply,
    )

    print(f"  scanned (priceable-candidate rows): {result.scanned}")
    print(f"  rows {'updated' if result.applied else 'that would update'}: {result.updated}")
    print(
        f"  total cost {'added' if result.applied else 'that would be added'}: ${result.total_added}"
    )
    if not result.applied and result.updated:
        print("  (dry-run: no writes. Re-run with --apply to commit.)")
    return 0


def main() -> int:
    args = _parse_args(sys.argv[1:])
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
