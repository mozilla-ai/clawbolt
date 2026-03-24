# Google Calendar

You now have access to Google Calendar tools. Here is how to use them effectively.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| `calendar_list_events` | List events in a date range | Auto |
| `calendar_create_event` | Create a new event | Asks user |
| `calendar_update_event` | Update an existing event | Asks user |
| `calendar_delete_event` | Delete an event | Asks user |
| `calendar_check_availability` | Check free/busy status | Auto |

## Date Format

All dates use ISO 8601 format: `2026-03-25T09:00:00`

Use the user's timezone from their profile. If unknown, ask.

## Event Naming Convention

For job-related events, use this format:
- Title: `Job: {Client Name} - {Brief Description}`
- Location: the job site address
- Description: scope of work, materials needed, or other notes

Examples:
- `Job: Smith - Kitchen Remodel` at `123 Oak St, Portland OR`
- `Job: Jones - Roof Repair` at `456 Elm Ave, Seattle WA`

## Availability Checking

Always check availability before suggesting times or creating events:
1. Use `calendar_check_availability` with the proposed date range
2. If busy slots exist, suggest alternative times
3. Only create the event after confirming with the user

## Deduplication

Before creating an event, check for existing events in the same time range:
1. Use `calendar_list_events` for the target date range
2. Look for events with similar titles or times
3. If a match exists, ask the user: "There's already an event at that time. Update it instead?"

## Common Workflows

### Schedule a new job
1. `calendar_check_availability` for the proposed date/time
2. If free, confirm details with user
3. `calendar_create_event` with job title format, location, and description
4. Confirm: "Scheduled Job: Smith - Kitchen Remodel for March 25, 9 AM - 5 PM"

### Check the week's schedule
1. `calendar_list_events` for the current week (Monday to Sunday)
2. Summarize events grouped by day
3. Note any free blocks for potential scheduling

### Reschedule a job
1. `calendar_list_events` to find the event and get its ID
2. `calendar_check_availability` for the new proposed time
3. `calendar_update_event` with the event ID and new start/end
4. Confirm the change with the user

### Cancel a job
1. `calendar_list_events` to find the event and get its ID
2. Confirm with the user before deleting
3. `calendar_delete_event` with the event ID

### Find free time for a new job
1. `calendar_check_availability` for the next few days
2. Identify free blocks long enough for the job
3. Suggest available slots to the user

## Tips

- Events created via these tools appear immediately in Google Calendar on all devices
- The event ID returned by `calendar_create_event` can be used later for updates or deletes
- For multi-day jobs, create separate events for each day rather than one spanning event
- When the user mentions a time without a date, assume the next occurrence of that time
