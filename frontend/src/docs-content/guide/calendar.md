# Calendar

With Google Calendar connected, you can check your schedule, book jobs, and manage appointments through text.

## Examples

```
You: What do I have tomorrow?

Clawbolt: Here's your schedule for tomorrow (Tuesday):
          - 8:00 AM: Site visit at Johnson residence
          - 11:30 AM: Lunch with supplier
          - 2:00 PM: Estimate walkthrough at 42 Oak St
```

```
You: Schedule a bathroom remodel at the Davis place
     for next Monday 8am to 4pm

Clawbolt: I'd like to create this event:
          "Job: Davis - Bathroom remodel"
          Monday March 30, 8:00 AM - 4:00 PM
          Approve?

You: Yes

Clawbolt: Done! Event created on your calendar.
```

Your assistant always asks for approval before creating, changing, or deleting events. Read-only actions (checking schedule, checking availability) happen automatically.

## Timezone

Your assistant uses your timezone when interpreting times. Make sure it's set correctly in the dashboard (User page).

## Getting connected

Ask your assistant to connect Google Calendar:

```
You: Connect my Google Calendar

Clawbolt: Here's a link to connect your Google account:
          [link]
          Tap it, sign in, and grant calendar access.
```

You can also connect from the Tools page in the web dashboard. You can control which calendar actions are enabled (list, create, update, delete) from there. See [Google Calendar](/docs/features/calendar) for technical setup details.
