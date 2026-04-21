"""CompanyCam checklist tools.

Implements list/get/create for CompanyCam project checklists. Built by
the factory in ``companycam_tools`` which passes the authenticated
service.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.companycam_params import (
    CompanyCamCreateChecklistParams,
    CompanyCamGetChecklistParams,
    CompanyCamListChecklistsParams,
)
from backend.app.agent.tools.companycam_receipts import _sanitize, project_url
from backend.app.agent.tools.names import ToolName
from backend.app.services.companycam import CompanyCamService

logger = logging.getLogger(__name__)


def build_checklist_tools(service: CompanyCamService) -> list[Tool]:
    """Return the CompanyCam checklist Tool instances.

    These tools only interact with the CompanyCam service, so the
    builder does not need the agent's ``ToolContext``.
    """

    async def companycam_list_checklists(project_id: str) -> ToolResult:
        """List checklists for a CompanyCam project."""
        try:
            checklists = await service.list_project_checklists(project_id)
        except Exception as exc:
            logger.exception("CompanyCam list checklists failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        if not checklists:
            return ToolResult(content="No checklists found on this project.")
        lines = [f"Found {len(checklists)} checklist(s):"]
        for cl in checklists:
            status = "completed" if cl.completed_at else "in progress"
            lines.append(f"- {cl.name or 'Untitled'} (ID: {cl.id}) [{status}]")
        return ToolResult(content="\n".join(lines))

    async def companycam_get_checklist(
        project_id: str,
        checklist_id: str,
    ) -> ToolResult:
        """Get detailed checklist with tasks and completion status."""
        try:
            cl = await service.get_checklist(project_id, checklist_id)
        except Exception as exc:
            logger.exception("CompanyCam get checklist failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        status = "completed" if cl.completed_at else "in progress"
        lines = [f"Checklist: {cl.name or 'Untitled'} (ID: {cl.id}) [{status}]"]
        all_tasks = list(cl.sectionless_tasks or [])
        for section in cl.sections or []:
            lines.append(f"\n## {section.title or 'Untitled Section'}")
            for task in section.tasks or []:
                done = "[x]" if task.completed_at else "[ ]"
                lines.append(f"  {done} {task.title or 'Untitled'}")
                all_tasks.append(task)
        if cl.sectionless_tasks:
            for task in cl.sectionless_tasks:
                done = "[x]" if task.completed_at else "[ ]"
                lines.append(f"  {done} {task.title or 'Untitled'}")
        total = len(all_tasks)
        completed = sum(1 for t in all_tasks if t.completed_at)
        lines.append(f"\nProgress: {completed}/{total} tasks completed")
        return ToolResult(content="\n".join(lines))

    async def companycam_create_checklist(
        project_id: str,
        template_id: str,
    ) -> ToolResult:
        """Create a checklist on a project from a template."""
        try:
            cl = await service.create_project_checklist(project_id, template_id)
        except Exception as exc:
            logger.exception("CompanyCam create checklist failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(
            content=(
                f"Created checklist '{cl.name or 'Untitled'}' (ID: {cl.id}) "
                f"on project {project_id}."
            ),
            receipt=ToolReceipt(
                action="Created CompanyCam checklist",
                target=_sanitize(cl.name or "", 40) or "checklist",
                url=project_url(project_id),
            ),
        )

    return [
        Tool(
            name=ToolName.COMPANYCAM_LIST_CHECKLISTS,
            description="List checklists for a CompanyCam project",
            function=companycam_list_checklists,
            params_model=CompanyCamListChecklistsParams,
            usage_hint="Check what checklists exist on a project and their status.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_GET_CHECKLIST,
            description="Get checklist details with tasks and completion status",
            function=companycam_get_checklist,
            params_model=CompanyCamGetChecklistParams,
            usage_hint="View full checklist details including all tasks and progress.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_CREATE_CHECKLIST,
            description="Create a checklist on a CompanyCam project from a template",
            function=companycam_create_checklist,
            params_model=CompanyCamCreateChecklistParams,
            usage_hint=(
                "Create a new checklist from a template. "
                "Use list_checklists or ask the user which template to use."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: "Create a checklist on a CompanyCam project",
            ),
        ),
    ]
