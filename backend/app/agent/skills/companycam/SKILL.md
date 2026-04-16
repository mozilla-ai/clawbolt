# CompanyCam

You now have access to CompanyCam tools for managing job site photo documentation.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| companycam_connect | Connect with an API token | Asks user |
| companycam_search_projects | Search projects by name or address | Auto |
| companycam_upload_photo | Upload a photo to a project | Asks user |
| companycam_create_project | Create a new project | Asks user |

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

Users connect by providing their CompanyCam API token. They can generate one at app.companycam.com/access_tokens. Use `companycam_connect` to validate and store the token.
