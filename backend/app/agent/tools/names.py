"""Canonical tool name constants.

All tool names should be defined here and imported by tool definition modules
and any business logic that checks tool names.  This prevents silent breakage
when a tool is renamed.
"""


class ToolName:
    # Messaging
    SEND_REPLY = "send_reply"
    SEND_MEDIA_REPLY = "send_media_reply"

    # Estimates
    GENERATE_ESTIMATE = "generate_estimate"

    # Invoices
    GENERATE_INVOICE = "generate_invoice"
    CONVERT_ESTIMATE_TO_INVOICE = "convert_estimate_to_invoice"

    # Email
    SEND_DOCUMENT_EMAIL = "send_document_email"

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
    QB_CREATE_ESTIMATE = "qb_create_estimate"
    QB_CREATE_INVOICE = "qb_create_invoice"
    QB_CREATE_CUSTOMER = "qb_create_customer"
    QB_SEND_INVOICE = "qb_send_invoice"
    QB_ESTIMATE_TO_INVOICE = "qb_estimate_to_invoice"

    # Meta-tools
    LIST_CAPABILITIES = "list_capabilities"

    # Heartbeat (not registered in the main tool registry)
    COMPOSE_MESSAGE = "compose_message"
    HEARTBEAT_DECISION = "heartbeat_decision"
