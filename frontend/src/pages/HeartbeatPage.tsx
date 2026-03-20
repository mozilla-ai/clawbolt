import { useEffect, useCallback } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import MarkdownEditor from '@/components/ui/MarkdownEditor';
import type { AppShellContext } from '@/layouts/AppShell';

export default function HeartbeatPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  const handleSave = useCallback(
    (text: string) => {
      updateProfile.mutate(
        { heartbeat_text: text },
        {
          onSuccess: () => toast.success('Heartbeat updated'),
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
        <h2 className="text-xl font-semibold font-display">Heartbeat</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Your assistant reads this to stay aware of your priorities.
        </p>
      </div>
      <MarkdownEditor
        value={profile.heartbeat_text}
        onSave={handleSave}
        isSaving={updateProfile.isPending}
        placeholder="Track tasks and priorities in markdown format, e.g. - [ ] Follow up with new leads"
        emptyMessage="No heartbeat text yet. Click Edit to track your tasks and priorities."
      />
    </div>
  );
}
