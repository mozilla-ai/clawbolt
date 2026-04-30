<!-- /autoplan restore point: /root/.gstack/projects/mozilla-ai-clawbolt/main-autoplan-restore-20260429-202408.md -->
<!-- Reviewed via /autoplan on 2026-04-29 (single-model: codex auth 401). -->
<!-- See "Review history" at bottom for full audit trail. -->

# Onboarding fixup — final plan

Single PR against `mozilla-ai/clawbolt` resolving 12 issues from the
2026-04-28 first-user observation session.

**In scope (12 issues):** #1050, #1047, #1046, #1045, #1044, #1043, #1041,
#1040, #1039, #1038, #1037, #1029.

**Carved out (own follow-up issues / PRs):**
- #1048 — comfort-level permission setting. Reframed to "audit per-tool
  ASK defaults and flip to ALLOW where approval isn't load-bearing" before
  reconsidering a per-user setting.
- #1051 — just-in-time feature explainers. Real multi-week feature.

---

## What changes (file-level)

| File | Change |
|---|---|
| `backend/app/agent/prompts/bootstrap.md` | Trim onboarding script (name + tz only). Remove personality question. Add anti-affirmation guidance with a positive-example reply. Add photo-access reassurance copy. |
| `backend/app/agent/prompts/instructions.md` + `backend/app/config.py` system preamble | Audit for affirmative-tone language; align with bootstrap.md. |
| `backend/app/agent/onboarding.py` | When bootstrap ends, auto-write a default SOUL.md ("direct and practical") if soul_text is still the template. **Do not** remove `_has_custom_soul` from the completion heuristic. |
| `backend/app/agent/system_prompt.py` | Tighten the vision-routing sentence at lines 131-141: change "Call analyze_photo if vision would help" to explicit positive triggers + negative cases. |
| `backend/app/agent/tools/media_tools.py` | Rewrite `analyze_photo` description with concrete positive triggers ("user asked what's in this", "estimate from photo") + negative cases ("DO NOT call when routing to CompanyCam, attaching to job, saving without question"). |
| `backend/app/agent/tools/integration_tools.py` | Add to `manage_integration`'s `usage_hint`: "Before offering a connect link, call action='status' first and skip integrations already showing as connected." This is the durable surface — bootstrap.md gets deleted, `usage_hint` doesn't. |
| `backend/app/agent/ingestion.py` | Verify `_send_early_typing_indicator` works on telegram + bluebubbles. Webchat client-side. Linq/SMS skip gracefully. **No** continuous-refresh task this PR (defer; cancellation complexity not worth it). |
| `frontend/src/lib/channel-utils.ts` | `getVisibleChannels` returns only servers-available channels (drop graydout). Audit all callsites (`GetStartedPage.tsx`, `ChannelsPage.tsx`) — `ChannelsPage` should still show configured-but-currently-unavailable routes so users can see what they had. |
| `frontend/src/pages/GetStartedPage.tsx` | **Mobile-first reframe** (see below). Drop disabled-channel rendering. Add empty-state when zero channels available. |
| `frontend/src/components/ChannelConfigForm.tsx` | Phone number entry: leading "US +1" prefix label inside the input, parsing rules below. Inline validation error string. Code comment explaining US-default. |
| `frontend/src/components/TextAssistantCard.tsx` | Mobile: render large primary "Open Messages" button (`sms:` URI) above the QR. QR remains for desktop. Add long-press / tap-to-copy on the phone number. Bump QR size from 80px → 160px when shown standalone. |
| `frontend/src/pages/OAuthCallbackPage.tsx` | Both success AND error states: replace `<Link>` with primary `<Button>` (`w-full sm:w-auto`). |
| `tests/test_onboarding.py` | Regression: name + tz + default-soul-write triggers completion via path 2. Existing user with `_has_custom_soul=False` and partial state does NOT silently auto-complete. |
| `tests/test_integration_tools.py` | Regression: agent does not re-prompt for an already-connected integration (fixture connects google_calendar out-of-band, drives one bootstrap turn, asserts no "connect google_calendar" outbound). |
| `tests/test_media_pipeline.py` | Regression: pipeline does not auto-run vision (already true; add explicit assertion). |
| `frontend/src/lib/__tests__/channel-utils.test.ts` (new) | `getVisibleChannels` — empty config → `[]`; telegram-only → `[telegram]`; linq-configured → `[telegram, linq]` (when telegram bot set) or `[linq]` (when not). |

---

## Mobile-first reframe (the bigger UX change)

Detect mobile via `useMediaQuery('(max-width: 640px)')` (existing
breakpoint). Two distinct layouts in `GetStartedPage.tsx`:

### Mobile layout — single screen
1. **Header**: "Hey, I'm Clawbolt. Text me to get started."
2. **Phone number input** with `+1` prefix label inside the input.
3. **Primary CTA**: "Text Clawbolt" — disabled until a valid phone is
   entered. On click:
   - Calls `setLinqLink(phoneNumber)` (premium) or `setBlueBubblesLink`
     (premium) or saves to `linq_allowed_numbers` (OSS).
   - On success, swaps the button to a real `<a href="sms:+1OURNUMBER">`
     anchor and auto-clicks it (or just renders it for the user to tap).
4. **Secondary**: small text link "Use web chat instead" → routes to
   `/app/chat`.
5. **Empty-state branch**: if no iMessage backend AND no Telegram, show
   "Your assistant isn't reachable from a phone yet. Use web chat or ask
   your admin to enable iMessage." with a chat link.

The 4-step wizard is gone on mobile. No QR. No channel radio (we know:
mobile = SMS). No multi-step.

### Desktop layout — keep current 4-step wizard
With the polish from B1/B3 (button sizes, phone +1) and A4/A5 (Telegram
hidden when not configured, no graydout). QR for cross-device pairing
(user's laptop, phone scans the QR).

### Out of scope (track as follow-up)
- **Auto-grab phone from first inbound** (variant b in the gate
  conversation) — would land users without typing their number, bound by
  a one-shot setup code. Real feature, not polish: backend setup-code
  table + endpoint + inbound matcher + expiry handling. Track as new
  issue: "zero-friction mobile pairing via setup code."

---

## Phone number parsing rules (`+1` UX)

`ChannelConfigForm.tsx` `PremiumChannelLinkForm` Linq + BlueBubbles
config:

- Default state: input shows a permanent `+1` prefix label (visual, not
  inside the value). User types digits.
- If user types `+` first, prefix label disappears and they can enter
  any country code.
- On paste: strip whitespace, dashes, parens. If the result has a leading
  `+`, accept as-is. Else prepend `+1`.
- On blur / submit: validate against E.164 (`/^\+[1-9]\d{6,14}$/`). On
  failure, show inline error: `"Use a phone number like +15551234567 (10
  digits, no spaces)."`
- Backend re-validates (defense in depth).
- Code comment: `// US-first: most clawbolt users today are US trades.
  i18n tracking issue #TBD.`

---

## Bootstrap.md edits (the agent script)

The new bootstrap.md script:
- Opens **direct, no warmth-performance**: "Hey, I'm Clawbolt. I'm an
  AI assistant for tradespeople. Who am I working with?" *(positive
  example, not just banned phrases.)*
- Asks for name and timezone (or city → infer tz).
- **Removes** the personality question entirely. After name + tz are
  saved and bootstrap ends, the system writes a default SOUL.md
  ("direct, practical, no fluff. No 'Great question!' / 'Absolutely!'
  / 'I'd love to help!' openers.") if soul_text is still the template.
- Mentions integrations contextually only ("if they bring up scheduling,
  offer to connect Google Calendar"). Not as an upfront step. Skip
  already-connected (durable rule lives in `manage_integration`'s
  `usage_hint`, not here, since bootstrap deletes on completion).
- Includes one passing reassurance about photo access: "I only see photos
  you send me directly. I can't browse your camera roll."
- Banned phrases listed AND a one-line example of the desired opening.
- Acceptance: a typical onboarding completes in ≤ 3 user turns when the
  user is cooperative.

---

## Order of work

1. **Backend prompt + heuristic edits** (smallest surface):
   - `bootstrap.md` rewrite.
   - `instructions.md` / `config.py` audit for affirmative tone.
   - `onboarding.py` default-SOUL.md write on bootstrap end.
   - `system_prompt.py:131-141` vision sentence tighten.
   - `media_tools.py` analyze_photo rewrite.
   - `integration_tools.py` usage_hint update.

2. **Frontend reframe + polish**:
   - `channel-utils.ts` empty-state semantics + callsite audit.
   - `GetStartedPage.tsx` mobile/desktop layout split.
   - `ChannelConfigForm.tsx` phone +1.
   - `TextAssistantCard.tsx` mobile CTA + tap-to-copy.
   - `OAuthCallbackPage.tsx` button upgrade (both branches).

3. **Tests**:
   - `tests/test_onboarding.py` default-SOUL write + completion heuristic
     unchanged for users without name+tz.
   - `tests/test_integration_tools.py` already-connected regression.
   - `tests/test_media_pipeline.py` pipeline-no-vision assertion.
   - `frontend/src/lib/__tests__/channel-utils.test.ts` new file.

4. **Manual visual verification via Playwright** (project CLAUDE.md
   blocking requirement):
   - Mobile viewport (375×812): single-screen flow renders, phone validation
     errors, sms: deep-link works.
   - Desktop viewport: 4-step wizard, QR visible, Telegram hidden when
     `telegram_bot_token_set=false`.
   - OAuth callback success + error states on mobile viewport — buttons
     full-width.

---

## Definition of Done

```bash
DATABASE_URL="postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_test" uv run pytest -v
uv run ruff check backend/ tests/ alembic/
uv run ruff format --check backend/ tests/ alembic/
uv run ty check --python .venv backend/ tests/ alembic/
cd frontend && npm run typecheck
cd frontend && npm run deadcode
cd frontend && npm test
# Regenerate OpenAPI types if any backend schema changed
uv run python scripts/export_openapi.py
cd frontend && npm run generate:api
```

PR template at `.github/pull_request_template.md`. Reference issues #1050,
#1047, #1046, #1045, #1044, #1043, #1041, #1040, #1039, #1038, #1037,
#1029 in the body. Note carve-outs (#1048, #1051) link to follow-up issues.

---

## Open follow-ups (file as new issues)

- **#1048-followup** — Audit per-tool ASK defaults and flip to ALLOW where
  approval isn't load-bearing. Reconsider a comfort-level setting only
  after the audit.
- **#1051-followup** — Just-in-time feature explainers (per-feature usage
  counters, tip emission, idempotency, opt-out).
- **mobile-pairing-setup-code** — Auto-grab user phone from first inbound
  message via one-shot setup code. Removes phone-number entry from
  mobile onboarding entirely.

---

# Review history

The following sections preserve the autoplan review trail (single-model
mode; codex was auth-failed). Captured for posterity; not load-bearing
for implementation.

## Phase summaries (single-model)

- **CEO**: 3 disagreements with stated direction. Critical: mobile-first
  reframe candidate (adopted in v2). High: SOUL.md default reframe (adopted
  via Eng's "default SOUL.md write" instead). High: deferring #1051
  entirely creates feature-discovery gap (acknowledged via follow-up).
- **Design**: 2/10 a11y, 2/10 DESIGN.md fidelity, 3/10 missing-states.
  Critical: empty-state when no channels (handled). Critical: same-device
  QR (handled by deferring QR to desktop only and using `sms:` deep-link
  on mobile).
- **Eng**: 2 critical (silent migration, status-check placement — both
  fixed). High: `<integrations>` marker creates duplicate state (dropped
  for v1). High: description-only tightening shadowed (also tighten
  `system_prompt.py:131-141` — adopted).
- **DX**: 3/10 errors, 2/10 migration story, 5/10 prompt clarity.
  Critical: status-check in bootstrap.md (moved to `usage_hint`). High:
  banned phrases need a positive example (adopted).

## Cross-phase themes resolved
1. Empty-channel state → mobile-first reframe handles it; desktop path
   shows empty-state copy.
2. Move status-check out of bootstrap.md → into `manage_integration`
   `usage_hint`.
3. Default SOUL.md write > heuristic loosening → adopted.
4. Phone +1 edge cases → spec'd above.
5. Banned-only is weak → positive example added to bootstrap.md edit.

## User challenges — final answers
1. **Mobile-first reframe**: adopt variant (a) in this PR. Track variant
   (b) — auto-grab from first inbound — as a follow-up.
2. **Drop Group D entirely**: carve out as own follow-up issue. Audit
   ASK defaults first.
3. **Ship minimal #1051**: defer to own issue.
4. **Default SOUL.md vs heuristic loosening**: adopt default SOUL.md
   write.

## Decision audit trail (autoplan auto-decisions, all accepted)

| # | Decision | Class | Principle |
|---|---|---|---|
| 1 | Defer #1051 entirely | TASTE→user-confirmed | P3 |
| 2 | Default SOUL.md write instead of heuristic loosening | MECHANICAL | P5 |
| 3 | Move status-check guidance to `manage_integration.usage_hint` | MECHANICAL | P3 |
| 4 | Add empty-state spec for empty-channel case | MECHANICAL | P1 |
| 5 | Spec phone-prefix edge cases (paste, +44, deletion) | MECHANICAL | P1 |
| 6 | Add positive-example reply to bootstrap.md anti-affirmation | MECHANICAL | P5 |
| 7 | Defer C3 typing refresh loop entirely | TASTE | P3 |
| 8 | Tighten `system_prompt.py:131-141` vision sentence too | MECHANICAL | P1 |
| 9 | Audit `getVisibleChannels` callsites before A4/A5 | MECHANICAL | P1 |
| 10 | Drop the `<integrations>` marker for v1 | TASTE | P3 |
| 11 | Mobile-first reframe (variant a) IN scope | USER CHALLENGE → confirmed | P1 |
| 12 | Drop Group D | USER CHALLENGE → confirmed | P3 |
