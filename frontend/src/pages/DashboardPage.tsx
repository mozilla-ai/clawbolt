import { useState, useEffect } from 'react';
import type { MouseEvent } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import Markdown from 'react-markdown';
import Card from '@/components/ui/card';
import { Switch } from '@heroui/switch';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useChannelRoutes, useChannelConfig, useToolConfig, useUpdateToolConfig, useOAuthStatus, useCalendarConfig, useMemory, useModelConfig, useUpdateProfile } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { MESSAGING_CHANNELS, getChannelState, getChannelStatusDisplay } from '@/lib/channel-utils';
import type { AppShellContext } from '@/layouts/AppShell';
import api from '@/api';

// Human-readable display names for tool factories (matches ToolsPage).
const TOOL_DISPLAY_NAMES: Record<string, string> = {
  quickbooks: 'QuickBooks',
  calendar: 'Google Calendar',
  workspace: 'Workspace',
  profile: 'Profile',
  memory: 'Memory',
  messaging: 'Messaging',
  file: 'File Storage',
  heartbeat: 'Heartbeat',
  permissions: 'Permissions',
};

// Maps domain tool names to their OAuth integration identifiers (matches ToolsPage).
const TOOL_OAUTH_MAP: Record<string, string> = {
  quickbooks: 'quickbooks',
  calendar: 'google_calendar',
};

// Per-calendar tools that can be individually toggled (matches ToolsPage).
const PER_CALENDAR_TOOLS = [
  'calendar_list_events',
  'calendar_create_event',
  'calendar_update_event',
  'calendar_delete_event',
] as const;

function toolDisplayName(name: string): string {
  return TOOL_DISPLAY_NAMES[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

/** Truncate text to maxLen characters, appending ellipsis if trimmed. */
function truncate(text: string, maxLen: number): string {
  const trimmed = text.trim();
  if (trimmed.length <= maxLen) return trimmed;
  return trimmed.slice(0, maxLen).trimEnd() + '...';
}

/** Prevent click from bubbling to the pressable Card wrapper. */
function stopCardPress(e: MouseEvent) {
  e.stopPropagation();
}

/** Capped-height markdown preview with a fade gradient at the bottom. */
function MarkdownPreview({ content, maxChars, className }: { content: string; maxChars: number; className?: string }) {
  return (
    <div className={`relative overflow-hidden ${className ?? 'max-h-24 md:max-h-40'}`}>
      <div className="prose-card">
        <Markdown>{truncate(content, maxChars)}</Markdown>
      </div>
      <div className="absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-card to-transparent pointer-events-none" />
    </div>
  );
}

// --- Shared card wrapper ---

interface DashboardCardProps {
  title: string;
  description: string;
  configured: boolean;
  icon: React.ReactNode;
  onClick: () => void;
  isLoading: boolean;
  isError: boolean;
  children?: React.ReactNode;
}

function DashboardCard({ title, description, configured, icon, onClick, isLoading, isError, children }: DashboardCardProps) {
  return (
    <Card onClick={onClick} className="bg-card">
      <div className="flex items-start gap-3">
        <div className="text-muted-foreground shrink-0 mt-0.5">{icon}</div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium font-body">{title}</h3>
            {!isLoading && !isError && (
              <span
                className={`size-2 rounded-full shrink-0 ${configured ? 'bg-success' : 'bg-warning'}`}
                aria-label={configured ? 'Configured' : 'Needs attention'}
              />
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">{description}</p>
        </div>
      </div>
      <div className="mt-3 pt-3 border-t border-border">
        {isLoading ? (
          <div>
            <Spinner size="sm" color="default" aria-label={`Loading ${title}`} />
          </div>
        ) : isError ? (
          <p className="text-xs text-danger">Unable to load</p>
        ) : (
          children
        )}
      </div>
    </Card>
  );
}

// --- Dashboard page ---

export default function DashboardPage() {
  const navigate = useNavigate();
  const { profile, reloadProfile } = useOutletContext<AppShellContext>();
  const { isPremium } = useAuth();

  const channels = useChannelRoutes();
  const channelConfigData = useChannelConfig();
  const tools = useToolConfig();
  const updateToolConfig = useUpdateToolConfig();
  const oauth = useOAuthStatus();
  const calendarConfig = useCalendarConfig();
  const memory = useMemory();
  const modelConfig = useModelConfig();
  const updateProfile = useUpdateProfile();

  // --- Premium channel link data (needed for correct state derivation) ---
  const [telegramLinkData, setTelegramLinkData] = useState<{ telegram_user_id?: string | null } | null>(null);
  const [linqLinkData, setLinqLinkData] = useState<{ phone_number?: string | null } | null>(null);

  useEffect(() => {
    if (isPremium) {
      api.getTelegramLink().then(setTelegramLinkData).catch(() => {});
      api.getLinqLink().then(setLinqLinkData).catch(() => {});
    }
  }, [isPremium]);

  const premiumData = isPremium
    ? { telegram_user_id: telegramLinkData?.telegram_user_id, phone_number: linqLinkData?.phone_number }
    : undefined;

  // --- Channels ---
  const allRoutes = channels.data?.routes ?? [];
  const channelConf = channelConfigData.data;
  const channelStates = channelConf
    ? MESSAGING_CHANNELS.map((ch) => ({
        ...ch,
        state: getChannelState(ch.key, channelConf, allRoutes, isPremium, premiumData),
      }))
    : [];
  const hasAnyActive = channelStates.some((ch) => ch.state === 'active');
  const hasAnyAvailable = channelStates.some(
    (ch) => ch.state === 'available' || ch.state === 'configured' || ch.state === 'active',
  );
  // Overall card dot: green if any active, amber if any available, gray otherwise
  const channelConfigured = hasAnyActive;

  // --- Tools ---
  const allTools = tools.data?.tools ?? [];
  const domainTools = allTools.filter((t) => t.category === 'domain');
  const integrations = oauth.data?.integrations ?? [];
  const oauthMap = Object.fromEntries(integrations.map((i) => [i.integration, i]));
  const enabledCalendars = calendarConfig.data?.calendars ?? [];
  const toolsConfigured = domainTools.some((t) => t.enabled);

  // --- Memory ---
  const memoryContent = memory.data?.content ?? '';
  const wordCount = memoryContent.trim() ? memoryContent.trim().split(/\s+/).length : 0;
  const memoryConfigured = wordCount > 0;

  // --- Heartbeat ---
  const heartbeatOptIn = profile?.heartbeat_opt_in ?? false;
  const heartbeatFreq = profile?.heartbeat_frequency ?? '';
  const heartbeatText = profile?.heartbeat_text ?? '';

  const handleHeartbeatToggle = (enabled: boolean) => {
    updateProfile.mutate(
      { heartbeat_opt_in: enabled },
      {
        onSuccess: () => {
          reloadProfile();
          toast.success(`Heartbeat ${enabled ? 'enabled' : 'disabled'}`);
        },
        onError: (e) => toast.error(e.message),
      },
    );
  };

  const handleToolToggle = (name: string, enabled: boolean) => {
    updateToolConfig.mutate([{ name, enabled }], {
      onSuccess: () => toast.success(`${toolDisplayName(name)} ${enabled ? 'enabled' : 'disabled'}`),
      onError: (e) => toast.error(e.message),
    });
  };

  // --- Soul ---
  const soulText = profile?.soul_text ?? '';
  const soulConfigured = soulText.trim().length > 0;

  // --- User ---
  const userText = profile?.user_text ?? '';
  const userConfigured = userText.trim().length > 0;

  // --- Settings ---
  const provider = modelConfig.data?.llm_provider ?? '';
  const model = modelConfig.data?.llm_model ?? '';
  const visionModel = modelConfig.data?.vision_model ?? '';
  const settingsConfigured = !!(provider && model);

  return (
    <div>
      <h2 className="text-xl font-semibold font-display mb-6">Dashboard</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">

        {/* Channels */}
        <DashboardCard
          title="Channels"
          description="Messaging platforms your assistant listens on."
          configured={channelConfigured}
          icon={<ChannelsIcon />}
          onClick={() => navigate('/app/channels')}
          isLoading={(channels.isPending && !channels.data) || (channelConfigData.isPending && !channelConfigData.data)}
          isError={(channels.isError && !channels.data) || (channelConfigData.isError && !channelConfigData.data)}
        >
          {hasAnyAvailable ? (
            <div className="space-y-2">
              {channelStates.map((ch) => {
                const display = getChannelStatusDisplay(ch.state);
                return (
                  <div key={ch.key} className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className={`size-1.5 rounded-full shrink-0 ${display.dotClass}`} />
                      <span className="text-xs text-foreground">{ch.label}</span>
                    </div>
                    <span className={`text-xs ${display.labelClass}`}>{display.label}</span>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Set up a messaging channel to start chatting with your assistant beyond web chat.</p>
          )}
        </DashboardCard>

        {/* Tools */}
        <DashboardCard
          title="Tools"
          description="Capabilities and integrations available to your assistant."
          configured={toolsConfigured}
          icon={<ToolsIcon />}
          onClick={() => navigate('/app/tools')}
          isLoading={(tools.isPending && !tools.data) || (oauth.isPending && !oauth.data)}
          isError={tools.isError && !tools.data}
        >
          {domainTools.length > 0 ? (
            <div className="space-y-2.5">
              {domainTools.map((tool) => {
                const oauthKey = TOOL_OAUTH_MAP[tool.name];
                const oauthEntry = oauthKey ? oauthMap[oauthKey] : undefined;
                const isConnected = oauthEntry?.connected ?? false;
                const isConfigured = oauthEntry?.configured ?? false;
                const enabledSubTools = (tool.sub_tools ?? []).filter((st) => st.enabled).length;
                const totalSubTools = (tool.sub_tools ?? []).length;
                return (
                  <div key={tool.name}>
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className={`text-xs ${isConfigured ? 'text-foreground' : 'text-muted-foreground'}`}>{toolDisplayName(tool.name)}</span>
                        {isConfigured ? (
                          <span className={`inline-flex items-center gap-1 text-xs ${isConnected ? 'text-success' : 'text-warning'}`}>
                            <span className={`size-1.5 rounded-full ${isConnected ? 'bg-success' : 'bg-warning'}`} />
                            {isConnected ? 'Connected' : 'Not connected'}
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                            <span className="size-1.5 rounded-full bg-default-300" />
                            Not configured
                          </span>
                        )}
                      </div>
                      {isConnected && (
                        /* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */
                        <div onClick={stopCardPress}>
                          <Switch
                            isSelected={tool.enabled}
                            isDisabled={updateToolConfig.isPending}
                            onValueChange={(val) => handleToolToggle(tool.name, val)}
                            size="sm"
                            aria-label={`Toggle ${toolDisplayName(tool.name)}`}
                          />
                        </div>
                      )}
                    </div>
                    {isConnected && tool.enabled && tool.name === 'calendar' && enabledCalendars.length > 0 && (
                      <div className="mt-1 space-y-0.5">
                        {enabledCalendars.map((cal) => {
                          const disabledCount = (cal.disabled_tools ?? []).length;
                          const enabledCount = Math.max(0, PER_CALENDAR_TOOLS.length - disabledCount);
                          return (
                            <div key={cal.calendar_id} className="flex items-center justify-between gap-2">
                              <span className="text-xs text-muted-foreground truncate">{cal.display_name}</span>
                              <span className="text-xs text-muted-foreground shrink-0">{enabledCount}/{PER_CALENDAR_TOOLS.length}</span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                    {isConnected && tool.enabled && tool.name !== 'calendar' && totalSubTools > 0 && (
                      <p className="text-xs text-muted-foreground mt-0.5 ml-0">
                        {enabledSubTools}/{totalSubTools} capabilities enabled
                      </p>
                    )}
                    {isConfigured && !isConnected && (
                      <p className="text-xs text-muted-foreground mt-0.5 ml-0">
                        Reconnect to enable
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Enable tools to let your assistant take actions on your behalf.</p>
          )}
        </DashboardCard>

        {/* Memory */}
        <DashboardCard
          title="Memory"
          description="Long-term facts your assistant has learned about your business."
          configured={memoryConfigured}
          icon={<MemoryIcon />}
          onClick={() => navigate('/app/memory')}
          isLoading={memory.isPending && !memory.data}
          isError={memory.isError && !memory.data}
        >
          {memoryConfigured ? (
            <div>
              <MarkdownPreview content={memoryContent} maxChars={300} />
              <p className="text-xs text-muted-foreground mt-2">{wordCount} {wordCount === 1 ? 'word' : 'words'}</p>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Chat with your assistant to build up knowledge, or add notes directly.</p>
          )}
        </DashboardCard>

        {/* Heartbeat */}
        <DashboardCard
          title="Heartbeat"
          description="Your assistant reads this to stay aware of your priorities."
          configured={heartbeatOptIn}
          icon={<HeartbeatIcon />}
          onClick={() => navigate('/app/heartbeat')}
          isLoading={false}
          isError={false}
        >
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-muted-foreground">Check-ins</span>
            {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
            <div onClick={stopCardPress}>
              <Switch
                isSelected={heartbeatOptIn}
                isDisabled={updateProfile.isPending}
                onValueChange={handleHeartbeatToggle}
                size="sm"
                aria-label="Toggle heartbeat check-ins"
              />
            </div>
          </div>
          {heartbeatOptIn ? (
            <div>
              <span className="inline-flex text-xs px-2 py-0.5 rounded-full bg-success-bg text-success mb-2">
                {heartbeatFreq}
              </span>
              {heartbeatText.trim() ? (
                <MarkdownPreview content={heartbeatText} maxChars={250} className="max-h-20 md:max-h-32" />
              ) : (
                <p className="text-xs text-muted-foreground">No heartbeat prompt set. Add tasks and priorities to track.</p>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Enable to let your assistant proactively follow up on your tasks.</p>
          )}
        </DashboardCard>

        {/* Soul */}
        <DashboardCard
          title="Soul"
          description="Guides your assistant's personality and communication style."
          configured={soulConfigured}
          icon={<SoulIcon />}
          onClick={() => navigate('/app/soul')}
          isLoading={false}
          isError={false}
        >
          {soulConfigured ? (
            <MarkdownPreview content={soulText} maxChars={300} />
          ) : (
            <p className="text-xs text-muted-foreground">Define how your assistant should behave, speak, and interact with clients.</p>
          )}
        </DashboardCard>

        {/* User */}
        <DashboardCard
          title="User"
          description="What your assistant knows about you."
          configured={userConfigured}
          icon={<UserIcon />}
          onClick={() => navigate('/app/user')}
          isLoading={false}
          isError={false}
        >
          {userConfigured ? (
            <div>
              <MarkdownPreview content={userText} maxChars={300} />
              {profile?.timezone && (
                <p className="text-xs text-muted-foreground mt-2">{profile.timezone}</p>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Tell your assistant about yourself: your name, phone, preferences.</p>
          )}
        </DashboardCard>

        {/* Settings */}
        <DashboardCard
          title="Settings"
          description="AI model and provider configuration."
          configured={settingsConfigured}
          icon={<SettingsIcon />}
          onClick={() => navigate('/app/settings')}
          isLoading={modelConfig.isPending && !modelConfig.data}
          isError={modelConfig.isError && !modelConfig.data}
        >
          {settingsConfigured ? (
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-xs">
                <span className="text-muted-foreground">Model</span>
                <span className="text-foreground font-medium">{model}</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className="text-muted-foreground">Provider</span>
                <span className="text-foreground font-medium">{provider}</span>
              </div>
              {visionModel && visionModel !== model && (
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-muted-foreground">Vision</span>
                  <span className="text-foreground font-medium">{visionModel}</span>
                </div>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Configure which AI model and provider your assistant uses.</p>
          )}
        </DashboardCard>

      </div>
    </div>
  );
}

// --- Icons (inline SVG, matching AppShell style) ---

function ChannelsIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  );
}

function ToolsIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

function MemoryIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
    </svg>
  );
}

function HeartbeatIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3.343 7.778a4.5 4.5 0 016.364 0L12 10.07l2.293-2.293a4.5 4.5 0 116.364 6.364L12 22.485l-8.657-8.343a4.5 4.5 0 010-6.364z" />
    </svg>
  );
}

function SoulIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  );
}
