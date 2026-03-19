"""Canonical tool name constants.

All tool names should be defined here and imported by tool definition modules
and any business logic that checks tool names.  This prevents silent breakage
when a tool is renamed.
"""


class ToolName:
    # Messaging
    SEND_REPLY = "send_reply"
    SEND_MEDIA_REPLY = "send_media_reply"

    # Heartbeat
    GET_HEARTBEAT = "get_heartbeat"
    UPDATE_HEARTBEAT = "update_heartbeat"

    # File management
    UPLOAD_TO_STORAGE = "upload_to_storage"
    ORGANIZE_FILE = "organize_file"

    # Workspace files
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    EDIT_FILE = "edit_file"
    DELETE_FILE = "delete_file"

    # QuickBooks
    QB_QUERY = "qb_query"
    QB_CREATE = "qb_create"
    QB_SEND = "qb_send"

    # Meta-tools
    LIST_CAPABILITIES = "list_capabilities"

    # Heartbeat (not registered in the main tool registry)
    COMPOSE_MESSAGE = "compose_message"
    HEARTBEAT_DECISION = "heartbeat_decision"
