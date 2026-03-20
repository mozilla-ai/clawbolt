import { useEffect, useCallback } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import MarkdownEditor from '@/components/ui/MarkdownEditor';
import type { AppShellContext } from '@/layouts/AppShell';

export default function UserPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  const handleSave = useCallback(
    async (text: string) => {
      await updateProfile.mutateAsync(
        { user_text: text },
        {
          onSuccess: () => toast.success('User info updated'),
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
        <h2 className="text-xl font-semibold font-display">User</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Updated over time as your assistant learns about you.
        </p>
      </div>
      <MarkdownEditor
        value={profile.user_text}
        onSave={handleSave}
        isSaving={updateProfile.isPending}
        placeholder="Tell your assistant about yourself: your name, phone, timezone, preferences, what projects you're working on..."
        emptyMessage="No user info yet. Click Edit to tell your assistant about yourself."
      />
    </div>
  );
}
