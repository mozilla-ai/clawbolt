"""Pydantic parameter models for CompanyCam tools.

Extracted from ``companycam_tools.py`` to keep that entrypoint module
focused on registration and factory wiring. Imported by
``companycam_projects``, ``companycam_photos``, and ``companycam_checklists``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CompanyCamSearchParams(BaseModel):
    query: str = Field(description="Search term: project name, address, or keyword")


class CompanyCamCreateProjectParams(BaseModel):
    name: str = Field(description="Project name (typically client name and address)")
    address: str = Field(default="", description="Street address for the project")


class CompanyCamUpdateProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to update")
    name: str = Field(default="", description="New project name (leave empty to keep current)")
    address: str = Field(default="", description="New street address (leave empty to keep current)")


class CompanyCamUploadPhotoParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to upload to")
    original_url: str = Field(
        default="",
        description=(
            "The original_url of a photo from the current conversation. "
            "If empty, uploads the most recent photo."
        ),
    )
    description: str = Field(default="", description="Photo description")
    tags: list[str] = Field(default_factory=list, description="Tags to apply to the photo")


class CompanyCamGetProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")


class CompanyCamArchiveProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to archive")


class CompanyCamDeleteProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to permanently delete")


class CompanyCamUpdateNotepadParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    notepad: str = Field(description="New notepad content for the project")


class CompanyCamListDocumentsParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    page: int = Field(default=1, description="Page number (default 1)")


class CompanyCamAddCommentParams(BaseModel):
    target_type: str = Field(description="Type of target: 'project' or 'photo'")
    target_id: str = Field(description="ID of the project or photo to comment on")
    content: str = Field(description="Comment text")


class CompanyCamListCommentsParams(BaseModel):
    target_type: str = Field(description="Type of target: 'project' or 'photo'")
    target_id: str = Field(description="ID of the project or photo")
    page: int = Field(default=1, description="Page number (default 1)")


class CompanyCamTagPhotoParams(BaseModel):
    photo_id: str = Field(description="CompanyCam photo ID to tag")
    tags: list[str] = Field(description="Tags to add to the photo")


class CompanyCamDeletePhotoParams(BaseModel):
    photo_id: str = Field(description="CompanyCam photo ID to permanently delete")


class CompanyCamSearchPhotosParams(BaseModel):
    project_id: str = Field(
        default="",
        description="Optional: filter to a specific project ID",
    )
    start_date: str = Field(
        default="",
        description="Optional: start date filter (ISO format, e.g. 2024-01-15)",
    )
    end_date: str = Field(
        default="",
        description="Optional: end date filter (ISO format, e.g. 2024-01-31)",
    )
    page: int = Field(default=1, description="Page number (default 1)")


class CompanyCamListChecklistsParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")


class CompanyCamGetChecklistParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    checklist_id: str = Field(description="Checklist ID to retrieve")


class CompanyCamCreateChecklistParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    template_id: str = Field(
        description="Checklist template ID to create from (use list_checklists to find templates)"
    )
