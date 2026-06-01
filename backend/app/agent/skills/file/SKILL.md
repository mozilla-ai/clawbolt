# File Tools (Google Drive)

Clawbolt stores files in the user's Google Drive under a top-level `Clawbolt` folder. Photos, documents, and text files uploaded or created here persist across conversations and are visible via `find_saved_files`.

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

- **Storage tools** (`upload_to_storage`, `write_to_storage`, `edit_storage_file`, `read_from_storage`, `move_file`, `find_saved_files`, `analyze_saved_file`) operate on the user's Google Drive. Files here persist across conversations and are visible outside the chat (the user can open them in Drive). Use storage tools for documents, notes, photos, invoices, and any content the user may want to access outside of Clawbolt.

- **Workspace tools** (`read_file`, `write_file`, `edit_file`) operate on Clawbolt's internal state files: `USER.md` (user profile), `SOUL.md` (personality), `MEMORY.md` (long-term facts), `PERMISSIONS.json` (tool permissions). These files are invisible to the user and only affect agent behavior. Use workspace tools to update what the agent knows about the user or how it behaves.

## Writing vs. Uploading

- `write_to_storage`: Use when the user asks you to create a file from text you write (notes, summaries, documents, lists, etc.). Provide the filename and the text content. The file is created as a new file; if the name already exists, a numeric suffix is added to avoid overwriting.

- `upload_to_storage`: Use when the user sends an attachment (photo, PDF, document) and asks you to save it. The tool reads the attachment bytes, not text you generate.

## Editing a file

`edit_storage_file` replaces exact text in an existing file. Steps:

1. Call `read_from_storage` to get the current file content.
2. Identify the exact `old_text` to replace. It must match uniquely.
3. Call `edit_storage_file` with `file_path`, `old_text`, and `new_text`.

If `old_text` appears more than once, the tool rejects the edit as ambiguous. Read the file again and provide more context (surrounding lines) to narrow the match.

## Reading a file

`read_from_storage` returns the full text content of a file. Use it before editing to see what is currently in the file, or when the user asks you to check the contents of a saved note or document.

## Finding saved files

`find_saved_files` searches filenames and descriptions. Pass a query string to narrow results, or leave it empty to list the most recent files. Results include the storage path; quote that path when calling `read_from_storage`, `edit_storage_file`, `move_file`, or `analyze_saved_file`.

## Connecting

The user must connect Google Drive via `manage_integration(action='connect', target='google_drive')`. Until they do, all file tools are hidden.
