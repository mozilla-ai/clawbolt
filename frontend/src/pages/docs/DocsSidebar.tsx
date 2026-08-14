import { Link, useLocation } from 'react-router-dom';
import { docsNav } from './docsNav';

export default function DocsSidebar() {
  const { pathname } = useLocation();
  const current = pathname.replace(/^\/docs\/?/, '').replace(/\/$/, '');

  return (
    <nav aria-label="Docs navigation" className="text-sm">
      {docsNav.map((section) => (
        <div key={section.label} className="mb-6">
          <h2 className="font-display text-xs uppercase tracking-wider text-white/40 mb-2 px-3">
            {section.label}
          </h2>
          <ul className="space-y-0.5">
            {section.items.map((item) => {
              const active = current === item.slug;
              return (
                <li key={item.slug}>
                  <Link
                    to={`/docs/${item.slug}`}
                    className={
                      'block rounded-md px-3 py-1.5 transition-colors ' +
                      (active
                        ? 'bg-white/10 text-white'
                        : 'text-white/60 hover:text-white hover:bg-white/5')
                    }
                    aria-current={active ? 'page' : undefined}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
