import { useState, useEffect, useCallback } from 'react';
import Card from '@/components/ui/card';
import Badge from '@/components/ui/badge';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import Select from '@/components/ui/select';
import Spinner from '@/components/ui/spinner';
import api from '@/api';
import type { ChecklistItem } from '@/types';

export default function ChecklistPage() {
  const [items, setItems] = useState<ChecklistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);

  // New item form
  const [newDescription, setNewDescription] = useState('');
  const [newSchedule, setNewSchedule] = useState('daily');
  const [creating, setCreating] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listChecklist()
      .then(setItems)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newDescription.trim()) return;
    setCreating(true);
    try {
      const item = await api.createChecklistItem({
        description: newDescription.trim(),
        schedule: newSchedule,
      });
      setItems((prev) => [...prev, item]);
      setNewDescription('');
      setNewSchedule('daily');
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await api.deleteChecklistItem(id);
      setItems((prev) => prev.filter((item) => item.id !== id));
      setDeleteConfirm(null);
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl font-semibold">Checklist</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Manage items your assistant will remind you about during heartbeat check-ins.
        </p>
      </div>

      {/* Add new item */}
      <Card className="mb-6">
        <form onSubmit={handleCreate} className="flex gap-2 items-end flex-wrap sm:flex-nowrap">
          <div className="flex-1 min-w-[200px]">
            <label className="text-xs font-medium text-muted-foreground block mb-1">
              New checklist item
            </label>
            <Input
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder="e.g. Follow up with new leads"
            />
          </div>
          <div className="w-36">
            <label className="text-xs font-medium text-muted-foreground block mb-1">
              Schedule
            </label>
            <Select
              value={newSchedule}
              onChange={(e) => setNewSchedule(e.target.value)}
            >
              <option value="daily">Daily</option>
              <option value="weekdays">Weekdays</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
            </Select>
          </div>
          <Button type="submit" disabled={creating || !newDescription.trim()}>
            {creating ? 'Adding...' : 'Add'}
          </Button>
        </form>
      </Card>

      {/* List */}
      {loading ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : error ? (
        <Card className="text-center py-8">
          <p className="text-sm text-danger">{error}</p>
          <Button variant="secondary" size="sm" className="mt-2" onClick={load}>Retry</Button>
        </Card>
      ) : items.length === 0 ? (
        <Card className="text-center py-8">
          <p className="text-sm text-muted-foreground">
            No checklist items yet. Add one above to get started.
          </p>
        </Card>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <Card key={item.id} className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm">{item.description}</p>
                <div className="flex items-center gap-2 mt-1">
                  <Badge>{item.schedule}</Badge>
                  <Badge className={
                    item.status === 'active'
                      ? 'bg-success-bg text-success-text'
                      : ''
                  }>
                    {item.status}
                  </Badge>
                  <span className="text-[10px] text-muted-foreground">
                    Added {new Date(item.created_at).toLocaleDateString()}
                  </span>
                </div>
              </div>
              <div className="shrink-0">
                {deleteConfirm === item.id ? (
                  <div className="flex gap-1">
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => handleDelete(item.id)}
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
                    onClick={() => setDeleteConfirm(item.id)}
                    aria-label={`Delete ${item.description}`}
                  >
                    Delete
                  </Button>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
