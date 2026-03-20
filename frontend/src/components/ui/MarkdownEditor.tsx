import { useState, useEffect, useCallback } from 'react';
import Markdown from 'react-markdown';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';

interface MarkdownEditorProps {
  value: string;
  onSave: (value: string) => Promise<void> | void;
  isSaving: boolean;
  placeholder?: string;
  emptyMessage?: string;
}

export default function MarkdownEditor({
  value,
  onSave,
  isSaving,
  placeholder,
  emptyMessage = 'Nothing here yet. Click Edit to add content.',
}: MarkdownEditorProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  const handleEdit = useCallback(() => {
    setDraft(value);
    setEditing(true);
  }, [value]);

  const handleCancel = useCallback(() => {
    setDraft(value);
    setEditing(false);
  }, [value]);

  const handleSave = useCallback(async () => {
    try {
      await onSave(draft);
      setEditing(false);
    } catch {
      // Stay in edit mode so the user doesn't lose their draft.
      // The caller is responsible for showing error feedback.
    }
  }, [draft, onSave]);

  if (editing) {
    return (
      <div>
        <div className="flex justify-end gap-2 mb-3">
          <Button variant="ghost" onClick={handleCancel} disabled={isSaving}>
            Cancel
          </Button>
          <Button onClick={handleSave} isLoading={isSaving} disabled={isSaving}>
            Save
          </Button>
        </div>
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={6}
          classNames={{ input: '!min-h-[65vh]' }}
          placeholder={placeholder}
          autoFocus
        />
      </div>
    );
  }

  return (
    <div>
      <div className="flex justify-end mb-3">
        <Button variant="secondary" onClick={handleEdit}>
          <EditIcon />
          Edit
        </Button>
      </div>
      <div
        className="prose-page min-h-[65vh] rounded-[var(--radius-lg)] border border-border bg-card p-6 cursor-pointer"
        onClick={handleEdit}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') handleEdit();
        }}
      >
        {value.trim() ? (
          <Markdown>{value}</Markdown>
        ) : (
          <p className="text-muted-foreground italic">{emptyMessage}</p>
        )}
      </div>
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
