/**
 * Shared tool display names and OAuth helpers.
 *
 * Centralised here so that DashboardPage, ToolsPage, and any future page
 * that renders tool status stay in sync. When adding a new tool:
 *
 * 1. Add an entry to DISPLAY_NAMES.
 * 2. Add sub-tool entries to SUB_TOOL_NAMES.
 *
 * The OAuth integration backing each tool is carried on the API response
 * itself (``ToolConfigEntryResponse.oauth_name``), so new OAuth-backed
 * integrations do not need to touch this file at all.
 */

/** Human-readable display names for tool factories. */
const DISPLAY_NAMES: Record<string, string> = {
  quickbooks: 'QuickBooks',
  calendar: 'Google Calendar',
  companycam: 'CompanyCam',
  supplier_pricing: 'Pricing Tools',
  workspace: 'Workspace',
  profile: 'Profile',
  memory: 'Memory',
  messaging: 'Messaging',
  file: 'Google Drive',
  heartbeat: 'Heartbeat',
  permissions: 'Permissions',
  gmail: 'Gmail',
  appfolio_vendor: 'AppFolio Vendor Portal',
  servicetitan: 'ServiceTitan',
};

/** Human-readable sub-tool display names. */
const SUB_TOOL_NAMES: Record<string, string> = {
  qb_query: 'Query entities',
  qb_create: 'Create entities',
  qb_update: 'Update entities',
  qb_send: 'Send documents',
  calendar_list_calendars: 'List calendars',
  calendar_list_events: 'List events',
  calendar_create_event: 'Create events',
  calendar_update_event: 'Update events',
  calendar_delete_event: 'Delete events',
  calendar_check_availability: 'Check availability',
  read_file: 'Read files',
  write_file: 'Write files',
  edit_file: 'Edit files',
  delete_file: 'Delete files',
  upload_to_storage: 'Upload files',
  organize_file: 'Organize files',
  get_heartbeat: 'Read heartbeat',
  update_heartbeat: 'Update heartbeat',
  send_reply: 'Send replies',
  send_media_reply: 'Send media',
  update_permission: 'Change permissions',
  companycam_search_projects: 'Search projects',
  companycam_create_project: 'Create project',
  companycam_update_project: 'Update project',
  companycam_upload_photo: 'Upload photo',
  supplier_search_products: 'Search products',
};

export function displayName(name: string): string {
  return DISPLAY_NAMES[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

export function subToolDisplayName(name: string): string {
  return SUB_TOOL_NAMES[name] ?? name.split('_').join(' ');
}

/**
 * Determine whether a tool needs auth and its connection/config status.
 *
 * For OAuth-backed tools (``oauthName`` non-empty): looks up the connection
 * state in *oauthMap*, which mirrors the /oauth/status response.
 * For non-OAuth tools: uses the ``configured`` field from /user/tools
 * (populated from the tool's ``auth_check``). If the backend says
 * configured=false, the tool shows as "Not configured" (e.g. missing
 * SERPAPI_API_KEY for supplier_pricing).
 */
export function getToolOAuthStatus(
  oauthName: string,
  oauthMap: Record<string, { configured?: boolean; connected?: boolean }>,
  backendConfigured?: boolean,
): { needsOAuth: boolean; isConfigured: boolean; isConnected: boolean } {
  const needsOAuth = !!oauthName;
  if (!needsOAuth) {
    const configured = backendConfigured ?? true;
    return { needsOAuth: false, isConfigured: configured, isConnected: configured };
  }
  const entry = oauthMap[oauthName];
  return {
    needsOAuth: true,
    isConfigured: entry?.configured ?? false,
    isConnected: entry?.connected ?? false,
  };
}
