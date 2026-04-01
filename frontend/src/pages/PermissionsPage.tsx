import { useState, useEffect, useCallback } from 'react';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { usePermissions, useUpdatePermissions } from '@/hooks/queries';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';

export default function PermissionsPage() {
  const { data, isPending, isError, error } = usePermissions();
  const updateMutation = useUpdatePermissions();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [jsonError, setJsonError] = useState<string | null>(null);

  const rawContent = data?.content ?? '';

  useEffect(() => {
    setDraft(rawContent);
  }, [rawContent]);

  const handleEdit = useCallback(() => {
    setDraft(rawContent);
    setJsonError(null);
    setEditing(true);
  }, [rawContent]);

  const handleCancel = useCallback(() => {
    setDraft(rawContent);
    setJsonError(null);
    setEditing(false);
  }, [rawContent]);

  const handleSave = useCallback(async () => {
    try {
      JSON.parse(draft);
    } catch {
      setJsonError('Invalid JSON. Fix the syntax before saving.');
      return;
    }
    setJsonError(null);
    try {
      await updateMutation.mutateAsync(
        { content: draft },
        {
          onSuccess: () => toast.success('Permissions updated'),
          onError: (e) => toast.error(e.message),
        },
      );
      setEditing(false);
    } catch {
      // Stay in edit mode
    }
  }, [draft, updateMutation]);

  if (isPending && !data) {
    return (
      <div className="flex justify-center py-12">
        <Spinner color="primary" size="md" aria-label="Loading" />
      </div>
    );
  }

  if (isError && !data) {
    return (
      <div className="text-center py-8">
        <p className="text-sm text-danger">{error.message}</p>
      </div>
    );
  }

  // Pretty-print JSON for display
  let displayJson = rawContent;
  try {
    displayJson = JSON.stringify(JSON.parse(rawContent), null, 2);
  } catch {
    // If it's not valid JSON, show as-is
  }

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-xl font-semibold font-display">Permissions</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Control which actions your assistant can take freely, which require approval, and which are blocked.
        </p>
      </div>

      {editing ? (
        <div>
          <div className="flex justify-end gap-2 mb-3">
            <Button variant="ghost" onClick={handleCancel} disabled={updateMutation.isPending}>
              Cancel
            </Button>
            <Button onClick={handleSave} isLoading={updateMutation.isPending} disabled={updateMutation.isPending}>
              Save
            </Button>
          </div>
          {jsonError && (
            <p className="text-sm text-danger mb-2">{jsonError}</p>
          )}
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={6}
            classNames={{ input: '!min-h-[65vh] font-mono text-sm' }}
            placeholder="No permissions configured yet."
            autoFocus
          />
        </div>
      ) : (
        <div>
          <div className="flex justify-end mb-3">
            <Button variant="secondary" onClick={handleEdit}>
              <EditIcon />
              Edit
            </Button>
          </div>
          <div
            className="min-h-[65vh] rounded-[var(--radius-lg)] border border-border bg-card p-6 cursor-pointer overflow-auto"
            onClick={handleEdit}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') handleEdit();
            }}
          >
            {displayJson.trim() ? (
              <pre className="text-sm font-mono whitespace-pre-wrap text-foreground">{displayJson}</pre>
            ) : (
              <p className="text-muted-foreground italic">
                No permissions configured yet. Permissions will be generated automatically when your assistant starts.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function EditIcon() {
  return (
    <svg className="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10"
      />
    </svg>
  );
}
