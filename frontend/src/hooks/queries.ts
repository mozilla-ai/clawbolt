import { useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/query-keys';
import api from '@/api';
import type {
  MemoryUpdate,
  PermissionsUpdate,
  ModelConfigUpdate,
  StorageConfigUpdate,
  ToolConfigUpdateEntry,
  ChannelConfigUpdate,
} from '@/types';

// --- Profile ---

export function useProfile(opts: { enabled?: boolean } = {}) {
  const query = useQuery({
    queryKey: queryKeys.profile,
    queryFn: () => api.getProfile(),
    enabled: opts.enabled ?? true,
  });
  const { mutate } = useUpdateProfile();
  const backfilled = useRef(false);

  useEffect(() => {
    if (query.data && !query.data.timezone && !backfilled.current) {
      backfilled.current = true;
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) {
        mutate({ timezone: tz });
      }
    }
  }, [query.data, mutate]);

  return query;
}

export function useUpdateProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.updateProfile,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.profile });
    },
  });
}

// --- Sessions ---

export function useSession(
  sessionId: string | null,
  refetchInterval?: number | false,
) {
  return useQuery({
    queryKey: queryKeys.sessions.detail(sessionId!),
    queryFn: () => api.getSession(sessionId!),
    enabled: !!sessionId,
    refetchInterval: refetchInterval ?? false,
    retry: false,
  });
}

// --- Memory ---

export function useMemory() {
  return useQuery({
    queryKey: queryKeys.memory.all,
    queryFn: () => api.getMemory(),
  });
}

export function useUpdateMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: MemoryUpdate) => api.updateMemory(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
    },
  });
}

// --- Permissions ---

export function usePermissions() {
  return useQuery({
    queryKey: queryKeys.permissions.all,
    queryFn: () => api.getPermissions(),
  });
}

export function useUpdatePermissions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PermissionsUpdate) => api.updatePermissions(body),
    onSuccess: (data) => {
      queryClient.setQueryData(queryKeys.permissions.all, data);
      void queryClient.invalidateQueries({ queryKey: queryKeys.tools });
    },
  });
}

// --- Tools ---

export function useToolConfig() {
  return useQuery({
    queryKey: queryKeys.tools,
    queryFn: () => api.getToolConfig(),
  });
}

export function useUpdateToolConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (tools: ToolConfigUpdateEntry[]) => api.updateToolConfig(tools),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.tools });
    },
  });
}

// --- OAuth ---

export function useOAuthStatus() {
  return useQuery({
    queryKey: queryKeys.oauth,
    queryFn: () => api.getOAuthStatus(),
  });
}

export function useOAuthDisconnect() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (integration: string) => api.disconnectOAuth(integration),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.oauth });
      void queryClient.invalidateQueries({ queryKey: queryKeys.tools });
    },
  });
}

export function useTokenConnect() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ integration, token }: { integration: string; token: string }) =>
      api.connectWithToken(integration, token),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.oauth });
      void queryClient.invalidateQueries({ queryKey: queryKeys.tools });
    },
  });
}

// --- Calendar config ---

export function useCalendarList() {
  return useQuery({
    queryKey: queryKeys.calendarList,
    queryFn: () => api.getCalendarList(),
  });
}

export function useCalendarConfig() {
  return useQuery({
    queryKey: queryKeys.calendarConfig,
    queryFn: () => api.getCalendarConfig(),
  });
}

export function useUpdateCalendarConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { calendars: Array<{ calendar_id: string; display_name: string; disabled_tools: string[]; access_role: string }> }) =>
      api.updateCalendarConfig(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.calendarConfig });
    },
  });
}

// --- Storage config ---

export function useStorageConfig() {
  return useQuery({
    queryKey: queryKeys.storageConfig,
    queryFn: () => api.getStorageConfig(),
  });
}

export function useUpdateStorageConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: StorageConfigUpdate) => api.updateStorageConfig(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.storageConfig });
    },
  });
}

// --- Model config ---

export function useModelConfig() {
  return useQuery({
    queryKey: queryKeys.modelConfig,
    queryFn: () => api.getModelConfig(),
  });
}

export function useUpdateModelConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ModelConfigUpdate) => api.updateModelConfig(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.modelConfig });
    },
  });
}

// --- Premium channel links ---

export function useTelegramLink(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.telegramLink,
    queryFn: () => api.getTelegramLink(),
    enabled,
  });
}

export function useTelegramBotInfo(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.telegramBotInfo,
    queryFn: () => api.getTelegramBotInfo(),
    enabled,
  });
}

export function useLinqLink(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.linqLink,
    queryFn: () => api.getLinqLink(),
    enabled,
  });
}

export function useBlueBubblesLink(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.blueBubblesLink,
    queryFn: () => api.getBlueBubblesLink(),
    enabled,
  });
}

// --- Channel routes ---

export function useChannelRoutes() {
  return useQuery({
    queryKey: queryKeys.channelRoutes,
    queryFn: () => api.getChannelRoutes(),
    // Poll while any enabled non-webchat route is still waiting for its first
    // inbound message so the channel picker can flip to "Verified" without the
    // user refreshing. Stops once every enabled route has a last_inbound_at.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      const hasUnverified = data.routes.some(
        (r) => r.enabled && r.channel !== 'webchat' && !r.last_inbound_at,
      );
      return hasUnverified ? 3000 : false;
    },
  });
}

export function useToggleChannelRoute() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ channel, enabled }: { channel: string; enabled: boolean }) =>
      api.toggleChannelRoute(channel, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.channelRoutes });
      void queryClient.invalidateQueries({ queryKey: queryKeys.channels });
    },
  });
}

// --- Channels ---

export function useChannelConfig() {
  return useQuery({
    queryKey: queryKeys.channels,
    queryFn: () => api.getChannelConfig(),
  });
}

export function useUpdateChannelConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ChannelConfigUpdate) => api.updateChannelConfig(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.channels });
      void queryClient.invalidateQueries({ queryKey: queryKeys.channelRoutes });
    },
  });
}
