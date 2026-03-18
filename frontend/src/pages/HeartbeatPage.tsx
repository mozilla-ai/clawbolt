import { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import Textarea from '@/components/ui/textarea';
import Input from '@/components/ui/input';
import Button from '@/components/ui/button';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import {
  useUpdateProfile,
  useHeartbeatItems,
  useCreateHeartbeatItem,
  useDeleteHeartbeatItem,
  useUpdateHeartbeatItem,
} from '@/hooks/queries';
import type { HeartbeatItem } from '@/types';
import type { AppShellContext } from '@/layouts/AppShell';

function HeartbeatItemRow({
  item,
  onDelete,
  onUpdate,
}: {
  item: HeartbeatItem;
  onDelete: (id: number) => void;
  onUpdate: (id: number, body: { description?: string; schedule?: string; status?: string }) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [desc, setDesc] = useState(item.description);
  const [schedule, setSchedule] = useState(item.schedule);

  const handleSave = () => {
    onUpdate(item.id, { description: desc, schedule });
    setEditing(false);
  };

  const handleCancel = () => {
    setDesc(item.description);
    setSchedule(item.schedule);
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="flex flex-col gap-2 p-3 rounded-lg border bg-content1">
        <Input
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          placeholder="Description"
        />
        <Input
          value={schedule}
          onChange={(e) => setSchedule(e.target.value)}
          placeholder="Schedule (e.g. daily, weekly, Mon/Wed/Fri)"
        />
        <div className="flex gap-2">
          <Button size="sm" onClick={handleSave}>Save</Button>
          <Button size="sm" variant="ghost" onClick={handleCancel}>Cancel</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start justify-between gap-3 p-3 rounded-lg border bg-content1">
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium">{item.description}</p>
        {item.schedule && (
          <p className="text-xs text-muted-foreground mt-0.5">{item.schedule}</p>
        )}
      </div>
      <div className="flex gap-1 shrink-0">
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
          Edit
        </Button>
        <Button size="sm" variant="danger" onClick={() => onDelete(item.id)}>
          Delete
        </Button>
      </div>
    </div>
  );
}

export default function HeartbeatPage() {
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const [heartbeatText, setHeartbeatText] = useState(profile?.heartbeat_text ?? '');
  const updateProfile = useUpdateProfile();

  const { data: items, isLoading: itemsLoading } = useHeartbeatItems();
  const createItem = useCreateHeartbeatItem();
  const deleteItem = useDeleteHeartbeatItem();
  const updateItem = useUpdateHeartbeatItem();

  const [newDesc, setNewDesc] = useState('');
  const [newSchedule, setNewSchedule] = useState('');

  useEffect(() => {
    reloadProfile();
  }, [reloadProfile]);

  useEffect(() => {
    if (profile) {
      setHeartbeatText(profile.heartbeat_text);
    }
  }, [profile]);

  if (!profile) {
    return (
      <div className="flex justify-center py-12">
        <Spinner color="primary" size="md" aria-label="Loading" />
      </div>
    );
  }

  const handleAddItem = () => {
    const desc = newDesc.trim();
    if (!desc) return;
    createItem.mutate(
      { description: desc, schedule: newSchedule.trim() || undefined },
      {
        onSuccess: () => {
          setNewDesc('');
          setNewSchedule('');
          toast.success('Item added');
        },
        onError: (e) => toast.error(e.message),
      },
    );
  };

  const handleDeleteItem = (id: number) => {
    deleteItem.mutate(id, {
      onSuccess: () => toast.success('Item removed'),
      onError: (e) => toast.error(e.message),
    });
  };

  const handleUpdateItem = (
    id: number,
    body: { description?: string; schedule?: string; status?: string },
  ) => {
    updateItem.mutate(
      { id, body },
      {
        onSuccess: () => toast.success('Item updated'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  const handleSaveText = () => {
    updateProfile.mutate(
      { heartbeat_text: heartbeatText },
      {
        onSuccess: () => toast.success('Notes saved'),
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-xl font-semibold">Heartbeat</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Your assistant reads this to stay aware of your priorities.
        </p>
      </div>

      {/* Structured heartbeat items */}
      <div className="mb-6">
        <h3 className="text-base font-medium mb-3">Active Items</h3>
        {itemsLoading ? (
          <div className="flex justify-center py-4">
            <Spinner color="primary" size="sm" aria-label="Loading items" />
          </div>
        ) : items && items.length > 0 ? (
          <div className="flex flex-col gap-2 mb-3">
            {items.map((item) => (
              <HeartbeatItemRow
                key={item.id}
                item={item}
                onDelete={handleDeleteItem}
                onUpdate={handleUpdateItem}
              />
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground mb-3">
            No items yet. Add one below, or ask your assistant to create them.
          </p>
        )}

        {/* Add new item form */}
        <div className="flex flex-col gap-2 p-3 rounded-lg border border-dashed">
          <Input
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder="New item description"
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleAddItem();
            }}
          />
          <div className="flex gap-2">
            <Input
              value={newSchedule}
              onChange={(e) => setNewSchedule(e.target.value)}
              placeholder="Schedule (optional, e.g. daily, weekly)"
              className="flex-1"
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleAddItem();
              }}
            />
            <Button
              onClick={handleAddItem}
              disabled={!newDesc.trim() || createItem.isPending}
              isLoading={createItem.isPending}
            >
              Add
            </Button>
          </div>
        </div>
      </div>

      {/* Freeform notes */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-base font-medium">Notes</h3>
          <Button
            size="sm"
            onClick={handleSaveText}
            disabled={updateProfile.isPending}
            isLoading={updateProfile.isPending}
          >
            Save
          </Button>
        </div>
        <Textarea
          value={heartbeatText}
          onChange={(e) => setHeartbeatText(e.target.value)}
          rows={6}
          classNames={{ input: '!min-h-[30vh]' }}
          placeholder="Additional notes for your assistant (markdown supported)"
        />
      </div>
    </div>
  );
}
