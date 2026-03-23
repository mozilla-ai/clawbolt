import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/query-keys';
import api from '@/api';
import type {
  MemoryUpdate,
  ModelConfigUpdate,
  StorageConfigUpdate,
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
