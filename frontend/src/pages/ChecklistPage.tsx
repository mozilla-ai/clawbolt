import { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import Spinner from '@/components/ui/spinner';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import type { AppShellContext } from '@/layouts/AppShell';

export default function ChecklistPage() {
  const { profile } = useOutletContext<AppShellContext>();
  const [checklistText, setChecklistText] = useState(profile?.checklist_text ?? '');
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    if (profile) {
      setChecklistText(profile.checklist_text);
    }
  }, [profile]);

  if (!profile) {
    return (
      <div className="flex justify-center py-12">
        <Spinner />
      </div>
    );
  }

  const handleSave = () => {
    updateProfile.mutate(
      { checklist_text: checklistText },
      {
        onSuccess: () => toast.success('Checklist updated'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

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
            <Button onClick={handleSave} disabled={updateProfile.isPending} isLoading={updateProfile.isPending}>
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
