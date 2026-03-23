import { lazy, Suspense, type ReactNode } from 'react';

const LazyAdminPanel = lazy(() => import('./admin/index'));

export function getAdminPageElement(_isAdmin: boolean): ReactNode {
  return (
    <Suspense fallback={<div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />}>
      <LazyAdminPanel />
    </Suspense>
  );
}
