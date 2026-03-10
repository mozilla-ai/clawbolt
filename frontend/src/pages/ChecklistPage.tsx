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
        .catch(() => { /* profile loaded via outlet; fallback fetch is best-effort */ })
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
          A general-purpose checklist for tracking tasks and to-dos your assistant can reference.
        </p>
      </div>
      <Card>
        <div className="grid gap-4">
          <Field label="Checklist (HEARTBEAT.md)">
            <Textarea
              value={checklistText}
              onChange={(e) => setChecklistText(e.target.value)}
              rows={14}
              placeholder="Track tasks and to-dos in markdown format, e.g. - [ ] Follow up with new leads"
            />
            <p className="helper-text">
              Your personal checklist, stored as HEARTBEAT.md. Your assistant can read this to stay aware of your priorities.
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
