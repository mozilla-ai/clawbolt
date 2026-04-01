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
export type LinqLinkData = Awaited<ReturnType<typeof api.getLinqLink>>;

interface ChannelConfigFormProps {
  channelKey: ChannelKey;
  isPremium: boolean;
  channelConfig: ChannelConfigResponse | undefined;
  telegramLinkData: TelegramLinkData | null;
  linqLinkData: LinqLinkData | null;
  onSaved: () => void;
}

export function ChannelConfigForm({ channelKey, isPremium, ...rest }: ChannelConfigFormProps) {
  if (channelKey === 'telegram') {
    return isPremium ? <PremiumTelegramForm {...rest} /> : <OssTelegramForm {...rest} />;
  }
  if (channelKey === 'linq') {
    return isPremium ? <PremiumLinqForm {...rest} /> : <OssLinqForm {...rest} />;
  }
  if (channelKey === 'bluebubbles') {
    return <BlueBubblesForm {...rest} />;
  }
  return null;
}

// --- Telegram forms ---

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

// --- Linq forms ---

function PremiumLinqForm({ linqLinkData, onSaved }: SubFormProps) {
  const [phoneNumber, setPhoneNumber] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const displayedNumber = phoneNumber ?? linqLinkData?.phone_number ?? '';
  const fromNumber = linqLinkData?.linq_from_number ?? '';

  const handleSave = async () => {
    if (linqLinkData && displayedNumber === (linqLinkData.phone_number ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await api.setLinqLink(displayedNumber);
      setPhoneNumber(null);
      toast.success('Text messaging settings updated');
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4">
      {fromNumber && <TextAssistantCard fromNumber={fromNumber} />}
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
        <Button onClick={handleSave} disabled={saving || linqLinkData === null} isLoading={saving}>
          Save
        </Button>
      </div>
    </div>
  );
}

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
        toast.success('Linq settings updated');
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

// --- BlueBubbles form ---

function BlueBubblesForm({ channelConfig, onSaved }: SubFormProps) {
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumbers, setAllowedNumbers] = useState<string | null>(null);
  const [imessageAddress, setImessageAddress] = useState<string | null>(null);

  const displayedNumbers = allowedNumbers ?? channelConfig?.bluebubbles_allowed_numbers ?? '';
  const displayedAddress = imessageAddress ?? channelConfig?.bluebubbles_imessage_address ?? '';

  const handleSave = () => {
    const updates: Record<string, string> = {};
    if (allowedNumbers !== null && allowedNumbers !== (channelConfig?.bluebubbles_allowed_numbers ?? '')) {
      updates.bluebubbles_allowed_numbers = allowedNumbers;
    }
    if (imessageAddress !== null && imessageAddress !== (channelConfig?.bluebubbles_imessage_address ?? '')) {
      updates.bluebubbles_imessage_address = imessageAddress;
    }
    if (Object.keys(updates).length === 0) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate(updates, {
      onSuccess: () => {
        setAllowedNumbers(null);
        setImessageAddress(null);
        toast.success('BlueBubbles settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      {displayedAddress && (
        <TextAssistantCard
          fromNumber={displayedAddress}
          subtitle="Send an iMessage to this address to reach your assistant."
        />
      )}
      <Field label="iMessage Address">
        <Input
          value={displayedAddress}
          onChange={(e) => setImessageAddress(e.target.value)}
          placeholder="e.g. user@icloud.com or +15551234567"
        />
        <p className="text-xs text-muted-foreground mt-1">
          The iCloud email or phone number people should text to reach your assistant.
        </p>
      </Field>
      <Field label="Allowed Sender">
        <Input
          value={displayedNumbers}
          onChange={(e) => setAllowedNumbers(e.target.value)}
          placeholder="e.g. +15551234567 or user@icloud.com"
        />
        <p className="text-xs text-muted-foreground mt-1">
          E.164 phone number or iCloud email, or * to allow all. Empty = deny all.
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
