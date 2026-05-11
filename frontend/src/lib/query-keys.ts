export const queryKeys = {
  profile: ['profile'] as const,
  dataSharingConsent: ['dataSharingConsent'] as const,
  conversation: {
    all: ['conversation'] as const,
    detail: ['conversation', 'detail'] as const,
    systemPrompt: ['conversation', 'system-prompt'] as const,
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
  oauth: ['oauth'] as const,
  calendarList: ['calendarList'] as const,
  calendarConfig: ['calendarConfig'] as const,
  telegramLink: ['telegramLink'] as const,
  telegramBotInfo: ['telegramBotInfo'] as const,
  linqLink: ['linqLink'] as const,
  blueBubblesLink: ['blueBubblesLink'] as const,
  twilioLink: ['twilioLink'] as const,
};
