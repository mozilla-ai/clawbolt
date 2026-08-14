import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { LOGIN_PATH } from '@/extensions/routes';

export default function HomePage() {
  const navigate = useNavigate();
  const { authState } = useAuth();
  const isLoggedIn = authState === 'ready';

  const [waitlistEmail, setWaitlistEmail] = useState('');
  const [waitlistName, setWaitlistName] = useState('');
  const [waitlistUseCase, setWaitlistUseCase] = useState('');
  const [waitlistLoading, setWaitlistLoading] = useState(false);
  const [waitlistDone, setWaitlistDone] = useState(false);
  const [waitlistError, setWaitlistError] = useState<string | null>(null);

  // Already authenticated: go straight to the console
  useEffect(() => {
    if (isLoggedIn) navigate('/app', { replace: true });
  }, [isLoggedIn, navigate]);

  const handleWaitlist = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!waitlistEmail.trim() || !waitlistName.trim()) return;
    setWaitlistLoading(true);
    setWaitlistError(null);
    try {
      const res = await fetch('/api/waitlist/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: waitlistEmail.trim(),
          name: waitlistName.trim(),
          use_case: waitlistUseCase.trim(),
          source: 'homepage',
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
    <div className="flex-1 flex flex-col items-center justify-center px-6">
      <div className="text-center max-w-3xl mx-auto">
        <img src="/clawbolt-white.svg" alt="" className="w-20 h-20 mx-auto mb-6" />

        <p className="text-xs font-medium tracking-[0.3em] uppercase text-white/50 mb-6">
          AI for the trades
        </p>

        <h1 className="text-[2rem] sm:text-5xl md:text-6xl font-bold font-display text-white tracking-tight leading-[1.1] mb-8">
          Your jobs, organized.
          <br />
          Your time, protected.
        </h1>

        <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-center gap-3 sm:gap-4 mb-8">
          <Link
            to={isLoggedIn ? '/app' : LOGIN_PATH}
            className="inline-flex justify-center px-8 py-3.5 rounded-lg bg-primary text-primary-foreground font-medium hover:bg-primary-hover transition-colors text-base"
          >
            {isLoggedIn ? 'Go to Clawbolt' : 'Get Started'}
          </Link>
          <Link
            to="/terms"
            className="inline-flex justify-center px-8 py-3.5 rounded-lg border border-white/30 text-white font-medium hover:bg-white/10 transition-colors text-base"
          >
            Terms of Use
          </Link>
        </div>

        <p className="text-lg text-white/60 max-w-lg mx-auto leading-relaxed mb-8">
          Clawbolt handles estimates, scheduling, and client follow-ups
          so you can focus on the work that matters.
        </p>

        {/* Waitlist form */}
        <div className="max-w-md mx-auto">
          {waitlistDone ? (
            <div className="p-4 rounded-lg bg-white/10 border border-white/15 text-white text-sm">
              You're on the list. We'll reach out when we're ready for you to give it a try.
            </div>
          ) : (
            <form onSubmit={handleWaitlist}>
              <p className="text-sm text-white/50 mb-3">Interested? Request early access.</p>
              <div className="flex flex-col gap-2">
                <input
                  type="text"
                  className="w-full px-3 py-2.5 text-sm bg-white/10 border border-white/15 rounded-lg text-white placeholder:text-white/40 focus:outline-none focus:ring-2 focus:ring-primary/40"
                  placeholder="Your name"
                  aria-label="Your name"
                  value={waitlistName}
                  onChange={(e) => setWaitlistName(e.target.value)}
                  maxLength={120}
                  required
                />
                <textarea
                  className="w-full px-3 py-2.5 text-sm bg-white/10 border border-white/15 rounded-lg text-white placeholder:text-white/40 focus:outline-none focus:ring-2 focus:ring-primary/40 resize-none"
                  placeholder="What trade are you in, and what would you use Clawbolt for? (optional)"
                  aria-label="What trade are you in, and what would you use Clawbolt for? (optional)"
                  value={waitlistUseCase}
                  onChange={(e) => setWaitlistUseCase(e.target.value)}
                  rows={3}
                  maxLength={2000}
                />
                <div className="flex flex-col sm:flex-row gap-2">
                  <input
                    type="email"
                    className="flex-1 min-w-0 px-3 py-2.5 text-sm bg-white/10 border border-white/15 rounded-lg text-white placeholder:text-white/40 focus:outline-none focus:ring-2 focus:ring-primary/40"
                    placeholder="Email address"
                    aria-label="Email address"
                    value={waitlistEmail}
                    onChange={(e) => setWaitlistEmail(e.target.value)}
                    required
                  />
                  <button
                    type="submit"
                    className="px-5 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary-hover transition-colors disabled:opacity-50 whitespace-nowrap"
                    disabled={waitlistLoading || !waitlistEmail.trim() || !waitlistName.trim()}
                  >
                    {waitlistLoading ? 'Joining...' : 'Request Early Access'}
                  </button>
                </div>
              </div>
              {waitlistError && (
                <p className="mt-2 text-xs text-red-300">{waitlistError}</p>
              )}
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
