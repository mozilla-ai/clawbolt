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
    QB_UPDATE = "qb_update"
    QB_SEND = "qb_send"

    # Calendar
    CALENDAR_LIST_CALENDARS = "calendar_list_calendars"
    CALENDAR_LIST_EVENTS = "calendar_list_events"
    CALENDAR_CREATE_EVENT = "calendar_create_event"
    CALENDAR_UPDATE_EVENT = "calendar_update_event"
    CALENDAR_DELETE_EVENT = "calendar_delete_event"
    CALENDAR_CHECK_AVAILABILITY = "calendar_check_availability"

    # CompanyCam
    COMPANYCAM_CONNECT = "companycam_connect"
    COMPANYCAM_SEARCH_PROJECTS = "companycam_search_projects"
    COMPANYCAM_CREATE_PROJECT = "companycam_create_project"
    COMPANYCAM_UPDATE_PROJECT = "companycam_update_project"
    COMPANYCAM_UPLOAD_PHOTO = "companycam_upload_photo"
    COMPANYCAM_GET_PROJECT = "companycam_get_project"
    COMPANYCAM_ARCHIVE_PROJECT = "companycam_archive_project"
    COMPANYCAM_DELETE_PROJECT = "companycam_delete_project"
    COMPANYCAM_UPDATE_NOTEPAD = "companycam_update_notepad"
    COMPANYCAM_LIST_DOCUMENTS = "companycam_list_documents"
    COMPANYCAM_ADD_COMMENT = "companycam_add_comment"
    COMPANYCAM_LIST_COMMENTS = "companycam_list_comments"
    COMPANYCAM_TAG_PHOTO = "companycam_tag_photo"
    COMPANYCAM_DELETE_PHOTO = "companycam_delete_photo"
    COMPANYCAM_SEARCH_PHOTOS = "companycam_search_photos"
    COMPANYCAM_LIST_CHECKLISTS = "companycam_list_checklists"
    COMPANYCAM_GET_CHECKLIST = "companycam_get_checklist"
    COMPANYCAM_CREATE_CHECKLIST = "companycam_create_checklist"

    # Supplier pricing
    SUPPLIER_SEARCH_PRODUCTS = "supplier_search_products"

    # Calculator
    CALCULATE = "calculate"

    # Meta-tools
    LIST_CAPABILITIES = "list_capabilities"
    MANAGE_INTEGRATION = "manage_integration"

    # Heartbeat (not registered in the main tool registry)
    COMPOSE_MESSAGE = "compose_message"
    HEARTBEAT_DECISION = "heartbeat_decision"
