/** OSS admin API: heartbeat logs, sessions, and LLM usage for the current user. */

import client from '@/lib/api-client';

// --- Heartbeat Logs ---

export interface HeartbeatLogItem {
  id: number;
  user_id: string;
  action_type: string;
  message_text: string;
  channel: string;
  reasoning: string;
  tasks: string;
  created_at: string;
}

export interface HeartbeatLogList {
  total: number;
  items: HeartbeatLogItem[];
}

// --- LLM Usage ---

export interface LLMUsageByPurpose {
  purpose: string;
  call_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cost: number;
}

export interface LLMUsageSummary {
  total_calls: number;
  total_tokens: number;
  total_cost: number;
  by_purpose: LLMUsageByPurpose[];
}

// --- Helpers ---

function throwApiError(error: unknown, fallback: string): never {
  const b = error as { detail?: string };
  throw new Error(b?.detail || fallback);
}

// --- API Calls ---

export async function getHeartbeatLogs(limit: number = 50): Promise<HeartbeatLogList> {
  const { data, error } = await client.GET(
    `/api/user/heartbeat-logs?limit=${limit}` as never,
  );
  if (error) throwApiError(error, 'Failed to load heartbeat logs');
  return data as HeartbeatLogList;
}

export async function deleteHeartbeatLogs(): Promise<{ status: string; deleted: number }> {
  const { data, error } = await client.DELETE('/api/user/heartbeat-logs' as never);
  if (error) throwApiError(error, 'Failed to delete heartbeat logs');
  return data as { status: string; deleted: number };
}

export async function getLLMUsage(days: number = 30): Promise<LLMUsageSummary> {
  const { data, error } = await client.GET(
    `/api/user/llm-usage?days=${days}` as never,
  );
  if (error) throwApiError(error, 'Failed to load LLM usage');
  return data as LLMUsageSummary;
}

// --- User Profile ---

export interface UserProfile {
  heartbeat_opt_in: boolean;
  heartbeat_frequency: string;
}

export async function getProfile(): Promise<UserProfile> {
  const { data, error } = await client.GET('/api/user/profile' as never);
  if (error) throwApiError(error, 'Failed to load profile');
  return data as UserProfile;
}
