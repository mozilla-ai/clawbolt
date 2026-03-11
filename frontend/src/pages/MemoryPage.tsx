import { useState, useEffect } from 'react';
import Textarea from '@/components/ui/textarea';
import Button from '@/components/ui/button';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useMemory, useUpdateMemory } from '@/hooks/queries';

export default function MemoryPage() {
  const { data, isPending, isError, error } = useMemory();
  const updateMutation = useUpdateMemory();
  const [text, setText] = useState('');

  useEffect(() => {
    if (data) {
      setText(data.content);
    }
  }, [data]);

  const handleSave = () => {
    updateMutation.mutate(
      { content: text },
      {
        onSuccess: () => toast.success('Memory updated'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <div>
      <div className="mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-semibold">Memory</h2>
            <p className="text-sm text-muted-foreground mt-1">
              Long-term facts your AI assistant has learned about your business.
              Updated automatically as you chat, or edit directly below.
            </p>
          </div>
          {data && (
            <Button
              onClick={handleSave}
              disabled={updateMutation.isPending || text === data.content}
              isLoading={updateMutation.isPending}
            >
              Save
            </Button>
          )}
        </div>
      </div>

      {isPending && !data ? (
        <div className="flex justify-center py-12">
          <Spinner color="primary" size="md" aria-label="Loading" />
        </div>
      ) : isError && !data ? (
        <div className="text-center py-8">
          <p className="text-sm text-danger">{error.message}</p>
        </div>
      ) : (
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          classNames={{ input: '!min-h-[65vh]' }}
          placeholder="No memories yet. Chat with your assistant on Telegram to build up knowledge, or add notes here directly."
        />
      )}
    </div>
  );
}
