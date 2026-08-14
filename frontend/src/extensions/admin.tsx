import { lazy, Suspense, type ReactNode } from 'react';

const LazyAdminPanel = lazy(() => import('./admin/index'));

export function getAdminPageElement(isAdmin: boolean): ReactNode {
  if (!isAdmin) return null;
  return (
    <Suspense fallback={<div className="animate-pulse h-48 bg-panel rounded-[--radius-md]" />}>
      <LazyAdminPanel />
    </Suspense>
  );
}
