# Bounded growth for agent-managed markdown

`backend/app/agent/markdown_registry.py` is the executable source of truth for every markdown surface the agent can read or write. This document explains the shared policy and how to add a surface.

Every surface has a 25 KiB UTF-8 byte budget. The cap bounds prompt cost and storage growth, and ensures an in-budget surface fits inside the compaction audit snapshot limit.

## Surface inventory

| Surface | Storage | Write mode | Prompt use | Enforcement |
|---|---|---|---|---|
| `USER.md` | `users.user_text` | Rewrite | Main agent | Write cap and read-side tail truncation |
| `SOUL.md` | `users.soul_text` | Rewrite | Main agent | Write cap and read-side tail truncation |
| `HEARTBEAT.md` | `users.heartbeat_text` | Rewrite | Heartbeat agent | Write cap and read-side tail truncation |
| `MEMORY.md` | `memory_documents.memory_text` | Rewrite | Main agent | Write cap and read-side tail truncation |
| `HISTORY.md` | `memory_documents.history_text` | Append | Audit only | Append with oldest-entry eviction |
| `BOOTSTRAP.md` | `data/users/{user_id}/BOOTSTRAP.md` | Transient | Onboarding | Write cap; removed after onboarding |

## Rewrite surfaces

Workspace tools and store methods reject content over the budget with `BudgetExceededError`. Compaction leaves the prior content unchanged when a rewrite is too large.

Read-side truncation protects prompts from legacy or manually inserted oversize rows. It retains the newest tail and adds a marker so the agent can curate the file.

## HISTORY.md

`HISTORY.md` is append-only. Each append drops the oldest complete timestamped entries until the file fits the budget. The full compaction audit remains in `compaction_events`.

Workspace `write_file` and `edit_file` reject `HISTORY.md` so callers cannot bypass append locking and windowing.

## Adding a surface

1. Add a `MarkdownPolicy` in `backend/app/agent/markdown_registry.py` with its storage, write mode, prompt exposure, and byte budget.
2. For column-backed storage, add the column to `COLUMN_TO_SURFACE`.
3. For append mode, implement an explicit windowing strategy.
4. Update `tests/test_markdown_registry.py` and the inventory above.

## Operator signals

- Compaction logs a warning when a rewrite exceeds its budget.
- Prompt builders log at info when they truncate a legacy oversize row.
- `compaction_events` retains bounded before-and-after snapshots for audits.
