# Monitoring and alerting

Monitoring requires `AUTH_MODE=multi_user`. Single-user deployments do not mount the alerting stack or `/api/monitoring` routes.

| Layer | Catches | Blind to |
|---|---|---|
| Error alerts | Exceptions, failed tool calls, and other `ERROR` logs | Silent failures and process outages |
| Health probes | Unreachable or unhealthy dependencies | Process outages |

Use an external uptime check as well. In-process monitoring cannot report that its own process is down.

## Prerequisites

Alerting is enabled when:

1. `SMTP_HOST` and `SMTP_FROM_EMAIL` are both set.
2. `ALERT_EMAIL`, or its `ADMIN_EMAIL` fallback, resolves to a recipient.

Check the current state with `GET /api/monitoring/status` using admin authentication:

```json
{
  "alerts": {"enabled": true, "pending_groups": 0, "dedupe_minutes": 30},
  "health_monitor": {"enabled": true, "interval_seconds": 300, "probes": {}},
  "recipient_configured": true
}
```

Send a test alert after configuring email:

```bash
curl -X POST https://your-domain.example/api/monitoring/test-alert \
  -H "Authorization: Bearer $ADMIN_API_KEY"
```

### Diagnose email delivery

`POST /api/monitoring/diagnose-email`, also available as **Diagnose delivery** in the admin Monitoring tab, checks TCP, EHLO, STARTTLS, and login from inside the application container without sending a message.

Its result distinguishes:

- No candidate port is reachable: the hosting platform or network blocks SMTP.
- Another STARTTLS port is reachable: change `SMTP_PORT`. Amazon SES also supports `2587`.
- The configured port is reachable but login fails: check credentials, sender verification, and SES sandbox restrictions.

Ports 465 and 2465 require implicit TLS and are not supported. `SMTP_TIMEOUT_SECONDS` bounds each operation; the overall send is capped at twice that value.

## Error alerts

A logging handler watches the `backend` and `uvicorn.error` trees. Every `ERROR` record enters the alert pipeline.

Alerts group by logger, exception type, and unformatted log template. Each group sends at most once per `ALERT_DEDUPE_MINUTES`, including the number of suppressed occurrences. `ALERT_MAX_EMAILS_PER_HOUR` caps total sends. A failed email does not start the cooldown.

Failures that do not raise or log at `ERROR` require a health probe.

## Health probes

Probes run every `HEALTH_CHECK_INTERVAL_SECONDS` and alert on status transitions. `HEALTH_FAILURE_THRESHOLD` consecutive failures are required before a probe becomes DOWN. Each probe is capped by `HEALTH_PROBE_TIMEOUT_SECONDS`.

| Probe | Check | Detects |
|---|---|---|
| `database` | Calls the `/health` handler | Unreachable Postgres |
| `llm` | Sends a single-token `amessages` request | Invalid credentials, retired model, provider outage |
| `bluebubbles` | Checks server info, send readiness, and webhook registration | Sleeping bridge, rejected password, signed-out Mac, missing webhook |
| `integration:<name>:<user_id>` | Runs the integration's `auth_check` | Expired or revoked user grant |
| `integration_check:<user_id>` | Tracks whether a user's integration sweep completed | Timed-out or failed `auth_check` |

Unconfigured dependencies are not registered as probes.

### BlueBubbles

The BlueBubbles probe verifies three independent conditions:

1. The server accepts the configured password.
2. iMessage is signed in and the configured send method is ready.
3. A webhook targets the current `APP_BASE_URL` with the current credential.

When the webhook is missing or stale, the probe attempts to register it and emits a throttled REPAIRED notice. Repair follows these limits:

- It does not modify registration when the webhook list cannot be read.
- It stops after three consecutive unsuccessful repair attempts.
- It skips repair on the process's first probe tick while startup registration may still be running.

Only one deployment should manage a given bridge. Multiple replicas or environments pointed at the same `BLUEBUBBLES_SERVER_URL` will compete over its single webhook registration. Leave the bridge unset in non-production environments or give each environment its own bridge.

Older BlueBubbles versions may omit readiness flags. Missing flags are treated as unknown, not as a failure.

### Admin Monitoring tab

`Admin -> Monitoring` shows current probe state, details, last-check time, consecutive failures, and recent transitions or repairs.

`POST /api/monitoring/run-probes` starts a run and returns immediately. Poll `GET /api/monitoring/status` for queued, checking, passed, or failed steps and elapsed times. Only one run executes at a time; a second request watches the run already in progress.

Per-user integrations are grouped by account with these summary states:

| Verdict | Meaning |
|---|---|
| `N not working` | At least one previously working integration is DOWN |
| `Status unknown` | The account's sweep did not complete |
| `N unknown` | Failures have not reached the threshold |
| `All N working` | Every connected integration authenticates |
| `Nothing connected` | No integration is connected |

Problem accounts appear first and expand automatically. The activity log is process-local and resets on deploy; alert emails are the durable record.

### Per-user integration baselines

An integration that was never connected and one with an expired token can both report "not authenticated." Initial integration observations are therefore silent; alerts begin after a genuine UP to DOWN transition. Recoveries are reported.

`integration_check:<user_id>` is not baseline-silent. It reports when the user's integration sweep cannot run, preventing stale probe states from looking healthy.

Probe state is process-local. A deploy resets whether an integration was previously UP, so a grant that lapses during replacement can appear as an initial disconnected state. Persisting that history remains a known gap.

Run probes manually and inspect progress with:

```bash
curl -X POST https://your-domain.example/api/monitoring/run-probes \
  -H "Authorization: Bearer $ADMIN_API_KEY"
curl -s https://your-domain.example/api/monitoring/status \
  -H "Authorization: Bearer $ADMIN_API_KEY" | jq .health_monitor.run
```

## Tuning alert volume

If alert volume is too high:

1. Raise `ALERT_DEDUPE_MINUTES`.
2. Raise `HEALTH_FAILURE_THRESHOLD` for flaky dependencies.
3. Change routine `logger.error` calls to `logger.warning` at the source.

Lower `ALERT_MAX_EMAILS_PER_HOUR` only as a final ceiling. Held-back alerts retain counts but discard individual details.
