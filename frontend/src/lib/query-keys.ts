export const queryKeys = {
  profile: ['profile'] as const,
  sessions: {
    all: ['sessions'] as const,
    list: (offset: number, limit: number) =>
      ['sessions', 'list', { offset, limit }] as const,
    detail: (id: string) => ['sessions', 'detail', id] as const,
  },
  memory: {
    all: ['memory'] as const,
    list: (category?: string) => ['memory', 'list', { category }] as const,
  },
  tools: ['tools'] as const,
  channels: ['channels'] as const,
  oauth: ['oauth'] as const,
};
