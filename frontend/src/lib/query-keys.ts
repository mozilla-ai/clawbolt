export const queryKeys = {
  profile: ['profile'] as const,
  sessions: {
    all: ['sessions'] as const,
    detail: (id: string) => ['sessions', 'detail', id] as const,
    systemPrompt: (id: string) => ['sessions', 'system-prompt', id] as const,
  },
  memory: {
    all: ['memory'] as const,
  },
  permissions: {
    all: ['permissions'] as const,
  },
  tools: ['tools'] as const,
  channels: ['channels'] as const,
  channelRoutes: ['channelRoutes'] as const,
  modelConfig: ['modelConfig'] as const,
  storageConfig: ['storageConfig'] as const,
  oauth: ['oauth'] as const,
  calendarList: ['calendarList'] as const,
  calendarConfig: ['calendarConfig'] as const,
  telegramLink: ['telegramLink'] as const,
  telegramBotInfo: ['telegramBotInfo'] as const,
  linqLink: ['linqLink'] as const,
  blueBubblesLink: ['blueBubblesLink'] as const,
};
