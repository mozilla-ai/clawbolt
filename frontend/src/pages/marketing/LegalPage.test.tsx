import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '@/test/test-utils';
import LegalPage from './LegalPage';

// ``text`` is Omit-ed rather than intersected: Response declares it as
// ``() => Promise<string>``, and intersecting a plain string with that
// yields a type nothing can satisfy.
function mockFetch({ text = '', ...rest }: Omit<Partial<Response>, 'text'> & { text?: string }) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ 'content-type': 'text/html' }),
      ...rest,
      text: async () => text,
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('LegalPage', () => {
  it('renders the prose the deployment supplied', async () => {
    mockFetch({ text: '<h1>Terms of Service</h1><p>Be excellent to each other.</p>' });

    renderWithRouter(<LegalPage src="/legal/terms.html" title="Terms of Service" />);

    await waitFor(() => {
      expect(screen.getByText('Be excellent to each other.')).toBeInTheDocument();
    });
  });

  it('explains itself when the deployment supplied nothing', async () => {
    mockFetch({ ok: false, status: 404 });

    renderWithRouter(<LegalPage src="/legal/terms.html" title="Terms of Service" />);

    await waitFor(() => {
      expect(screen.getByText(/has not published/i)).toBeInTheDocument();
    });
    // The operator needs the path, not just the fact that it is missing.
    expect(screen.getByText('public/legal/terms.html')).toBeInTheDocument();
  });

  it('treats an SPA fallback as nothing supplied', async () => {
    // A dev server and many static hosts answer an unknown path with
    // index.html and a 200, so status alone cannot distinguish "supplied"
    // from "absent". Without this guard the page would render the app's
    // own HTML shell inside itself.
    mockFetch({ text: '<!doctype html><html><body><div id="root"></div></body></html>' });

    renderWithRouter(<LegalPage src="/legal/privacy.html" title="Privacy Policy" />);

    await waitFor(() => {
      expect(screen.getByText(/has not published/i)).toBeInTheDocument();
    });
  });

  it('treats a non-HTML response as nothing supplied', async () => {
    mockFetch({ headers: new Headers({ 'content-type': 'application/json' }), text: '{}' });

    renderWithRouter(<LegalPage src="/legal/terms.html" title="Terms of Service" />);

    await waitFor(() => {
      expect(screen.getByText(/has not published/i)).toBeInTheDocument();
    });
  });
});
