import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { BetaBadge } from '@/extensions/header-badge';
import '@/styles/marketing.css';

/**
 * Read auth_error and rejected_email from the URL hash fragment and clean the URL.
 * The OAuth callback redirects here with #auth_error=<encoded>&rejected_email=<encoded>
 * when sign-in fails.
 */
function consumeAuthHash(): { error: string | null; rejectedEmail: string | null } {
  const hash = window.location.hash;
  if (!hash || hash.length < 2) return { error: null, rejectedEmail: null };
  const params = new URLSearchParams(hash.slice(1));
  const error = params.get('auth_error');
  const rejectedEmail = params.get('rejected_email');
  if (error || rejectedEmail) {
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  return { error: error || null, rejectedEmail: rejectedEmail || null };
}

export default function LoginPage() {
  const navigate = useNavigate();
  const { authState } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [waitlistEmail, setWaitlistEmail] = useState('');
  const [waitlistName, setWaitlistName] = useState('');
  const [waitlistUseCase, setWaitlistUseCase] = useState('');
  const [waitlistLoading, setWaitlistLoading] = useState(false);
  const [waitlistDone, setWaitlistDone] = useState(false);
  const [waitlistError, setWaitlistError] = useState<string | null>(null);

  // Already authenticated: skip the login form entirely
  useEffect(() => {
    if (authState === 'ready') navigate('/app', { replace: true });
  }, [authState, navigate]);

  useEffect(() => {
    const { error: authError, rejectedEmail } = consumeAuthHash();
    if (authError) setError(authError);
    if (rejectedEmail) setWaitlistEmail(rejectedEmail);
  }, []);

  const handleGoogleOAuth = () => {
    // Redirect to the backend, which handles the full OAuth flow server-side
    window.location.href = '/api/auth/oauth/google';
  };

  const isNotApproved = error?.includes('not been approved') ?? false;

  const handleWaitlist = async (e: React.FormEvent) => {
    e.preventDefault();
    const target = waitlistEmail.trim();
    const targetName = waitlistName.trim();
    if (!target || !targetName) return;
    setWaitlistLoading(true);
    setWaitlistError(null);
    try {
      const res = await fetch('/api/waitlist/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: target,
          name: targetName,
          use_case: waitlistUseCase.trim(),
          source: 'login',
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || 'Something went wrong. Try again.');
      }
      setWaitlistDone(true);
    } catch (err) {
      setWaitlistError((err as Error).message);
    } finally {
      setWaitlistLoading(false);
    }
  };

  return (
    <div className="relative flex items-center justify-center min-h-dvh overflow-hidden" style={{ background: 'var(--brand-gradient-base-deep)' }}>
      {/* Gradient brand overlay */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background: 'radial-gradient(circle at 50% 40%, var(--brand-gradient-glow-1) 0%, var(--brand-gradient-glow-2) 15%, var(--brand-gradient-glow-3) 30%, var(--brand-gradient-warm-1) 50%, var(--brand-gradient-warm-3) 70%, var(--brand-gradient-base-deep) 100%)',
          mixBlendMode: 'screen',
          opacity: 0.55,
        }}
      />
      {/* Subtle grain texture */}
      <div
        className="absolute inset-0 pointer-events-none opacity-[0.05]"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
          backgroundSize: '128px 128px',
        }}
      />
      {/* Frosted glass card */}
      <div className="relative z-10 w-full max-w-sm p-8 rounded-2xl bg-white/10 backdrop-blur-xl border border-white/15 shadow-xl supports-[backdrop-filter]:bg-white/10">
        <div className="text-center mb-8">
          <Link to="/" className="inline-flex flex-col items-center gap-3 group">
            <img src="/clawbolt-white.svg" alt="" className="w-16 h-16" />
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-bold font-display text-white">Clawbolt</h1>
              <BetaBadge variant="dark" />
            </div>
          </Link>
          <p className="text-sm text-white/60 mt-1">AI assistant for the trades</p>
        </div>

        {error && !isNotApproved && (
          <div className="mb-4 p-3 rounded-lg bg-red-500/20 border border-red-400/30 text-red-200 text-sm">
            {error}
          </div>
        )}

        {isNotApproved && !waitlistDone && (
          <div className="mb-4">
            <div className="p-3 rounded-lg bg-amber-500/20 border border-amber-400/30 text-amber-200 text-sm mb-3">
              {error}
            </div>
            <form onSubmit={handleWaitlist}>
              <input
                type="text"
                className="w-full px-3 py-2.5 text-sm bg-white/10 border border-white/15 rounded-lg text-white placeholder:text-white/40 focus:outline-none focus:ring-2 focus:ring-primary/40 mb-2"
                placeholder="Your name"
                aria-label="Your name"
                value={waitlistName}
                onChange={(e) => setWaitlistName(e.target.value)}
                maxLength={120}
                required
              />
              <textarea
                className="w-full px-3 py-2.5 text-sm bg-white/10 border border-white/15 rounded-lg text-white placeholder:text-white/40 focus:outline-none focus:ring-2 focus:ring-primary/40 mb-2 resize-none"
                placeholder="What trade are you in, and what would you use Clawbolt for? (optional)"
                aria-label="What trade are you in, and what would you use Clawbolt for? (optional)"
                value={waitlistUseCase}
                onChange={(e) => setWaitlistUseCase(e.target.value)}
                rows={3}
                maxLength={2000}
              />
              <input
                type="email"
                className="w-full px-3 py-2.5 text-sm bg-white/10 border border-white/15 rounded-lg text-white placeholder:text-white/40 focus:outline-none focus:ring-2 focus:ring-primary/40 mb-2"
                placeholder="Email address"
                aria-label="Email address"
                value={waitlistEmail}
                onChange={(e) => setWaitlistEmail(e.target.value)}
                required
              />
              <button
                type="submit"
                className="w-full px-4 py-2.5 rounded-lg bg-primary text-primary-foreground font-medium hover:bg-primary-hover transition-colors disabled:opacity-50"
                disabled={waitlistLoading || !waitlistEmail.trim() || !waitlistName.trim()}
              >
                {waitlistLoading ? 'Joining...' : 'Request Early Access'}
              </button>
              {waitlistError && (
                <p className="mt-2 text-xs text-red-300">{waitlistError}</p>
              )}
            </form>
          </div>
        )}

        {waitlistDone && (
          <div className="mb-4 p-3 rounded-lg bg-white/10 border border-white/15 text-white text-sm">
            You're on the list. We'll reach out when we're ready for you to give it a try.
          </div>
        )}

        {/* Google OAuth */}
        <button
          className="w-full flex items-center justify-center gap-3 px-4 py-2.5 rounded-lg bg-white text-neutral-800 font-medium hover:bg-neutral-100 transition-colors"
          onClick={handleGoogleOAuth}
        >
          <svg className="w-5 h-5" viewBox="0 0 24 24">
            <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4" />
            <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
            <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
            <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
          </svg>
          Sign in with Google
        </button>

        <div className="mt-8 text-center flex items-center justify-center gap-4">
          <Link to="/terms" className="text-xs text-white/40 hover:text-white/70 transition-colors">
            Terms of Service
          </Link>
          <span className="text-xs text-white/20">|</span>
          <Link to="/privacy" className="text-xs text-white/40 hover:text-white/70 transition-colors">
            Privacy Notice
          </Link>
        </div>
      </div>
    </div>
  );
}
