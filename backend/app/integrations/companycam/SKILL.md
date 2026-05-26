# CompanyCam

CompanyCam is a photo documentation platform for the trades. Projects, photos, comments, tags, checklists, and documents live in the account; this integration covers the full project lifecycle, photo upload with deduplication, comments and tags, and checklist management.

## Available Tools

### Projects
| Tool | Purpose | Approval |
|------|---------|----------|
| `companycam_search_projects` | Search projects by name or address | Auto |
| `companycam_get_project` | Fetch full details for one project | Auto |
| `companycam_list_documents` | List contracts, specs, and files on a project | Auto |
| `companycam_create_project` | Create a new project | Ask |
| `companycam_update_project` | Rename a project or update its address | Ask |
| `companycam_update_notepad` | Add or update project notes | Ask |
| `companycam_archive_project` | Archive a completed project | Ask |
| `companycam_delete_project` | Permanently delete a project (cannot be undone) | Ask |

### Photos and Comments
| Tool | Purpose | Approval |
|------|---------|----------|
| `companycam_search_photos` | Search photos by project or date range | Auto |
| `companycam_list_comments` | List comments on a project or photo | Auto |
| `companycam_upload_photo` | Upload a photo to a project | Ask |
| `companycam_add_comment` | Add a comment to a project or photo | Ask |
| `companycam_tag_photo` | Add tags to a photo | Ask |
| `companycam_delete_photo` | Permanently delete a photo (cannot be undone) | Ask |

### Checklists
| Tool | Purpose | Approval |
|------|---------|----------|
| `companycam_list_checklists` | List checklists on a project | Auto |
| `companycam_get_checklist` | View full checklist with task completion | Auto |
| `companycam_create_checklist` | Create a checklist on a project from a template | Ask |

## Entity vocabulary

- **Project**: a job site or property. Has `id`, `name`, `address`, `status`, `notepad`, primary contact. Photos, documents, checklists, and comments hang off the project.
- **Photo**: an uploaded image. Has `id`, optional `description`, tags, and a CompanyCam-side `processing_status` (`pending`, `processing`, `processed`, `duplicate`, `processing_error`).
- **Checklist**: a task list bound to a project, instantiated from a template. Sections contain tasks; each task carries a `completed_at` timestamp when done.

## Photo handles

`companycam_upload_photo` requires an `original_url` handle. Pass the value exactly as it appears in the conversation context (`handle=media_XXXXXX`). A blank handle is rejected; do not call the tool without one.

The handle is idempotent within the staging TTL: a retry with the same handle returns the prior receipt rather than re-uploading. If the user actually wants a fresh upload, they need to send the photo again to get a new handle.

CompanyCam may report two non-success outcomes:
- `duplicate`: the same image bytes (MD5) already exist on the project. Surface this so the user knows a re-send was a no-op.
- `processing_error`: CompanyCam could not fetch the temporary URL. The upload is not recorded; a fresh retry on the same handle will try again.

## Dates

`companycam_search_photos` takes ISO 8601 dates for `start_date` and `end_date` (`2026-05-11` or `2026-05-11T08:00:00`). End-of-day is applied automatically to `end_date`.

## Tags

Derive tags from the conversation context. Common dimensions:
- Room or area: `kitchen`, `bathroom`, `exterior`, `roof`
- Work type: `demo`, `framing`, `finish`, `inspection`
- Stage: `before`, `during`, `after`

Tags are trimmed at 50 chars and capped at 10 per call.

## Common Workflows

### Upload a photo with job context
1. `companycam_search_projects(query="<address or client>")`.
2. If no match, `companycam_create_project(name="<Client - Address>", address="<address>")`.
3. `companycam_upload_photo(project_id="...", original_url="media_XXXXXX", tags=[...], description="...")`. One call per photo, one handle per call.
4. Summarize what you assumed (project, tags) in one line so the user can amend.

### Document progress on existing photos
1. After upload, if the user adds context ("this is the finished kitchen"), use `companycam_tag_photo` or `companycam_add_comment(target_type="photo", target_id="...", content="...")`.
2. Use project-level comments (`target_type="project"`) for site-wide notes, not photo-specific observations.

### Project handoff or close-out
1. `companycam_get_project(project_id="...")` to confirm address, contact, status.
2. `companycam_list_documents(project_id="...")` for attached contracts or specs.
3. `companycam_archive_project(project_id="...")` when the job is complete. Use `companycam_delete_project` only on explicit request.

### Work a checklist
1. `companycam_list_checklists(project_id="...")` to see what exists.
2. `companycam_get_checklist(project_id="...", checklist_id="...")` for task-by-task progress.
3. `companycam_create_checklist` requires a `template_id`; ask the user which template to use rather than guessing.

### Find recent work across projects
`companycam_search_photos(start_date="2026-05-01", end_date="2026-05-15")` browses recent photos across all projects. Constrain to one project with `project_id`.

## Companion integrations

- **ServiceTitan**: CompanyCam projects for an ST job are keyed by the customer's address. Resolve the address via `st_get_customer` before `companycam_search_projects`. ServiceTitan customer ids are not CompanyCam project ids.
- **QuickBooks**: cross-link customers by name plus address; the IDs are not interchangeable. Reference uploaded photos in invoice or estimate notes by their CompanyCam URL.
- **AppFolio Vendor**: both tools accept `media_XXXXXX` handles directly. A photo already staged for one can be attached to the other in the same conversation without re-sending.

## Connecting

CompanyCam uses OAuth 2.0 authorization code flow. Use `manage_integration(action='connect', target='companycam')` to start the flow; the user is redirected to CompanyCam to grant access. Until that runs, the tools stay surfaced under "Not connected" in `list_capabilities` and refuse to execute.
