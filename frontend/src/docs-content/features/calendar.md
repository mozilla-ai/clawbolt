# Google Calendar

> **Experimental**
>
> The Google Calendar integration is experimental. Use with caution while this feature is being developed. Double-check events created or modified by the assistant before relying on them.

Clawbolt can connect to your Google Calendar so you can manage your schedule from chat. Ask "am I free Thursday afternoon?" or "schedule a site visit at 123 Main Street for 9am tomorrow."

## What it can do

The integration provides six tools the agent uses to interact with Google Calendar:

| Tool | Purpose | Approval |
|------|---------|----------|
| `calendar_list_calendars` | List enabled calendars and access roles | Automatic |
| `calendar_list_events` | List events in a date range | Automatic |
| `calendar_create_event` | Create a new event | Asks permission |
| `calendar_update_event` | Update an existing event | Asks permission |
| `calendar_delete_event` | Delete an event | Asks permission |
| `calendar_check_availability` | Check free/busy time slots | Automatic |

Read-only tools run automatically. Tools that modify your calendar ask for your confirmation first.

## Setup

### 1. Create a Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project (or use an existing one)
2. Enable the **Google Calendar API** under APIs & Services
3. Configure the **OAuth consent screen** (External type is fine for personal use)

### 2. Create OAuth credentials

1. Go to **APIs & Services > Credentials**
2. Click **Create Credentials > OAuth client ID**
3. Select **Web application** as the application type
4. Add your Clawbolt URL as an authorized redirect URI: `https://<your-domain>/api/oauth/callback`
5. Copy the **Client ID** and **Client Secret**

### 3. Configure Clawbolt

Add these to your `.env` file:

```bash
GOOGLE_CALENDAR_CLIENT_ID=your_client_id
GOOGLE_CALENDAR_CLIENT_SECRET=your_client_secret
```

Also make sure `APP_BASE_URL` is set to your public-facing URL so the OAuth callback works:

```bash
APP_BASE_URL=https://your-clawbolt-domain.com
```

### 4. Connect your account

Users can connect their Google account in two ways:

- **Over chat (preferred):** Ask the assistant "connect my Google Calendar" and it will generate an authorization link. The user taps the link, signs in, and the callback page confirms the connection.
- **From the dashboard:** Open the **Tools** page (`/app/tools`), find Google Calendar, and click **Connect**.

Once connected, the calendar tools become available to the agent.

For chat examples, timezone behavior, and approval behavior, see the [Calendar guide](/docs/guide/calendar).

## Disconnecting

To disconnect Google Calendar, go to the **Tools** page in the web dashboard and click **Disconnect** next to Google Calendar. This revokes the OAuth token. You can reconnect at any time.

## Reference

See the [Configuration](https://github.com/mozilla-ai/clawbolt/blob/main/docs/self-host/configuration.md#google-calendar) page for the full list of environment variables.
