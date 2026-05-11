import { useState } from 'react';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import Field from '@/components/ui/field';
import Select from '@/components/ui/select';
import { Tooltip } from '@heroui/tooltip';
import { toast } from '@/lib/toast';
import { useUpdateChannelConfig } from '@/hooks/queries';
import { normalizeUsPhone, isValidE164, PHONE_FORMAT_ERROR } from '@/lib/phone';
import type { ChannelKey } from '@/lib/channel-utils';
import type { ChannelConfigResponse } from '@/types';
import api from '@/api';

// Types derived from API return types, exported for consumers
export type TelegramLinkData = Awaited<ReturnType<typeof api.getTelegramLink>>;
export type TwilioLinkData = Awaited<ReturnType<typeof api.getTwilioLink>>;

// Generic premium link data shape (all premium link endpoints share this structure)
export type PremiumLinkData = { identifier: string | null; connected: boolean };

interface ChannelConfigFormProps {
  channelKey: ChannelKey;
  isPremium: boolean;
  channelConfig: ChannelConfigResponse | undefined;
  telegramLinkData: TelegramLinkData | null;
  twilioLinkData: TwilioLinkData | null;
  premiumLinkData: PremiumLinkData | null;
  onSaved: () => void;
}

export function ChannelConfigForm({ channelKey, isPremium, ...rest }: ChannelConfigFormProps) {
  if (channelKey === 'telegram') {
    return isPremium ? <PremiumTelegramForm {...rest} /> : <OssTelegramForm {...rest} />;
  }
  if (channelKey === 'twilio') {
    return isPremium ? <PremiumTwilioForm {...rest} /> : <OssTwilioForm {...rest} />;
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
    setLink: (id) => api.setLinqLink(id),
  },
  bluebubbles: {
    channelKey: 'bluebubbles',
    displayName: 'iMessage',
    label: 'Your Phone Number or iCloud Email',
    placeholder: 'e.g. +15551234567 or user@icloud.com',
    helpText: 'The phone number or iCloud email you send iMessages from.',
    setLink: (id) => api.setBlueBubblesLink(id),
  },
};

function PremiumChannelLinkForm({
  config,
  premiumLinkData,
  onSaved,
}: { config: PremiumLinkConfig } & Omit<ChannelConfigFormProps, 'channelKey' | 'isPremium'>) {
  const [identifier, setIdentifier] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const displayedValue = identifier ?? premiumLinkData?.identifier ?? '';
  const isPhoneInput = config.inputMode === 'tel';

  const handleSave = async () => {
    setError(null);
    const normalized = isPhoneInput ? normalizeUsPhone(displayedValue) : displayedValue.trim();
    if (isPhoneInput && normalized && !isValidE164(normalized)) {
      setError(PHONE_FORMAT_ERROR);
      return;
    }
    if (premiumLinkData && normalized === (premiumLinkData.identifier ?? '')) {
      toast.error('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await config.setLink(normalized);
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
      <Field label={config.label}>
        <Input
          value={displayedValue}
          onChange={(e) => {
            setIdentifier(e.target.value);
            if (error) setError(null);
          }}
          placeholder={config.placeholder}
          inputMode={config.inputMode}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'channel-link-error' : undefined}
        />
        {error ? (
          <p id="channel-link-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p className="text-xs text-muted-foreground mt-1">{config.helpText}</p>
        )}
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
  const [error, setError] = useState<string | null>(null);

  const displayedNumber = allowedNumber ?? channelConfig?.linq_allowed_numbers ?? '';
  const displayedService = preferredService ?? channelConfig?.linq_preferred_service ?? 'iMessage';

  const handleSave = () => {
    setError(null);
    const updates: Record<string, string> = {};
    // Allow "*" (allow-all) and "" (deny-all) verbatim; only normalize phone-shaped input.
    const trimmed = (allowedNumber ?? '').trim();
    const normalized = trimmed && trimmed !== '*' ? normalizeUsPhone(trimmed) : trimmed;
    if (normalized && normalized !== '*' && !isValidE164(normalized)) {
      setError(PHONE_FORMAT_ERROR);
      return;
    }
    if (allowedNumber !== null && normalized !== (channelConfig?.linq_allowed_numbers ?? '')) {
      updates.linq_allowed_numbers = normalized;
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
      <Field label="Allowed Phone Number">
        <Input
          value={displayedNumber}
          onChange={(e) => {
            setAllowedNumber(e.target.value);
            if (error) setError(null);
          }}
          placeholder="e.g. +15551234567"
          inputMode="tel"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'oss-linq-error' : undefined}
        />
        {error ? (
          <p id="oss-linq-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p className="text-xs text-muted-foreground mt-1">
            E.164 phone number, or * to allow all. Empty = deny all.
          </p>
        )}
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
  const [error, setError] = useState<string | null>(null);

  const displayedNumbers = allowedNumbers ?? channelConfig?.bluebubbles_allowed_numbers ?? '';

  const handleSave = () => {
    setError(null);
    const trimmed = (allowedNumbers ?? '').trim();
    // BlueBubbles also accepts iCloud email, so only normalize phone-shaped input.
    const looksLikeEmail = trimmed.includes('@');
    const normalized = trimmed && trimmed !== '*' && !looksLikeEmail
      ? normalizeUsPhone(trimmed)
      : trimmed;
    if (normalized && normalized !== '*' && !looksLikeEmail && !isValidE164(normalized)) {
      setError(PHONE_FORMAT_ERROR);
      return;
    }
    if (allowedNumbers === null || normalized === (channelConfig?.bluebubbles_allowed_numbers ?? '')) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ bluebubbles_allowed_numbers: normalized }, {
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
          onChange={(e) => {
            setAllowedNumbers(e.target.value);
            if (error) setError(null);
          }}
          placeholder="e.g. +15551234567 or user@icloud.com"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'bluebubbles-error' : undefined}
        />
        {error ? (
          <p id="bluebubbles-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p className="text-xs text-muted-foreground mt-1">
            E.164 phone number or iCloud email, or * to allow all. Empty = deny all.
            The iMessage address is set by the administrator and shown on the channel card.
          </p>
        )}
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- OSS Twilio form (operator allowlist config) ---

function OssTwilioForm({ channelConfig, onSaved }: SubFormProps) {
  const updateMutation = useUpdateChannelConfig();
  const [allowedNumbers, setAllowedNumbers] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const displayedNumbers = allowedNumbers ?? channelConfig?.twilio_allowed_numbers ?? '';

  const handleSave = () => {
    setError(null);
    const trimmed = (allowedNumbers ?? '').trim();
    const normalized = trimmed && trimmed !== '*' ? normalizeUsPhone(trimmed) : trimmed;
    if (normalized && normalized !== '*' && !isValidE164(normalized)) {
      setError(PHONE_FORMAT_ERROR);
      return;
    }
    if (allowedNumbers === null || normalized === (channelConfig?.twilio_allowed_numbers ?? '')) {
      toast.error('No changes to save');
      return;
    }
    updateMutation.mutate({ twilio_allowed_numbers: normalized }, {
      onSuccess: () => {
        setAllowedNumbers(null);
        toast.success('Twilio settings updated');
        onSaved();
      },
      onError: (e) => toast.error(e.message),
    });
  };

  return (
    <div className="grid gap-4">
      <Field label="Allowed Phone Number">
        <Input
          value={displayedNumbers}
          onChange={(e) => {
            setAllowedNumbers(e.target.value);
            if (error) setError(null);
          }}
          placeholder="e.g. +15551234567"
          inputMode="tel"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'oss-twilio-error' : undefined}
        />
        {error ? (
          <p id="oss-twilio-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p className="text-xs text-muted-foreground mt-1">
            E.164 phone number, or * to allow all. Empty = deny all.
            Twilio account credentials are managed in server settings.
          </p>
        )}
      </Field>
      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={updateMutation.isPending || channelConfig === undefined} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </div>
  );
}

// --- Premium Twilio form (per-user number provisioning) ---

const TWILIO_NUMBER_TYPES = [
  { value: 'toll-free', label: 'Toll-free (+1 8xx)' },
  { value: 'local', label: 'Local (US area code)' },
] as const;

function PremiumTwilioForm({ twilioLinkData, onSaved }: SubFormProps) {
  // The connect form is shown when the user hasn't provisioned a number
  // yet (status: not_provisioned / released / failed). Once active, we
  // show the connected number plus a disconnect button.
  const [personalPhone, setPersonalPhone] = useState('');
  const [numberType, setNumberType] = useState<string>('toll-free');
  const [areaCode, setAreaCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const status = twilioLinkData?.status ?? 'not_provisioned';
  const isActive = status === 'active' && twilioLinkData?.twilio_phone_number;
  // The operator may lock the number-type / area-code picker to the
  // configured defaults (premium config: ``twilio_user_number_choice_enabled``).
  // Older premium deployments don't ship the field; default to showing
  // the picker so we don't quietly remove it.
  const userNumberChoiceEnabled = twilioLinkData?.user_number_choice_enabled ?? true;

  // ``provisioning`` is a transient state, normally only seen for a few
  // seconds during connect. If we land here on page load it usually
  // means the previous connect request crashed mid-flow; the server
  // recovers stale rows after a timeout but the user needs an escape
  // hatch in the meantime. The Refresh button re-fetches the link
  // state so the user can poll for resolution without a full reload.
  if (status === 'provisioning') {
    return (
      <div className="grid gap-2">
        <p className="text-sm">Provisioning your number...</p>
        <p className="text-xs text-muted-foreground">This usually takes a few seconds.</p>
        <div className="flex justify-end">
          <Button onClick={onSaved} variant="secondary">
            Refresh
          </Button>
        </div>
      </div>
    );
  }

  if (isActive) {
    return <PremiumTwilioConnected twilioLinkData={twilioLinkData!} onSaved={onSaved} />;
  }

  const handleConnect = async () => {
    setError(null);
    const normalized = normalizeUsPhone(personalPhone);
    if (!isValidE164(normalized)) {
      setError(PHONE_FORMAT_ERROR);
      return;
    }
    setSubmitting(true);
    try {
      await api.connectTwilio({
        personal_phone: normalized,
        number_type: numberType,
        area_code: areaCode.trim(),
      });
      toast.success('Twilio number provisioned');
      onSaved();
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to connect Twilio';
      setError(msg);
      toast.error(msg);
    } finally {
      setSubmitting(false);
    }
  };

  // Previously-failed attempts surface the reason inline so the user
  // can adjust (e.g. different area code) before retrying.
  const previousError = status === 'failed' ? twilioLinkData?.error_message : '';

  return (
    <div className="grid gap-4">
      {previousError && (
        <p className="text-xs text-danger" role="alert">
          Last attempt failed: {previousError}
        </p>
      )}
      <Field label="Your Phone Number">
        <Input
          value={personalPhone}
          onChange={(e) => {
            setPersonalPhone(e.target.value);
            if (error) setError(null);
          }}
          placeholder="e.g. +15551234567"
          inputMode="tel"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? 'twilio-personal-error' : undefined}
        />
        {error ? (
          <p id="twilio-personal-error" className="text-xs text-danger mt-1">{error}</p>
        ) : (
          <p className="text-xs text-muted-foreground mt-1">
            E.164 format. Only this number will be able to message your assistant via SMS.
          </p>
        )}
      </Field>
      {userNumberChoiceEnabled && (
        <>
          <Field label="Number Type">
            <Select
              value={numberType}
              onChange={(e) => setNumberType(e.target.value)}
              aria-label="Twilio number type"
            >
              {TWILIO_NUMBER_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground mt-1">
              Toll-free skips US A2P 10DLC registration. Local is cheaper per message but the operator
              must have a registered messaging service.
            </p>
          </Field>
          <Field label="Area Code (optional)">
            <Input
              value={areaCode}
              onChange={(e) => setAreaCode(e.target.value)}
              placeholder={numberType === 'toll-free' ? 'e.g. 800' : 'e.g. 415'}
              inputMode="numeric"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Leave blank to let Twilio pick from available inventory.
            </p>
          </Field>
        </>
      )}
      <div className="flex justify-end">
        <Button onClick={handleConnect} disabled={submitting || !personalPhone} isLoading={submitting}>
          Connect Twilio
        </Button>
      </div>
    </div>
  );
}

function PremiumTwilioConnected({
  twilioLinkData,
  onSaved,
}: { twilioLinkData: TwilioLinkData; onSaved: () => void }) {
  const [disconnecting, setDisconnecting] = useState(false);

  const handleDisconnect = async () => {
    setDisconnecting(true);
    try {
      await api.disconnectTwilio();
      toast.success('Twilio disconnected');
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to disconnect');
    } finally {
      setDisconnecting(false);
    }
  };

  return (
    <div className="grid gap-3">
      <div className="text-sm">
        Text{' '}
        <span className="font-mono font-medium text-primary">
          {twilioLinkData.twilio_phone_number}
        </span>{' '}
        from{' '}
        <span className="font-mono font-medium">{twilioLinkData.personal_phone}</span>{' '}
        to chat with your assistant.
      </div>
      <div className="flex justify-end">
        <Button
          onClick={handleDisconnect}
          isLoading={disconnecting}
          disabled={disconnecting}
          variant="secondary"
        >
          Disconnect
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
