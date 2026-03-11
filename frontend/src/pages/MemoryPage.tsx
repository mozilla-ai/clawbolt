import { lazy, Suspense, useState, useEffect, useCallback } from 'react';
import Card from '@/components/ui/card';
import Badge from '@/components/ui/badge';
import Button from '@/components/ui/button';
import Spinner from '@/components/ui/spinner';
import { Modal, ModalContent, ModalHeader, ModalBody } from '@heroui/modal';
import api from '@/api';
import type { MemoryFact } from '@/types';

const EditFactForm = lazy(() => import('@/pages/EditFactForm'));

export default function MemoryPage() {
  const [facts, setFacts] = useState<MemoryFact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [editingFact, setEditingFact] = useState<MemoryFact | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listMemoryFacts()
      .then(setFacts)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const categories = [...new Set(facts.map((f) => f.category))].sort();
  const filtered = filter
    ? facts.filter((f) => f.category === filter)
    : facts;

  const handleDelete = async (key: string) => {
    try {
      await api.deleteMemoryFact(key);
      setFacts((prev) => prev.filter((f) => f.key !== key));
      setDeleteConfirm(null);
    } catch (e) {
      alert((e as Error).message);
    }
  };

  const handleSaveEdit = async (key: string, value: string) => {
    try {
      const updated = await api.updateMemoryFact(key, { value });
      setFacts((prev) => prev.map((f) => (f.key === key ? updated : f)));
      setEditingFact(null);
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <div>
      <div className="mb-6">
        <h2 className="heading-page">Memory / Facts</h2>
        <p className="page-subtitle">
          Review and correct what your AI assistant knows about you and your business.
        </p>
      </div>

      {loading ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : error ? (
        <Card className="text-center py-8">
          <p className="text-sm text-danger">{error}</p>
          <Button variant="secondary" size="sm" className="mt-2" onClick={load}>Retry</Button>
        </Card>
      ) : facts.length === 0 ? (
        <Card className="text-center py-8">
          <p className="text-sm text-muted-foreground">
            No memory facts yet. Chat with your assistant on Telegram to build up knowledge.
          </p>
        </Card>
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
                All ({facts.length})
              </Button>
              {categories.map((cat) => {
                const count = facts.filter((f) => f.category === cat).length;
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

          <div className="space-y-2">
            {filtered.map((fact) => (
              <Card key={fact.key} className="flex items-start justify-between gap-3">
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
                <div className="flex gap-1 shrink-0">
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
              </Card>
            ))}
          </div>
        </>
      )}

      {/* Edit modal - lazy-loaded */}
      <Modal isOpen={!!editingFact} onOpenChange={(open) => { if (!open) setEditingFact(null); }}>
        <ModalContent>
          <ModalHeader>Edit Fact: {editingFact?.key}</ModalHeader>
          <ModalBody>
            {editingFact && (
              <Suspense fallback={null}>
                <EditFactForm
                  fact={editingFact}
                  onSave={handleSaveEdit}
                  onCancel={() => setEditingFact(null)}
                />
              </Suspense>
            )}
          </ModalBody>
        </ModalContent>
      </Modal>
    </div>
  );
}
