# Storage Providers

Clawbolt can catalog job photos and documents to a storage backend. Three providers are supported.

## Local (default)

Works out of the box with no configuration. Files are saved to `data/storage/` on disk.

```
data/storage/
└── Job Photos/
    └── 2026-02-28/
        ├── site-front.jpg
        └── site-back.jpg
```

This is ideal for development and demos. Set `STORAGE_PROVIDER=local` or leave it unset (it's the default).

When running with Docker Compose, the `data/` directory is bind-mounted to the host (default: `./data` in the project root) so files persist across container rebuilds. Set `CLAWBOLT_DATA_DIR` to change the host path:

```bash
CLAWBOLT_DATA_DIR=/mnt/storage/clawbolt
```

## Dropbox

1. Go to the [Dropbox App Console](https://www.dropbox.com/developers/apps) and create a new app
2. Choose **Scoped access** and **Full Dropbox** (or **App folder** for sandboxed access)
3. Under **Permissions**, enable: `files.content.write`, `files.content.read`, `sharing.write`, `sharing.read`
4. Generate an **access token** on the app's settings page
5. Set environment variables:

```bash
STORAGE_PROVIDER=dropbox
DROPBOX_ACCESS_TOKEN=sl.xxxxx...
```

## Google Drive

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project
2. Enable the **Google Drive API**
3. Create **OAuth 2.0 credentials** (Desktop app type)
4. Complete the OAuth flow to get a credentials JSON
5. Set environment variables:

```bash
STORAGE_PROVIDER=google_drive
GOOGLE_DRIVE_CREDENTIALS_JSON='{"token": "...", "refresh_token": "...", ...}'
```

> **Note:** Storage is currently global: one storage account per deployment. Future versions will support per-user storage credentials so each user's files go to their own cloud account.
