import client from '@/lib/api-client';

// --- Types (match backend schemas exactly) ---

export interface AdminUser {
  id: string;
  user_id: string;
  email: string;
  plan: string;
  status: string;
  role: string;
  is_active: boolean;
  onboarding_complete: boolean;
  created_at?: string | null;
  last_login_at?: string | null;
  last_message_at?: string | null;
  messages_this_month?: number;
  // Research data sharing snapshot (added on the backend in #353).
  // ``data_sharing_consent`` is the current opt-in flag, ``consent_at``
  // is the latest toggle time (opt-in OR opt-out), and
  // ``conversation_count`` is the per-user session total. The Users
  // tab uses these to render the consent badge and the
  // "shared only / no consent" filter without a second round trip.
  data_sharing_consent?: boolean;
  data_sharing_consent_at?: string | null;
  conversation_count?: number;
}

export type UserSort = 'recent' | 'oldest' | 'last_message' | 'plan' | 'email' | 'consent';
export type ConsentFilter = 'all' | 'shared' | 'none';

export interface AdminUserList {
  total: number;
  skip: number;
  limit: number;
  items: AdminUser[];
}

export interface AdminStats {
  telegram_configured?: boolean;
  bluebubbles_configured?: boolean;
  twilio_configured?: boolean;
}

export interface AllowedEmail {
  id: number;
  email: string;
  note: string;
  created_at: string;
}

export interface AllowedEmailList {
  total: number;
  items: AllowedEmail[];
}

// User-authored content (memory, soul, user text, heartbeat directives,
// message bodies, tool-call args/results) was removed from the default
// admin response in #325 work item 2. Surfaces only via the consent-gated
// paths once items 3 + 4 land. The Admin{Message,ToolCall} types from
// before are gone; they'll come back with the consent-path interfaces.

export interface AdminToolConfigEntry {
  tool_name: string;
  enabled: boolean;
}

export interface AdminChannelRouteEntry {
  channel: string;
  channel_identifier: string;
  enabled: boolean;
  last_inbound_at: string | null;
}

export interface AdminUserPermissionEntry {
  tool_name: string;
  level: string;
}

export interface AdminUserResourcePermissionEntry {
  tool_name: string;
  resource: string;
  level: string;
}

export interface AdminUserPermissions {
  tools: AdminUserPermissionEntry[];
  resources: AdminUserResourcePermissionEntry[];
}

export interface AdminUserDetail {
  id: string;
  user_id: string;
  email: string;
  plan: string;
  status: string;
  role: string;
  is_active: boolean;
  onboarding_complete: boolean;
  subscription_created_at: string | null;
  subscription_updated_at: string | null;
  // Profile config (not content)
  timezone: string;
  preferred_channel: string;
  heartbeat_opt_in: boolean;
  heartbeat_frequency: string;
  // Integrations / configuration
  tool_configs: AdminToolConfigEntry[];
  channel_routes: AdminChannelRouteEntry[];
  permissions: AdminUserPermissions;
}

export interface HeartbeatLogItem {
  id: number;
  user_id: string;
  action_type: string;
  channel: string;
  created_at: string;
}

export interface HeartbeatLogList {
  total: number;
  items: HeartbeatLogItem[];
}

export interface LLMUsageLogItem {
  id: number;
  timestamp: string;
  provider: string;
  model: string;
  purpose: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: string;
  cache_creation_input_tokens: number | null;
  cache_read_input_tokens: number | null;
}

export interface LLMUsageLogList {
  total: number;
  items: LLMUsageLogItem[];
}

// --- Helpers ---

function throwApiError(error: unknown, fallback: string): never {
  const b = error as { detail?: string };
  throw new Error(b?.detail || fallback);
}

// --- Stats ---

export async function getAdminStats(): Promise<AdminStats> {
  const { data, error } = await client.GET('/api/admin/stats' as never);
  if (error) throwApiError(error, 'Failed to load stats');
  return data as AdminStats;
}

// --- Version metadata (overview card + auto-reload) ---
//
// ``started_at`` is the load-bearing field for auto-reload: it changes on
// every fresh process, so the admin shell can detect a deploy by comparing
// the value seen on mount against subsequent polls. Commit / version are
// display-only.

export interface AdminVersion {
  premium_version: string;
  premium_commit: string;
  oss_version: string;
  oss_commit: string;
  started_at: string;
}

export async function getAdminVersion(): Promise<AdminVersion> {
  const { data, error } = await client.GET('/api/admin/version' as never);
  if (error) throwApiError(error, 'Failed to load version');
  return data as AdminVersion;
}

// --- Users ---

export async function getAdminUsers(params?: {
  limit?: number;
  offset?: number;
  search?: string;
  sort?: UserSort;
  consent?: ConsentFilter;
}): Promise<AdminUserList> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set('limit', String(params.limit));
  if (params?.offset != null) query.set('offset', String(params.offset));
  if (params?.search) query.set('search', params.search);
  if (params?.sort) query.set('sort', params.sort);
  if (params?.consent && params.consent !== 'all') query.set('consent', params.consent);
  const qs = query.toString();
  const { data, error } = await client.GET(`/api/admin/users${qs ? `?${qs}` : ''}` as never);
  if (error) throwApiError(error, 'Failed to load users');
  return data as AdminUserList;
}

/**
 * Find one user by id, paging the list endpoint until the row turns up.
 *
 * ``/admin/users/{id}`` returns the detail projection, which deliberately
 * omits the consent snapshot, conversation count, and messages-this-month
 * that user-detail renders. Those exist only on the list response, and the
 * list has no "filter by id" parameter (``search`` matches email / user_id,
 * not the primary key), so we page.
 *
 * ``limit`` is capped at 200 server-side. The scan stops at ``maxPages`` so
 * a mistyped id costs a bounded number of round-trips rather than walking
 * the whole table.
 */
export async function findAdminUserById(
  id: string,
  { maxPages = 10 }: { maxPages?: number } = {},
): Promise<AdminUser | null> {
  const pageSize = 200;
  for (let page = 0; page < maxPages; page++) {
    const res = await getAdminUsers({ limit: pageSize, offset: page * pageSize });
    const match = res.items.find(u => u.id === id);
    if (match) return match;
    if (res.items.length < pageSize || (page + 1) * pageSize >= res.total) break;
  }
  return null;
}

export async function activateUser(id: string): Promise<void> {
  const { error } = await client.POST(`/api/admin/users/${id}/activate` as never);
  if (error) throwApiError(error, 'Failed to activate user');
}

export async function deactivateUser(id: string): Promise<void> {
  const { error } = await client.POST(`/api/admin/users/${id}/deactivate` as never);
  if (error) throwApiError(error, 'Failed to deactivate user');
}

export async function resetUserQuota(id: string): Promise<void> {
  const { error } = await client.POST(`/api/admin/users/${id}/reset-quota` as never);
  if (error) throwApiError(error, 'Failed to reset quota');
}

export interface AdminUserUsage {
  messages: { used: number; limit: number };
  tokens: { used: number; limit: number };
  period_start: string | null;
  period_cost_usd: string;
  lifetime_cost_usd: string;
}

export async function getUserUsage(userId: string): Promise<AdminUserUsage> {
  const { data, error } = await client.GET(
    `/api/admin/usage/${encodeURIComponent(userId)}` as never,
  );
  if (error) throwApiError(error, 'Failed to load usage');
  return data as AdminUserUsage;
}

// Admin-triggered compaction of a user's currently-visible context.
// ``keepRecent`` preserves the tail; ``hint`` is prepended to the
// compaction LLM's <conversation> block as ``[admin note: ...]``.
export interface CompactUserContextResult {
  compacted_message_count: number;
  new_watermark: number | null;
  memory_updated: boolean;
  event_id: number | null;
}

export async function compactUserContext(
  id: string,
  body: { keepRecent?: number; hint?: string } = {},
): Promise<CompactUserContextResult> {
  const { data, error } = await client.POST(
    `/api/admin/users/${id}/compact-now` as never,
    {
      body: {
        keep_recent: body.keepRecent ?? 0,
        hint: body.hint && body.hint.trim() ? body.hint.trim() : null,
      },
    } as never,
  );
  if (error) throwApiError(error, 'Failed to compact user context');
  return data as CompactUserContextResult;
}

export async function deleteUser(id: string): Promise<void> {
  const { error } = await client.DELETE(`/api/admin/users/${id}` as never);
  if (error) throwApiError(error, 'Failed to delete user');
}

// Captured LLM request payloads (consent-gated). Returns the JSON body
// of the export endpoint; the caller is responsible for triggering the
// browser download. Throws when the user has no captures yet (404),
// has revoked consent (404), or any other API error.
export interface LLMPayloadEra {
  payload: Record<string, unknown>;
  captured_at: string | null;
  min_message_seq: number | null;
  request_id: string | null;
  payload_bytes: number | null;
}

export interface LLMPayloadExport {
  user_id: string;
  exported_at: string;
  current_era: LLMPayloadEra;
  previous_era: LLMPayloadEra | null;
}

export async function exportUserLLMPayloads(id: string): Promise<LLMPayloadExport> {
  const { data, error } = await client.GET(
    `/api/admin/users/${encodeURIComponent(id)}/llm-payloads` as never,
  );
  if (error) throwApiError(error, 'Failed to export LLM payloads');
  return data as LLMPayloadExport;
}

export async function getUserDetail(id: string): Promise<AdminUserDetail> {
  const { data, error } = await client.GET(`/api/admin/users/${encodeURIComponent(id)}` as never);
  if (error) throwApiError(error, 'Failed to load user detail');
  return data as AdminUserDetail;
}

// --- Allowed Emails ---

export async function listAllowedEmails(): Promise<AllowedEmailList> {
  const { data, error } = await client.GET('/api/admin/allowed-emails' as never);
  if (error) throwApiError(error, 'Failed to load allowed emails');
  return data as AllowedEmailList;
}

export async function addAllowedEmail(body: {
  email: string;
  note?: string;
}): Promise<AllowedEmail> {
  const { data, error } = await client.POST('/api/admin/allowed-emails' as never, {
    body,
  } as never);
  if (error) throwApiError(error, 'Failed to add allowed email');
  return data as AllowedEmail;
}

export async function removeAllowedEmail(id: number): Promise<void> {
  const { error } = await client.DELETE(`/api/admin/allowed-emails/${id}` as never);
  if (error) throwApiError(error, 'Failed to remove allowed email');
}

// --- Waitlist ---

export interface WaitlistEntry {
  id: number;
  email: string;
  name: string;
  use_case: string | null;
  source: string;
  created_at: string;
}

export interface WaitlistEntryList {
  total: number;
  items: WaitlistEntry[];
}

export async function listWaitlistEntries(
  params: { offset?: number; limit?: number } = {},
): Promise<WaitlistEntryList> {
  const qs = new URLSearchParams();
  if (params.offset) qs.set('offset', String(params.offset));
  if (params.limit) qs.set('limit', String(params.limit));
  const suffix = qs.toString() ? `?${qs}` : '';
  const { data, error } = await client.GET(`/api/admin/waitlist${suffix}` as never);
  if (error) throwApiError(error, 'Failed to load waitlist');
  return data as WaitlistEntryList;
}

export async function approveWaitlistEntry(id: number): Promise<AllowedEmail> {
  const { data, error } = await client.POST(`/api/admin/waitlist/${id}/approve` as never);
  if (error) throwApiError(error, 'Failed to approve waitlist entry');
  return data as AllowedEmail;
}

export async function dismissWaitlistEntry(id: number): Promise<void> {
  const { error } = await client.DELETE(`/api/admin/waitlist/${id}` as never);
  if (error) throwApiError(error, 'Failed to dismiss waitlist entry');
}

// --- Channel Config ---

export interface AdminChannelConfig {
  bluebubbles_server_url: string;
  bluebubbles_password_set: boolean;
  bluebubbles_imessage_address: string;
  bluebubbles_send_method: string;
  bluebubbles_configured: boolean;
  telegram_bot_token_set: boolean;
  telegram_allowed_chat_id: string;
  linq_api_token_set: boolean;
  linq_from_number: string;
  linq_allowed_numbers: string;
  linq_preferred_service: string;
  twilio_account_sid_set: boolean;
  twilio_auth_token_set: boolean;
  twilio_api_key_sid_set: boolean;
  twilio_api_key_secret_set: boolean;
  twilio_configured: boolean;
  twilio_phone_number: string;
  twilio_messaging_service_sid: string;
  twilio_allowed_numbers: string;
}

export async function getAdminChannelConfig(): Promise<AdminChannelConfig> {
  const { data, error } = await client.GET('/api/admin/channels/config' as never);
  if (error) throwApiError(error, 'Failed to load channel config');
  return data as AdminChannelConfig;
}

export async function updateAdminChannelConfig(
  body: Record<string, string>,
): Promise<AdminChannelConfig> {
  const { data, error } = await client.PUT('/api/admin/channels/config' as never, {
    body,
  } as never);
  if (error) throwApiError(error, 'Failed to update channel config');
  return data as AdminChannelConfig;
}

// --- LLM Config (admin-only) ---

export interface AdminLLMConfig {
  llm_provider: string;
  llm_model: string;
  llm_api_base: string | null;
}

export interface AdminLLMConfigUpdate {
  llm_provider?: string;
  llm_model?: string;
  llm_api_base?: string;
}

export interface AdminUserLLMOverride {
  user_id: string;
  llm_provider_override: string;
  llm_model_override: string;
  effective_llm_provider: string;
  effective_llm_model: string;
}

export interface AdminUserLLMOverrideUpdate {
  llm_provider_override?: string;
  llm_model_override?: string;
}

export async function getAdminLLMConfig(): Promise<AdminLLMConfig> {
  const { data, error } = await client.GET('/api/admin/config/llm' as never);
  if (error) throwApiError(error, 'Failed to load LLM config');
  return data as AdminLLMConfig;
}

export async function updateAdminLLMConfig(
  body: AdminLLMConfigUpdate,
): Promise<AdminLLMConfig> {
  const { data, error } = await client.PUT('/api/admin/config/llm' as never, {
    body,
  } as never);
  if (error) throwApiError(error, 'Failed to update LLM config');
  return data as AdminLLMConfig;
}

export async function getUserLLMOverride(userId: string): Promise<AdminUserLLMOverride> {
  const { data, error } = await client.GET(
    `/api/admin/users/${encodeURIComponent(userId)}/llm-config` as never,
  );
  if (error) throwApiError(error, 'Failed to load per-user LLM override');
  return data as AdminUserLLMOverride;
}

export async function updateUserLLMOverride(
  userId: string,
  body: AdminUserLLMOverrideUpdate,
): Promise<AdminUserLLMOverride> {
  const { data, error } = await client.PUT(
    `/api/admin/users/${encodeURIComponent(userId)}/llm-config` as never,
    { body } as never,
  );
  if (error) throwApiError(error, 'Failed to update per-user LLM override');
  return data as AdminUserLLMOverride;
}

export interface AdminUserPlan {
  user_id: string;
  plan: string;
  messages_limit: number;
  tokens_limit: number;
}

export async function updateUserPlan(
  userId: string,
  plan: string,
): Promise<AdminUserPlan> {
  const { data, error } = await client.PUT(
    `/api/admin/users/${encodeURIComponent(userId)}/plan` as never,
    { body: { plan } } as never,
  );
  if (error) throwApiError(error, 'Failed to update plan');
  return data as AdminUserPlan;
}

// --- LLM provider/model enumeration (uses OSS endpoints; available to admins) ---

export interface ProviderInfo {
  name: string;
  local: boolean;
}

/**
 * Module-scoped cache for ``listProviders`` and ``listProviderModels``.
 *
 * The provider list is static for the life of the page (any-llm enumerates
 * a fixed enum), so we hold it forever. The per-provider model list comes
 * from an external API call to the LLM provider, which costs API quota; we
 * memoize per provider so flipping between Provider A and Provider B in the
 * dropdown only hits each provider's models endpoint once.
 *
 * In-flight promises are deduped so a fast double-click on the dropdown
 * doesn't fire two parallel API calls for the same provider.
 */
const _providersCache: { value: ProviderInfo[] | null } = { value: null };
let _providersInFlight: Promise<ProviderInfo[]> | null = null;
const _modelsCache = new Map<string, ProviderModelsResult>();
const _modelsInFlight = new Map<string, Promise<ProviderModelsResult>>();

export interface ProviderModelsResult {
  provider: string;
  models: string[];
  supports_listing: boolean;
  error: string | null;
}

export async function listProviders(): Promise<ProviderInfo[]> {
  if (_providersCache.value) return _providersCache.value;
  if (_providersInFlight) return _providersInFlight;

  _providersInFlight = (async () => {
    const { data, error } = await client.GET(
      '/api/admin/config/llm/providers' as never,
    );
    if (error) throwApiError(error, 'Failed to load LLM providers');
    const result = (data as { providers: ProviderInfo[] }).providers;
    _providersCache.value = result;
    return result;
  })().finally(() => {
    _providersInFlight = null;
  });
  return _providersInFlight;
}

export async function listProviderModels(
  provider: string,
): Promise<ProviderModelsResult> {
  const cached = _modelsCache.get(provider);
  if (cached) return cached;
  const inFlight = _modelsInFlight.get(provider);
  if (inFlight) return inFlight;

  const promise = (async () => {
    const { data, error } = await client.GET(
      `/api/admin/config/llm/providers/${encodeURIComponent(provider)}/models` as never,
    );
    if (error) throwApiError(error, 'Failed to load models');
    const result = data as ProviderModelsResult;
    // Cache successful AND structured-error responses both. The latter
    // (e.g. "missing API key") doesn't change on retry; an explicit
    // ``invalidateProviderModels(provider)`` call is the way to retry.
    _modelsCache.set(provider, result);
    return result;
  })().finally(() => {
    _modelsInFlight.delete(provider);
  });
  _modelsInFlight.set(provider, promise);
  return promise;
}

/** Drop the cached models result for one provider so the next call refetches. */
export function invalidateProviderModels(provider: string): void {
  _modelsCache.delete(provider);
}

// --- Heartbeat Logs ---

export async function getHeartbeatLogs(
  userId: string,
  limit: number = 50,
): Promise<HeartbeatLogList> {
  const query = new URLSearchParams({ limit: String(limit) });
  const { data, error } = await client.GET(
    `/api/admin/users/${encodeURIComponent(userId)}/heartbeat-logs?${query}` as never,
  );
  if (error) throwApiError(error, 'Failed to load heartbeat logs');
  return data as HeartbeatLogList;
}

// --- LLM Usage Logs ---

export async function getLLMUsageLogs(
  userId: string,
  limit: number = 100,
): Promise<LLMUsageLogList> {
  const query = new URLSearchParams({ limit: String(limit) });
  const { data, error } = await client.GET(
    `/api/admin/users/${encodeURIComponent(userId)}/llm-usage-logs?${query}` as never,
  );
  if (error) throwApiError(error, 'Failed to load LLM usage logs');
  return data as LLMUsageLogList;
}

// ---------------------------------------------------------------------------
// Shared data (consent-gated content access, issue #325 item 3)
// ---------------------------------------------------------------------------

export interface SharedDataUser {
  id: string;
  user_id: string;
  email: string;
  consent_at: string | null;
  conversation_count: number;
  last_message_at: string | null;
}

export interface SharedDataUserList {
  total: number;
  items: SharedDataUser[];
}

export interface SharedDataConversation {
  session_id: string;
  channel: string;
  created_at: string | null;
  last_message_at: string | null;
  message_count: number;
  // Highest ``messages.seq`` the agent's trim path has dropped from
  // live LLM context for this session. Messages with seq <=
  // last_trim_seq are still present in the conversation but the agent
  // no longer sees them on the next inbound. Null when nothing has
  // been trimmed yet (legacy or never-trimmed sessions).
  last_trim_seq: number | null;
}

// One PII-redacted message inside a conversation. Surfaces only as a
// nested field on the turn-grouped response (``user_message`` /
// ``agent_reply``); the flat-list endpoint that previously consumed
// it was retired in #361's follow-up.
export interface SharedDataMessage {
  seq: number;
  direction: string;
  body: string;
  // Extended-thinking text for outbound messages, PII-redacted server-side.
  // Empty for inbound messages and for outbound rows persisted before the
  // OSS thinking-capture path landed (migration 033).
  thinking: string;
  timestamp: string | null;
}

export interface SharedDataTopUser {
  id: string;
  email: string;
  user_id: string;
  messages_this_week: number;
}

export interface SharedDataSummary {
  consenting_user_count: number;
  // Counts users currently consenting whose data_sharing_consent_at
  // toggled within the last 7 days. The OSS column ticks on every
  // change (opt-in OR opt-out), so the count surfaces "consent state
  // moved recently" rather than "first-time opt-ins". A user who
  // toggles off and back on within the week still counts.
  consents_changed_this_week: number;
  conversations_this_week: number;
  heartbeats_this_week: number;
  open_reports_count: number;
  top_users_this_week: SharedDataTopUser[];
}

export async function getSharedDataSummary(): Promise<SharedDataSummary> {
  const { data, error } = await client.GET('/api/admin/shared-data/summary' as never);
  if (error) throwApiError(error, 'Failed to load research pilot summary');
  return data as SharedDataSummary;
}

export async function getSharedDataUsers(params?: {
  limit?: number;
  offset?: number;
}): Promise<SharedDataUserList> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set('limit', String(params.limit));
  if (params?.offset != null) query.set('offset', String(params.offset));
  const qs = query.toString();
  const { data, error } = await client.GET(
    `/api/admin/shared-data/users${qs ? `?${qs}` : ''}` as never,
  );
  if (error) throwApiError(error, 'Failed to load shared-data users');
  return data as SharedDataUserList;
}

export async function getSharedDataConversation(
  userId: string,
): Promise<SharedDataConversation | null> {
  const { data, error, response } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/conversation` as never,
  );
  // 404 = consenting user has no conversation yet (normal for fresh signups).
  // Surface as null so the caller can render an empty state without try/catch.
  if (response?.status === 404) return null;
  if (error) throwApiError(error, 'Failed to load shared-data conversation');
  return data as SharedDataConversation;
}

// Turn-grouped view of a conversation: pairs each inbound user message
// with the agent's outbound reply(ies) and the tool calls fired in
// between, so admins can debug "why did the agent do that this turn?"
// without reading raw tool_interactions_json.

export interface SharedDataReceipt {
  action: string;
  target: string;
  url: string | null;
}

export interface SharedDataToolCall {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  result: string;
  is_error: boolean;
  receipt: SharedDataReceipt | null;
}

export interface SharedDataTurn {
  turn_index: number;
  user_message: SharedDataMessage | null;
  agent_reply: SharedDataMessage | null;
  tool_calls: SharedDataToolCall[];
  started_at: string | null;
  finished_at: string | null;
}

export interface SharedDataConversationTurns {
  session_id: string;
  user_id: string;
  consent_at: string | null;
  turns: SharedDataTurn[];
  total: number;
  // See SharedDataConversation.last_trim_seq. Carried here too so the
  // turn-grouped activity feed can render the trim watermark without a
  // second roundtrip.
  last_trim_seq: number | null;
}

export async function getSharedDataConversationTurns(
  userId: string,
  params?: { limit?: number },
): Promise<SharedDataConversationTurns | null> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set('limit', String(params.limit));
  const qs = query.toString();
  const { data, error, response } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/conversation/turns${qs ? `?${qs}` : ''}` as never,
  );
  // 404 = no conversation yet for this consenting user. Mirrors
  // getSharedDataConversation; the caller renders an empty state.
  if (response?.status === 404) return null;
  if (error) throwApiError(error, 'Failed to load conversation turns');
  return data as SharedDataConversationTurns;
}

// Per-user content surfaces (profile / heartbeat / memory). Each one
// surfaces what /admin/users/{id} dropped in #336 for consenting users
// only. Phone / email / token shapes are redacted at the backend.

export interface SharedDataProfile {
  user_id: string;
  consent_at: string | null;
  soul_text: string;
  user_text: string;
  heartbeat_text: string;
  heartbeat_opt_in: boolean;
  heartbeat_frequency: string;
  heartbeat_max_daily: number;
}

export interface SharedDataHeartbeatLogEntry {
  id: number;
  action_type: string;
  channel: string;
  message_text: string;
  reasoning: string;
  tasks: string;
  created_at: string | null;
}

export interface SharedDataHeartbeatLogList {
  user_id: string;
  consent_at: string | null;
  items: SharedDataHeartbeatLogEntry[];
  total: number;
}

export interface SharedDataMemoryDocument {
  user_id: string;
  consent_at: string | null;
  memory_text: string;
  history_text: string;
  updated_at: string | null;
}

export async function getSharedDataProfile(
  userId: string,
): Promise<SharedDataProfile> {
  const { data, error } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/profile` as never,
  );
  if (error) throwApiError(error, 'Failed to load shared-data profile');
  return data as SharedDataProfile;
}

export async function getSharedDataHeartbeatLogs(
  userId: string,
  params?: { limit?: number; start_date?: string; end_date?: string },
): Promise<SharedDataHeartbeatLogList> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set('limit', String(params.limit));
  if (params?.start_date) query.set('start_date', params.start_date);
  if (params?.end_date) query.set('end_date', params.end_date);
  const qs = query.toString();
  const { data, error } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/heartbeat-logs${qs ? `?${qs}` : ''}` as never,
  );
  if (error) throwApiError(error, 'Failed to load shared-data heartbeat logs');
  return data as SharedDataHeartbeatLogList;
}

export async function getSharedDataMemory(
  userId: string,
): Promise<SharedDataMemoryDocument> {
  const { data, error } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/memory` as never,
  );
  if (error) throwApiError(error, 'Failed to load shared-data memory');
  return data as SharedDataMemoryDocument;
}

// Per-event compaction metadata plus before/after snapshots of the four
// memory files this event touched (decrypted on read). The accumulated
// ``MemoryDocument.history_text`` still surfaces via
// getSharedDataMemory above for the running narrative; the snapshots
// here are per-event diffs.

export interface SharedDataCompactionSnapshot {
  // Plaintext when the file was small enough to fit under
  // ``settings.compaction_event_snapshot_max_bytes_per_file``. Null
  // when the field was unchanged by this event, the row is still
  // ``pending``, the row predates the feature, OR when ``truncated``
  // is true (in which case ``head`` / ``tail`` carry the visible
  // portion instead).
  text: string | null;
  truncated: boolean;
  size_bytes: number | null;
  head: string | null;
  tail: string | null;
  sha256: string | null;
}

export interface SharedDataCompactionEvent {
  id: number;
  triggered_at: string | null;
  duration_ms: number;
  trimmed_count: number;
  trimmed_chars: number;
  input_tokens: number;
  output_tokens: number;
  min_message_seq: number | null;
  max_message_seq: number | null;
  // ``pending`` while the async LLM call is still running or after a
  // crash; ``completed`` once the call lands and snapshots are
  // populated. Legacy rows default to ``completed``.
  status: string;
  memory_updated: boolean;
  user_profile_updated: boolean;
  soul_updated: boolean;
  summary_len: number;
  memory_text_before: SharedDataCompactionSnapshot;
  memory_text_after: SharedDataCompactionSnapshot;
  history_text_before: SharedDataCompactionSnapshot;
  history_text_after: SharedDataCompactionSnapshot;
  user_text_before: SharedDataCompactionSnapshot;
  user_text_after: SharedDataCompactionSnapshot;
  soul_text_before: SharedDataCompactionSnapshot;
  soul_text_after: SharedDataCompactionSnapshot;
  // Capture of the compaction LLM call (OSS migration 031).
  // ``prompt`` is the trimmed conversation that was sent, ``raw_response``
  // is the unparsed model output (catches malformed JSON), and
  // ``parsed_response`` is a JSON-string of the four parsed fields. All
  // three reuse the snapshot envelope so the UI renders them with the
  // same SnapshotPane. Pending events have all three empty until the
  // async LLM call lands.
  prompt: SharedDataCompactionSnapshot;
  raw_response: SharedDataCompactionSnapshot;
  parsed_response: SharedDataCompactionSnapshot;
}

export interface SharedDataCompactionEventList {
  user_id: string;
  consent_at: string | null;
  items: SharedDataCompactionEvent[];
  total: number;
}

export async function getSharedDataCompactionEvents(
  userId: string,
  params?: { limit?: number; start_date?: string; end_date?: string },
): Promise<SharedDataCompactionEventList> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set('limit', String(params.limit));
  if (params?.start_date) query.set('start_date', params.start_date);
  if (params?.end_date) query.set('end_date', params.end_date);
  const qs = query.toString();
  const { data, error } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/compaction-events${qs ? `?${qs}` : ''}` as never,
  );
  if (error) throwApiError(error, 'Failed to load compaction events');
  return data as SharedDataCompactionEventList;
}

// Per-event tool-approval lifecycle. The OSS approval gate writes one
// row to ``approval_events`` per transition (requested / decided /
// timed_out / recovered) so admins can see when the agent was blocked
// on a permission prompt and how the request resolved.

export interface SharedDataApprovalEvent {
  id: number;
  event_type: string;
  tool_name: string;
  description: string;
  channel: string;
  chat_id: string;
  decision: string | null;
  created_at: string | null;
}

export interface SharedDataApprovalEventList {
  user_id: string;
  consent_at: string | null;
  items: SharedDataApprovalEvent[];
  total: number;
}

export async function getSharedDataApprovalEvents(
  userId: string,
  params?: { limit?: number; start_date?: string; end_date?: string },
): Promise<SharedDataApprovalEventList> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set('limit', String(params.limit));
  if (params?.start_date) query.set('start_date', params.start_date);
  if (params?.end_date) query.set('end_date', params.end_date);
  const qs = query.toString();
  const { data, error } = await client.GET(
    `/api/admin/shared-data/users/${encodeURIComponent(userId)}/approval-events${qs ? `?${qs}` : ''}` as never,
  );
  if (error) throwApiError(error, 'Failed to load approval events');
  return data as SharedDataApprovalEventList;
}

// ---------------------------------------------------------------------------
// Reported conversations (user-initiated reports, issue #325 item 5)
// ---------------------------------------------------------------------------

export type ReportedStatus = 'open' | 'dismissed';

export interface ReportedConversation {
  id: number;
  user_id: string;
  user_email: string;
  session_id: string;
  channel: string;
  anchor_seq: number | null;
  reason: string;
  status: ReportedStatus;
  created_at: string;
  dismissed_at: string | null;
  reviewed_admin_email: string | null;
}

export interface ReportedConversationList {
  total: number;
  open_count: number;
  items: ReportedConversation[];
}

export interface ReportedConversationMessage {
  seq: number;
  direction: string;
  body: string;
  timestamp: string | null;
  is_anchor: boolean;
}

export interface ReportedConversationMessageList {
  report_id: number;
  session_id: string;
  user_id: string;
  anchor_seq: number | null;
  items: ReportedConversationMessage[];
}

export interface DismissReportedConversationResponse {
  id: number;
  dismissed_at: string;
  reviewed_admin_user_id: string;
}

export async function getReportedConversations(params?: {
  status?: ReportedStatus;
  limit?: number;
  offset?: number;
}): Promise<ReportedConversationList> {
  const query = new URLSearchParams();
  if (params?.status) query.set('status', params.status);
  if (params?.limit != null) query.set('limit', String(params.limit));
  if (params?.offset != null) query.set('offset', String(params.offset));
  const qs = query.toString();
  const { data, error } = await client.GET(
    `/api/admin/reported-conversations${qs ? `?${qs}` : ''}` as never,
  );
  if (error) throwApiError(error, 'Failed to load reported conversations');
  return data as ReportedConversationList;
}

export async function getReportedConversationMessages(
  reportId: number,
  params?: { window?: number },
): Promise<ReportedConversationMessageList> {
  const query = new URLSearchParams();
  if (params?.window != null) query.set('window', String(params.window));
  const qs = query.toString();
  const { data, error } = await client.GET(
    `/api/admin/reported-conversations/${reportId}/messages${qs ? `?${qs}` : ''}` as never,
  );
  if (error) throwApiError(error, 'Failed to load reported conversation messages');
  return data as ReportedConversationMessageList;
}

export async function dismissReportedConversation(
  reportId: number,
): Promise<DismissReportedConversationResponse> {
  const { data, error } = await client.POST(
    `/api/admin/reported-conversations/${reportId}/dismiss` as never,
  );
  if (error) throwApiError(error, 'Failed to dismiss reported conversation');
  return data as DismissReportedConversationResponse;
}

// --- Admin API Keys ---
//
// Long-lived bearer tokens an admin mints for CLI / curl / script auth.
// The cleartext token is shown ONCE at mint time; subsequent reads only
// expose the prefix. Each admin scopes to their own keys (the backend
// filters by the calling admin's user id).

export interface AdminApiKeyItem {
  id: number;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface AdminApiKeyListResponse {
  items: AdminApiKeyItem[];
}

export interface AdminApiKeyMintResponse {
  id: number;
  token: string;
  key_prefix: string;
  label: string;
  created_at: string;
}

export async function listAdminApiKeys(): Promise<AdminApiKeyListResponse> {
  const { data, error } = await client.GET('/api/admin/api-keys' as never);
  if (error) throwApiError(error, 'Failed to load API keys');
  return data as AdminApiKeyListResponse;
}

export async function createAdminApiKey(body: {
  label: string;
}): Promise<AdminApiKeyMintResponse> {
  const { data, error } = await client.POST('/api/admin/api-keys' as never, {
    body,
  } as never);
  if (error) throwApiError(error, 'Failed to mint API key');
  return data as AdminApiKeyMintResponse;
}

export async function revokeAdminApiKey(id: number): Promise<void> {
  const { error } = await client.DELETE(`/api/admin/api-keys/${id}` as never);
  if (error) throwApiError(error, 'Failed to revoke API key');
}

// --- Monitoring (admin) ---
//
// Backed by GET /api/monitoring/status. Two layers report here: the error
// alerter (layer 1) and the dependency probes (layer 2). The history is
// in-process, so a redeploy resets it; the alert emails are the durable record.

export type ProbeStatus = 'up' | 'down' | 'unknown';

export interface MonitoringProbe {
  label: string;
  status: ProbeStatus;
  detail: string;
  consecutive_failures: number;
  since: string;
  last_checked: string | null;
  /**
   * DOWN only because the user never connected it. Baseline seeding stores
   * these as DOWN so a later disconnect is still reportable, but they are not
   * breakage and must not be counted as failures.
   */
  never_connected: boolean;
  /**
   * Grouping metadata for per-user checks, empty on infrastructure probes.
   * Carried as fields rather than parsed back out of the probe key: the key is
   * an internal identifier and splitting it on ":" breaks as soon as a factory
   * name or a user id contains one.
   *
   * ``integration`` is empty on the per-user sweep check
   * (``integration_check:<user_id>``), which covers a whole user rather than
   * one integration.
   */
  user_id: string;
  user_label: string;
  integration: string;
}

export interface MonitoringEvent {
  at: string;
  key: string;
  label: string;
  /** ``repaired`` is not a probe state: it marks something the app fixed itself. */
  status: ProbeStatus | 'repaired';
  detail: string;
}

/**
 * One unit of work in a probe run. Distinct from ``MonitoringProbe``: a step
 * says whether the *check* completed, a probe says whether the dependency is
 * healthy. A step is ``failed`` both when the dependency is down and when the
 * probe never answered inside its timeout.
 */
export type RunStepStatus = 'pending' | 'running' | 'ok' | 'failed';

export interface MonitoringRunStep {
  key: string;
  label: string;
  status: RunStepStatus;
  detail: string;
  started_at: string | null;
  finished_at: string | null;
  /** Computed live while the step is running, so a slow probe looks slow. */
  elapsed_ms: number | null;
}

export interface MonitoringRun {
  trigger: string;
  running: boolean;
  started_at: string;
  finished_at: string | null;
  error: string;
  steps: MonitoringRunStep[];
}

/** SMTP transport config plus the last send's outcome, for the delivery card. */
export interface MonitoringEmail {
  configured: boolean;
  host: string;
  port: number;
  timeout_seconds: number;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error: string;
  last_error_at: string | null;
}

export interface MonitoringStatus {
  alerts: {
    enabled: boolean;
    pending_groups: number;
    dedupe_minutes: number;
    flush_interval_seconds: number;
    max_emails_per_hour: number;
  };
  health_monitor: {
    enabled: boolean;
    interval_seconds: number;
    failure_threshold: number;
    probes: Record<string, MonitoringProbe>;
    last_run_at: string | null;
    history: MonitoringEvent[];
    /** Null until the first run of this process. */
    run: MonitoringRun | null;
  };
  email: MonitoringEmail;
  recipient_configured: boolean;
  timestamp: string;
}

/**
 * Probe runs are started, not awaited: a full pass can take minutes, and the
 * old awaiting endpoint left the tab on "Running" with nothing to show. The
 * caller polls ``getMonitoringStatus`` and reads ``health_monitor.run``.
 */
export interface RunProbesResponse {
  started: boolean;
  detail: string;
  run: MonitoringRun | null;
}

export interface TestAlertResponse {
  sent: boolean;
  recipient_configured: boolean;
  detail: string;
  email: MonitoringEmail;
}

export interface EmailPortCheck {
  port: number;
  reachable: boolean;
  detail: string;
}

export interface EmailDiagnostics {
  configured: boolean;
  host: string;
  port: number;
  ports: EmailPortCheck[];
  handshake_ok: boolean;
  handshake_detail: string;
  /** One operator-facing sentence: what is wrong and what to do about it. */
  conclusion: string;
}

export async function getMonitoringStatus(): Promise<MonitoringStatus> {
  const { data, error } = await client.GET('/api/monitoring/status' as never);
  if (error) throwApiError(error, 'Failed to load monitoring status');
  return data as MonitoringStatus;
}

export async function runHealthProbes(): Promise<RunProbesResponse> {
  const { data, error } = await client.POST('/api/monitoring/run-probes' as never);
  if (error) throwApiError(error, 'Failed to run health probes');
  return data as RunProbesResponse;
}

export async function sendMonitoringTestAlert(): Promise<TestAlertResponse> {
  const { data, error } = await client.POST('/api/monitoring/test-alert' as never);
  if (error) throwApiError(error, 'Failed to send test alert');
  return data as TestAlertResponse;
}

export async function diagnoseEmailDelivery(): Promise<EmailDiagnostics> {
  const { data, error } = await client.POST('/api/monitoring/diagnose-email' as never);
  if (error) throwApiError(error, 'Failed to run the email delivery diagnostic');
  return data as EmailDiagnostics;
}

// --- Model-swap evaluator ---
//
// Replays a user's recent turns through their current model and a candidate
// model. Consent-gated: every endpoint 403s for a user who has not opted into
// data sharing, because a run reads their real conversations and the report
// renders them back. Content is PII-redacted server-side.

export type EvalRunStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'interrupted'
  | 'cancelled';

export type EvalRecommendation =
  | 'safe_to_switch'
  | 'switch_with_monitoring'
  | 'do_not_switch'
  | 'inconclusive';

export interface EvalModelTotals {
  provider: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  cache_read_ratio: number;
  /**
   * Cached share of prompt tokens, read plus written. Prefer this over
   * cache_read_ratio for any comparison: the read ratio depends on whether an
   * earlier run left warm cache entries behind, so it swings between runs of
   * the same models.
   */
  cache_participation_ratio: number;
  total_cost_usd: string;
  // False when the pricing library has no entry for this model. The cost is
  // then zero and must be rendered as "unknown", never as "free".
  pricing_available: boolean;
  latency_p50_ms: number;
  latency_p95_ms: number;
}

export interface EvalSummary {
  turns_total: number;
  turns_completed: number;
  turns_failed: number;
  agreement_counts: Record<string, number>;
  safety_counts: Record<string, number>;
  // Subset of safety_counts that actually blocks a switch. A provider error
  // is recorded above but is a failure to measure, not candidate behavior.
  blocking_findings: number;
  judge_counts: Record<string, number>;
  /**
   * Why the unjudged turns were skipped. Added to judge_counts these account
   * for every turn, so the report never leaves a silent remainder between the
   * judged count and the turn count.
   */
  judge_skip_counts: Record<string, number>;
  identical_rate: number;
  divergence_rate: number;
  silent_noop_rate: number;
  /**
   * The subset of silent_noop_rate the judge did not score for the candidate,
   * which is what the recommendation blocks on. Prose is the right answer to
   * some messages.
   */
  silent_noop_blocking_rate: number;
  baseline: EvalModelTotals;
  candidate: EvalModelTotals;
  recommendation: EvalRecommendation;
  reasons: string[];
  warnings: string[];
}

export interface EvalRun {
  /** The run's public id, which is also its report URL segment. */
  id: string;
  user_id: string;
  user_email: string;
  /** False once the user withdraws consent: the report is no longer readable. */
  user_consented: boolean;
  baseline_provider: string;
  baseline_model: string;
  candidate_provider: string;
  candidate_model: string;
  judge_model: string;
  requested_samples: number;
  status: EvalRunStatus;
  progress_completed: number;
  progress_total: number;
  recommendation: string;
  error: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  summary?: EvalSummary | null;
}

export interface EvalToolCall {
  name: string;
  arguments: Record<string, unknown>;
}

export interface EvalDecision {
  text: string;
  tool_calls: EvalToolCall[];
  stop_reason: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  /** Prompt tokens written to cache. Needed to read the token columns: a
   * model whose whole prompt is a fresh cache write reports a tiny
   * input_tokens next to an uncached model's enormous one, same prompt. */
  cache_creation_tokens: number;
  latency_ms: number;
  error: string;
}

export interface EvalSafetyIssue {
  finding: string;
  tool_name: string;
  detail: string;
}

export interface EvalTurn {
  message_seq: number;
  message_timestamp: string;
  user_message: string;
  historic_reply: string;
  historic_tool_names: string[];
  baseline: EvalDecision;
  candidate: EvalDecision;
  agreement: string;
  safety_issues: EvalSafetyIssue[];
  judge_verdict: string;
  judge_rationale: string;
  /** Set when judge_verdict is 'not_judged': which skip reason applied. */
  judge_skip_reason: string;
}

export interface EvalReport {
  run: EvalRun;
  turns: EvalTurn[];
  total_turns: number;
}

export interface EvalRunList {
  runs: EvalRun[];
  /** Runs matching the query, not just this page. */
  total: number;
  /** LLM_EVAL_MAX_SAMPLES: the largest run the API will start. */
  max_samples: number;
  /** Below this many compared turns a run reports inconclusive, not a pass. */
  min_turns_for_verdict: number;
}

/** Runs across every user, or one user's when ``userId`` is given. */
export async function listEvalRuns(
  opts: { userId?: string; limit?: number; offset?: number } = {},
): Promise<EvalRunList> {
  const params = new URLSearchParams();
  if (opts.userId) params.set('user_id', opts.userId);
  params.set('limit', String(opts.limit ?? 25));
  if (opts.offset) params.set('offset', String(opts.offset));
  const { data, error } = await client.GET(
    `/api/admin/llm-eval/runs?${params.toString()}` as never,
  );
  if (error) throwApiError(error, 'Failed to load evaluation runs');
  return data as EvalRunList;
}

export async function startEvalRun(
  userId: string,
  body: {
    candidateProvider: string;
    candidateModel: string;
    sampleCount: number;
    judgeEnabled: boolean;
  },
): Promise<EvalRun> {
  const { data, error } = await client.POST(
    `/api/admin/llm-eval/users/${encodeURIComponent(userId)}/runs` as never,
    {
      body: {
        candidate_provider: body.candidateProvider,
        candidate_model: body.candidateModel,
        sample_count: body.sampleCount,
        judge_enabled: body.judgeEnabled,
      },
    } as never,
  );
  if (error) throwApiError(error, 'Failed to start evaluation');
  return data as EvalRun;
}

export async function getEvalReport(runId: string, limit = 50): Promise<EvalReport> {
  const { data, error } = await client.GET(
    `/api/admin/llm-eval/runs/${encodeURIComponent(runId)}?limit=${limit}` as never,
  );
  if (error) throwApiError(error, 'Failed to load evaluation report');
  return data as EvalReport;
}

export async function cancelEvalRun(runId: string): Promise<EvalRun> {
  const { data, error } = await client.POST(
    `/api/admin/llm-eval/runs/${encodeURIComponent(runId)}/cancel` as never,
  );
  if (error) throwApiError(error, 'Failed to cancel evaluation');
  return data as EvalRun;
}
