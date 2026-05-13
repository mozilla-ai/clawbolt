# File Cataloging

Clawbolt can save files into **your own Google Drive**. The agent picks the destination folder from the conversation: a job folder for client work, `Inbox` for general saves, or whatever path you ask for.

## How storage is enabled

File storage is per-user and opt-in. To turn it on, ask the assistant to connect Drive (or use the integrations panel). The OAuth flow grants the `drive.file` scope, which means Clawbolt only sees files it created itself, not the rest of your Drive.

Until you connect Drive, the file tools (`upload_to_storage`, `find_saved_files`, etc.) stay disabled. Other integrations like CompanyCam continue to work without Drive.

## File organization

Files land under your Drive's `Clawbolt` folder. The agent organizes by context:

```
Clawbolt/
├── John Smith - 116 Virginia Ave/
│   ├── photos/
│   │   ├── kitchen-before_001.jpg
│   │   └── kitchen-after_002.jpg
│   ├── estimates/
│   │   └── kitchen-remodel_001.pdf
│   └── documents/
│       └── signed-contract_001.pdf
└── Inbox/
    └── reference-photo_001.jpg
```

When the conversation has a clear client, files go under `Client - Address/photos|estimates|documents`. Otherwise they land in `Inbox`. You can also ask for a specific folder (`save this to /Receipts/2026`); Clawbolt will use it as long as the path is sane.

## How it works

When you send media, the agent uses these tools:

1. `upload_to_storage` uploads the file into the folder you (or it) picked. The reply includes a direct Drive link.
2. `move_file` relocates a saved file later when you tell it where it really belongs.
3. `find_saved_files` searches your saved files by filename or description.
4. `analyze_saved_file` runs vision analysis on a previously saved image without asking you to resend it.

The agent references saved files by their storage path (e.g. `/John Smith - 116 Virginia Ave/photos/kitchen-before_001.jpg`). Drive is the source of truth: filenames, locations, and descriptions live on the file in your Drive, not in a separate Clawbolt database.

## Operator setup (self-hosted only)

If you're self-hosting Clawbolt, the operator must register an OAuth client and set:

```
GOOGLE_DRIVE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_DRIVE_CLIENT_SECRET=...
```

See [Google Drive Setup](https://github.com/mozilla-ai/clawbolt/blob/main/docs/self-host/google-drive-setup.md) for the full walkthrough.
