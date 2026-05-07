import type { components } from '@/generated/api';

// --- Backend types (derived from OpenAPI spec, single source of truth) ---

export type UserProfileResponse = components['schemas']['UserProfileResponse'];
export type UserProfileUpdate = components['schemas']['UserProfileUpdate'];
export type DataSharingConsentRequest = components['schemas']['DataSharingConsentRequest'];
export type DataSharingConsentResponse = components['schemas']['DataSharingConsentResponse'];
export type SessionDetailResponse = components['schemas']['SessionDetailResponse'];
export type SessionSystemPromptResponse = components['schemas']['SessionSystemPromptResponse'];
export type MemoryResponse = components['schemas']['MemoryResponse'];
export type MemoryUpdate = components['schemas']['MemoryUpdate'];
export type PermissionsResponse = components['schemas']['PermissionsResponse'];
export type PermissionsUpdate = components['schemas']['PermissionsUpdate'];
export type ChannelConfigResponse = components['schemas']['ChannelConfigResponse'];
export type ChannelConfigUpdate = components['schemas']['ChannelConfigUpdate'];
export type ChannelRouteResponse = components['schemas']['ChannelRouteResponse'];
export type ChannelRouteListResponse = components['schemas']['ChannelRouteListResponse'];
export type ModelConfigResponse = components['schemas']['ModelConfigResponse'];
export type ModelConfigUpdate = components['schemas']['ModelConfigUpdate'];
export type SubToolEntryResponse = components['schemas']['SubToolEntryResponse'];
export type ToolConfigEntryResponse = components['schemas']['ToolConfigEntryResponse'];
export type ToolConfigResponse = components['schemas']['ToolConfigResponse'];
export type ToolConfigUpdateEntry = components['schemas']['ToolConfigUpdateEntry'];
export type OAuthStatusEntry = components['schemas']['OAuthStatusEntry'];
export type OAuthStatusResponse = components['schemas']['OAuthStatusResponse'];
export type OAuthAuthorizeResponse = components['schemas']['OAuthAuthorizeResponse'];
export type ProviderInfo = components['schemas']['ProviderInfo'];

// --- Frontend-only types (no backend equivalent, stay manual) ---

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
}

export interface ChatAccepted {
  request_id: string;
}

export interface ToolInteraction {
  [key: string]: unknown;
}
