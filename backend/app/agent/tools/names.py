"""Canonical tool name constants.

All tool names should be defined here and imported by tool definition modules
and any business logic that checks tool names.  This prevents silent breakage
when a tool is renamed.
"""


class ToolName:
    # Memory
    SAVE_FACT = "save_fact"
    RECALL_FACTS = "recall_facts"
    FORGET_FACT = "forget_fact"

    # Messaging
    SEND_REPLY = "send_reply"
    SEND_MEDIA_REPLY = "send_media_reply"

    # Estimates
    GENERATE_ESTIMATE = "generate_estimate"

    # Checklist
    ADD_CHECKLIST_ITEM = "add_checklist_item"
    LIST_CHECKLIST_ITEMS = "list_checklist_items"
    REMOVE_CHECKLIST_ITEM = "remove_checklist_item"

    # File management
    UPLOAD_TO_STORAGE = "upload_to_storage"
    ORGANIZE_FILE = "organize_file"

    # Profile
    VIEW_PROFILE = "view_profile"
    UPDATE_PROFILE = "update_profile"

    # Workspace files
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    EDIT_FILE = "edit_file"

    # Meta-tools
    LIST_CAPABILITIES = "list_capabilities"

    # Heartbeat (not registered in the main tool registry)
    COMPOSE_MESSAGE = "compose_message"
