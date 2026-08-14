import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { MonitoringStatus } from '../admin-api';
import MonitoringTab from './monitoring';

// ---------------------------------------------------------------------------
// Admin Monitoring tab.
//
// The tab exists so an admin can see every dependency check at once, so the
// tests here care most about what could hide a problem: a failing per-user
// integration collapsed out of view, a dormant alert pipeline presented as
// coverage, and repairs leaving no trace.
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  healthy: {
    alerts: {
      enabled: true,
      pending_groups: 0,
      dedupe_minutes: 30,
      flush_interval_seconds: 60,
      max_emails_per_hour: 20,
    },
    health_monitor: {
      enabled: true,
      interval_seconds: 300,
      failure_threshold: 2,
      last_run_at: '2026-08-12T12:00:00Z',
      probes: {
        database: {
          label: 'PostgreSQL',
          status: 'up' as const,
          detail: '',
          consecutive_failures: 0,
          since: '2026-08-12T10:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: '',
          user_label: '',
          integration: '',
        },
        bluebubbles: {
          label: 'BlueBubbles bridge',
          status: 'down' as const,
          detail: 'the Mac is not signed in to iMessage',
          consecutive_failures: 3,
          since: '2026-08-12T11:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: '',
          user_label: '',
          integration: '',
        },
        home_depot_pricing: {
          label: 'Home Depot search',
          status: 'unknown' as const,
          detail: 'Probe did not answer within 45s and was abandoned',
          consecutive_failures: 1,
          since: '2026-08-12T11:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: '',
          user_label: '',
          integration: '',
        },
        lowes_pricing: {
          label: "Lowe's search",
          status: 'up' as const,
          detail: '',
          consecutive_failures: 0,
          since: '2026-08-12T11:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: '',
          user_label: '',
          integration: '',
        },
        // alice: one integration that worked and then stopped. Leads the list.
        'integration:quickbooks:user-1': {
          label: 'quickbooks for alice@example.com',
          status: 'down' as const,
          detail: 'token expired',
          consecutive_failures: 2,
          since: '2026-08-12T09:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: 'user-1',
          user_label: 'alice@example.com',
          integration: 'quickbooks',
        },
        'integration:calendar:user-1': {
          label: 'calendar for alice@example.com',
          status: 'up' as const,
          detail: '',
          consecutive_failures: 0,
          since: '2026-08-12T09:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: 'user-1',
          user_label: 'alice@example.com',
          integration: 'calendar',
        },
        // bob: everything he connected works.
        'integration:calendar:user-2': {
          label: 'calendar for bob@example.com',
          status: 'up' as const,
          detail: '',
          consecutive_failures: 0,
          since: '2026-08-12T09:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: 'user-2',
          user_label: 'bob@example.com',
          integration: 'calendar',
        },
        // carol: the sweep itself could not answer, so her integrations are
        // unknown rather than healthy.
        'integration_check:user-3': {
          label: 'Integration checks for carol@example.com',
          status: 'down' as const,
          detail: 'Check did not answer within 45s. This user’s status is unknown.',
          consecutive_failures: 2,
          since: '2026-08-12T09:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: false,
          user_id: 'user-3',
          user_label: 'carol@example.com',
          integration: '',
        },
        // Never connected: DOWN in the backend state machine, but not breakage.
        'integration:gmail:user-3': {
          label: 'gmail for carol@example.com',
          status: 'down' as const,
          detail: 'Gmail is not connected.',
          consecutive_failures: 4,
          since: '2026-08-12T09:00:00Z',
          last_checked: '2026-08-12T12:00:00Z',
          never_connected: true,
          user_id: 'user-3',
          user_label: 'carol@example.com',
          integration: 'gmail',
        },
      },
      history: [
        {
          at: '2026-08-12T11:30:00Z',
          key: 'bluebubbles',
          label: 'BlueBubbles inbound webhook',
          status: 'repaired' as const,
          detail: 'no webhooks are registered Re-registered the endpoint.',
        },
      ],
      run: {
        trigger: 'manual',
        running: false,
        started_at: '2026-08-12T11:59:50Z',
        finished_at: '2026-08-12T12:00:00Z',
        error: '',
        steps: [
          {
            key: 'database',
            label: 'PostgreSQL',
            status: 'ok' as const,
            detail: '',
            started_at: '2026-08-12T11:59:50Z',
            finished_at: '2026-08-12T11:59:50Z',
            elapsed_ms: 40,
          },
          {
            key: 'bluebubbles',
            label: 'BlueBubbles bridge',
            status: 'failed' as const,
            detail: 'the Mac is not signed in to iMessage',
            started_at: '2026-08-12T11:59:50Z',
            finished_at: '2026-08-12T12:00:00Z',
            elapsed_ms: 9800,
          },
        ],
      },
    },
    email: {
      configured: true,
      host: 'smtp.example.com',
      port: 587,
      timeout_seconds: 10,
      last_attempt_at: '2026-08-12T12:00:00Z',
      last_success_at: '2026-08-12T12:00:00Z',
      last_error: '',
      last_error_at: null,
    },
    recipient_configured: true,
    timestamp: '2026-08-12T12:00:00Z',
  },
  running: {
    alerts: {
      enabled: true,
      pending_groups: 0,
      dedupe_minutes: 30,
      flush_interval_seconds: 60,
      max_emails_per_hour: 20,
    },
    health_monitor: {
      enabled: true,
      interval_seconds: 300,
      failure_threshold: 2,
      last_run_at: '2026-08-12T12:00:00Z',
      probes: {},
      history: [],
      run: {
        trigger: 'manual',
        running: true,
        started_at: '2026-08-12T12:00:00Z',
        finished_at: null,
        error: '',
        steps: [
          {
            key: 'database',
            label: 'PostgreSQL',
            status: 'ok' as const,
            detail: '',
            started_at: '2026-08-12T12:00:00Z',
            finished_at: '2026-08-12T12:00:00Z',
            elapsed_ms: 40,
          },
          {
            key: 'bluebubbles',
            label: 'BlueBubbles bridge',
            status: 'running' as const,
            detail: '',
            started_at: '2026-08-12T12:00:00Z',
            finished_at: null,
            elapsed_ms: 2100,
          },
          {
            key: 'integrations',
            label: 'Per-user integrations',
            status: 'pending' as const,
            detail: '',
            started_at: null,
            finished_at: null,
            elapsed_ms: null,
          },
        ],
      },
    },
    email: {
      configured: true,
      host: 'smtp.example.com',
      port: 587,
      timeout_seconds: 10,
      last_attempt_at: '2026-08-12T12:00:00Z',
      last_success_at: '2026-08-12T12:00:00Z',
      last_error: '',
      last_error_at: null,
    },
    recipient_configured: true,
    timestamp: '2026-08-12T12:00:00Z',
  },
  brokenTransport: {
    alerts: {
      enabled: true,
      pending_groups: 0,
      dedupe_minutes: 30,
      flush_interval_seconds: 60,
      max_emails_per_hour: 20,
    },
    health_monitor: {
      enabled: true,
      interval_seconds: 300,
      failure_threshold: 2,
      last_run_at: '2026-08-12T12:00:00Z',
      probes: {},
      history: [],
      run: null,
    },
    email: {
      configured: true,
      host: 'smtp.example.com',
      port: 587,
      timeout_seconds: 10,
      last_attempt_at: '2026-08-12T12:00:00Z',
      last_success_at: null,
      last_error:
        'Timed out after 20s talking to smtp.example.com:587. Nothing answered, which usually means outbound SMTP is blocked.',
      last_error_at: '2026-08-12T12:00:00Z',
    },
    recipient_configured: true,
    timestamp: '2026-08-12T12:00:00Z',
  },
  dormant: {
    alerts: {
      enabled: false,
      pending_groups: 0,
      dedupe_minutes: 30,
      flush_interval_seconds: 60,
      max_emails_per_hour: 20,
    },
    health_monitor: {
      enabled: false,
      interval_seconds: 300,
      failure_threshold: 2,
      last_run_at: null,
      probes: {},
      history: [],
      run: null,
    },
    email: {
      configured: false,
      host: '',
      port: 587,
      timeout_seconds: 10,
      last_attempt_at: null,
      last_success_at: null,
      last_error: '',
      last_error_at: null,
    },
    recipient_configured: false,
    timestamp: '2026-08-12T12:00:00Z',
  },
}));

vi.mock('../admin-api', () => ({
  getMonitoringStatus: vi.fn(),
  runHealthProbes: vi.fn(),
  sendMonitoringTestAlert: vi.fn(),
  diagnoseEmailDelivery: vi.fn(),
}));

beforeEach(async () => {
  const api = await import('../admin-api');
  vi.mocked(api.getMonitoringStatus).mockReset();
  vi.mocked(api.runHealthProbes).mockReset();
  vi.mocked(api.sendMonitoringTestAlert).mockReset();
  vi.mocked(api.diagnoseEmailDelivery).mockReset();
});

describe('MonitoringTab', () => {
  it('shows every infrastructure probe with its status', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    // Scoped to the section: the same labels also appear as steps of the last
    // run, which is a different question about the same dependency.
    await waitFor(() => expect(screen.getByText('Infrastructure')).toBeInTheDocument());
    const infra = screen.getByText('Infrastructure').closest('section') as HTMLElement;
    expect(within(infra).getByText('PostgreSQL')).toBeInTheDocument();
    expect(within(infra).getByText('BlueBubbles bridge')).toBeInTheDocument();
    expect(within(infra).getByText("Lowe's search")).toBeInTheDocument();
    expect(within(infra).getByText('the Mac is not signed in to iMessage')).toBeInTheDocument();
  });

  it('counts real failures but not integrations nobody connected', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    // bluebubbles + alice's quickbooks + carol's failed sweep check, out of
    // nine probes. The never-connected gmail row is DOWN in the backend but is
    // not breakage: counting it here is what would put several hundred
    // "failures" on a healthy multi-tenant deploy.
    await waitFor(() => expect(screen.getByText('Failing checks')).toBeInTheDocument());
    const card = screen.getByText('Failing checks').closest('div');
    expect(card).toHaveTextContent('3');
    expect(card).toHaveTextContent('9 total');
  });

  it('names a below-threshold failure instead of calling it unchecked', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.healthy);

    render(<MonitoringTab />);

    // DOWN is withheld until the threshold so one timed-out request to a
    // residential host is not an outage, but "Not yet checked" beside a
    // timeout detail reads as a bug in the panel rather than a probe state.
    await waitFor(() => expect(screen.getByText('Failing 1 of 2')).toBeInTheDocument());
    expect(screen.queryByText('Not yet checked')).not.toBeInTheDocument();
  });

  it('gives each user one verdict line, worst first', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    // The question an admin has is "whose account is broken", which a flat
    // (user, integration) list cannot answer without scrolling and grouping in
    // their head. One row per user, named by email rather than by UUID.
    await waitFor(() => expect(screen.getByText('Per-user integrations')).toBeInTheDocument());
    const section = screen.getByText('Per-user integrations').closest('section') as HTMLElement;

    const headings = within(section)
      .getAllByRole('button', { expanded: true })
      .map(b => b.getAttribute('aria-label'));
    // alice has real breakage, carol only an unanswered check: breakage leads.
    expect(headings[0]).toMatch(/^alice@example\.com: 1 not working/);
    expect(headings[1]).toMatch(/^carol@example\.com: Status unknown/);

    // The breakdown is on the heading itself, so nothing has to be expanded to
    // see how much of the account is affected.
    expect(headings[0]).toContain('1 working · 1 not working');
  });

  it('separates a user whose checks could not run from a healthy one', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    // "We could not check" must never render as "everything is fine": that is
    // the reading that hides a token which lapsed while the sweep was failing.
    await waitFor(() => expect(screen.getByText('Status unknown')).toBeInTheDocument());
    expect(
      screen.getByText(/1 of 3 users has an integration that stopped working/),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 has an unknown status/)).toBeInTheDocument();
  });

  it('collapses healthy users out of the default view but keeps them reachable', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument());
    // bob has nothing wrong, so he is not in the problems-only default.
    expect(screen.queryByText('bob@example.com')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Show all 3 users/ }));

    expect(screen.getByText('bob@example.com')).toBeInTheDocument();
    expect(screen.getByText('All 1 working')).toBeInTheDocument();
  });

  it('keeps never-connected integrations out of a user verdict', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    // carol's gmail is DOWN in the backend but nobody ever connected it, so it
    // must not read as breakage on her row. Her group opens by default because
    // the sweep check failed, so the row is visible without a click.
    await waitFor(() =>
      expect(screen.getByRole('group', { name: 'carol@example.com' })).toBeInTheDocument(),
    );
    const carol = screen.getByRole('group', { name: 'carol@example.com' });
    expect(within(carol).getByText('Not connected')).toBeInTheDocument();
    // Rows drop the "<integration> for <user>" label the group heading already
    // carries.
    expect(within(carol).getByText('gmail')).toBeInTheDocument();
    expect(within(carol).getByText('Integration checks')).toBeInTheDocument();
  });

  it('does not present a stale failed integration as current when a sweep cannot run', async () => {
    const api = await import('../admin-api');
    const status: MonitoringStatus = structuredClone(fixtures.healthy);
    status.health_monitor.probes['integration:quickbooks:user-3'] = {
      label: 'quickbooks for carol@example.com',
      status: 'down',
      detail: 'token expired during the previous sweep',
      consecutive_failures: 2,
      since: '2026-08-12T09:00:00Z',
      last_checked: '2026-08-12T12:00:00Z',
      never_connected: false,
      user_id: 'user-3',
      user_label: 'carol@example.com',
      integration: 'quickbooks',
    };
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(status);

    render(<MonitoringTab />);

    // The failed sweep makes the old DOWN state stale. The verdict must not
    // claim the integration is currently broken until the user can be checked.
    const carol = await screen.findByRole('group', { name: 'carol@example.com' });
    expect(within(carol).getByText('Status unknown')).toBeInTheDocument();
    expect(within(carol).getByText(/could not be checked.*last known/)).toBeInTheDocument();
  });

  it('renders self-repairs in the activity log', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.healthy);

    render(<MonitoringTab />);

    // A repair fixes the problem before any DOWN alert fires, so this log is
    // the only place it is visible in the UI.
    await waitFor(() =>
      expect(screen.getByText('BlueBubbles inbound webhook')).toBeInTheDocument(),
    );
    expect(screen.getByText('Repaired')).toBeInTheDocument();
  });

  it('warns when alerting is dormant instead of implying coverage', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValueOnce(fixtures.dormant);

    render(<MonitoringTab />);

    await waitFor(() =>
      expect(screen.getByText(/Email alerting is not fully active/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/set ALERT_EMAIL or ADMIN_EMAIL/)).toBeInTheDocument();
    expect(screen.getByText('never')).toBeInTheDocument();
  });

  it('re-reads status after starting a run on demand', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.healthy);
    vi.mocked(api.runHealthProbes).mockResolvedValueOnce({
      started: true,
      detail: 'Probe run started.',
      run: null,
    });

    render(<MonitoringTab />);
    await waitFor(() => expect(screen.getByText('Infrastructure')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Run safe probes now' }));

    await waitFor(() => expect(api.getMonitoringStatus).toHaveBeenCalledTimes(2));
  });

  it('shows each check in the last run, with how long it took', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.healthy);

    render(<MonitoringTab />);

    // A run that only reports "done" cannot tell an operator which dependency
    // was slow, which is the whole question when a run takes minutes.
    await waitFor(() => expect(screen.getByText('Last run')).toBeInTheDocument());
    expect(screen.getByText('Passed')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('9.8s')).toBeInTheDocument();
  });

  it('reports a run in flight step by step and keeps polling', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus)
      .mockResolvedValueOnce(fixtures.running)
      .mockResolvedValue(fixtures.healthy);

    render(<MonitoringTab />);

    // In-flight steps are visible while they are still being waited on, and
    // queued ones are listed rather than hidden.
    await waitFor(() => expect(screen.getByText('Checking…')).toBeInTheDocument());
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(screen.getByText(/1 of 3 checks done/)).toBeInTheDocument();
    // The button reflects the server's run, not a local flag, so a reload
    // mid-run cannot show an idle button while probes are running.
    expect(screen.getByRole('button', { name: 'Running…' })).toBeDisabled();

    // Polls on its own until the run finishes.
    await waitFor(() => expect(screen.getByText('Last run')).toBeInTheDocument(), {
      timeout: 4000,
    });
  });

  it('keeps the test alert inside the email delivery section', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.healthy);

    render(<MonitoringTab />);
    await waitFor(() => expect(screen.getByText('Email delivery')).toBeInTheDocument());

    // Sending a test is a rare, deliberate act. Given equal billing next to
    // "Run probes now" it invited clicking as the first move on any question.
    const section = screen.getByText('Email delivery').closest('section');
    expect(section).not.toBeNull();
    expect(
      within(section as HTMLElement).getByRole('button', { name: 'Send test alert' }),
    ).toBeInTheDocument();
    expect(within(section as HTMLElement).getByText(/smtp.example.com:587/)).toBeInTheDocument();
  });

  it('reports the test-alert outcome verbatim', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.healthy);
    vi.mocked(api.sendMonitoringTestAlert).mockResolvedValueOnce({
      sent: false,
      recipient_configured: true,
      detail: 'Timed out after 20s talking to smtp.example.com:587.',
      email: fixtures.healthy.email,
    });

    render(<MonitoringTab />);
    await waitFor(() => expect(screen.getByText('Email delivery')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Send test alert' }));

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Timed out after 20s/));
  });

  it('surfaces the last transport failure without being asked', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.brokenTransport);

    render(<MonitoringTab />);

    // A dead transport hides every failure it was supposed to report, so it
    // cannot wait for somebody to click a test.
    await waitFor(() =>
      expect(screen.getByText(/outbound SMTP is blocked/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/No successful send recorded/)).toBeInTheDocument();
  });

  it('renders the delivery diagnostic with per-port reachability', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockResolvedValue(fixtures.brokenTransport);
    vi.mocked(api.diagnoseEmailDelivery).mockResolvedValueOnce({
      configured: true,
      host: 'smtp.example.com',
      port: 587,
      ports: [
        { port: 587, reachable: false, detail: 'no response within 5s' },
        { port: 2587, reachable: true, detail: 'connected in 4ms' },
      ],
      handshake_ok: false,
      handshake_detail: 'Timed out after 20s',
      conclusion: 'Port 587 is blocked from this container but 2587 is reachable.',
    });

    render(<MonitoringTab />);
    await waitFor(() => expect(screen.getByText('Email delivery')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Diagnose delivery' }));

    // Separates "the network will not let us out" from "the mail server said
    // no", which are different people's problems.
    await waitFor(() =>
      expect(screen.getByText(/Port 587 is blocked/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/587 \(configured\)/)).toBeInTheDocument();
    expect(screen.getByText('2587')).toBeInTheDocument();
  });

  it('surfaces a load failure rather than an empty page', async () => {
    const api = await import('../admin-api');
    vi.mocked(api.getMonitoringStatus).mockRejectedValueOnce(new Error('403 Forbidden'));

    render(<MonitoringTab />);

    await waitFor(() => expect(screen.getByText('403 Forbidden')).toBeInTheDocument());
  });
});
