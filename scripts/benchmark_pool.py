"""Benchmark the async_sessionmaker pool under load.

Issue #1179. Drives N concurrent queries against an async engine across a
matrix of ``pool_size`` and ``max_overflow`` values, then writes a markdown
report. The goal is to pick explicit pool tuning values for production
that keep p99 connection-acquisition latency low under the worst-case
concurrency the box actually sees.

Usage::

    DATABASE_URL=postgresql://clawbolt:clawbolt@localhost:5432/clawbolt \\
        uv run python scripts/benchmark_pool.py

    # Optional flags:
    #   --workers   Comma-separated list of concurrent worker counts.
    #   --query-ms  Simulated query duration via pg_sleep, in ms.
    #   --iters     How many queries each worker issues.
    #   --output    Path for the markdown report (default: scripts/benchmark_pool_report.md).

The script does not modify the production engine. It builds throwaway
async engines per configuration so the matrix stays isolated.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Default matrices. The product covers the realistic range we expect under
# the async migration: from "tighter than today" (5/0) up to "double the
# current worst case" (30/10).
DEFAULT_POOL_SIZES: tuple[int, ...] = (5, 10, 20, 30)
DEFAULT_MAX_OVERFLOWS: tuple[int, ...] = (0, 5, 10)
DEFAULT_WORKERS: tuple[int, ...] = (10, 25, 50, 100)


def _async_database_url(url: str) -> str:
    """Translate a sync postgres URL to its asyncpg equivalent."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    return url


@dataclass
class RunResult:
    pool_size: int
    max_overflow: int
    workers: int
    iters: int
    query_ms: int
    total_seconds: float
    acquire_p50_ms: float
    acquire_p95_ms: float
    acquire_p99_ms: float
    acquire_max_ms: float
    throughput_qps: float
    errors: int
    notes: str = ""


@dataclass
class RunConfig:
    pool_size: int
    max_overflow: int
    workers: int
    iters: int
    query_ms: int
    acquire_latencies: list[float] = field(default_factory=list)
    errors: int = 0


async def _worker(
    factory: async_sessionmaker,
    cfg: RunConfig,
    query_sql: str,
) -> None:
    for _ in range(cfg.iters):
        t0 = time.perf_counter()
        try:
            async with factory() as session:
                acquire_ms = (time.perf_counter() - t0) * 1000.0
                cfg.acquire_latencies.append(acquire_ms)
                await session.execute(text(query_sql))
        except Exception:
            cfg.errors += 1


async def _run_one(
    database_url: str,
    pool_size: int,
    max_overflow: int,
    workers: int,
    iters: int,
    query_ms: int,
) -> RunResult:
    engine = create_async_engine(
        _async_database_url(database_url),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=False,
        # Surface pool-acquisition stalls quickly instead of letting them hang
        # the benchmark indefinitely.
        pool_timeout=30,
    )
    factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cfg = RunConfig(
        pool_size=pool_size,
        max_overflow=max_overflow,
        workers=workers,
        iters=iters,
        query_ms=query_ms,
    )
    # pg_sleep(N) takes seconds. Convert ms to fractional seconds.
    query_sql = f"SELECT pg_sleep({query_ms / 1000.0:.4f})"

    # Warm up the pool with one cheap query so connection establishment
    # latency is not folded into the first worker's first iteration.
    async with factory() as session:
        await session.execute(text("SELECT 1"))

    t_start = time.perf_counter()
    await asyncio.gather(*[_worker(factory, cfg, query_sql) for _ in range(workers)])
    total = time.perf_counter() - t_start
    await engine.dispose()

    if cfg.acquire_latencies:
        sorted_l = sorted(cfg.acquire_latencies)
        p50 = statistics.median(sorted_l)
        p95 = sorted_l[max(0, int(len(sorted_l) * 0.95) - 1)]
        p99 = sorted_l[max(0, int(len(sorted_l) * 0.99) - 1)]
        p_max = sorted_l[-1]
    else:
        p50 = p95 = p99 = p_max = 0.0

    completed = workers * iters - cfg.errors
    throughput = completed / total if total > 0 else 0.0

    notes = ""
    if workers > pool_size + max_overflow:
        notes = "saturated (workers > pool+overflow)"

    return RunResult(
        pool_size=pool_size,
        max_overflow=max_overflow,
        workers=workers,
        iters=iters,
        query_ms=query_ms,
        total_seconds=total,
        acquire_p50_ms=p50,
        acquire_p95_ms=p95,
        acquire_p99_ms=p99,
        acquire_max_ms=p_max,
        throughput_qps=throughput,
        errors=cfg.errors,
        notes=notes,
    )


def _format_report(
    results: list[RunResult],
    *,
    database_url_redacted: str,
    pool_sizes: tuple[int, ...],
    max_overflows: tuple[int, ...],
    workers_list: tuple[int, ...],
    iters: int,
    query_ms: int,
) -> str:
    lines: list[str] = []
    lines.append("# async_sessionmaker pool benchmark")
    lines.append("")
    lines.append("_Generated by `scripts/benchmark_pool.py` for issue #1179._")
    lines.append("")
    lines.append("## Setup")
    lines.append("")
    lines.append(f"- Database URL: `{database_url_redacted}`")
    lines.append(f"- Pool sizes: {list(pool_sizes)}")
    lines.append(f"- Max overflows: {list(max_overflows)}")
    lines.append(f"- Worker counts: {list(workers_list)}")
    lines.append(f"- Iterations per worker: {iters}")
    lines.append(f"- Simulated query duration (`pg_sleep`): {query_ms} ms")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "For each `(pool_size, max_overflow, workers)` triple, the script builds a fresh "
        "async engine, warms the pool with a cheap `SELECT 1`, then issues `workers * iters` "
        "queries using `asyncio.gather`. Each query runs `pg_sleep(query_ms)` to simulate "
        "real DB time and force connection contention when `workers > pool_size + max_overflow`."
    )
    lines.append("")
    lines.append(
        "We measure connection-acquisition latency (`time.perf_counter()` around "
        "`async with factory()`), total wall-clock duration, and computed QPS. The interesting "
        "signal is p99 acquire latency: when it spikes, the pool is the bottleneck, not the DB."
    )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(
        "| pool_size | max_overflow | workers | total (s) | QPS | acquire p50 (ms) | "
        "acquire p95 (ms) | acquire p99 (ms) | acquire max (ms) | errors | notes |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in results:
        lines.append(
            f"| {r.pool_size} | {r.max_overflow} | {r.workers} | {r.total_seconds:.2f} | "
            f"{r.throughput_qps:.1f} | {r.acquire_p50_ms:.2f} | {r.acquire_p95_ms:.2f} | "
            f"{r.acquire_p99_ms:.2f} | {r.acquire_max_ms:.2f} | {r.errors} | {r.notes} |"
        )
    lines.append("")
    lines.append("## How to read this")
    lines.append("")
    lines.append(
        "- A configuration is comfortable when p99 acquire latency stays in the low-ms range "
        "even with `workers >> pool_size + max_overflow`. If p99 explodes, the pool is the "
        "bottleneck."
    )
    lines.append(
        "- Throughput should plateau at roughly `(pool_size + max_overflow) / query_seconds` "
        "once workers exceed pool capacity. Anything materially below that suggests overhead "
        "(driver, event loop scheduling) is dominating."
    )
    lines.append(
        "- Pick the smallest `(pool_size + max_overflow)` that keeps p99 acquire latency "
        "comfortably below your request budget at the highest realistic concurrency. Bigger "
        "pools are not free: each connection is a server-side process, and Postgres has its "
        "own `max_connections` ceiling shared across all workers."
    )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- Numbers from a localhost Postgres are an upper bound. Network RTT to a hosted "
        "Postgres adds real acquire-time variance; rerun this on a prod-like host before "
        "locking in values."
    )
    lines.append(
        "- `pg_sleep(N)` holds the server-side connection but does no work, so this "
        "exercises pool contention rather than CPU contention. Real query mix (joins, "
        "writes, locks) will shift the throughput plateau."
    )
    lines.append(
        "- Production also runs the sync engine in parallel during the dual-API window. "
        "Total connections per worker = sync pool + async pool; budget against Postgres "
        "`max_connections` accordingly."
    )
    lines.append("")
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--pool-sizes",
        type=str,
        default=",".join(str(x) for x in DEFAULT_POOL_SIZES),
        help="Comma-separated list of pool_size values to sweep.",
    )
    p.add_argument(
        "--max-overflows",
        type=str,
        default=",".join(str(x) for x in DEFAULT_MAX_OVERFLOWS),
        help="Comma-separated list of max_overflow values to sweep.",
    )
    p.add_argument(
        "--workers",
        type=str,
        default=",".join(str(x) for x in DEFAULT_WORKERS),
        help="Comma-separated list of concurrent worker counts to sweep.",
    )
    p.add_argument(
        "--iters",
        type=int,
        default=10,
        help="Number of queries each worker issues per run.",
    )
    p.add_argument(
        "--query-ms",
        type=int,
        default=20,
        help="Simulated query duration in milliseconds (driven by pg_sleep).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "benchmark_pool_report.md",
        help="Path for the markdown report.",
    )
    p.add_argument(
        "--database-url",
        type=str,
        default=os.environ.get("DATABASE_URL"),
        help="Override DATABASE_URL for this run.",
    )
    return p.parse_args(argv)


def _redact_url(url: str) -> str:
    # Strip credentials for the report.
    if "@" not in url:
        return url
    scheme_split = url.split("://", 1)
    if len(scheme_split) != 2:
        return url
    scheme, rest = scheme_split
    _, host = rest.split("@", 1)
    return f"{scheme}://<redacted>@{host}"


async def _amain(args: argparse.Namespace) -> int:
    if not args.database_url:
        print(
            "DATABASE_URL not set and --database-url not provided; "
            "writing a stub report explaining the script ran in a Postgres-less env.",
            file=sys.stderr,
        )
        stub = (
            "# async_sessionmaker pool benchmark\n\n"
            "_Generated by `scripts/benchmark_pool.py` for issue #1179._\n\n"
            "Run skipped: no `DATABASE_URL` was available in this environment. "
            "Re-run on a prod-like host with Postgres reachable to populate the "
            "results table.\n"
        )
        args.output.write_text(stub)
        return 0

    pool_sizes = tuple(int(x) for x in args.pool_sizes.split(",") if x.strip())
    max_overflows = tuple(int(x) for x in args.max_overflows.split(",") if x.strip())
    workers_list = tuple(int(x) for x in args.workers.split(",") if x.strip())

    results: list[RunResult] = []
    total_runs = len(pool_sizes) * len(max_overflows) * len(workers_list)
    run_idx = 0
    for pool_size in pool_sizes:
        for max_overflow in max_overflows:
            for workers in workers_list:
                run_idx += 1
                print(
                    f"[{run_idx}/{total_runs}] pool_size={pool_size} "
                    f"max_overflow={max_overflow} workers={workers} ...",
                    file=sys.stderr,
                    flush=True,
                )
                r = await _run_one(
                    args.database_url,
                    pool_size=pool_size,
                    max_overflow=max_overflow,
                    workers=workers,
                    iters=args.iters,
                    query_ms=args.query_ms,
                )
                print(
                    f"    total={r.total_seconds:.2f}s qps={r.throughput_qps:.1f} "
                    f"p99_acquire={r.acquire_p99_ms:.2f}ms errors={r.errors}",
                    file=sys.stderr,
                    flush=True,
                )
                results.append(r)

    report = _format_report(
        results,
        database_url_redacted=_redact_url(args.database_url),
        pool_sizes=pool_sizes,
        max_overflows=max_overflows,
        workers_list=workers_list,
        iters=args.iters,
        query_ms=args.query_ms,
    )
    args.output.write_text(report)
    print(f"Wrote report to {args.output}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
