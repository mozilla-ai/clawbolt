import { useState, useEffect } from 'react';
import Card from '@/components/ui/card';
import TextAssistantCard from '@/components/TextAssistantCard';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Field from '@/components/ui/field';
import Select from '@/components/ui/select';
import { Tooltip } from '@heroui/tooltip';
import { toast } from '@/lib/toast';
import { useChannelConfig, useUpdateChannelConfig } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { getAccessToken } from '@/lib/api-client';

export default function ChannelsPage() {
  const { isPremium } = useAuth();

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Channels</h2>
      <div className="grid gap-6 lg:grid-cols-2">
        <TelegramSection />
        {isPremium ? <PremiumTextMessagingSection /> : <TextMessagingSection />}
      </div>
    </div>
  );
}

// --- Premium linking helpers ---

interface TelegramLinkData {
  telegram_user_id: string | null;
  connected: boolean;
}

interface TelegramBotInfo {
  bot_username: string;
  bot_link: string;
}

interface LinqLinkData {
  phone_number: string | null;
  connected: boolean;
  linq_from_number?: string;
}

function _authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getTelegramLink(): Promise<TelegramLinkData> {
  const res = await fetch('/api/channels/telegram', { headers: _authHeaders() });
  if (!res.ok) throw new Error('Failed to fetch Telegram link');
  return res.json() as Promise<TelegramLinkData>;
}

async function getTelegramBotInfo(): Promise<TelegramBotInfo | null> {
  const res = await fetch('/api/channels/telegram/bot-info', { headers: _authHeaders() });
  if (!res.ok) return null;
  return res.json() as Promise<TelegramBotInfo>;
}

async function setTelegramLink(telegramUserId: string): Promise<TelegramLinkData> {
  const res = await fetch('/api/channels/telegram', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ..._authHeaders() },
    body: JSON.stringify({ telegram_user_id: telegramUserId }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: string };
    throw new Error(body.detail || `Failed to save: ${res.status}`);
  }
  return res.json() as Promise<TelegramLinkData>;
}

async function getLinqLink(): Promise<LinqLinkData> {
  const res = await fetch('/api/channels/linq', { headers: _authHeaders() });
  if (!res.ok) throw new Error('Failed to fetch Linq link');
  return res.json() as Promise<LinqLinkData>;
}

async function setLinqLink(phoneNumber: string): Promise<LinqLinkData> {
  const res = await fetch('/api/channels/linq', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ..._authHeaders() },
    body: JSON.stringify({ phone_number: phoneNumber }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: string };
    throw new Error(body.detail || `Failed to save: ${res.status}`);
  }
  return res.json() as Promise<LinqLinkData>;
}

// --- Premium Telegram section ---

function PremiumTelegramSection() {
  const { data: channelConfig } = useChannelConfig();
  const [linkData, setLinkData] = useState<TelegramLinkData | null>(null);
  const [botInfo, setBotInfo] = useState<TelegramBotInfo | null>(null);
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const isConfigured = channelConfig?.telegram_bot_token_set ?? false;

  useEffect(() => {
    getTelegramLink().then(setLinkData).catch(() => {});
    getTelegramBotInfo().then(setBotInfo).catch(() => {});
  }, []);

  const displayedId = telegramUserId ?? linkData?.telegram_user_id ?? '';

  const handleSave = async () => {
    if (linkData && displayedId === (linkData.telegram_user_id ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      const result = await setTelegramLink(displayedId);
      setLinkData(result);
      setTelegramUserId(null);
      toast.success('Telegram settings updated');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-6">
      {botInfo && (
        <Card>
          <div className="flex items-center gap-3">
            <span className="text-sm">
              Message{' '}
              <a
                href={botInfo.bot_link}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-primary hover:underline"
              >
                @{botInfo.bot_username}
              </a>
              {' '}on Telegram to chat with your assistant.
            </span>
          </div>
        </Card>
      )}

      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Telegram</h3>
          {!isConfigured && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
              Not configured
            </span>
          )}
        </div>
        {!isConfigured && (
          <p className="text-xs text-muted-foreground mb-4">
            A Telegram bot token must be configured by an administrator to enable this channel.
          </p>
        )}
        <div className="grid gap-4">
          <TelegramUserIdField
            value={displayedId}
            onChange={(v) => setTelegramUserId(v)}
            disabled={!isConfigured}
          />
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || saving || linkData === null} isLoading={saving}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

// --- OSS Telegram section ---

function OssTelegramSection() {
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);

  const displayedId = telegramUserId ?? config?.telegram_allowed_chat_id ?? '';
  const isConfigured = config?.telegram_bot_token_set ?? false;

  const handleSave = () => {
    if (config && displayedId === config.telegram_allowed_chat_id) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ telegram_allowed_chat_id: displayedId }, {
      onSuccess: () => {
        setTelegramUserId(null);
        toast.success('Telegram settings updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-6">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Telegram</h3>
          <span className={`text-xs px-2 py-0.5 rounded-full ${isConfigured ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}`}>
            {isConfigured ? 'Connected' : 'Not configured'}
          </span>
        </div>
        {!isConfigured && (
          <p className="text-xs text-muted-foreground mb-4">
            Set <code className="font-mono text-[11px]">TELEGRAM_BOT_TOKEN</code> in your environment
            or in <a href="/app/settings/telegram" className="underline">Settings &gt; Telegram</a> to enable.
          </p>
        )}
        <div className="grid gap-4">
          <TelegramUserIdField
            value={displayedId}
            onChange={(v) => setTelegramUserId(v)}
            disabled={!isConfigured}
          />
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

function InfoIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="10" strokeWidth="2" />
      <path strokeWidth="2" strokeLinecap="round" d="M12 16v-4M12 8h.01" />
    </svg>
  );
}

const TELEGRAM_ID_TOOLTIP =
  'Clawbolt uses your numeric ID because Telegram usernames are optional' +
  ' and can change at any time. The numeric ID is permanent and will' +
  ' always identify your account.';

function TelegramUserIdField({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <Field label="Your Telegram User ID">
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. 123456789"
        inputMode="numeric"
        disabled={disabled}
      />
      <p className="text-xs text-muted-foreground mt-1">
        Your numeric Telegram user ID. Send /start to @userinfobot on Telegram to find it.{' '}
        <Tooltip content={TELEGRAM_ID_TOOLTIP} delay={400} closeDelay={0}>
          <span className="inline-flex items-center align-middle cursor-help text-muted-foreground/70 hover:text-muted-foreground">
            <InfoIcon />
            <span className="ml-0.5 underline decoration-dotted">Why not my username?</span>
          </span>
        </Tooltip>
      </p>
    </Field>
  );
}

function TelegramSection() {
  const { isPremium } = useAuth();

  if (isPremium) {
    return <PremiumTelegramSection />;
  }
  return <OssTelegramSection />;
}

// --- Premium Linq section ---

function PremiumTextMessagingSection() {
  const [linkData, setLinkData] = useState<LinqLinkData | null>(null);
  const [phoneNumber, setPhoneNumber] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getLinqLink().then(setLinkData).catch(() => {});
  }, []);

  const displayedNumber = phoneNumber ?? linkData?.phone_number ?? '';
  const fromNumber = linkData?.linq_from_number ?? '';

  const handleSave = async () => {
    if (linkData && displayedNumber === (linkData.phone_number ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      const result = await setLinqLink(displayedNumber);
      setLinkData(result);
      setPhoneNumber(null);
      toast.success('Text messaging settings updated');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-6">
      {fromNumber && <TextAssistantCard fromNumber={fromNumber} />}
      <Card>
        <h3 className="text-sm font-medium mb-3">Text Messaging (iMessage / RCS / SMS)</h3>
        <div className="grid gap-4">
          <Field label="Your Phone Number">
            <Input
              value={displayedNumber}
              onChange={(e) => setPhoneNumber(e.target.value)}
              placeholder="e.g. +15551234567"
              inputMode="tel"
            />
            <p className="text-xs text-muted-foreground mt-1">
              E.164 format phone number. This is the number you'll text from.
            </p>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={saving || linkData === null} isLoading={saving}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

// --- OSS Linq section ---

const LINQ_SERVICES = ['iMessage', 'SMS', 'RCS'] as const;

function TextMessagingSection() {
  const { data: config } = useChannelConfig();
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumber, setAllowedNumber] = useState<string | null>(null);
  const [preferredService, setPreferredService] = useState<string | null>(null);

  const displayedNumber = allowedNumber ?? config?.linq_allowed_numbers ?? '';
  const displayedService = preferredService ?? config?.linq_preferred_service ?? 'iMessage';
  const isConfigured = config?.linq_api_token_set ?? false;

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumber !== null && allowedNumber !== (config?.linq_allowed_numbers ?? '')) {
      updates.linq_allowed_numbers = allowedNumber;
    }
    if (preferredService !== null && preferredService !== (config?.linq_preferred_service ?? 'iMessage')) {
      updates.linq_preferred_service = preferredService;
    }
    if (Object.keys(updates).length === 0) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate(updates, {
      onSuccess: () => {
        setAllowedNumber(null);
        setPreferredService(null);
        toast.success('Linq settings updated');
      },
      onError: (e) => toast.error(e.message),
    });
  };

  const fromNumber = config?.linq_from_number ?? '';

  return (
    <div className="grid gap-6">
      {isConfigured && fromNumber && (
        <TextAssistantCard fromNumber={fromNumber} />
      )}
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Text Messaging (iMessage / RCS / SMS)</h3>
          <span className={`text-xs px-2 py-0.5 rounded-full ${isConfigured ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}`}>
            {isConfigured ? 'Connected' : 'Not configured'}
          </span>
        </div>
        {!isConfigured && (
          <p className="text-xs text-muted-foreground mb-4">
            Let users text your assistant from their phone's native messaging app.
            Set <code className="font-mono text-[11px]">LINQ_API_TOKEN</code> in your environment to enable.
          </p>
        )}
        <div className="grid gap-4">
          <Field label="Allowed Phone Number">
            <Input
              value={displayedNumber}
              onChange={(e) => setAllowedNumber(e.target.value)}
              placeholder="e.g. +15551234567"
              inputMode="tel"
              disabled={!isConfigured}
            />
            <p className="text-xs text-muted-foreground mt-1">
              E.164 phone number, or * to allow all. Empty = deny all.
            </p>
          </Field>
          <Field label="Preferred Service">
            <Select
              value={displayedService}
              onChange={(e) => setPreferredService(e.target.value)}
              aria-label="Preferred messaging service"
              disabled={!isConfigured}
            >
              {LINQ_SERVICES.map((svc) => (
                <option key={svc} value={svc}>{svc}</option>
              ))}
            </Select>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={!isConfigured || updateMutation.isPending || config === undefined} isLoading={updateMutation.isPending}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
