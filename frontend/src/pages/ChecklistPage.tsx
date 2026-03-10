import { useState, useEffect, useCallback } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import Spinner from '@/components/ui/spinner';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import api from '@/api';
import type { AppShellContext } from '@/layouts/AppShell';

export default function ChecklistPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const [checklistText, setChecklistText] = useState(profile?.checklist_text ?? '');
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(!profile);

  useEffect(() => {
    if (!profile) {
      setLoading(true);
      api.getProfile()
        .then((p) => {
          setChecklistText(p.checklist_text);
        })
        .catch(() => {})
        .finally(() => setLoading(false));
    }
  }, [profile]);

  useEffect(() => {
    if (profile) {
      setChecklistText(profile.checklist_text);
      setLoading(false);
    }
  }, [profile]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateProfile({ checklist_text: checklistText });
      reloadProfile();
      toast.success('Checklist updated');
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [checklistText, reloadProfile]);

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="heading-page">Checklist</h2>
        <p className="page-subtitle">
          Items your assistant will check on during heartbeat reminders.
        </p>
      </div>
      <Card>
        <div className="grid gap-4">
          <Field label="Checklist (CHECKLIST.md)">
            <Textarea
              value={checklistText}
              onChange={(e) => setChecklistText(e.target.value)}
              rows={14}
              placeholder="Add checklist items for your assistant to remind you about. Use markdown format, e.g. - [ ] Follow up with new leads"
            />
            <p className="helper-text">
              Your assistant uses this checklist during heartbeat check-ins to remind you about important tasks.
            </p>
          </Field>
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={saving}>
              {saving ? 'Saving...' : 'Save'}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
