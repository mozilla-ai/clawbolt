import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/query-keys';
import api from '@/api';
import type {
  MemoryFactUpdate,
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
  });
}

export function useSession(sessionId: string | null) {
  return useQuery({
    queryKey: queryKeys.sessions.detail(sessionId!),
    queryFn: () => api.getSession(sessionId!),
    enabled: !!sessionId,
  });
}

// --- Memory ---

export function useMemoryFacts() {
  return useQuery({
    queryKey: queryKeys.memory.list(),
    queryFn: () => api.listMemoryFacts(),
  });
}

export function useUpdateMemoryFact() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, body }: { key: string; body: MemoryFactUpdate }) =>
      api.updateMemoryFact(key, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
    },
  });
}

export function useDeleteMemoryFact() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.deleteMemoryFact(key),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
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
