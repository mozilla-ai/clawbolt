# Heartbeat

The heartbeat system lets Clawbolt proactively reach out with reminders and follow-ups. Instead of waiting for you to message first, Clawbolt checks in periodically when there is something useful to say.

## How it works

The heartbeat runs on a timer and uses a two-stage evaluation.

Before either stage, Clawbolt skips users who have not opted in, have no heartbeat items, recently sent a message, or reached their daily proactive-message limit.

### Stage 1: Lightweight evaluation

A small LLM call reviews the user's heartbeat items, recent activity, and prior heartbeat history. It decides whether an item is actionable now.

### Stage 2: Task execution

When Stage 1 selects work, the full agent executes the task and sends any useful result. If nothing is due, no message is sent.

## Quiet by default

Clawbolt avoids interrupting you when there is nothing useful to say:

- The scheduler skips users with no items in their HEARTBEAT.md (no nag-without-purpose).
- During an active conversation (a message in the last few minutes), the heartbeat LLM call is skipped so you do not get a proactive message on top of your back-and-forth.
- A daily cap on proactive messages prevents pile-on if many items become eligible at once.

## Rate limiting

The heartbeat system includes rate limiting to prevent spam. Outbound heartbeat messages are logged, and cooldown periods are enforced between messages. Configurable via `HEARTBEAT_MAX_DAILY_MESSAGES`.
