import { useEffect, useMemo, useState } from 'react';
import type { ComponentProps, ReactNode } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { docsNav, findNavItem } from './docsNav';
import 'highlight.js/styles/github-dark.css';

type Loader = () => Promise<string>;

const markdownLoaders = import.meta.glob<string>(
  '/src/docs-content/**/*.md',
  { query: '?raw', import: 'default' },
) as Record<string, Loader>;

function resolveSlug(slug: string): Loader | null {
  const candidates = [
    `/src/docs-content/${slug}.md`,
    `/src/docs-content/${slug}/index.md`,
  ];
  for (const path of candidates) {
    if (markdownLoaders[path]) return markdownLoaders[path];
  }
  return null;
}

function flattenNav() {
  return docsNav.flatMap((s) => s.items);
}

function NotFound() {
  return (
    <article className="text-white/85 leading-relaxed">
      <h1 className="font-display text-3xl md:text-4xl font-semibold text-white mt-2 mb-5 leading-tight">
        Page not found
      </h1>
      <p>
        That page doesn't exist. Head back to the{' '}
        <Link to="/docs/guide" className="text-amber-300 underline underline-offset-2 hover:text-amber-200">
          User Guide
        </Link>
        .
      </p>
    </article>
  );
}

export default function DocsPage() {
  const params = useParams<{ '*': string }>();
  const navigate = useNavigate();
  const slug = (params['*'] ?? 'guide').replace(/\/$/, '');
  const [content, setContent] = useState<string | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'missing'>(
    'loading',
  );

  useEffect(() => {
    const loader = resolveSlug(slug);
    if (!loader) {
      setStatus('missing');
      return;
    }
    setStatus('loading');
    let cancelled = false;
    loader().then((text) => {
      if (!cancelled) {
        setContent(text);
        setStatus('ready');
      }
    });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [slug]);

  const currentItem = findNavItem(slug);
  const flat = flattenNav();
  const currentIndex = currentItem ? flat.findIndex((n) => n.slug === slug) : -1;
  const prev = currentIndex > 0 ? flat[currentIndex - 1] : null;
  const next =
    currentIndex >= 0 && currentIndex < flat.length - 1
      ? flat[currentIndex + 1]
      : null;

  const components = useMemo(
    () => ({
      h1: ({ children }: { children?: ReactNode }) => (
        <h1 className="font-display text-3xl md:text-4xl font-semibold text-white mt-2 mb-6 leading-tight tracking-tight">
          {children}
        </h1>
      ),
      h2: ({ children }: { children?: ReactNode }) => (
        <h2 className="font-display text-2xl font-semibold text-white mt-12 mb-3 leading-tight">
          {children}
        </h2>
      ),
      h3: ({ children }: { children?: ReactNode }) => (
        <h3 className="font-display text-lg font-semibold text-white mt-8 mb-2">
          {children}
        </h3>
      ),
      p: ({ children }: { children?: ReactNode }) => (
        <p className="text-white/85 leading-relaxed my-4">{children}</p>
      ),
      ul: ({ children }: { children?: ReactNode }) => (
        <ul className="text-white/85 leading-relaxed my-4 pl-6 list-disc marker:text-white/40 space-y-1">
          {children}
        </ul>
      ),
      ol: ({ children }: { children?: ReactNode }) => (
        <ol className="text-white/85 leading-relaxed my-4 pl-6 list-decimal marker:text-white/40 space-y-1">
          {children}
        </ol>
      ),
      li: ({ children }: { children?: ReactNode }) => (
        <li className="pl-1">{children}</li>
      ),
      strong: ({ children }: { children?: ReactNode }) => (
        <strong className="text-white font-semibold">{children}</strong>
      ),
      em: ({ children }: { children?: ReactNode }) => (
        <em className="italic">{children}</em>
      ),
      hr: () => <hr className="my-10 border-white/10" />,
      blockquote: ({ children }: { children?: ReactNode }) => (
        <blockquote className="my-5 border-l-[3px] border-amber-400/70 bg-amber-500/5 py-2 pl-4 pr-3 text-white/85 rounded-r">
          {children}
        </blockquote>
      ),
      code: ({ className, children, ...rest }: ComponentProps<'code'>) => {
        const isBlock =
          typeof className === 'string' && className.startsWith('language-');
        if (isBlock) {
          return (
            <code className={className} {...rest}>
              {children}
            </code>
          );
        }
        return (
          <code className="rounded bg-white/10 px-1.5 py-0.5 text-[0.9em] font-mono text-amber-200">
            {children}
          </code>
        );
      },
      pre: ({ children }: { children?: ReactNode }) => (
        <pre className="my-5 overflow-x-auto rounded-lg border border-white/5 bg-black/40 p-4 font-mono text-[0.875rem] leading-relaxed">
          {children}
        </pre>
      ),
      table: ({ children }: { children?: ReactNode }) => (
        <div className="my-5 overflow-x-auto">
          <table className="w-full text-sm border-collapse">{children}</table>
        </div>
      ),
      thead: ({ children }: { children?: ReactNode }) => (
        <thead className="border-b border-white/20">{children}</thead>
      ),
      th: ({ children }: { children?: ReactNode }) => (
        <th className="py-2 px-3 text-left font-semibold text-white">
          {children}
        </th>
      ),
      td: ({ children }: { children?: ReactNode }) => (
        <td className="py-2 px-3 border-b border-white/10 text-white/85 align-top">
          {children}
        </td>
      ),
      a: ({ href, children }: { href?: string; children?: ReactNode }) => {
        const internal =
          !!href && (href.startsWith('/') || href.startsWith('#'));
        if (internal) {
          return (
            <Link
              to={href!}
              className="text-amber-300 underline underline-offset-2 decoration-amber-400/50 hover:text-amber-200 hover:decoration-amber-300"
            >
              {children}
            </Link>
          );
        }
        return (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-amber-300 underline underline-offset-2 decoration-amber-400/50 hover:text-amber-200 hover:decoration-amber-300"
          >
            {children}
          </a>
        );
      },
    }),
    [],
  );
  // navigate is unused now that Link handles internal nav on its own;
  // keep it around for potential future interception without re-render churn.
  void navigate;

  if (status === 'missing') return <NotFound />;
  if (status === 'loading' || content === null) {
    return <div className="text-white/40 text-sm">Loading…</div>;
  }

  return (
    <article>
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={components}
      >
        {content}
      </Markdown>
      {(prev || next) && (
        <nav
          aria-label="Page navigation"
          className="mt-16 pt-6 border-t border-white/10 flex justify-between gap-4 text-sm"
        >
          <div className="flex-1">
            {prev && (
              <Link
                to={`/docs/${prev.slug}`}
                className="block rounded-lg border border-white/10 px-4 py-3 hover:border-white/30 hover:bg-white/[0.03] transition-colors"
              >
                <div className="text-xs text-white/40 mb-0.5">Previous</div>
                <div className="text-white font-medium">{prev.label}</div>
              </Link>
            )}
          </div>
          <div className="flex-1 text-right">
            {next && (
              <Link
                to={`/docs/${next.slug}`}
                className="block rounded-lg border border-white/10 px-4 py-3 hover:border-white/30 hover:bg-white/[0.03] transition-colors"
              >
                <div className="text-xs text-white/40 mb-0.5">Next</div>
                <div className="text-white font-medium">{next.label}</div>
              </Link>
            )}
          </div>
        </nav>
      )}
    </article>
  );
}
