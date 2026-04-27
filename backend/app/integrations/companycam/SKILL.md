# CompanyCam

You now have access to CompanyCam tools for managing job site photo documentation.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| companycam_search_projects | Search projects by name or address | Auto |
| companycam_upload_photo | Upload a photo to a project | Asks user |
| companycam_create_project | Create a new project | Asks user |
| companycam_update_project | Rename a project or update its address | Asks user |

## Workflow: Uploading a Photo

When the user sends a photo with job context (client name, address, work type):

1. Search for the CompanyCam project: `companycam_search_projects(query="123 Main St")`
2. If no project found, create one: `companycam_create_project(name="Smith - 123 Main St", address="123 Main St")`
3. Upload the photo with tags: `companycam_upload_photo(project_id="...", tags=["kitchen", "demo"], description="Kitchen demolition progress")`

## Tags

Derive tags from the conversation context:
- Room or area: "kitchen", "bathroom", "exterior", "roof"
- Work type: "demo", "framing", "finish", "inspection"
- Stage: "before", "during", "after"

## Connection

Users connect via OAuth. Use `manage_integration(action='connect', target='companycam')` to start the authorization flow. The user will be redirected to CompanyCam to grant access.

## Trade-off: Sending Photos via Messaging vs. the CompanyCam App

When a user sends a photo through a messaging channel (iMessage, SMS, Telegram) for you to file into CompanyCam, the photo is also saved to their device camera roll by the messaging app. Photos taken directly inside the CompanyCam mobile app skip the camera roll entirely.

Be aware of this trade-off. Don't lecture the user about it on every upload, but if you notice signs that camera roll clutter is bothering them (they mention it, ask how to keep their photos organized, or are filing many job photos at once), it is appropriate to mention once that snapping photos directly in the CompanyCam app avoids adding them to their camera roll.
