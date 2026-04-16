import { useEffect, useCallback } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import MarkdownEditor from '@/components/ui/MarkdownEditor';
import type { AppShellContext } from '@/layouts/AppShell';

export default function SoulPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  const handleSave = useCallback(
    async (text: string) => {
      await updateProfile.mutateAsync(
        { soul_text: text },
        {
          onSuccess: () => toast.success('Soul settings updated'),
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
        <h2 className="text-xl font-semibold font-display">Personality</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Guides how your assistant behaves and communicates.
        </p>
      </div>
      <MarkdownEditor
        value={profile.soul_text}
        onSave={handleSave}
        isSaving={updateProfile.isPending}
        placeholder="Describe how your assistant should behave, speak, and interact with clients. Include what it should call itself (e.g. 'Your name is Claw')..."
        emptyMessage="Click Edit to define your assistant's personality."
      />
    </div>
  );
}
