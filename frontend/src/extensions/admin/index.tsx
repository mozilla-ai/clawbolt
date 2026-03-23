import { useState, useEffect, useCallback, type KeyboardEvent } from 'react';
import OverviewTab from './tabs/overview';
import HeartbeatsTab from './tabs/heartbeats';
import SessionsTab from './tabs/sessions';
import UsageTab from './tabs/usage';

// --- Tab definitions ---

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'heartbeats', label: 'Heartbeats' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'usage', label: 'Usage' },
] as const;

type TabId = (typeof TABS)[number]['id'];

function getInitialTab(): TabId {
  const hash = window.location.hash.replace('#', '');
  if (TABS.some(t => t.id === hash)) return hash as TabId;
  return 'overview';
}

// --- Tab Bar ---

function TabBar({
  active,
  onChange,
}: {
  active: TabId;
  onChange: (id: TabId) => void;
}) {
  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      const currentIndex = TABS.findIndex(t => t.id === active);
      let nextIndex = currentIndex;

      if (e.key === 'ArrowRight') {
        nextIndex = (currentIndex + 1) % TABS.length;
      } else if (e.key === 'ArrowLeft') {
        nextIndex = (currentIndex - 1 + TABS.length) % TABS.length;
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        return;
      } else {
        return;
      }

      e.preventDefault();
      onChange(TABS[nextIndex].id);
    },
    [active, onChange],
  );

  return (
    <div
      className="flex border-b border-border overflow-x-auto gap-1"
      role="tablist"
      onKeyDown={handleKeyDown}
    >
      {TABS.map(tab => (
        <button
          key={tab.id}
          role="tab"
          aria-selected={active === tab.id}
          tabIndex={active === tab.id ? 0 : -1}
          className={`px-4 py-2.5 text-[13px] font-medium cursor-pointer transition-colors whitespace-nowrap min-h-[44px] ${
            active === tab.id
              ? 'text-foreground border-b-2 border-primary'
              : 'text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

// --- Main Admin Panel ---

export default function AdminPanel() {
  const [activeTab, setActiveTab] = useState<TabId>(getInitialTab);

  const handleTabChange = useCallback((id: TabId) => {
    setActiveTab(id);
    window.location.hash = id;
  }, []);

  // Sync with browser back/forward
  useEffect(() => {
    const onHashChange = () => {
      const hash = window.location.hash.replace('#', '');
      if (TABS.some(t => t.id === hash)) {
        setActiveTab(hash as TabId);
      }
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl font-semibold font-display mb-1">Admin</h2>
        <p className="text-muted-foreground text-sm">Your assistant at a glance.</p>
      </div>

      <TabBar active={activeTab} onChange={handleTabChange} />

      <div className="mt-4" role="tabpanel" aria-labelledby={`tab-${activeTab}`}>
        {activeTab === 'overview' && <OverviewTab onSwitchTab={handleTabChange} />}
        {activeTab === 'heartbeats' && <HeartbeatsTab />}
        {activeTab === 'sessions' && <SessionsTab />}
        {activeTab === 'usage' && <UsageTab />}
      </div>
    </div>
  );
}
