export interface UsageBucket {
  used: number;
  limit: number;
}

export interface UsageSummary {
  messages: UsageBucket;
  tokens: UsageBucket;
  period_start: string | null;
}
