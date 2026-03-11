import { useState } from 'react';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import type { MemoryFact } from '@/types';

export default function EditFactForm({
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
        <label className="section-label">Value</label>
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
