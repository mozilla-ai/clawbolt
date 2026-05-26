import { useCallback, useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import MarkdownEditor from '@/components/ui/MarkdownEditor';
import Card from '@/components/ui/card';
import Input from '@/components/ui/input';
import Select from '@/components/ui/select';
import Button from '@/components/ui/button';
import Checkbox from '@/components/ui/checkbox';
import Field from '@/components/ui/field';
import type { AppShellContext } from '@/layouts/AppShell';
import type { UserProfileResponse } from '@/types';

const HEARTBEAT_PRESETS = [
  { value: '15m', label: 'Every 15 minutes' },
  { value: '30m', label: 'Every 30 minutes' },
  { value: '1h', label: 'Every hour' },
  { value: '2h', label: 'Every 2 hours' },
  { value: '4h', label: 'Every 4 hours' },
  { value: '8h', label: 'Every 8 hours' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekdays', label: 'Weekdays only' },
  { value: 'weekly', label: 'Weekly' },
] as const;

export default function HeartbeatPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  const handleSaveText = useCallback(
    async (text: string) => {
      await updateProfile.mutateAsync(
        { heartbeat_text: text },
        {
          onSuccess: () => toast.success('Priorities updated'),
          onError: (e) => toast.error(e.message),
        },
      );
    },
    [updateProfile],
  );

  if (!profile) {
    return (
      <div className="flex justify-center py-12">
        <Spinner color="primary" size="md" aria-label="Loading" />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-xl font-semibold font-display">Priorities</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Track what you are working on and how often you want a proactive check-in from your assistant.
        </p>
      </div>
      <div className="grid gap-6">
        <CheckInSettings profile={profile} />
        <div>
          <h3 className="text-sm font-medium mb-2">What you are working on</h3>
          <p className="text-xs text-muted-foreground mb-3">
            Your assistant reads this to stay aware of your active priorities.
          </p>
          <MarkdownEditor
            value={profile.heartbeat_text}
            onSave={handleSaveText}
            isSaving={updateProfile.isPending}
            placeholder="Track tasks and priorities in markdown format, e.g. - [ ] Follow up with new leads"
            emptyMessage="No priorities yet. Click Edit to track what you're working on."
          />
        </div>
      </div>
    </div>
  );
}

function CheckInSettings({ profile }: { profile: UserProfileResponse }) {
  const updateProfile = useUpdateProfile();
  const isPreset = HEARTBEAT_PRESETS.some((p) => p.value === profile.heartbeat_frequency);
  const [form, setForm] = useState({
    heartbeat_opt_in: profile.heartbeat_opt_in,
    heartbeat_frequency: isPreset ? profile.heartbeat_frequency : 'custom',
    custom_frequency: isPreset ? '' : profile.heartbeat_frequency,
    heartbeat_max_daily: profile.heartbeat_max_daily,
  });

  useEffect(() => {
    const preset = HEARTBEAT_PRESETS.some((p) => p.value === profile.heartbeat_frequency);
    setForm({
      heartbeat_opt_in: profile.heartbeat_opt_in,
      heartbeat_frequency: preset ? profile.heartbeat_frequency : 'custom',
      custom_frequency: preset ? '' : profile.heartbeat_frequency,
      heartbeat_max_daily: profile.heartbeat_max_daily,
    });
  }, [profile.heartbeat_opt_in, profile.heartbeat_frequency, profile.heartbeat_max_daily]);

  const effectiveFrequency = form.heartbeat_frequency === 'custom'
    ? form.custom_frequency
    : form.heartbeat_frequency;

  const handleSave = () => {
    updateProfile.mutate(
      {
        heartbeat_opt_in: form.heartbeat_opt_in,
        heartbeat_frequency: effectiveFrequency,
        heartbeat_max_daily: form.heartbeat_max_daily,
      },
      {
        onSuccess: () => toast.success('Check-in settings updated'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <Card>
      <h3 className="text-sm font-medium mb-3">Proactive check-ins</h3>
      <div className="grid gap-4">
        <div className="flex items-center gap-3">
          <Checkbox
            id="heartbeat-opt-in"
            checked={form.heartbeat_opt_in}
            onChange={(e) => setForm((prev) => ({ ...prev, heartbeat_opt_in: e.target.checked }))}
          />
          <label htmlFor="heartbeat-opt-in" className="text-sm">
            Enable proactive check-ins
          </label>
        </div>
        <p className="text-xs text-muted-foreground">
          When enabled, your assistant will reach out with reminders and updates based on the priorities below.
        </p>
        <Field label="Frequency">
          <Select
            value={form.heartbeat_frequency}
            onChange={(e) => setForm((prev) => ({ ...prev, heartbeat_frequency: e.target.value }))}
            disabled={!form.heartbeat_opt_in}
          >
            {HEARTBEAT_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
            <option value="custom">Custom interval</option>
          </Select>
        </Field>
        {form.heartbeat_frequency === 'custom' && (
          <Field label="Custom Interval">
            <Input
              value={form.custom_frequency}
              onChange={(e) => setForm((prev) => ({ ...prev, custom_frequency: e.target.value }))}
              disabled={!form.heartbeat_opt_in}
              placeholder="e.g. 45m, 3h, 2d"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Use a number followed by m (minutes), h (hours), or d (days).
            </p>
          </Field>
        )}
        <Field label="Max daily check-ins">
          <Input
            type="number"
            min={0}
            value={form.heartbeat_max_daily}
            onChange={(e) => setForm((prev) => ({ ...prev, heartbeat_max_daily: parseInt(e.target.value, 10) || 0 }))}
            disabled={!form.heartbeat_opt_in}
            placeholder="0"
          />
          <p className="text-xs text-muted-foreground mt-1">
            Maximum check-ins per day. Set to 0 to use the server default.
          </p>
        </Field>
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={updateProfile.isPending} isLoading={updateProfile.isPending}>
            Save check-in settings
          </Button>
        </div>
      </div>
    </Card>
  );
}
