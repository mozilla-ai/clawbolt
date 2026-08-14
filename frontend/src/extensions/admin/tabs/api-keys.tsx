import { useState, useEffect, useCallback } from 'react';
import { toast } from '@/lib/toast';
import {
  listAdminApiKeys,
  createAdminApiKey,
  revokeAdminApiKey,
  type AdminApiKeyItem,
  type AdminApiKeyMintResponse,
} from '../admin-api';
import { formatAbsolute, formatRelative } from '../format';
import ConfirmDialog from '../ConfirmDialog';

// ---------------------------------------------------------------------------
// API Keys tab
//
// Long-lived bearer tokens an admin mints for CLI / curl / script auth. The
// cleartext token is shown ONCE at mint time and never re-exposed. Revoked
// keys are kept in the list (greyed out) so an admin can audit their own
// rotation history; a permanent delete is intentionally not offered.
// ---------------------------------------------------------------------------

const inputClass =
  'w-full px-3 py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30';

export default function ApiKeysTab() {
  const [keys, setKeys] = useState<AdminApiKeyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [newLabel, setNewLabel] = useState('');
  const [minting, setMinting] = useState(false);

  // The cleartext from the most recent mint, surfaced once. Cleared when
  // the admin dismisses the reveal panel; subsequent loads of this page
  // never see the token again.
  const [reveal, setReveal] = useState<AdminApiKeyMintResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const [revokeTarget, setRevokeTarget] = useState<AdminApiKeyItem | null>(null);
  const [revoking, setRevoking] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listAdminApiKeys()
      .then((res) => setKeys(res.items))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleMint = async () => {
    const label = newLabel.trim();
    if (!label) {
      toast.error('Give the key a label so you can identify it later');
      return;
    }
    setMinting(true);
    try {
      const minted = await createAdminApiKey({ label });
      setReveal(minted);
      setCopied(false);
      setNewLabel('');
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setMinting(false);
    }
  };

  const handleCopy = async (token: string) => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      toast.success('Token copied to clipboard');
      // Reset the visual ack after a moment so a follow-up copy still
      // gives feedback.
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error('Could not copy: select the text and copy manually');
    }
  };

  const doRevoke = async () => {
    const target = revokeTarget;
    if (!target) return;
    setRevoking(true);
    try {
      await revokeAdminApiKey(target.id);
      toast.success(`Revoked ${target.label || 'key'}`);
      setRevokeTarget(null);
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRevoking(false);
    }
  };

  if (loading) return <div className="animate-pulse h-32 bg-panel rounded-[--radius-md]" />;
  if (error)
    return (
      <div className="text-danger text-sm">
        {error}{' '}
        <button className="text-primary hover:underline" onClick={load}>
          Retry
        </button>
      </div>
    );

  const activeCount = keys.filter((k) => !k.revoked_at).length;

  return (
    <div>
      <p className="text-xs text-muted-foreground mb-3">
        Long-lived tokens for CLI and script access. Send as{' '}
        <code className="text-[11px] px-1 py-0.5 rounded-[--radius-sm] bg-panel">
          Authorization: Bearer ck_...
        </code>
        . Each key inherits your admin role; demoting an admin invalidates every key
        they minted.
      </p>

      {reveal && (
        <RevealPanel
          mint={reveal}
          copied={copied}
          onCopy={() => void handleCopy(reveal.token)}
          onDismiss={() => {
            setReveal(null);
            setCopied(false);
          }}
        />
      )}

      <form
        className="bg-card border border-border rounded-[--radius-md] p-4 mb-4"
        onSubmit={(e) => {
          e.preventDefault();
          void handleMint();
        }}
      >
        <h5 className="text-xs font-semibold mb-3 text-muted-foreground">
          Create new key
        </h5>
        <div className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-end">
          <div className="flex-1">
            <label
              htmlFor="api-key-label"
              className="text-xs text-muted-foreground block mb-1"
            >
              Label
            </label>
            <input
              id="api-key-label"
              className={inputClass}
              placeholder="e.g. laptop, ci-runner, analytics-script"
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
              maxLength={200}
              required
            />
          </div>
          <button
            type="submit"
            className="px-4 py-2 text-sm font-medium rounded-[--radius-md] bg-primary text-primary-foreground hover:bg-primary-hover disabled:opacity-50"
            disabled={!newLabel.trim() || minting}
          >
            {minting ? 'Creating...' : 'Create key'}
          </button>
        </div>
      </form>

      {keys.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          You have no API keys yet. Create one above to authenticate from a CLI.
        </p>
      ) : (
        <div className="bg-card border border-border rounded-[--radius-md] divide-y divide-border/50">
          {keys.map((k) => (
            <KeyRow
              key={k.id}
              keyRow={k}
              onRevokeClick={() => setRevokeTarget(k)}
            />
          ))}
        </div>
      )}

      {keys.length > 0 && (
        <p className="text-xs text-muted-foreground mt-3">
          {activeCount} active · {keys.length} total
        </p>
      )}

      <ConfirmDialog
        open={revokeTarget !== null}
        onClose={() => setRevokeTarget(null)}
        onConfirm={doRevoke}
        title={`Revoke "${revokeTarget?.label || 'key'}"?`}
        description={
          <p>
            Any CLI or script using this key will start receiving 401 immediately.
            Revocation is permanent: you cannot re-enable a revoked key, only mint
            a new one.
          </p>
        }
        confirmLabel="Revoke"
        destructive
        busy={revoking}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// One-time cleartext reveal. Standard SaaS pattern: a prominent "save this
// now" panel with a copy button. Once dismissed, the cleartext is
// unrecoverable; the row in the list below shows the prefix only.
// ---------------------------------------------------------------------------

function RevealPanel({
  mint,
  copied,
  onCopy,
  onDismiss,
}: {
  mint: AdminApiKeyMintResponse;
  copied: boolean;
  onCopy: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="mb-4 bg-warning-bg border border-warning/40 rounded-[--radius-md] p-4">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h5 className="text-sm font-semibold mb-1">Save your new key now</h5>
          <p className="text-xs text-muted-foreground">
            This is the only time the full token is shown. Copy it somewhere safe
            (a password manager) before dismissing this panel.
          </p>
        </div>
        <button
          type="button"
          className="shrink-0 px-2 py-1 text-xs rounded-[--radius-sm] border border-border hover:bg-panel"
          onClick={onDismiss}
          aria-label="Dismiss key reveal"
        >
          Dismiss
        </button>
      </div>
      <div className="flex items-stretch gap-2">
        <code
          className="flex-1 min-w-0 px-3 py-2 text-xs font-mono bg-card border border-border rounded-[--radius-sm] truncate"
          title={mint.token}
        >
          {mint.token}
        </code>
        <button
          type="button"
          className="shrink-0 px-3 py-2 text-xs font-medium rounded-[--radius-sm] bg-primary text-primary-foreground hover:bg-primary-hover"
          onClick={onCopy}
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
      <p className="text-xs text-muted-foreground mt-2">
        Labelled <span className="font-medium">"{mint.label}"</span>.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single key row in the list. Shows label, prefix, status, created/last-used,
// and a revoke button (hidden once already revoked).
// ---------------------------------------------------------------------------

function KeyRow({
  keyRow,
  onRevokeClick,
}: {
  keyRow: AdminApiKeyItem;
  onRevokeClick: () => void;
}) {
  const isRevoked = keyRow.revoked_at !== null;
  return (
    <div
      className={`flex flex-col sm:flex-row sm:items-center justify-between px-3 py-3 gap-2 ${
        isRevoked ? 'opacity-60' : ''
      }`}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-sm truncate">
            {keyRow.label || <span className="italic text-muted-foreground">unlabeled</span>}
          </span>
          <StatusPill revoked={isRevoked} />
        </div>
        <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground flex-wrap">
          <code className="font-mono text-[11px] px-1.5 py-0.5 rounded-[--radius-sm] bg-panel">
            {keyRow.key_prefix}…
          </code>
          <span title={formatAbsolute(keyRow.created_at)}>
            Created {formatRelative(keyRow.created_at)}
          </span>
          <span title={formatAbsolute(keyRow.last_used_at)}>
            {keyRow.last_used_at
              ? `Last used ${formatRelative(keyRow.last_used_at)}`
              : 'Never used'}
          </span>
          {isRevoked && (
            <span title={formatAbsolute(keyRow.revoked_at)}>
              Revoked {formatRelative(keyRow.revoked_at)}
            </span>
          )}
        </div>
      </div>
      {!isRevoked && (
        <button
          type="button"
          className="shrink-0 px-3 py-1.5 text-xs rounded-[--radius-sm] border border-danger/40 text-danger hover:bg-danger/10"
          onClick={onRevokeClick}
        >
          Revoke
        </button>
      )}
    </div>
  );
}

function StatusPill({ revoked }: { revoked: boolean }) {
  if (revoked) {
    return (
      <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-[--radius-full] bg-panel text-muted-foreground">
        Revoked
      </span>
    );
  }
  return (
    <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-[--radius-full] bg-success-bg text-success">
      Active
    </span>
  );
}
