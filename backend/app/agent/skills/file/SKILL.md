# File Tools (Google Drive)

Clawbolt stores persistent files in the user's Google Drive under a top-level `Clawbolt` folder.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| `upload_to_storage` | Upload a photo or document the user sent as an attachment | Ask |
| `write_to_storage` | Create a new text file from content you generate | Ask |
| `edit_storage_file` | Replace exact text in an existing file (read first via `read_from_storage`) | Ask |
| `read_from_storage` | Read a text file and return its contents | Ask |
| `move_file` | Move a saved file to a different folder, optionally rename | Ask |
| `find_saved_files` | Search previously saved files by name or description | Ask |
| `analyze_saved_file` | Run vision analysis on a saved image | Ask |

## When to use storage tools vs. workspace tools

- **Storage tools** operate on user-visible, persistent Google Drive files.
- **Workspace tools** (`read_file`, `write_file`, `edit_file`) operate on internal behavior files such as `USER.md`, `SOUL.md`, `MEMORY.md`, and `PERMISSIONS.json`.

## Writing vs. Uploading

- Use `write_to_storage` for text you generate.
- Use `upload_to_storage` for an attachment the user sent.

## Editing a file

`edit_storage_file` replaces exact text in an existing file. Steps:

1. Call `read_from_storage` to get the current file content.
2. Identify the exact `old_text` to replace. It must match uniquely.
3. Call `edit_storage_file` with `file_path`, `old_text`, and `new_text`.

If `old_text` appears more than once, the tool rejects the edit as ambiguous. Read the file again and provide more context (surrounding lines) to narrow the match.

## Finding saved files

`find_saved_files` searches filenames and descriptions. Pass a query string to narrow results, or leave it empty to list the most recent files. Results include the storage path; quote that path when calling `read_from_storage`, `edit_storage_file`, `move_file`, or `analyze_saved_file`.

## Connecting

Connect Google Drive with `manage_integration(action='connect', target='google_drive')`. Until then, file tools are hidden.
