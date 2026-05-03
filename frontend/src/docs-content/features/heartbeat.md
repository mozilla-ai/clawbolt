# Heartbeat

The heartbeat system lets Clawbolt proactively reach out with reminders and follow-ups. Instead of waiting for you to message first, Clawbolt checks in periodically when there is something useful to say.

## How it works

The heartbeat runs on a timer and uses a two-stage design: **cheap checks first, LLM only when needed**.

### Stage 1: Deterministic checks (no LLM cost)

Fast checks look for actionable items:

- **Time-sensitive memory facts**: Facts containing keywords like "remind", "follow-up", "deadline"
- **Idle users**: No inbound messages for a configurable number of days
- **Heartbeat notes**: User-defined notes stored via the `update_heartbeat` tool

### Stage 2: LLM evaluation (only if flags found)

If any checks return results, the LLM composes an appropriate message. It can decide to send the message or take no action, based on priority and context.

## Quiet by default

Clawbolt avoids interrupting you when there is nothing useful to say:

- The scheduler skips users with no items in their HEARTBEAT.md (no nag-without-purpose).
- During an active conversation (a message in the last few minutes), the heartbeat LLM call is skipped so you do not get a proactive message on top of your back-and-forth.
- A daily cap on proactive messages prevents pile-on if many items become eligible at once.

## Rate limiting

The heartbeat system includes rate limiting to prevent spam. Outbound heartbeat messages are logged, and cooldown periods are enforced between messages. Configurable via `HEARTBEAT_MAX_DAILY_MESSAGES`.
