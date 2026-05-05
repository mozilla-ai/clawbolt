# CompanyCam

You now have access to CompanyCam tools for managing job site photo documentation.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| companycam_search_projects | Search projects by name or address | Auto |
| companycam_upload_photo | Upload a photo to a project | Asks user |
| companycam_create_project | Create a new project | Asks user |
| companycam_update_project | Rename a project or update its address | Asks user |

## Propose-then-veto

When the user sends a photo with partial job context, do not interview them for tags, project name, or description. Infer defaults from context (recent client, conversation topic, photo content) and upload. Surface what you tagged it as so the user can correct in one reply: "Uploaded to Smith - 123 Main St, tagged kitchen/demo. Change anything?"

## Workflow: Uploading a Photo

When the user sends a photo with job context (client name, address, work type):

1. Search for the CompanyCam project: `companycam_search_projects(query="123 Main St")`
2. If no project found, create one with a sensible default name: `companycam_create_project(name="Smith - 123 Main St", address="123 Main St")`
3. Upload the photo with tags inferred from context: `companycam_upload_photo(project_id="...", tags=["kitchen", "demo"], description="Kitchen demolition progress")`
4. Summarize what you assumed (project, tags) in one short line so the user can amend.

## Tags

Derive tags from the conversation context:
- Room or area: "kitchen", "bathroom", "exterior", "roof"
- Work type: "demo", "framing", "finish", "inspection"
- Stage: "before", "during", "after"

## Connection

Users connect via OAuth. Use `manage_integration(action='connect', target='companycam')` to start the authorization flow. The user will be redirected to CompanyCam to grant access.
