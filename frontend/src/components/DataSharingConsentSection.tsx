import { useCallback } from 'react';
import { Spinner } from '@heroui/spinner';
import Checkbox from '@/components/ui/checkbox';
import { toast } from '@/lib/toast';
import {
  useDataSharingConsent,
  useUpdateDataSharingConsent,
} from '@/hooks/queries';

interface Props {
  /** Heading rendered above the checkbox. Falls back to "Help improve Clawbolt". */
  heading?: string;
  /** Visible only on first-run flows (e.g. Get Started) where we want a softer affordance. */
  variant?: 'default' | 'compact';
}

function formatToggledAt(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export default function DataSharingConsentSection({
  heading = 'Help improve Clawbolt',
  variant = 'default',
}: Props) {
  const { data, isLoading } = useDataSharingConsent();
  const updateConsent = useUpdateDataSharingConsent();

  const consent = data?.data_sharing_consent ?? false;
  const lastChange = formatToggledAt(data?.data_sharing_consent_at ?? null);

  const handleToggle = useCallback(
    (next: boolean) => {
      // Optimistic-ish: react-query updates the cache after the mutation
      // resolves via setQueryData. If it fails, we surface the error and
      // the cached value stays at its prior truth.
      updateConsent.mutate(
        { consent: next },
        {
          onSuccess: () =>
            toast.success(
              next
                ? "Thanks. We'll use your chats to improve Clawbolt."
                : 'Sharing turned off. Your chats are private again.',
            ),
          onError: (e) => toast.error(e.message),
        },
      );
    },
    [updateConsent],
  );

  const headingClass =
    variant === 'compact'
      ? 'text-sm font-semibold font-display'
      : 'text-sm font-medium';

  return (
    <div className="grid gap-3">
      <div>
        <h3 className={headingClass}>{heading}</h3>
        <p className="text-sm text-muted-foreground mt-1">
          Let our team read your chat history to improve Clawbolt and study
          how people use AI assistants. You can turn this off any time and
          we'll stop reading from that moment on.
        </p>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner size="sm" aria-label="Loading consent" />
          <span>Loading…</span>
        </div>
      ) : (
        <div>
          <Checkbox
            id="data-sharing-consent"
            checked={consent}
            disabled={updateConsent.isPending}
            onChange={(e) => handleToggle(e.target.checked)}
          >
            <span className="text-sm">
              Share my chat history with the Clawbolt team for product
              research.
            </span>
          </Checkbox>
          {lastChange && (
            <p className="text-xs text-muted-foreground mt-2">
              Last updated {lastChange}.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
