# ServiceNow FSM

You now have access to ServiceNow Field Service Management tools for managing work orders, tasks, and time tracking.

## Available Tools

| Tool | Purpose | Approval |
|------|---------|----------|
| servicenow_list_work_orders | List work orders assigned to you | Auto |
| servicenow_get_work_order | Get full work order details | Auto |
| servicenow_list_tasks | List tasks for a work order | Auto |
| servicenow_update_task | Update task state | Asks user |
| servicenow_add_work_order_note | Add a work note to a work order | Asks user |
| servicenow_add_task_note | Add a work note to a task | Asks user |
| servicenow_log_time | Log time worked on a task | Asks user |
| servicenow_search | Search work orders by text | Auto |

## Task States

Work order tasks use these states (in order):

1. **Pending Dispatch** - Not yet assigned
2. **Assigned** - Assigned to a technician
3. **Accepted** - Technician accepted the assignment
4. **Work In Progress** - Work has started
5. **Closed Complete** - Work finished successfully
6. **Closed Incomplete** - Work could not be completed
7. **Cancelled** - Task was cancelled

## Common Workflows

### Check assigned work
1. `servicenow_list_work_orders()` - Lists work orders assigned to the current user
2. For a specific work order: `servicenow_get_work_order(sys_id="...")`
3. To see task breakdown: `servicenow_list_tasks(work_order_id="...")`

### Update task progress
1. Accept: `servicenow_update_task(sys_id="...", state="Accepted")`
2. Start work: `servicenow_update_task(sys_id="...", state="Work In Progress")`
3. Complete: `servicenow_update_task(sys_id="...", state="Closed Complete")`

### Add notes
Use notes to document findings, issues, or status updates visible to the team:
- `servicenow_add_task_note(sys_id="...", note="Found mold behind old unit, documented with photos")`
- `servicenow_add_work_order_note(sys_id="...", note="Customer requested rescheduling to next week")`

### Log time
Record hours worked on a task:
- `servicenow_log_time(task_id="...", hours=2.5, date="2026-04-23", category="labor")`

## Connection

Users connect via OAuth. Use `manage_integration(action='connect', target='servicenow')` to start the authorization flow. The user will be redirected to their ServiceNow instance to grant access.
