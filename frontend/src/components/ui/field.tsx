import type { ReactNode } from 'react';

export default function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <label className="section-label">{label}</label>
      {children}
    </div>
  );
}
