import { useEffect, useRef, useState, type FormEvent } from 'react';
import Button from '@/components/ui/button';
import Input from '@/components/ui/input';
import Field from '@/components/ui/field';
import { toast } from '@/lib/toast';
import { useConnectServiceTitan, useConnectAppFolio } from '@/hooks/queries';

/**
 * Credential-entry modal for web-form integrations.
 *
 * ServiceTitan and AppFolio authenticate with pasted secrets (client
 * credentials, a single-use magic link). Those secrets are entered here, over
 * an authenticated web session, instead of in a chat thread where they would
 * persist in the user's message history (issue #1337).
 */

interface ConnectIntegrationModalProps {
  /** Backend integration key, e.g. ``servicetitan`` or ``appfolio_vendor``. */
  integration: string;
  /** Human-readable label shown in the heading. */
  displayName: string;
  isOpen: boolean;
  onClose: () => void;
}

export default function ConnectIntegrationModal({
  integration,
  displayName,
  isOpen,
  onClose,
}: ConnectIntegrationModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const connectServiceTitan = useConnectServiceTitan();
  const connectAppFolio = useConnectAppFolio();

  const [tenantId, setTenantId] = useState('');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [magicLink, setMagicLink] = useState('');

  const isPending = connectServiceTitan.isPending || connectAppFolio.isPending;

  // Reset fields whenever the modal is reopened so a prior attempt's input
  // (including secrets) never lingers in component state.
  useEffect(() => {
    if (isOpen) {
      setTenantId('');
      setClientId('');
      setClientSecret('');
      setMagicLink('');
    }
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isPending) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, isPending, onClose]);

  if (!isOpen) return null;

  const onConnected = () => {
    toast.success(`${displayName} connected`);
    onClose();
  };
  const onError = (e: unknown) => {
    toast.error(e instanceof Error ? e.message : 'Failed to connect');
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    // Trim before sending: a trailing newline from a copied credential or
    // magic-link email would otherwise cause an avoidable auth failure.
    if (integration === 'servicetitan') {
      connectServiceTitan.mutate(
        {
          tenant_id: tenantId.trim(),
          client_id: clientId.trim(),
          client_secret: clientSecret.trim(),
        },
        { onSuccess: onConnected, onError },
      );
    } else if (integration === 'appfolio_vendor') {
      connectAppFolio.mutate({ magic_link: magicLink.trim() }, { onSuccess: onConnected, onError });
    }
  };

  const canSubmit =
    integration === 'servicetitan'
      ? !!(tenantId.trim() && clientId.trim() && clientSecret.trim())
      : !!magicLink.trim();

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === overlayRef.current && !isPending) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="connect-modal-title"
    >
      <form
        onSubmit={handleSubmit}
        className="bg-card border border-border rounded-lg shadow-lg w-full max-w-md mx-4 p-5 animate-message-in"
      >
        <h3
          id="connect-modal-title"
          className="text-base font-semibold font-display text-foreground"
        >
          Connect {displayName}
        </h3>

        {integration === 'servicetitan' ? (
          <div className="mt-3 space-y-3">
            <p className="text-xs text-muted-foreground">
              Find these in ServiceTitan under Settings, Integrations, API Application Access.
            </p>
            <Field label="Tenant ID">
              <Input value={tenantId} onChange={(e) => setTenantId(e.target.value)} disabled={isPending} autoComplete="off" />
            </Field>
            <Field label="Client ID">
              <Input value={clientId} onChange={(e) => setClientId(e.target.value)} disabled={isPending} autoComplete="off" />
            </Field>
            <Field label="Client Secret">
              <Input type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} disabled={isPending} autoComplete="off" />
            </Field>
          </div>
        ) : (
          <div className="mt-3 space-y-3">
            <p className="text-xs text-muted-foreground">
              Request a sign-in link at vendor.appfolio.com, then paste the full link from the
              email below. Magic links are single-use and expire quickly.
            </p>
            <Field label="Magic link">
              <Input value={magicLink} onChange={(e) => setMagicLink(e.target.value)} disabled={isPending} autoComplete="off" placeholder="https://vendor.appfolio.com/...magic_link_token=..." />
            </Field>
          </div>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="sm" disabled={isPending || !canSubmit} isLoading={isPending}>
            Connect
          </Button>
        </div>
      </form>
    </div>
  );
}
