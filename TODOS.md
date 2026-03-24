# TODOS

## QuickBooks

### Material cost research assistance

**What:** Help users look up material costs when building estimates (supplier APIs, personal price book).

**Why:** Users like Jesse research material costs separately and manually enter them. Automating this removes another manual step from the estimate workflow and is a differentiator vs generic tools.

**Context:** The voice-to-estimate design doc (2026-03-19) explicitly defers this. Jesse described needing to research material costs as a separate step outside QB. Two approaches: (1) Supplier API integrations (Home Depot, Lowe's) are complex and vary by region. (2) A "personal price book" stored in MEMORY.md or a dedicated table, built up over time from the user's own estimates, is simpler and self-improving. Start with approach 2: when the agent creates an estimate with material costs, persist those prices to memory. Over time, the agent can suggest prices based on past jobs. Approach 1 is a bigger lift and depends on supplier API availability.

**Effort:** M (personal price book) / XL (supplier APIs)
**Priority:** P2
**Depends on:** Core voice-to-estimate workflow must ship first

## Calendar

### Calendar Phase 2: CalendarEvent table + iCal feed

**What:** Add a local CalendarEvent table as a write-through cache, plus an iCal feed endpoint (ICalFeedService) that lets users subscribe to their job schedule from any calendar app.

**Why:** Phase 1 only supports Google Calendar users. An iCal feed is provider-agnostic: anyone with Apple Calendar, Outlook, or any CalDAV client can subscribe. This completes the calendar vision from issue #708.

**Context:** The CalendarProvider protocol from Phase 1 is designed to support this. CalendarEvent table stores events locally (write-through on create/update/delete). ICalFeedService generates .ics feeds per user with a secret feed_token for auth. The CalendarConfig model already has a feed_token column ready for this.

**Effort:** M (human: ~1 week / CC: ~30 min)
**Priority:** P2
**Depends on:** Phase 1 calendar integration must ship first

### Multi-calendar selection

**What:** Add a list_calendars tool and calendar picker so users can choose which Google Calendar to use (e.g., a dedicated "Jobs" calendar vs personal).

**Why:** Phase 1 defaults to "primary" calendar. Contractors who keep separate work/personal calendars need to target the right one. Without this, job events end up on the personal calendar.

**Context:** Requires a new calendar_list_calendars tool (GET /users/me/calendarList), a selection UI in the frontend, and storing the chosen calendar_id in CalendarConfig. The CalendarConfig.calendar_id column already exists with default="primary".

**Effort:** S (human: ~3 hours / CC: ~15 min)
**Priority:** P2
**Depends on:** Phase 1 calendar integration must ship first

## Permissions

### /permissions chat command

**What:** Let users view and reset their permission settings via a /permissions chat command. Show current AUTO/ASK/DENY state for each tool category. Support /permissions reset to return to defaults.

**Why:** Without this, users can only change permissions through "always"/"never" responses during approval prompts. They cannot see what they have approved or denied, or reset to defaults. Visibility into permission state builds trust: "I want to see what I've approved." Reduces support burden: "why is my bot auto-sending estimates?"

**Context:** The ApprovalStore already persists all permission data in data/{user_id}/permissions.json. The command would read this file and format it as a human-readable summary grouped by tool category (File operations, Estimates, QuickBooks, Client messaging, Memory). Support "auto estimates" or "ask quickbooks" natural language to change settings. Design doc (2026-03-19) deferred this from v1 to reduce scope.

**Effort:** S (human: ~2 hours / CC: ~10 min)
**Priority:** P2
**Depends on:** Core batch approval system (trust tiers + plan-based approval)

### skip N / only N response parsing

**What:** Extend approval response parsing to support "skip N" (approve all except step N) and "only N" (approve only step N) for granular plan approval.

**Why:** The numbered plan format already shows step numbers (1, 2, 3...) but users can only say yes (all) or no (none). "skip 4" lets users say "create the estimate but don't send it yet" without rejecting the whole plan. Maps to real contractor workflows where you want to prepare but not deliver.

**Context:** Requires extending _parse_approval_response in approval.py with regex for "skip N" and "only N". Edge cases to handle: out-of-range numbers, "skip 0", "only" without a number, "skip 1,3" (multiple). The plan approval response flow in _execute_tool_round needs to support partial approval (some tools execute, some denied). Design doc (2026-03-19) deferred this from v1. The plan message format already numbers steps, so the UX affordance is in place.

**Effort:** S (human: ~3 hours / CC: ~15 min)
**Priority:** P3
**Depends on:** Core batch approval system (trust tiers + plan-based approval)
