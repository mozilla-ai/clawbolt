import { Outlet, Link } from 'react-router-dom';
import { LOGIN_PATH } from '@/extensions/routes';
import '@/styles/marketing.css';

export default function MarketingLayout() {
  return (
    <div className="min-h-dvh flex flex-col relative overflow-hidden" style={{ background: 'var(--brand-gradient-base)' }}>
      {/* Gradient brand overlay - covers entire page including header/footer */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background: 'radial-gradient(ellipse at 25% 20%, var(--brand-gradient-glow-1) 0%, var(--brand-gradient-glow-2) 12%, var(--brand-gradient-glow-3) 25%, var(--brand-gradient-warm-1) 45%, var(--brand-gradient-warm-2) 65%, var(--brand-gradient-base) 100%)',
          mixBlendMode: 'screen',
          opacity: 0.6,
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

      {/* Header */}
      <header className="relative z-10 sticky top-0">
        <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-3 text-3xl font-bold font-display text-white">
            <img src="/clawbolt-white.svg" alt="" className="w-14 h-14" />
            Clawbolt
          </Link>
          <Link
            to={LOGIN_PATH}
            className="px-4 py-1.5 text-sm font-medium rounded-lg bg-white/10 text-white hover:bg-white/20 transition-colors"
          >
            Log in
          </Link>
        </div>
      </header>

      {/* Content */}
      <main className="relative z-10 flex-1 flex flex-col">
        <Outlet />
      </main>

      {/* Footer */}
      <footer className="relative z-10 py-6 px-6 text-xs text-white/30">
        <div className="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-3">
          <a href="https://mozilla.ai" target="_blank" rel="noopener noreferrer" className="hover:text-white/60 transition-colors">Built by Mozilla.ai</a>
          <div className="flex items-center gap-4">
            <Link to="/terms" className="hover:text-white/60 transition-colors">
              Terms
            </Link>
            <Link to="/privacy" className="hover:text-white/60 transition-colors">
              Privacy
            </Link>
            <a
              href="/docs/"
              className="hover:text-white/60 transition-colors"
            >
              Docs
            </a>
            <a
              href="https://github.com/mozilla-ai/clawbolt"
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-white/60 transition-colors"
            >
              GitHub
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
