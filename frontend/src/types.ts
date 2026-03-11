// API response types matching backend schemas

export interface UserProfile {
  id: number;
  user_id: string;
  phone: string;
  timezone: string;
  soul_text: string;
  user_text: string;
  checklist_text: string;
  preferred_channel: string;
  channel_identifier: string;
  heartbeat_opt_in: boolean;
  heartbeat_frequency: string;
  onboarding_complete: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface UserProfileUpdate {
  phone?: string;
  timezone?: string;
  soul_text?: string;
  user_text?: string;
  checklist_text?: string;
  heartbeat_opt_in?: boolean;
  heartbeat_frequency?: string;
}

export interface SessionSummary {
  id: string;
  start_time: string;
  message_count: number;
  last_message_preview: string;
  channel: string;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  offset: number;
  limit: number;
}

export interface ToolInteraction {
  [key: string]: unknown;
}

export interface SessionMessage {
  seq: number;
  direction: string;
  body: string;
  timestamp: string;
  tool_interactions: ToolInteraction[];
}

export interface SessionDetail {
  session_id: string;
  user_id: number;
  created_at: string;
  last_message_at: string;
  is_active: boolean;
  channel: string;
  messages: SessionMessage[];
}

export interface MemoryFact {
  key: string;
  value: string;
  category: string;
  confidence: number;
}

export interface MemoryFactUpdate {
  value?: string;
  category?: string;
  confidence?: number;
}

export interface ChecklistItem {
  id: number;
  description: string;
  schedule: string;
  status: string;
  created_at: string;
}

export interface ChecklistItemUpdate {
  description?: string;
  schedule?: string;
  status?: string;
}

export interface AuthConfig {
  required: boolean;
  method?: string;
  provider?: string;
  client_id?: string;
}

export interface AuthUser {
  id: number;
  name: string;
  role?: string;
}

export interface ChatResponse {
  reply: string;
  session_id: string;
}

export interface ChatAccepted {
  request_id: string;
  session_id: string;
}

export interface ChannelConfig {
  telegram_bot_token_set: boolean;
  telegram_allowed_usernames: string;
}

export interface ChannelConfigUpdate {
  telegram_bot_token?: string;
  telegram_allowed_usernames?: string;
}

export interface ToolConfigEntry {
  name: string;
  description: string;
  category: string;
  domain_group: string;
  domain_group_order: number;
  enabled: boolean;
}

export interface ToolConfigResponse {
  tools: ToolConfigEntry[];
}

export interface ToolConfigUpdateEntry {
  name: string;
  enabled: boolean;
}

export interface OAuthStatusEntry {
  integration: string;
  configured: boolean;
  connected: boolean;
}

export interface OAuthStatusResponse {
  integrations: OAuthStatusEntry[];
}

export interface OAuthAuthorizeResponse {
  url: string;
  integration: string;
}
