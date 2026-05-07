# File Cataloging

Clawbolt can automatically catalog job photos and documents to **your own Google Drive**. Files are organized by client and type, under a top-level `Clawbolt` folder in your Drive.

## How storage is enabled

File storage is per-user and opt-in. To turn it on, ask the assistant to connect Drive (or use the integrations panel). The OAuth flow grants the `drive.file` scope, which means Clawbolt only sees files it created itself, not the rest of your Drive.

Until you connect Drive, the file tools (`upload_to_storage`, `find_saved_files`, etc.) stay disabled. Other integrations like CompanyCam continue to work without Drive.

## File organization

Files land under your Drive's `Clawbolt` folder, organized by client and type:

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
└── Unsorted/
    └── 2026-02-28/
        └── site-photo_001.jpg
```

Files without a known client land under `Unsorted/<date>/` so nothing is lost when context is missing. The agent can move them later via `organize_file`.

## How it works

When you send media, the agent uses these tools:

1. `upload_to_storage` uploads the file into the appropriate folder in your Drive.
2. `organize_file` moves files between folders (e.g., out of `Unsorted/` into the right client folder).
3. `find_saved_files` searches your saved files by client, filename, or saved description.
4. `analyze_saved_file` runs vision analysis on a previously saved image without asking you to resend it.

The agent references saved files by their storage path (e.g. `/John Smith - 116 Virginia Ave/photos/kitchen-before_001.jpg`). Drive is the source of truth: filenames, locations, and descriptions are stored on the file in your Drive, not in a separate Clawbolt database.

## Operator setup (self-hosted only)

If you're self-hosting Clawbolt, the operator must register an OAuth client and set:

```
GOOGLE_DRIVE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_DRIVE_CLIENT_SECRET=...
```

See [Google Drive Setup](https://github.com/mozilla-ai/clawbolt/blob/main/docs/self-host/google-drive-setup.md) for the full walkthrough.
