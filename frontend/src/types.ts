// API response types matching backend schemas

export interface ContractorProfile {
  id: number;
  user_id: string;
  name: string;
  phone: string;
  trade: string;
  location: string;
  hourly_rate: number | null;
  business_hours: string;
  timezone: string;
  assistant_name: string;
  soul_text: string;
  preferred_channel: string;
  channel_identifier: string;
  heartbeat_opt_in: boolean;
  heartbeat_frequency: string;
  onboarding_complete: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ContractorProfileUpdate {
  name?: string;
  phone?: string;
  trade?: string;
  location?: string;
  hourly_rate?: number | null;
  business_hours?: string;
  timezone?: string;
  assistant_name?: string;
  soul_text?: string;
  heartbeat_opt_in?: boolean;
  heartbeat_frequency?: string;
}

export interface SessionSummary {
  id: string;
  start_time: string;
  message_count: number;
  last_message_preview: string;
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
  contractor_id: number;
  created_at: string;
  last_message_at: string;
  is_active: boolean;
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

export interface ContractorStats {
  total_sessions: number;
  messages_this_month: number;
  active_checklist_items: number;
  total_memory_facts: number;
  last_conversation_at: string | null;
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
