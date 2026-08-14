import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ApiKeysTab from './api-keys';

// ---------------------------------------------------------------------------
// /admin/api-keys frontend tests.
//
// Covers the four user-visible journeys:
// * Empty state when no keys exist yet.
// * Mint flow: form -> POST -> reveal panel shows cleartext exactly once.
// * Revoke flow: confirm dialog -> DELETE -> list refreshes.
// * Existing-keys list: status pill, prefix, never-used label.
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  empty: { items: [] },
  oneActive: {
    items: [
      {
        id: 1,
        label: 'laptop',
        key_prefix: 'ck_a1b2c3d4',
        created_at: '2026-05-01T12:00:00Z',
        last_used_at: '2026-05-03T09:00:00Z',
        revoked_at: null,
      },
    ],
  },
  activeAndRevoked: {
    items: [
      {
        id: 1,
        label: 'laptop',
        key_prefix: 'ck_a1b2c3d4',
        created_at: '2026-05-01T12:00:00Z',
        last_used_at: null,
        revoked_at: null,
      },
      {
        id: 2,
        label: 'old-ci',
        key_prefix: 'ck_d4e5f6g7',
        created_at: '2026-04-01T12:00:00Z',
        last_used_at: '2026-04-15T12:00:00Z',
        revoked_at: '2026-04-20T12:00:00Z',
      },
    ],
  },
  mintResp: {
    id: 99,
    token: 'ck_brand-new-cleartext-shown-once',
    key_prefix: 'ck_brand-n',
    label: 'fresh',
    created_at: '2026-05-03T15:00:00Z',
  },
}));

vi.mock('../admin-api', () => ({
  listAdminApiKeys: vi.fn(),
  createAdminApiKey: vi.fn(),
  revokeAdminApiKey: vi.fn(),
}));

vi.mock('@/lib/toast', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

beforeEach(async () => {
  const api = await import('../admin-api');
  vi.mocked(api.listAdminApiKeys).mockReset();
  vi.mocked(api.createAdminApiKey).mockReset();
  vi.mocked(api.revokeAdminApiKey).mockReset();
});

describe('ApiKeysTab list', () => {
  it('renders empty state when admin has no keys', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys).mockResolvedValueOnce(fixtures.empty);

    render(<ApiKeysTab />);

    await waitFor(() =>
      expect(screen.getByText(/no API keys yet/i)).toBeInTheDocument(),
    );
  });

  it('renders an active key with its prefix and Active pill', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys).mockResolvedValueOnce(fixtures.oneActive);

    render(<ApiKeysTab />);

    await waitFor(() => expect(screen.getByText('laptop')).toBeInTheDocument());
    // Prefix shown as monospace code with the ck_ marker visible.
    expect(screen.getByText(/ck_a1b2c3d4/)).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('greys out revoked keys, hides the Revoke button, shows the Revoked pill', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys).mockResolvedValueOnce(fixtures.activeAndRevoked);

    render(<ApiKeysTab />);

    await waitFor(() => expect(screen.getByText('old-ci')).toBeInTheDocument());
    expect(screen.getByText('Revoked')).toBeInTheDocument();
    // One Revoke button (the active key's), not two — the revoked row's
    // action is suppressed.
    expect(screen.getAllByRole('button', { name: 'Revoke' })).toHaveLength(1);
  });

  it('labels never-used keys explicitly instead of leaving the slot blank', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys).mockResolvedValueOnce({
      items: [
        {
          ...fixtures.oneActive.items[0]!,
          last_used_at: null,
        },
      ],
    });

    render(<ApiKeysTab />);

    await waitFor(() => expect(screen.getByText('Never used')).toBeInTheDocument());
  });
});

describe('ApiKeysTab mint flow', () => {
  it('shows the cleartext token in a one-time reveal panel after a successful mint', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys).mockResolvedValue(fixtures.empty);
    vi.mocked(api.createAdminApiKey).mockResolvedValueOnce(fixtures.mintResp);

    render(<ApiKeysTab />);

    await waitFor(() =>
      expect(screen.getByText(/no API keys yet/i)).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Label'), 'fresh');
    await user.click(screen.getByRole('button', { name: /create key/i }));

    // Cleartext shown verbatim once.
    await waitFor(() =>
      expect(
        screen.getByText('ck_brand-new-cleartext-shown-once'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/save your new key now/i)).toBeInTheDocument();
    // Backend call carried the trimmed label.
    expect(vi.mocked(api.createAdminApiKey)).toHaveBeenCalledWith({ label: 'fresh' });
  });

  it('refuses to submit when the label is blank without hitting the API', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys).mockResolvedValueOnce(fixtures.empty);

    render(<ApiKeysTab />);

    await waitFor(() =>
      expect(screen.getByText(/no API keys yet/i)).toBeInTheDocument(),
    );

    // The button is disabled when the label is empty; nothing to click.
    const button = screen.getByRole('button', { name: /create key/i });
    expect(button).toBeDisabled();
    expect(vi.mocked(api.createAdminApiKey)).not.toHaveBeenCalled();
  });
});

describe('ApiKeysTab revoke flow', () => {
  it('opens a confirm dialog and revokes on confirm, refreshing the list', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.listAdminApiKeys)
      .mockResolvedValueOnce(fixtures.oneActive)
      .mockResolvedValueOnce({
        items: [
          {
            ...fixtures.oneActive.items[0]!,
            revoked_at: '2026-05-03T16:00:00Z',
          },
        ],
      });
    vi.mocked(api.revokeAdminApiKey).mockResolvedValueOnce(undefined);

    render(<ApiKeysTab />);

    await waitFor(() => expect(screen.getByText('laptop')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Revoke' }));

    // Confirm dialog opens with a destructive message naming the label.
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Revoke "laptop"\?/)).toBeInTheDocument();

    // Click the dialog's confirm button (scoped: the row also has a
    // "Revoke" button which is now obscured by the dialog).
    await user.click(within(dialog).getByRole('button', { name: 'Revoke' }));

    await waitFor(() =>
      expect(vi.mocked(api.revokeAdminApiKey)).toHaveBeenCalledWith(1),
    );
    // List refreshed, so the row now shows Revoked pill instead.
    await waitFor(() => expect(screen.getByText('Revoked')).toBeInTheDocument());
  });
});
