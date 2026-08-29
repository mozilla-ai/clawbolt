# Monitoring and alerting

Everything here requires `AUTH_MODE=multi_user`. A single-user self-host mounts
neither the alerting stack nor the `/api/monitoring` router.

Two layers live in the app, and each catches failures the other structurally
cannot:

| Layer | Catches | Blind to |
|---|---|---|
| 1. Error alerts | Anything logged at `ERROR`: exceptions, failed tool calls, unhandled 500s | Failures that do not raise; the app being down |
| 2. Health probes | Silent breakage: dead bridge, stale scraper, expired token, unreachable provider | The app being down |

Both go silent when the process dies, so a deployment also needs an external
uptime check. That one cannot be code in this repo; see your deployment's own
runbook.

## Prerequisites

All in-app alerting is dormant until both are true:

1. `SMTP_HOST` and `SMTP_FROM_EMAIL` are set. Setting only one fails startup by
   design, so a typo cannot silently disable email.
2. A recipient resolves: `ALERT_EMAIL`, falling back to `ADMIN_EMAIL`.

Verify with `GET /api/monitoring/status` (admin auth required):

```json
{
  "alerts": {"enabled": true, "pending_groups": 0, "dedupe_minutes": 30},
  "health_monitor": {"enabled": true, "interval_seconds": 300, "probes": {...}},
  "recipient_configured": true
}
```

Then send a synthetic alert to confirm delivery end to end, rather than
discovering an `ALERT_EMAIL` typo during a real incident:

```bash
curl -X POST https://your-domain/api/monitoring/test-alert \
  -H "Authorization: Bearer $ADMIN_API_KEY"
```

A failed test alert returns the transport's own explanation (blocked port,
rejected credentials, unverified sender), not a bare "not sent".

### When no email arrives at all

`POST /api/monitoring/diagnose-email`, or `Diagnose delivery` in the admin
Monitoring tab, probes the mail path from inside the running container: TCP
reachability for every candidate SMTP port, then a full EHLO / STARTTLS / login
handshake against the configured one. No message is sent. It exists to separate
two failures that look identical from the outside:

- **Nothing is reachable.** The platform is blocking outbound SMTP. Some hosts
  permit it only on paid tiers, in which case no `SMTP_PORT` value will work and
  alerting needs an HTTPS email API instead.
- **Only the configured port is blocked.** SES publishes `2587` as a STARTTLS
  alternate for networks that filter `587`; the diagnostic names the reachable
  ports so this is a one-line config fix. Ports 465 and 2465 expect TLS from
  the first byte and are not supported by this sender.
- **The port is open but the session is refused.** Then it is a credential,
  sender-identity, or SES-sandbox problem, and the handshake error says which.

`SMTP_TIMEOUT_SECONDS` (default 10) bounds one SMTP operation, and a send is
capped at twice that. The cap matters because `socket.create_connection` retries
every address the SMTP host resolves to: at a 15s timeout, a blocked port on a
three-A-record host such as SES held the caller for 45s before reporting
anything.

The Monitoring tab's Email delivery card shows the configured endpoint, the last
successful send, and the last failure with its explanation, so a dead transport
is visible without anyone clicking a test.

## Layer 1: error alerts

A logging handler on the `backend` and `uvicorn.error` trees turns every `ERROR`
record into an alert. No call-site changes are needed to cover a new failure
path, because `logger.exception(...)` is already the convention.

**Grouping.** Alerts collapse on `(logger, exception type, log template)`. The
unformatted template is the key, so `"LLM failed for user %s"` with a thousand
different user ids is one alert with `count: 1000`, not a thousand emails.

**Throttling.** Each group emails at most once per `ALERT_DEDUPE_MINUTES`
(default 30), carrying the suppressed occurrence count. A global
`ALERT_MAX_EMAILS_PER_HOUR` (default 20) caps the worst case. Held-back alerts
keep accumulating rather than being discarded, and a failed send deliberately
does not start the cooldown, so a transient SES outage does not silence a
fingerprint for half an hour.

**What it will not catch.** Anything that does not raise or log at `ERROR`. A
scraper returning zero results, a channel that quietly stops delivering, an
OAuth token that expired but has not been used yet. That is layer 2's job.

## Layer 2: health probes

Runs every `HEALTH_CHECK_INTERVAL_SECONDS` (default 300) and emails on **status
transitions**, not on state. A six-hour outage is two emails (down, then
recovered), not seventy-two. `HEALTH_FAILURE_THRESHOLD` consecutive failures
(default 2) are required before declaring DOWN, so one timed-out request to a
residential host is not an incident.

| Probe | Mechanism | Detects |
|---|---|---|
| `database` | Calls the `/health` handler | Postgres unreachable |
| `llm` | Single-token `amessages` call against the primary model | Revoked key, retired model, provider outage |
| `bluebubbles` | Three checks in order: the channel's `/api/v1/server/info` result, the send-readiness flags in it, then webhook registration | Bridge asleep, rejected password, Mac signed out of iMessage, inbound webhook missing |
| `integration:<name>:<user_id>` | Each factory's `auth_check` per user | Expired refresh token, revoked grant |
| `integration_check:<user_id>` | Whether that user's sweep answered at all | A stuck or exploding `auth_check`, which leaves every integration under it unknown |

Probes for unconfigured dependencies are not registered, so an unused
integration is not a permanently-red check.

Every probe is capped at `HEALTH_PROBE_TIMEOUT_SECONDS` (default 45, floor 5),
including each user's turn in the integration sweep. A probe past its budget is
abandoned and reported DOWN with a timeout detail, which is the honest reading: a
dependency that cannot answer in 45s is not healthy. Without the cap one wedged
socket, typically a residential BlueBubbles host or the scraping sidecar, stalls
the entire run.

### The BlueBubbles probe, and why reachable is not enough

The bridge answering is necessary and nowhere near sufficient. Three distinct
failures leave it answering normally while messages stop:

1. **A rejected password.** `/api/v1/server/info` returns 401. The old
   reachability check accepted any status below 500, so this read as healthy.
   Now `reachable` and `authenticated` are tracked separately and the alert
   names the misconfiguration.
2. **A Mac signed out of iMessage.** The server reports no iCloud account, so
   every send fails. The probe reads that flag rather than only the status
   code. `BLUEBUBBLES_SEND_METHOD=private-api` additionally requires the
   Private API to be enabled and its helper connected.
3. **No inbound webhook.** This is the one nothing else catches. Registration
   is attempted once per deploy in a background task; if the Mac is asleep at
   that moment the attempt fails, is never retried, and the bridge later comes
   back with every reachability signal green while inbound stays dead. A
   changed `APP_BASE_URL` produces the same silence, leaving the old
   registration pointed at a URL that no longer answers.

**Case 3 self-repairs.** When the webhook is missing or carries a token from a
previous password, the probe re-registers it against the current
`APP_BASE_URL` and emails a separate "REPAIRED" notice, throttled by
`ALERT_DEDUPE_MINUTES`. The notice is separate because the repair resolves the
failure before `HEALTH_FAILURE_THRESHOLD` consecutive failures accumulate, so
no DOWN alert would ever fire for it: without its own email, inbound would keep
breaking and silently fixing itself with nobody the wiser.

Three guards keep that self-repair from becoming its own problem, because
registration is delete-then-POST and each attempt reopens a window with no
webhook registered at all:

- **It never repairs on a guess.** If the webhook list cannot be retrieved, that
  is reported as unverified rather than treated as missing, and nothing is
  written to the operator's Mac.
- **It stops after three consecutive attempts** that do not stick. Past that the
  probe reports a plain failure and escalates through the ordinary DOWN alert,
  rather than rewriting the registration every tick while the email cooldown
  keeps the operator from hearing about it again. A passing check resets the
  budget.
- **It skips the process's first tick,** because the lifespan registers this
  webhook in a background task and a check landing inside that window would
  repair and email about a deploy where nothing was wrong.

**One writer per bridge.** Every replica runs its own monitor, so more than one
replica means several independent delete-and-register cycles against one
BlueBubbles server. The same applies to any staging or preview environment that
shares `BLUEBUBBLES_SERVER_URL` with production but has its own `APP_BASE_URL`:
it will now assert its own registration on a schedule instead of failing once at
boot, and the two environments will fight over the bridge. Point non-production
environments at their own bridge, or leave `BLUEBUBBLES_SERVER_URL` unset there
so the probe is not registered at all.

Readiness flags are tri-state. Older BlueBubbles builds omit them, and a
missing field is treated as unknown rather than as an outage.

### Admin Monitoring tab

`Admin -> Monitoring` in the web app shows every probe, not just BlueBubbles:
current status and detail, when each was last checked, consecutive failures,
and an activity log of status changes and self-repairs. `Run probes now` is the
one action at the top; `Send test alert` and `Diagnose delivery` sit in the Email
delivery card at the bottom, where a failure is explained rather than merely
reported.

**Runs are started, not awaited.** `POST /api/monitoring/run-probes` schedules a
run and returns immediately with its step list, all pending. The tab polls
`GET /api/monitoring/status` and reads `health_monitor.run`, showing each step as
queued, checking, passed, or failed with its elapsed time. Awaiting the run
instead meant a request open for minutes on a large tenant count, a tab stuck on
"Running" with no way to tell a slow probe from a wedged one, and a lost result
whenever a proxy timed out first. A run also reports the alert email as its own
step, so a transition that could not be delivered is visible rather than silent.

Only one run happens at a time: an operator's run and the timer tick serialize on
a lock, and a second `run-probes` request returns `started: false` and watches the
run already in flight. Two overlapping passes would double every outbound call and
race on the transition bookkeeping.

**Per-user integrations are grouped by user, one row per account.** A flat list
of every (user, integration) pair answers "is anything broken"; the question an
admin actually has is "whose account is broken", and several hundred rows sorted
by probe key cannot answer it without scrolling and grouping in your head. Each
user gets a single verdict, worst first:

| Verdict | Meaning |
|---|---|
| `N not working` | At least one integration that used to work is DOWN |
| `Status unknown` | The sweep could not check this user, so nothing below can be claimed |
| `N unknown` | A failure that has not yet reached `HEALTH_FAILURE_THRESHOLD` |
| `All N working` | Every connected integration authenticates |
| `Nothing connected` | This user has connected nothing |

Users are named by their subscription email rather than by `users.id`, because a
UUID says something is broken without saying whose account to go fix. Rows expand
under each user with the detail and timings, and accounts with a problem expand
themselves. Only users with a problem are listed by default; `Show all N users`
reveals the rest.

An integration nobody ever connected shows as "Not connected" and is excluded
from the verdict: baseline seeding records those as DOWN so a later disconnect is
still reportable, but they are not breakage, and with 8 specialist integrations
across `HEALTH_PROBE_MAX_USERS` accounts they would otherwise read as several
hundred failures on a completely healthy deployment.

The activity log lives in process memory, so a deploy resets it and each
replica has its own. The alert emails are the durable record.

### Per-user integration checks are baseline-silent

An integration a user never connected reports the same "not authenticated"
reason as one whose token just expired. Alerting on the former would be constant
noise, so these keys establish their baseline silently and alert only on a
genuine UP to DOWN transition: it worked, then it stopped. Recoveries are always
reported. That transition is the email you get when a tenant's connection
breaks, and it names the tenant by email, not by user id.

**The sweep reports on itself, and that key is not baseline-silent.** A user
whose `auth_check` times out or raises used to be skipped in silence. Their probe
states then kept their last known status forever, so an integration that broke
while the check was failing produced no transition and no email. Now that user
gets an `integration_check:<user_id>` failure, which alerts like any
infrastructure probe: a check that cannot run has no legitimate steady state,
unlike a connection the user never made.

**Known gap: a deploy reseeds every baseline.** Probe state lives in process
memory, and `ever_up` with it. An integration that lapses while the process is
being replaced comes back as a first observation, so it is recorded silently and
shows as "Not connected" rather than as breakage, which is indistinguishable
from a user who never connected it. Closing that needs the "has this ever
authenticated" bit persisted, which is not implemented.

Start a run on demand instead of waiting out the interval, then poll for its
progress:

```bash
curl -X POST https://your-domain/api/monitoring/run-probes \
  -H "Authorization: Bearer $ADMIN_API_KEY"
curl -s https://your-domain/api/monitoring/status \
  -H "Authorization: Bearer $ADMIN_API_KEY" | jq .health_monitor.run
```

## Tuning noise

If alert volume is too high:

1. Raise `ALERT_DEDUPE_MINUTES` before disabling anything.
2. Raise `HEALTH_FAILURE_THRESHOLD` if a flaky dependency flaps.
3. Check whether an existing `logger.error` call is actually routine and should
   be a `logger.warning`. That fixes the noise at the source, and warnings are
   not captured.

Lower `ALERT_MAX_EMAILS_PER_HOUR` as a blunt ceiling only if the above is not
enough; held-back alerts are counted but their detail is dropped.
