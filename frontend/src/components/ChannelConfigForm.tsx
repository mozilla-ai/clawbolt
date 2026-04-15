import { useState } from 'react';
import TextAssistantCard from '@/components/TextAssistantCard';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Field from '@/components/ui/field';
import Select from '@/components/ui/select';
import { Tooltip } from '@heroui/tooltip';
import { toast } from '@/lib/toast';
import { useUpdateChannelConfig } from '@/hooks/queries';
import type { ChannelKey } from '@/lib/channel-utils';
import type { ChannelConfigResponse } from '@/types';
import api from '@/api';

// Types derived from API return types, exported for consumers
export type TelegramLinkData = Awaited<ReturnType<typeof api.getTelegramLink>>;

// Generic premium link data shape (all premium link endpoints share this structure)
export type PremiumLinkData = { identifier: string | null; connected: boolean };

interface ChannelConfigFormProps {
  channelKey: ChannelKey;
  isPremium: boolean;
  channelConfig: ChannelConfigResponse | undefined;
  telegramLinkData: TelegramLinkData | null;
  premiumLinkData: PremiumLinkData | null;
  onSaved: () => void;
}

export function ChannelConfigForm({ channelKey, isPremium, ...rest }: ChannelConfigFormProps) {
  if (channelKey === 'telegram') {
    return isPremium ? <PremiumTelegramForm {...rest} /> : <OssTelegramForm {...rest} />;
  }
  if (isPremium) {
    const config = PREMIUM_LINK_CONFIGS[channelKey];
    if (config) {
      return <PremiumChannelLinkForm config={config} {...rest} />;
    }
  }
  if (channelKey === 'linq') {
    return <OssLinqForm {...rest} />;
  }
  if (channelKey === 'bluebubbles') {
    return <BlueBubblesForm {...rest} />;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Generic premium link form (data-driven)
// ---------------------------------------------------------------------------

interface PremiumLinkConfig {
  channelKey: ChannelKey;
  displayName: string;
  label: string;
  placeholder: string;
  helpText: string;
  inputMode?: 'tel' | 'text';
  /** Return the display address for the TextAssistantCard, or empty to hide it. */
  getAssistantAddress: (config: ChannelConfigResponse | undefined) => string;
  assistantSubtitle?: string;
  setLink: (identifier: string) => Promise<unknown>;
}

const PREMIUM_LINK_CONFIGS: Partial<Record<ChannelKey, PremiumLinkConfig>> = {
  linq: {
    channelKey: 'linq',
    displayName: 'iMessage',
    label: 'Your Phone Number',
    placeholder: 'e.g. +15551234567',
    helpText: "E.164 format phone number. This is the number you'll send messages from.",
    inputMode: 'tel',
    getAssistantAddress: (config) => config?.linq_from_number ?? '',
    assistantSubtitle: 'Send an iMessage to this address to reach your assistant.',
    setLink: (id) => api.setLinqLink(id),
  },
  bluebubbles: {
    channelKey: 'bluebubbles',
    displayName: 'iMessage',
    label: 'Your Phone Number or iCloud Email',
    placeholder: 'e.g. +15551234567 or user@icloud.com',
    helpText: 'The phone number or iCloud email you send iMessages from.',
    getAssistantAddress: (config) => config?.bluebubbles_imessage_address ?? '',
    assistantSubtitle: 'Send an iMessage to this address to reach your assistant.',
    setLink: (id) => api.setBlueBubblesLink(id),
  },
};

function PremiumChannelLinkForm({
  config,
  premiumLinkData,
  channelConfig,
  onSaved,
}: { config: PremiumLinkConfig } & Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const [identifier, setIdentifier] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const displayedValue = identifier ?? premiumLinkData?.identifier ?? '';
  const assistantAddress = config.getAssistantAddress(channelConfig);

  const handleSave = async () => {
    if (premiumLinkData && displayedValue === (premiumLinkData.identifier ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await config.setLink(displayedValue);
      setIdentifier(null);
      toast.success(`${config.displayName} settings updated`);
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4">
      {assistantAddress && (
        <TextAssistantCard
          fromNumber={assistantAddress}
          subtitle={config.assistantSubtitle}
        />
      )}
      <Field label={config.label}>
        <Input
          value={displayedValue}
          onChange={(e) => setIdentifier(e.target.value)}
          placeholder={config.placeholder}
          inputMode={config.inputMode}
        />
        <p className="text-xs text-muted-foreground mt-1">
          {config.helpText}
        </p>
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saving || premiumLinkData === null} isLoading={saving}>
          Save
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Telegram forms (Telegram is special: has its own field type + tooltip)
// ---------------------------------------------------------------------------

type SubFormProps = Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>;

function PremiumTelegramForm({ telegramLinkData, onSaved }: SubFormProps) {
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const displayedId = telegramUserId ?? telegramLinkData?.telegram_user_id ?? '';

  const handleSave = async () => {
    if (telegramLinkData && displayedId === (telegramLinkData.telegram_user_id ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await api.setTelegramLink(displayedId);
      setTelegramUserId(null);
      toast.success('Telegram settings updated');
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4">
      <TelegramUserIdField
        value={displayedId}
        onChange={(v) => setTelegramUserId(v)}
      />
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saving || telegramLinkData === null} isLoading={saving}>
          Save
        </Button>
      </div>
    </div>
  );
}

function OssTelegramForm({ channelConfig, onSaved }: SubFormProps) {
  const updateMutation = useUpdateChannelConfig();
  const [telegramUserId, setTelegramUserId] = useState<string | null>(null);

  const displayedId = telegramUserId ?? channelConfig?.telegram_allowed_chat_id ?? '';

  const handleSave = () => {
    if (channelConfig && displayedId === channelConfig.telegram_allowed_chat_id) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ telegram_allowed_chat_id: displayedId }, {
      onSuccess: () => {
        setTelegramUserId(null);
        toast.success('Telegram settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      <TelegramUserIdField
        value={displayedId}
        onChange={(v) => setTelegramUserId(v)}
      />
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- OSS Linq form ---

const LINQ_SERVICES = ['iMessage', 'SMS', 'RCS'] as const;

function OssLinqForm({ channelConfig, onSaved }: SubFormProps) {
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumber, setAllowedNumber] = useState<string | null>(null);
  const [preferredService, setPreferredService] = useState<string | null>(null);

  const displayedNumber = allowedNumber ?? channelConfig?.linq_allowed_numbers ?? '';
  const displayedService = preferredService ?? channelConfig?.linq_preferred_service ?? 'iMessage';
  const fromNumber = channelConfig?.linq_from_number ?? '';

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumber !== null && allowedNumber !== (channelConfig?.linq_allowed_numbers ?? '')) {
      updates.linq_allowed_numbers = allowedNumber;
    }
    if (preferredService !== null && preferredService !== (channelConfig?.linq_preferred_service ?? 'iMessage')) {
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
        toast.success('iMessage settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      {fromNumber && <TextAssistantCard fromNumber={fromNumber} />}
      <Field label="Allowed Phone Number">
        <Input
          value={displayedNumber}
          onChange={(e) => setAllowedNumber(e.target.value)}
          placeholder="e.g. +15551234567"
          inputMode="tel"
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
        >
          {LINQ_SERVICES.map((svc) => (
            <option key={svc} value={svc}>{svc}</option>
          ))}
        </Select>
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- OSS BlueBubbles form ---

function BlueBubblesForm({ channelConfig, onSaved }: SubFormProps) {
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumbers, setAllowedNumbers] = useState<string | null>(null);

  const displayedNumbers = allowedNumbers ?? channelConfig?.bluebubbles_allowed_numbers ?? '';

  const handleSave = () => {
    if (allowedNumbers === null || allowedNumbers === (channelConfig?.bluebubbles_allowed_numbers ?? '')) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ bluebubbles_allowed_numbers: allowedNumbers }, {
      onSuccess: () => {
        setAllowedNumbers(null);
        toast.success('iMessage settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      <Field label="Allowed Sender">
        <Input
          value={displayedNumbers}
          onChange={(e) => setAllowedNumbers(e.target.value)}
          placeholder="e.g. +15551234567 or user@icloud.com"
        />
        <p className="text-xs text-muted-foreground mt-1">
          E.164 phone number or iCloud email, or * to allow all. Empty = deny all.
          The iMessage address is set by the administrator and shown on the channel card.
        </p>
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- Shared UI ---

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
