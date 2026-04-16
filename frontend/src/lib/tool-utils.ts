/**
 * Shared tool display names and OAuth mappings.
 *
 * Centralised here so that DashboardPage, ToolsPage, and any future page
 * that renders tool status stay in sync. When adding a new tool:
 *
 * 1. Add an entry to DISPLAY_NAMES.
 * 2. Add sub-tool entries to SUB_TOOL_NAMES.
 * 3. If the tool requires OAuth or token auth, add it to TOOL_OAUTH_MAP.
 *    If it does NOT require auth (like supplier_pricing), leave it out
 *    and it will be treated as "always available."
 * 4. If the tool uses API tokens (not OAuth), add it to TOKEN_BASED_INTEGRATIONS.
 */

/** Map tool factory names to integration identifiers. Tools NOT in this
 *  map are treated as non-connectable (always configured, always connected). */
export const TOOL_OAUTH_MAP: Record<string, string> = {
  quickbooks: 'quickbooks',
  calendar: 'google_calendar',
  companycam: 'companycam',
};

/** Integrations that use API tokens instead of OAuth.
 *  These show a token input field instead of an OAuth redirect. */
export const TOKEN_BASED_INTEGRATIONS = new Set(['companycam']);

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
  file: 'File Storage',
  heartbeat: 'Heartbeat',
  permissions: 'Permissions',
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
  companycam_connect: 'Connect to CompanyCam',
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
 * For connectable tools: checks TOOL_OAUTH_MAP + oauthMap for configured/connected.
 * For non-connectable tools: uses the `configured` field from the backend API response
 * (populated from the tool's auth_check). If the backend says configured=false,
 * the tool shows as "Not configured" (e.g. missing SERPAPI_API_KEY).
 */
export function getToolOAuthStatus(
  toolName: string,
  oauthMap: Record<string, { configured?: boolean; connected?: boolean }>,
  backendConfigured?: boolean,
): { needsOAuth: boolean; isConfigured: boolean; isConnected: boolean } {
  const oauthIntegration = TOOL_OAUTH_MAP[toolName];
  const needsOAuth = !!oauthIntegration;
  if (!needsOAuth) {
    const configured = backendConfigured ?? true;
    return { needsOAuth: false, isConfigured: configured, isConnected: configured };
  }
  const entry = oauthMap[oauthIntegration];
  return {
    needsOAuth: true,
    isConfigured: entry?.configured ?? false,
    isConnected: entry?.connected ?? false,
  };
}
