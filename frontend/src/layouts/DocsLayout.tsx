import { Outlet, Link } from 'react-router-dom';
import DocsSidebar from '@/pages/docs/DocsSidebar';
import { LOGIN_PATH } from '@/extensions/routes';
import { BetaBadge } from '@/extensions/header-badge';
import '@/styles/marketing.css';

export default function DocsLayout() {
  return (
    <div
      className="dark min-h-dvh flex flex-col relative overflow-hidden"
      style={{ background: 'var(--brand-gradient-base)' }}
    >
      {/* Amber radial gradient, softer than marketing so docs stay readable */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            'radial-gradient(ellipse at 15% -5%, var(--brand-gradient-glow-1) 0%, var(--brand-gradient-glow-3) 22%, var(--brand-gradient-warm-1) 45%, var(--brand-gradient-warm-2) 65%, var(--brand-gradient-base) 100%)',
          mixBlendMode: 'screen',
          opacity: 0.35,
        }}
      />
      {/* Subtle grain */}
      <div
        className="absolute inset-0 pointer-events-none opacity-[0.04]"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
          backgroundSize: '128px 128px',
        }}
      />

      <header className="relative z-10 sticky top-0 border-b border-white/10 backdrop-blur-md bg-[var(--brand-gradient-base)]/75">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center justify-between">
          <Link
            to="/"
            className="flex items-center gap-3 text-2xl font-bold font-display text-white"
          >
            <img src="/clawbolt-white.svg" alt="" className="w-10 h-10" />
            Clawbolt
            <BetaBadge variant="dark" />
          </Link>
          <div className="flex items-center gap-5 text-sm">
            <Link to="/docs/guide" className="text-white/80 hover:text-white transition-colors">
              Docs
            </Link>
            <Link
              to={LOGIN_PATH}
              className="px-4 py-1.5 font-medium rounded-lg bg-white/10 text-white hover:bg-white/20 transition-colors"
            >
              Log in
            </Link>
          </div>
        </div>
      </header>

      <div className="relative z-10 flex-1 max-w-7xl w-full mx-auto px-6 py-10 grid grid-cols-1 md:grid-cols-[15rem_minmax(0,1fr)] gap-10">
        <aside className="hidden md:block md:sticky md:top-20 md:self-start md:max-h-[calc(100dvh-6rem)] md:overflow-y-auto">
          <DocsSidebar />
        </aside>
        <main className="min-w-0 max-w-3xl">
          <Outlet />
        </main>
      </div>

      <footer className="relative z-10 py-6 px-6 text-xs text-white/30 border-t border-white/5">
        <div className="max-w-7xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-3">
          <a
            href="https://mozilla.ai"
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-white/60 transition-colors"
          >
            Built by Mozilla.ai
          </a>
          <div className="flex items-center gap-4">
            <Link to="/terms" className="hover:text-white/60 transition-colors">
              Terms
            </Link>
            <Link to="/privacy" className="hover:text-white/60 transition-colors">
              Privacy
            </Link>
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
