import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
// These routes sit outside MarketingLayout, so the sheet carrying
// ``.legal-prose`` has to come in here rather than from a parent.
import '@/styles/marketing.css';

/**
 * Renders legal prose the deployment supplies as a static asset.
 *
 * The text of a terms-of-service or privacy policy is a contract between a
 * specific operator and their users, so it does not belong in a repo anyone
 * can fork and deploy: a fork that inherited it would be presenting someone
 * else's terms as its own. This repo owns the route, the chrome, and the
 * typography; the operator drops the prose at ``public/legal/<name>.html``
 * and it appears here.
 *
 * A deployment that supplies nothing gets the placeholder rather than a
 * blank page or a crash, which is the honest answer for a self-host that
 * never agreed to any terms.
 */
export default function LegalPage({
  src,
  title,
}: {
  /** Path under ``public/``, e.g. ``/legal/terms.html``. */
  src: string;
  /** Shown while loading and when nothing is supplied. */
  title: string;
}) {
  const navigate = useNavigate();
  const [html, setHtml] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(src)
      .then((res) => {
        // A dev server and some static hosts answer an unknown path with
        // index.html rather than a 404, so the content type is what
        // actually distinguishes "supplied" from "absent" here.
        const isHtml = res.headers.get('content-type')?.includes('text/html');
        if (!res.ok || !isHtml) throw new Error('not supplied');
        return res.text();
      })
      .then((text) => {
        if (cancelled) return;
        if (text.includes('<div id="root">')) throw new Error('SPA fallback');
        setHtml(text);
      })
      .catch(() => {
        if (!cancelled) setMissing(true);
      });
    return () => {
      cancelled = true;
    };
  }, [src]);

  return (
    <div className="w-full bg-background min-h-dvh">
      <div className="max-w-4xl mx-auto px-6 py-10">
        <div className="mb-6">
          <button
            onClick={() => navigate(-1)}
            className="text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            &larr; Back
          </button>
        </div>

        {missing ? (
          <>
            <h1 className="text-3xl font-bold font-display mb-6 text-foreground">{title}</h1>
            <p className="text-muted-foreground">
              This deployment has not published {title.toLowerCase()}. An operator adds them by
              placing the prose at{' '}
              <code className="text-foreground">public{src}</code>.
            </p>
          </>
        ) : html === null ? (
          <h1 className="text-3xl font-bold font-display mb-6 text-foreground">{title}</h1>
        ) : (
          // Same-origin static asset the operator controls and ships in
          // their own image, so this is their markup rendering in their
          // own page, not third-party input.
          <div
            className="legal-prose text-foreground leading-relaxed"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        )}
      </div>
      <div className="h-16" />
    </div>
  );
}
