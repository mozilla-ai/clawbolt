import { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import { Spinner } from '@heroui/spinner';
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
        <Spinner color="primary" size="md" aria-label="Loading" />
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
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-xl font-semibold">Checklist</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Your assistant reads this to stay aware of your priorities.
          </p>
        </div>
        <Button onClick={handleSave} disabled={updateProfile.isPending} isLoading={updateProfile.isPending}>
          Save
        </Button>
      </div>
      <Textarea
        value={checklistText}
        onChange={(e) => setChecklistText(e.target.value)}
        rows={28}
        placeholder="Track tasks and to-dos in markdown format, e.g. - [ ] Follow up with new leads"
      />
    </div>
  );
}
