import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/query-keys';
import api from '@/api';
import type {
  HeartbeatItemUpdate,
  MemoryUpdate,
  ToolConfigUpdateEntry,
  ChannelConfigUpdate,
} from '@/types';

// --- Profile ---

export function useProfile() {
  return useQuery({
    queryKey: queryKeys.profile,
    queryFn: () => api.getProfile(),
  });
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

export function useSessions(offset: number, limit: number) {
  return useQuery({
    queryKey: queryKeys.sessions.list(offset, limit),
    queryFn: () => api.listSessions(offset, limit),
    refetchInterval: 10_000,
  });
}

export function useSession(
  sessionId: string | null,
  refetchInterval?: number | false,
) {
  return useQuery({
    queryKey: queryKeys.sessions.detail(sessionId!),
    queryFn: () => api.getSession(sessionId!),
    enabled: !!sessionId,
    refetchInterval: refetchInterval ?? false,
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

// --- Heartbeat Items ---

export function useHeartbeatItems() {
  return useQuery({
    queryKey: queryKeys.heartbeat,
    queryFn: () => api.listHeartbeatItems(),
  });
}

export function useCreateHeartbeatItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { description: string; schedule?: string }) =>
      api.createHeartbeatItem(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.heartbeat });
    },
  });
}

export function useUpdateHeartbeatItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: HeartbeatItemUpdate }) =>
      api.updateHeartbeatItem(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.heartbeat });
    },
  });
}

export function useDeleteHeartbeatItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteHeartbeatItem(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.heartbeat });
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
    },
  });
}
