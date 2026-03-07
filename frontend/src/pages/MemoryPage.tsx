import { useState, useEffect, useCallback } from 'react';
import Card from '@/components/ui/card';
import Badge from '@/components/ui/badge';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import Spinner from '@/components/ui/spinner';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import api from '@/api';
import type { MemoryFact } from '@/types';

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
        <h2 className="text-xl font-semibold">Memory / Facts</h2>
        <p className="text-sm text-muted-foreground mt-1">
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
              <button
                className={`px-3 py-1 text-xs rounded-[--radius-full] border transition-colors ${
                  !filter ? 'bg-primary text-white border-primary' : 'border-border text-muted-foreground hover:text-foreground'
                }`}
                onClick={() => setFilter('')}
              >
                All ({facts.length})
              </button>
              {categories.map((cat) => {
                const count = facts.filter((f) => f.category === cat).length;
                return (
                  <button
                    key={cat}
                    className={`px-3 py-1 text-xs rounded-[--radius-full] border transition-colors ${
                      filter === cat ? 'bg-primary text-white border-primary' : 'border-border text-muted-foreground hover:text-foreground'
                    }`}
                    onClick={() => setFilter(cat)}
                  >
                    {cat} ({count})
                  </button>
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

      {/* Edit dialog */}
      <Dialog open={!!editingFact} onOpenChange={() => setEditingFact(null)}>
        <DialogContent>
          <DialogTitle>Edit Fact: {editingFact?.key}</DialogTitle>
          {editingFact && (
            <EditFactForm
              fact={editingFact}
              onSave={handleSaveEdit}
              onCancel={() => setEditingFact(null)}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function EditFactForm({
  fact,
  onSave,
  onCancel,
}: {
  fact: MemoryFact;
  onSave: (key: string, value: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(fact.value);
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      await onSave(fact.key, value);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="mt-4 space-y-4">
      <div>
        <label className="text-xs text-muted-foreground block mb-1">Value</label>
        <Input value={value} onChange={(e) => setValue(e.target.value)} />
      </div>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving || value === fact.value}>
          {saving ? 'Saving...' : 'Save'}
        </Button>
      </div>
    </form>
  );
}
