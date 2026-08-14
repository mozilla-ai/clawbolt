import { useState, useEffect, useCallback } from 'react';
import { toast } from '@/lib/toast';
import {
  listAllowedEmails,
  addAllowedEmail,
  removeAllowedEmail,
  listWaitlistEntries,
  approveWaitlistEntry,
  dismissWaitlistEntry,
  type AllowedEmail,
  type WaitlistEntry,
} from '../admin-api';
import ConfirmDialog from '../ConfirmDialog';
import { formatAbsolute, formatRelative } from '../format';

interface AccessAndWaitlistTabProps {
  /** Called whenever the pending waitlist count changes, so the parent can
   * keep the tab badge in sync after Approve / Dismiss. */
  onWaitlistCount?: (count: number) => void;
}

export default function AccessAndWaitlistTab({
  onWaitlistCount,
}: AccessAndWaitlistTabProps = {}) {
  // --- Allowed emails state ---
  const [emails, setEmails] = useState<AllowedEmail[]>([]);
  const [totalEmails, setTotalEmails] = useState(0);
  const [loadingEmails, setLoadingEmails] = useState(true);
  const [errorEmails, setErrorEmails] = useState<string | null>(null);
  const [newEmail, setNewEmail] = useState('');
  const [newNote, setNewNote] = useState('');
  const [adding, setAdding] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<AllowedEmail | null>(null);
  const [removing, setRemoving] = useState(false);

  // --- Waitlist state ---
  const [entries, setEntries] = useState<WaitlistEntry[]>([]);
  const [totalWaitlist, setTotalWaitlist] = useState(0);
  const [loadingWaitlist, setLoadingWaitlist] = useState(true);
  const [errorWaitlist, setErrorWaitlist] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  const [dismissTarget, setDismissTarget] = useState<WaitlistEntry | null>(null);

  const loadEmails = useCallback(() => {
    setLoadingEmails(true);
    setErrorEmails(null);
    listAllowedEmails()
      .then((res) => {
        setEmails(res.items);
        setTotalEmails(res.total);
      })
      .catch((e: Error) => setErrorEmails(e.message))
      .finally(() => setLoadingEmails(false));
  }, []);

  const loadWaitlist = useCallback(() => {
    setLoadingWaitlist(true);
    setErrorWaitlist(null);
    listWaitlistEntries()
      .then((res) => {
        setEntries(res.items);
        setTotalWaitlist(res.total);
        onWaitlistCount?.(res.total);
      })
      .catch((e: Error) => setErrorWaitlist(e.message))
      .finally(() => setLoadingWaitlist(false));
  }, [onWaitlistCount]);

  useEffect(() => {
    loadEmails();
    loadWaitlist();
  }, [loadEmails, loadWaitlist]);

  // --- Email handlers ---

  const handleAdd = async () => {
    const email = newEmail.trim();
    if (!email) return;
    // Lightweight client-side validation so obvious typos don't round-trip.
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      toast.error('Enter a valid email address');
      return;
    }
    setAdding(true);
    try {
      await addAllowedEmail({ email, note: newNote.trim() || undefined });
      toast.success(`Added ${email}`);
      setNewEmail('');
      setNewNote('');
      loadEmails();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setAdding(false);
    }
  };

  const doRemove = async () => {
    const target = removeTarget;
    if (!target) return;
    setRemoving(true);
    try {
      await removeAllowedEmail(target.id);
      toast.success(`Removed ${target.email} from allowlist`);
      setRemoveTarget(null);
      loadEmails();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRemoving(false);
    }
  };

  // --- Waitlist handlers ---

  const handleApprove = async (entry: WaitlistEntry) => {
    setActionLoading(entry.id);
    try {
      await approveWaitlistEntry(entry.id);
      toast.success(`${entry.email} approved. They can sign in now.`);
      loadWaitlist();
      loadEmails();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(null);
    }
  };

  const doDismiss = async () => {
    const target = dismissTarget;
    if (!target) return;
    setActionLoading(target.id);
    try {
      await dismissWaitlistEntry(target.id);
      toast.success(`Dismissed ${target.email}`);
      setDismissTarget(null);
      loadWaitlist();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(null);
    }
  };

  const inputClass =
    'w-full px-3 py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30';

  // --- Loading / error states (shared) ---

  const isLoading = loadingWaitlist && loadingEmails;
  const hasError = errorWaitlist || errorEmails;

  if (isLoading)
    return <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />;
  if (hasError)
    return (
      <div className="text-danger text-sm">
        {errorWaitlist || errorEmails}{' '}
        <button className="text-primary hover:underline" onClick={() => { loadWaitlist(); loadEmails(); }}>
          Retry
        </button>
      </div>
    );

  return (
    <div>
      {/* ── Waitlist section ── */}
      <div className="mb-6">
        <h4 className="text-sm font-semibold mb-2">Pending requests</h4>
        <p className="text-xs text-muted-foreground mb-3">
          Users who requested access. Approve to add them to the allowlist so
          they can sign in.
        </p>

        {entries.length === 0 ? (
          <p className="text-sm text-muted-foreground italic">
            No waitlist entries yet.
          </p>
        ) : (
          <div className="bg-card border border-border rounded-[--radius-md] divide-y divide-border/50">
            {entries.map((e) => (
              <div
                key={e.id}
                className="flex flex-col sm:flex-row sm:items-start gap-2 px-3 py-2.5"
              >
                <div className="flex-1 min-w-0">
                  <span className="font-medium text-sm truncate block">
                    {e.name}
                  </span>
                  <span className="text-xs text-muted-foreground truncate block">
                    {e.email}
                  </span>
                  {e.use_case && (
                    <p className="text-xs text-foreground/80 mt-1 whitespace-pre-wrap">
                      {e.use_case}
                    </p>
                  )}
                  <div className="text-xs text-muted-foreground mt-0.5">
                    <span>via {e.source}</span>
                    {e.created_at && (
                      <span
                        className="ml-2"
                        title={formatAbsolute(e.created_at)}
                      >
                        · {formatRelative(e.created_at)}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex gap-2 shrink-0">
                  <button
                    type="button"
                    className="px-3 py-1.5 text-xs font-medium rounded-[--radius-sm] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
                    onClick={() => void handleApprove(e)}
                    disabled={actionLoading === e.id}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="px-3 py-1.5 text-xs font-medium rounded-[--radius-sm] border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-50"
                    onClick={() => setDismissTarget(e)}
                    disabled={actionLoading === e.id}
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {totalWaitlist > 0 && (
          <p className="text-xs text-muted-foreground mt-2">
            {totalWaitlist} entr{totalWaitlist !== 1 ? 'ies' : 'y'} pending.
          </p>
        )}
      </div>

      {/* ── Allowed emails section ── */}
      <hr className="border-border mb-6" />

      <div>
        <h4 className="text-sm font-semibold mb-2">Allowed emails</h4>
        <p className="text-xs text-muted-foreground mb-3">
          Only people on this list can sign up. Add emails to pre-approve new
          users, or leave it empty to allow anyone (if open registration is
          enabled on the server).
        </p>

        {emails.length === 0 ? (
          <p className="text-sm text-muted-foreground italic mb-3">
            No allowed emails yet. Add one below to pre-approve a user.
          </p>
        ) : (
          <div className="mb-4 bg-card border border-border rounded-[--radius-md] divide-y divide-border/50">
            {emails.map((e) => (
              <div
                key={e.id}
                className="flex items-center justify-between px-3 py-2 gap-2"
              >
                <div className="min-w-0">
                  <span className="font-medium text-sm truncate block">
                    {e.email}
                  </span>
                  {e.note && (
                    <span className="text-xs text-muted-foreground">
                      {e.note}
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  className="shrink-0 px-2 py-1 text-xs rounded-[--radius-sm] border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-50"
                  onClick={() => setRemoveTarget(e)}
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}

        <p className="text-xs text-muted-foreground mb-3">
          {totalEmails} email{totalEmails !== 1 ? 's' : ''} allowed.
        </p>

        <form
          className="bg-card border border-border rounded-[--radius-md] p-4"
          onSubmit={(e) => {
            e.preventDefault();
            void handleAdd();
          }}
        >
          <h5 className="text-xs font-semibold mb-3 text-muted-foreground">
            Add allowed email
          </h5>
          <div className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-end">
            <div className="flex-1">
              <label
                htmlFor="access-email"
                className="text-xs text-muted-foreground block mb-1"
              >
                Email
              </label>
              <input
                id="access-email"
                type="email"
                className={inputClass}
                placeholder="user@example.com"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                required
              />
            </div>
            <div className="sm:w-48">
              <label
                htmlFor="access-note"
                className="text-xs text-muted-foreground block mb-1"
              >
                Note (optional)
              </label>
              <input
                id="access-note"
                className={inputClass}
                placeholder="e.g. Team lead"
                value={newNote}
                onChange={(e) => setNewNote(e.target.value)}
              />
            </div>
            <button
              type="submit"
              className="px-4 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
              disabled={!newEmail.trim() || adding}
            >
              {adding ? 'Adding...' : 'Add'}
            </button>
          </div>
        </form>
      </div>

      <ConfirmDialog
        open={removeTarget !== null}
        onClose={() => setRemoveTarget(null)}
        onConfirm={doRemove}
        title={`Remove ${removeTarget?.email || 'email'} from allowlist?`}
        description={
          <p>
            They won't be able to sign up until re-added. Existing users with
            this email are not affected.
          </p>
        }
        confirmLabel="Remove"
        busy={removing}
      />

      <ConfirmDialog
        open={dismissTarget !== null}
        onClose={() => setDismissTarget(null)}
        onConfirm={doDismiss}
        title={`Dismiss ${dismissTarget?.email || 'request'}?`}
        description={
          <p>
            This removes the waitlist entry. The user can request access
            again, but their original request and timestamp are gone.
          </p>
        }
        confirmLabel="Dismiss"
        busy={actionLoading === dismissTarget?.id}
      />
    </div>
  );
}
