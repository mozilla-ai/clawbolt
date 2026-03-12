import { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import type { AppShellContext } from '@/layouts/AppShell';

export default function UserPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const [text, setText] = useState(profile?.user_text ?? '');
  const updateProfile = useUpdateProfile();

  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  useEffect(() => {
    if (profile) {
      setText(profile.user_text);
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
      { user_text: text },
      {
        onSuccess: () => toast.success('User info updated'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-xl font-semibold">User</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Updated over time as your assistant learns about you.
          </p>
        </div>
        <Button onClick={handleSave} disabled={updateProfile.isPending} isLoading={updateProfile.isPending}>
          Save
        </Button>
      </div>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        classNames={{ input: '!min-h-[65vh]' }}
        placeholder="Tell your assistant about yourself: your name, phone, timezone, preferences, what projects you're working on..."
      />
    </div>
  );
}
