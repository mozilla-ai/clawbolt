import { useState, lazy, Suspense } from 'react';
import Badge from '@/components/ui/badge';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import { Spinner } from '@heroui/spinner';
import { ModalContent, ModalHeader, ModalBody } from '@heroui/modal';
import { toast } from '@/lib/toast';
import { useMemoryFacts, useUpdateMemoryFact, useDeleteMemoryFact } from '@/hooks/queries';
import type { MemoryFact } from '@/types';

const Modal = lazy(() => import('@heroui/modal').then(m => ({ default: m.Modal })));

export default function MemoryPage() {
  const { data: facts, isPending, isError, error } = useMemoryFacts();
  const [filter, setFilter] = useState('');
  const [editingFact, setEditingFact] = useState<MemoryFact | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const deleteMutation = useDeleteMemoryFact();

  const handleDelete = (key: string) => {
    deleteMutation.mutate(key, {
      onSuccess: () => setDeleteConfirm(null),
      onError: (e) => toast.error(e.message),
    });
  };

  const allFacts = facts ?? [];
  const categories = [...new Set(allFacts.map((f) => f.category))].sort();
  const filtered = filter
    ? allFacts.filter((f) => f.category === filter)
    : allFacts;

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl font-semibold">Memory / Facts</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Review and correct what your AI assistant knows about you and your business.
        </p>
      </div>

      {isPending && !facts ? (
        <div className="flex justify-center py-12"><Spinner color="primary" size="md" aria-label="Loading" /></div>
      ) : isError && !facts ? (
        <div className="text-center py-8">
          <p className="text-sm text-danger">{error.message}</p>
        </div>
      ) : allFacts.length === 0 ? (
        <div className="text-center py-8">
          <p className="text-sm text-muted-foreground">
            No memory facts yet. Chat with your assistant on Telegram to build up knowledge.
          </p>
        </div>
      ) : (
        <>
          {/* Category filter */}
          {categories.length > 1 && (
            <div className="flex flex-wrap gap-2 mb-4">
              <Button
                variant={!filter ? 'primary' : 'secondary'}
                size="sm"
                onClick={() => setFilter('')}
                className="rounded-full text-xs px-3 py-1"
              >
                All ({allFacts.length})
              </Button>
              {categories.map((cat) => {
                const count = allFacts.filter((f) => f.category === cat).length;
                return (
                  <Button
                    key={cat}
                    variant={filter === cat ? 'primary' : 'secondary'}
                    size="sm"
                    onClick={() => setFilter(cat)}
                    className="rounded-full text-xs px-3 py-1"
                  >
                    {cat} ({count})
                  </Button>
                );
              })}
            </div>
          )}

          <div className="divide-y divide-border">
            {filtered.map((fact) => (
              <div key={fact.key} className="group flex items-start justify-between gap-3 py-3 px-1">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium">{fact.key}</span>
                    <Badge>{fact.category}</Badge>
                    {fact.confidence < 1 && (
                      <span className="text-[10px] text-muted-foreground">
                        {Math.round(fact.confidence * 100)}% confidence
                      </span>
                    )}
                  </div>
                  <p className="text-sm text-muted-foreground">{fact.value}</p>
                </div>
                <div className="flex gap-1 shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity duration-150">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setEditingFact(fact)}
                    aria-label={`Edit ${fact.key}`}
                  >
                    Edit
                  </Button>
                  {deleteConfirm === fact.key ? (
                    <div className="flex gap-1">
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleDelete(fact.key)}
                      >
                        Confirm
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setDeleteConfirm(null)}
                      >
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setDeleteConfirm(fact.key)}
                      aria-label={`Delete ${fact.key}`}
                    >
                      Delete
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Edit modal */}
      <Suspense fallback={null}>
        <Modal isOpen={!!editingFact} onOpenChange={(open) => { if (!open) setEditingFact(null); }}>
          <ModalContent>
            <ModalHeader>Edit Fact: {editingFact?.key}</ModalHeader>
            <ModalBody>
              {editingFact && (
                <EditFactForm
                  fact={editingFact}
                  onDone={() => setEditingFact(null)}
                />
              )}
            </ModalBody>
          </ModalContent>
        </Modal>
      </Suspense>
    </div>
  );
}

function EditFactForm({
  fact,
  onDone,
}: {
  fact: MemoryFact;
  onDone: () => void;
}) {
  const [value, setValue] = useState(fact.value);
  const updateMutation = useUpdateMemoryFact();

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    updateMutation.mutate(
      { key: fact.key, body: { value } },
      {
        onSuccess: () => onDone(),
        onError: (err) => toast.error(err.message),
      },
    );
  };

  return (
    <form onSubmit={handleSubmit} className="mt-4 space-y-4">
      <div>
        <label className="text-xs font-medium text-muted-foreground block mb-1">Value</label>
        <Input value={value} onChange={(e) => setValue(e.target.value)} />
      </div>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onDone}>Cancel</Button>
        <Button type="submit" disabled={updateMutation.isPending || value === fact.value} isLoading={updateMutation.isPending}>
          Save
        </Button>
      </div>
    </form>
  );
}
