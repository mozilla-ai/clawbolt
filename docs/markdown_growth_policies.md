# Bounded-growth policies for agent-managed markdown surfaces

This document is the source of truth for the growth policy of every
markdown file the Clawbolt agent can read or write. It is the
human-readable companion to `backend/app/agent/markdown_registry.py`,
which encodes the same policies in code so they can be enforced at
write time and verified by tests.

The motivation is issue #1244 (follow-up to #1243): without explicit
bounds, a long-lived user's `MEMORY.md`, `USER.md`, `SOUL.md`,
`HEARTBEAT.md`, and `HISTORY.md` can grow without limit, bloating
every prompt and degrading both cost and response quality. Some
surfaces are injected directly into the system prompt; others (like
`HISTORY.md`) accumulate compaction summaries forever and were not
windowed at all before this change.

The pattern adopted here mirrors prior art:

- **Claude Code** caps `MEMORY.md` at ~25 KB injected into the
  sub-agent prompt and instructs the agent to curate the file when it
  exceeds that limit.
- **MemGPT / Letta** enforces a per-block character limit on core
  memory and forces eviction to archival memory on overflow.
- **Anthropic harness guidance (2025)** treats prompt-size management
  as the harness's responsibility, not the model's.

A **uniform 25 KiB byte budget** is applied to every surface. The
uniform value matches the existing
`compaction_event_snapshot_max_bytes_per_file` audit cap (so any
in-budget surface fits in a single audit row without truncation) and
is generous enough for normal use while still catching catastrophic
growth.

## Surface inventory

| Surface | Storage | Write mode | Injected | Byte budget | Enforcement |
|---|---|---|---|---|---|
| `USER.md` | `users.user_text` (Text) | full rewrite | yes (every prompt) | 25 KiB | write-time hard cap; read-side tail truncation |
| `SOUL.md` | `users.soul_text` (Text) | full rewrite | yes (every prompt) | 25 KiB | write-time hard cap; read-side tail truncation |
| `HEARTBEAT.md` | `users.heartbeat_text` (Text) | full rewrite | yes (heartbeat prompt) | 25 KiB | write-time hard cap; read-side tail truncation |
| `MEMORY.md` | `memory_documents.memory_text` (encrypted) | full rewrite | yes (every prompt) | 25 KiB | write-time hard cap; read-side tail truncation |
| `HISTORY.md` | `memory_documents.history_text` (encrypted) | append | no (audit only) | 25 KiB | append-with-window: oldest entries dropped FIFO; `write_file` rejected |
| `BOOTSTRAP.md` | disk: `data/users/{user_id}/BOOTSTRAP.md` | transient | no | 25 KiB | deleted by `OnboardingSubscriber` on completion |

Audit details (column types, encryption, prior compaction behavior)
live in inline docstrings on each surface in the registry module.

## Per-surface rationale

### `USER.md`, `SOUL.md`, `MEMORY.md`

These are the three markdown surfaces injected into the **main agent
system prompt on every turn**. The 25 KiB cap serves two purposes:

1. **Write-time hard cap.** `workspace_tools.write_file` /
   `workspace_tools.edit_file`, `MemoryStore.write_memory_async`,
   `MemoryStore.write_user_async`, and
   `MemoryStore.write_soul_async` all reject content over 25 KiB
   with a `BudgetExceededError`. The agent gets a tool error
   describing the actual size and the cap so it can rewrite smaller.
   Compaction (`compact_session`) catches the same error, logs a
   warning, and leaves the previous file in place; the next
   compaction gets another chance.

2. **Read-time tail truncation.** A row that pre-dates the cap (or
   was inserted via raw SQL) would otherwise still bloat every
   prompt. The system-prompt builders (`build_user_section`,
   `build_soul_prompt`, `build_memory_section`) run the value through
   `truncate_for_injection` before handing it to the LLM. The tail
   is kept (most-recent content) with a one-line marker so the agent
   can detect it was clipped and rewrite the file smaller.

### `HEARTBEAT.md`

Same enforcement model as `USER.md` / `SOUL.md` / `MEMORY.md`, but
the heartbeat content is only injected into the heartbeat
sub-system's prompt (not the main agent prompt). The cap is the
same 25 KiB because the heartbeat call is no less expensive per
token, and a runaway list of stale tasks creates the same
context-bloat problem.

### `HISTORY.md`

Append-only by design (compaction summaries are timestamped
breadcrumbs). The risk profile is different from the other
surfaces:

- It is **not injected** into the agent prompt, so unbounded growth
  affects storage cost and admin tool latency, not LLM cost.
- It is **append-only** with row-level lock semantics for integrity
  (issue #1243 / PR #1273), and a full rewrite would silently bypass
  that invariant.

The policy is **append-with-window**: each call to `append_history`
appends the new entry, then drops the oldest entries (whole, on
timestamped boundaries) until the post-append text fits within 25
KiB. The full archive of compaction events is still preserved in
`compaction_events` rows for admin observability; HISTORY.md itself
just keeps the most recent window. `workspace_tools.write_file` and
`workspace_tools.edit_file` reject HISTORY.md outright so the
windowing invariant cannot be circumvented.

### `BOOTSTRAP.md`

A transient file written on user provisioning and deleted by
`OnboardingSubscriber` when onboarding completes. It is not edited
post-provision and is not injected into any prompt. The 25 KiB cap
is enforced on disk writes via `workspace_tools.write_file` for
defense in depth, but in practice the file lives only as long as
onboarding does and is never modified by the agent.

## Adding a new markdown surface

If a future feature introduces a new markdown surface that the agent
can mutate or that is injected into a prompt:

1. Add a `MarkdownPolicy` entry in
   `backend/app/agent/markdown_registry.py` declaring the storage,
   write mode, prompt exposure, and byte budget.
2. If it is column-backed, add the column to
   `COLUMN_TO_SURFACE` so the store-write helpers route through
   the cap.
3. If it is append-mode, decide on a windowing strategy (the
   `test_history_md_is_the_only_append_surface` registry test will
   force you to update it explicitly).
4. The `tests/test_markdown_registry.py` consistency tests will
   catch missing or inconsistent entries.

The intent is to make adding a new unbounded markdown surface
either impossible (the integration paths route through the
registry) or noisily test-failing.

## What this PR deliberately does not do

- **Periodic / size-triggered compaction of working memory.** The
  current design only triggers compaction on conversation trim. A
  size-triggered rewrite (e.g. "compact when MEMORY.md exceeds 80%
  of its budget") is a reasonable follow-up but is out of scope for
  this PR. Until it lands, an agent that grows MEMORY.md to the cap
  will start receiving budget errors on the next write and have to
  curate the file itself.
- **Importance-based compaction.** Stanford's Generative Agents
  pattern ranks observations by importance and reflects only when the
  cumulative score crosses a threshold. Worth exploring; out of scope
  here.
- **Admin observability dashboard for surface sizes.** The
  observability hook is the existing structured `compaction.summary`
  log line plus the new `markdown_registry: truncated ...` warning
  log. A dedicated UI for tracking per-user file sizes over time is
  separate work.
- **Rewriting the SOUL.md / USER.md / HEARTBEAT.md compaction
  prompts.** Issue #1243 already tightened the durable-memory
  rules; this PR is about adding policy enforcement, not rewriting
  the prompts.
