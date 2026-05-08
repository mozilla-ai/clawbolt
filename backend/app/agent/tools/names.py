"""Canonical tool name constants.

All tool names should be defined here and imported by tool definition modules
and any business logic that checks tool names.  This prevents silent breakage
when a tool is renamed.
"""


class ToolName:
    # Messaging
    SEND_MEDIA_REPLY = "send_media_reply"

    # Heartbeat
    GET_HEARTBEAT = "get_heartbeat"
    UPDATE_HEARTBEAT = "update_heartbeat"

    # File management
    UPLOAD_TO_STORAGE = "upload_to_storage"
    ORGANIZE_FILE = "organize_file"
    FIND_SAVED_FILES = "find_saved_files"
    ANALYZE_SAVED_FILE = "analyze_saved_file"

    # Media (agent-native storage)
    ANALYZE_PHOTO = "analyze_photo"
    DISCARD_MEDIA = "discard_media"

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

    # AppFolio Vendor Portal
    APPFOLIO_CONNECT = "appfolio_connect"
    APPFOLIO_LIST_WORK_ORDERS = "appfolio_list_work_orders"
    APPFOLIO_SEARCH_WORK_ORDERS = "appfolio_search_work_orders"
    APPFOLIO_GET_WORK_ORDER = "appfolio_get_work_order"
    APPFOLIO_LIST_PAYMENTS = "appfolio_list_payments"
    APPFOLIO_GET_PROFILE = "appfolio_get_profile"
    APPFOLIO_ACCEPT_WORK_ORDER = "appfolio_accept_work_order"
    APPFOLIO_SCHEDULE_WORK_ORDER = "appfolio_schedule_work_order"
    APPFOLIO_UPDATE_WORK_ORDER_STATUS = "appfolio_update_work_order_status"
    APPFOLIO_UNDO_WORK_ORDER_STATUS = "appfolio_undo_work_order_status"
    APPFOLIO_LIST_NOTES = "appfolio_list_notes"
    APPFOLIO_ADD_NOTE = "appfolio_add_note"
    APPFOLIO_UPDATE_NOTE = "appfolio_update_note"
    APPFOLIO_MESSAGE_TENANT = "appfolio_message_tenant"
    APPFOLIO_CREATE_INVOICE = "appfolio_create_invoice"
    APPFOLIO_UPLOAD_INVOICE_PDF = "appfolio_upload_invoice_pdf"
    APPFOLIO_UPLOAD_COMPLIANCE_DOC = "appfolio_upload_compliance_doc"
    APPFOLIO_GET_ESTIMATE = "appfolio_get_estimate"
    APPFOLIO_UPDATE_ESTIMATE = "appfolio_update_estimate"
    APPFOLIO_UPDATE_PROFILE = "appfolio_update_profile"

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
