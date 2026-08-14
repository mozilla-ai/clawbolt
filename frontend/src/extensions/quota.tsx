import { useState, useEffect } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { UsageSummary } from './types';
import { getSubscription, getTelegramBotInfo, type TelegramBotInfo } from './api';

export function QuotaBanner(): ReactNode {
  const [usage, setUsage] = useState<UsageSummary | null>(null);

  useEffect(() => {
    getSubscription()
      .then(setUsage)
      .catch(() => {});
  }, []);

  if (!usage) return null;

  const msgPct = usage.messages.limit > 0
    ? (usage.messages.used / usage.messages.limit) * 100
    : 0;
  const maxPct = msgPct;

  if (maxPct < 80) return null;

  const isExhausted = maxPct >= 100;

  return (
    <div className={`px-4 py-3 rounded-[--radius-md] text-sm ${
      isExhausted
        ? 'bg-error-bg border border-error-border text-error-text'
        : 'bg-warning-bg border border-warning-border text-warning-text'
    }`}>
      {isExhausted ? (
        <>
          You've reached your usage limit for this month. Email support@clawbolt.ai for help.
        </>
      ) : (
        <>
          You're approaching your monthly limit ({Math.round(maxPct)}% used).{' '}
          <Link to="/app/settings/usage" className="underline font-medium">
            View usage
          </Link>
        </>
      )}
    </div>
  );
}

export function OnboardingBanner({ children }: { children?: ReactNode }): ReactNode {
  const [botInfo, setBotInfo] = useState<TelegramBotInfo | null>(null);

  useEffect(() => {
    getTelegramBotInfo()
      .then(setBotInfo)
      .catch(() => {});
  }, []);

  if (children) return children;

  if (!botInfo?.bot_username) return null;

  return (
    <div className="px-4 py-3 rounded-[--radius-md] text-sm bg-info-bg border border-info-border text-info-text">
      <p className="font-medium mb-1">Get started with Telegram</p>
      <p>
        Message{' '}
        <a
          href={botInfo.bot_link!}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium underline"
        >
          @{botInfo.bot_username}
        </a>
        {' '}on Telegram to start chatting. Make sure to{' '}
        <Link to="/app/settings/channels" className="underline font-medium">
          link your Telegram ID
        </Link>
        {' '}in settings first.
      </p>
    </div>
  );
}

export function isQuotaError(message: string): boolean {
  const lower = message.toLowerCase();
  return lower.includes('quota') || lower.includes('limit reached') || lower.includes('usage limit');
}
