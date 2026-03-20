import { useCallback } from 'react';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useMemory, useUpdateMemory } from '@/hooks/queries';
import MarkdownEditor from '@/components/ui/MarkdownEditor';

export default function MemoryPage() {
  const { data, isPending, isError, error } = useMemory();
  const updateMutation = useUpdateMemory();

  const handleSave = useCallback(
    async (text: string) => {
      await updateMutation.mutateAsync(
        { content: text },
        {
          onSuccess: () => toast.success('Memory updated'),
          onError: (e) => toast.error(e.message),
        },
      );
    },
    [updateMutation],
  );

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

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-xl font-semibold font-display">Memory</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Long-term facts your AI assistant has learned about your business.
          Updated automatically as you chat, or edit directly below.
        </p>
      </div>
      <MarkdownEditor
        value={data?.content ?? ''}
        onSave={handleSave}
        isSaving={updateMutation.isPending}
        placeholder="No memories yet. Chat with your assistant on Telegram to build up knowledge, or add notes here directly."
        emptyMessage="No memories yet. Click Edit to add notes, or chat with your assistant to build up knowledge automatically."
      />
    </div>
  );
}
