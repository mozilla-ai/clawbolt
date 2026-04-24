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

## Business hours awareness

Clawbolt respects your schedule:

- Messages are only sent during your configured business hours (e.g., 7am-5pm)
- Your timezone is respected (set in your profile)
- Global quiet hours prevent late-night messages (default 8pm-7am)

## Rate limiting

The heartbeat system includes rate limiting to prevent spam. Outbound heartbeat messages are logged, and cooldown periods are enforced between messages. Configurable via `HEARTBEAT_MAX_DAILY_MESSAGES`.
