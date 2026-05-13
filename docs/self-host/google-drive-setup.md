# Google Drive Setup

Google Drive is the integration Clawbolt uses to catalog job photos and documents in **each user's own Drive**. It is wired up the same way as any other integration (Google Calendar, QuickBooks, etc.): the operator registers an OAuth client, then each user opts in through the agent (`manage_integration(action='connect', target='google_drive')`) or the integrations panel.

When a user connects Drive, files land in their own account under a top-level `Clawbolt` folder. The integration uses the narrow `drive.file` scope, so the app only sees files it created itself, not the user's existing Drive contents.

Without a Drive connection, the file tools (`upload_to_storage`, `move_file`, `find_saved_files`, `analyze_saved_file`) stay disabled for that user. Other integrations like CompanyCam still work without Drive.

## Operator setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project (or reuse the one you set up for Google Calendar).
2. Enable the **Google Drive API**.
3. Create **OAuth 2.0 credentials** (Web application type).
4. Add an authorized redirect URI matching your deployment: `https://your-host/api/oauth/callback`.
5. Set environment variables:

```bash
GOOGLE_DRIVE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_DRIVE_CLIENT_SECRET=...
```

Once these are set, every user can connect their own Drive.

## Per-user connection flow

In chat, the agent generates an OAuth link via `manage_integration(action='connect', target='google_drive')`. The user taps it, grants `drive.file` scope to your app, and the OAuth callback returns them to Clawbolt with the file tools now enabled for their account.

Disconnect anytime with `manage_integration(action='disconnect', target='google_drive')`.
