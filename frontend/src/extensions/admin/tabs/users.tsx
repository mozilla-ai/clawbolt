import { useState, useEffect, useCallback, useRef } from 'react';
import { toast } from '@/lib/toast';
import {
  getAdminUsers,
  activateUser,
  deactivateUser,
  deleteUser,
  resetUserQuota,
  type AdminUser,
  type ConsentFilter,
  type UserSort,
} from '../admin-api';
import ConfirmDialog from '../ConfirmDialog';
import ConsentBadge from '../components/ConsentBadge';
import Pagination from '../components/Pagination';
import { formatAbsolute, formatRelative, planPillClass } from '../format';

const PAGE_SIZE = 50;

const SORT_OPTIONS: { value: UserSort; label: string }[] = [
  { value: 'recent', label: 'Newest signup' },
  { value: 'oldest', label: 'Oldest signup' },
  { value: 'last_message', label: 'Last message' },
  { value: 'consent', label: 'Newest consent' },
  { value: 'email', label: 'Email (A→Z)' },
  { value: 'plan', label: 'Plan' },
];

const CONSENT_FILTERS: { value: ConsentFilter; label: string; help: string }[] = [
  { value: 'all', label: 'All', help: 'Show every user.' },
  { value: 'shared', label: 'Shared only', help: 'Users who opted into research data sharing.' },
  { value: 'none', label: 'No consent', help: 'Users who have not opted in.' },
];

interface UsersTabProps {
  onSelectUser: (user: AdminUser) => void;
  currentUserId?: string;
}

export default function UsersTab({ onSelectUser, currentUserId }: UsersTabProps) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<UserSort>('recent');
  const [consentFilter, setConsentFilter] = useState<ConsentFilter>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(
    (p: number, q: string, s: UserSort, c: ConsentFilter) => {
      setLoading(true);
      setError(null);
      getAdminUsers({
        limit: PAGE_SIZE,
        offset: p * PAGE_SIZE,
        search: q || undefined,
        sort: s,
        consent: c,
      })
        .then(res => {
          setUsers(res.items);
          setTotal(res.total);
        })
        .catch((e: Error) => setError(e.message))
        .finally(() => setLoading(false));
    },
    [],
  );

  useEffect(() => {
    load(page, search, sort, consentFilter);
  }, [load, page, search, sort, consentFilter]);

  // Close menu when clicking outside
  useEffect(() => {
    if (!menuOpen) return;
    const onClick = () => setMenuOpen(null);
    document.addEventListener('click', onClick);
    return () => document.removeEventListener('click', onClick);
  }, [menuOpen]);

  const handleSearchChange = (value: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setPage(0);
      setSearch(value);
    }, 300);
  };

  const runAction = async (
    id: string,
    successMessage: string,
    action: () => Promise<void>,
  ) => {
    setActionLoading(id);
    setMenuOpen(null);
    try {
      await action();
      toast.success(successMessage);
      load(page, search, sort, consentFilter);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(null);
    }
  };

  const doDelete = async () => {
    const target = deleteTarget;
    if (!target) return;
    setActionLoading(target.id);
    try {
      await deleteUser(target.id);
      toast.success(`Deleted ${target.email || target.user_id}`);
      setDeleteTarget(null);
      load(page, search, sort, consentFilter);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setActionLoading(null);
    }
  };

  function ActionsMenu({ u, isSelf }: { u: AdminUser; isSelf: boolean }) {
    const open = menuOpen === u.id;
    return (
      <div className="relative">
        <button
          type="button"
          aria-label="Row actions"
          aria-haspopup="menu"
          aria-expanded={open}
          className="p-1.5 rounded-[--radius-sm] hover:bg-secondary-hover disabled:opacity-50"
          disabled={actionLoading === u.id}
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen(open ? null : u.id);
          }}
        >
          <svg
            className="w-4 h-4 text-muted-foreground"
            viewBox="0 0 24 24"
            fill="currentColor"
            aria-hidden
          >
            <circle cx="5" cy="12" r="1.6" />
            <circle cx="12" cy="12" r="1.6" />
            <circle cx="19" cy="12" r="1.6" />
          </svg>
        </button>
        {open && (
          <div
            role="menu"
            className="absolute right-0 top-full mt-1 z-10 min-w-[160px] bg-card border border-border rounded-[--radius-md] shadow-md py-1"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              role="menuitem"
              className="w-full text-left px-3 py-2 text-sm hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={isSelf}
              title={isSelf ? 'You cannot reset your own quota' : undefined}
              onClick={() => void runAction(u.id, 'Quota reset', () => resetUserQuota(u.id))}
            >
              Reset quota
            </button>
            {u.is_active ? (
              <button
                type="button"
                role="menuitem"
                className="w-full text-left px-3 py-2 text-sm hover:bg-secondary-hover disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={isSelf}
                title={isSelf ? 'You cannot deactivate your own account' : undefined}
                onClick={() => void runAction(u.id, 'User deactivated', () => deactivateUser(u.id))}
              >
                Deactivate
              </button>
            ) : (
              <button
                type="button"
                role="menuitem"
                className="w-full text-left px-3 py-2 text-sm hover:bg-secondary-hover disabled:opacity-50"
                onClick={() => void runAction(u.id, 'User activated', () => activateUser(u.id))}
              >
                Activate
              </button>
            )}
            <div className="my-1 border-t border-border" />
            <button
              type="button"
              role="menuitem"
              className="w-full text-left px-3 py-2 text-sm text-danger hover:bg-danger/10 disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={isSelf}
              title={isSelf ? 'You cannot delete your own account' : undefined}
              onClick={() => {
                setMenuOpen(null);
                setDeleteTarget(u);
              }}
            >
              Delete user...
            </button>
          </div>
        )}
      </div>
    );
  }

  function RoleBadge({ role }: { role: string }) {
    if (role === 'admin') {
      return (
        <span
          className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-[--radius-full] bg-primary-light text-primary font-medium"
          title="Platform admin"
        >
          <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z" />
          </svg>
          admin
        </span>
      );
    }
    return null;
  }

  function PlanPill({ plan }: { plan: string }) {
    return (
      <span className={`text-[11px] px-1.5 py-0.5 rounded-[--radius-full] font-medium ${planPillClass(plan)}`}>
        {plan}
      </span>
    );
  }

  function StatusText({ active }: { active: boolean }) {
    return (
      <span className={active ? 'text-success' : 'text-danger'}>
        {active ? 'active' : 'inactive'}
      </span>
    );
  }

  return (
    <div>
      <div className="mb-3 flex flex-col sm:flex-row gap-2">
        <input
          className="flex-1 px-3 py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
          placeholder="Search by email or user ID..."
          defaultValue=""
          onChange={e => handleSearchChange(e.target.value)}
        />
        <label className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground whitespace-nowrap">Sort</span>
          <select
            value={sort}
            onChange={e => { setPage(0); setSort(e.target.value as UserSort); }}
            className="px-2 py-2 text-sm bg-card border border-border rounded-[--radius-md]"
          >
            {SORT_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
      </div>

      {/* Consent filter pills. Surfaces three buckets so an admin can
          jump between "everyone" / "shared only" / "no consent" without
          bouncing to the old top-level Shared tab. The "Shared only"
          option is what replaces that tab's user list once PR4 lands. */}
      <div className="mb-3 flex items-center gap-2 flex-wrap" role="tablist" aria-label="Consent filter">
        <span className="text-xs text-muted-foreground">Consent:</span>
        {CONSENT_FILTERS.map(opt => (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={consentFilter === opt.value}
            title={opt.help}
            className={`text-xs px-2.5 py-1 rounded-[--radius-sm] border ${
              consentFilter === opt.value
                ? 'bg-primary text-primary-foreground border-primary'
                : 'border-border text-muted-foreground hover:text-foreground'
            }`}
            onClick={() => {
              setPage(0);
              setConsentFilter(opt.value);
            }}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />
      ) : error ? (
        <div className="text-danger text-sm">
          {error}{' '}
          <button className="text-primary hover:underline" onClick={() => load(page, search, sort, consentFilter)}>Retry</button>
        </div>
      ) : users.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          {search
            ? 'No users match that search.'
            : consentFilter === 'shared'
              ? 'No users have opted into data sharing yet.'
              : consentFilter === 'none'
                ? 'No users without consent in this view.'
                : 'No users yet.'}
        </p>
      ) : (
        <>
          {/* Desktop table */}
          <div className="hidden md:block overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left">
                  <th className="py-2 px-2 font-medium">User</th>
                  <th className="py-2 px-2 font-medium">Plan</th>
                  <th className="py-2 px-2 font-medium">Status</th>
                  <th className="py-2 px-2 font-medium">Signed up</th>
                  <th className="py-2 px-2 font-medium">Last message</th>
                  <th className="py-2 px-2 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map(u => {
                  const isSelf = !!currentUserId && currentUserId === u.id;
                  return (
                    <tr key={u.id} className="border-b border-border/50 hover:bg-secondary-hover/30">
                      <td className="py-2 px-2 max-w-[320px]">
                        <button
                          type="button"
                          className="text-left w-full truncate hover:underline decoration-dotted underline-offset-2 focus:outline-none focus:ring-2 focus:ring-primary/30 rounded-sm"
                          onClick={() => onSelectUser(u)}
                        >
                          <span className="font-medium">{u.email || u.user_id}</span>
                          {isSelf && (
                            <span className="ml-2 text-[11px] text-muted-foreground">(you)</span>
                          )}
                        </button>
                      </td>
                      <td className="py-2 px-2">
                        <div className="flex items-center gap-1.5">
                          <PlanPill plan={u.plan} />
                          <RoleBadge role={u.role} />
                          {u.data_sharing_consent && (
                            <ConsentBadge
                              consentAt={u.data_sharing_consent_at}
                              compact
                            />
                          )}
                        </div>
                      </td>
                      <td className="py-2 px-2">
                        <StatusText active={u.is_active} />
                      </td>
                      <td className="py-2 px-2 text-muted-foreground">
                        <span title={formatAbsolute(u.created_at)}>{formatRelative(u.created_at) || '—'}</span>
                      </td>
                      <td className="py-2 px-2 text-muted-foreground">
                        <span title={formatAbsolute(u.last_message_at)}>{formatRelative(u.last_message_at) || '—'}</span>
                      </td>
                      <td className="py-2 px-2 text-right">
                        <ActionsMenu u={u} isSelf={isSelf} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Mobile card layout */}
          <ul className="md:hidden space-y-2">
            {users.map(u => {
              const isSelf = !!currentUserId && currentUserId === u.id;
              return (
                <li
                  key={u.id}
                  className="bg-card border border-border rounded-[--radius-md] p-3"
                >
                  <div className="flex items-start gap-2">
                    <button
                      type="button"
                      className="flex-1 text-left min-w-0 focus:outline-none"
                      onClick={() => onSelectUser(u)}
                    >
                      <div className="font-medium text-sm truncate">
                        {u.email || u.user_id}
                        {isSelf && (
                          <span className="ml-2 text-[11px] text-muted-foreground">(you)</span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                        <PlanPill plan={u.plan} />
                        <RoleBadge role={u.role} />
                        <StatusText active={u.is_active} />
                        {u.data_sharing_consent && (
                          <ConsentBadge consentAt={u.data_sharing_consent_at} compact />
                        )}
                      </div>
                      <div className="text-[11px] text-muted-foreground mt-1.5">
                        {formatRelative(u.created_at) && <span>Joined {formatRelative(u.created_at)}</span>}
                        {u.last_message_at && (
                          <span className="ml-2">· Last message {formatRelative(u.last_message_at)}</span>
                        )}
                      </div>
                    </button>
                    <ActionsMenu u={u} isSelf={isSelf} />
                  </div>
                </li>
              );
            })}
          </ul>

          <Pagination
            page={page}
            total={total}
            pageSize={PAGE_SIZE}
            onChange={setPage}
            showGoTo
            countLabel={`${total} user${total !== 1 ? 's' : ''}`}
          />
        </>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onConfirm={doDelete}
        title={`Delete ${deleteTarget?.email || deleteTarget?.user_id || 'user'}?`}
        description={
          <div className="space-y-2">
            <p>
              This permanently removes the user, all chat sessions, memory documents,
              media files, heartbeat logs, and quotas. It cannot be undone.
            </p>
            <p className="text-xs">
              Messages this month: <strong>{deleteTarget?.messages_this_month ?? 0}</strong>
            </p>
          </div>
        }
        confirmLabel="Delete user"
        destructive
        busy={actionLoading === deleteTarget?.id}
      />
    </div>
  );
}
