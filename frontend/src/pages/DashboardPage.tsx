import type { MouseEvent } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import Markdown from 'react-markdown';
import Card from '@/components/ui/card';
import { Switch } from '@heroui/switch';
import { Spinner } from '@heroui/spinner';
import { toast } from '@/lib/toast';
import { useToolConfig, useUpdateToolConfig, useOAuthStatus, useCalendarConfig, useMemory, useModelConfig, useUpdateProfile } from '@/hooks/queries';
import { useChannelStates } from '@/hooks/useChannelStates';
import { getVisibleChannels, getChannelStatusDisplay } from '@/lib/channel-utils';
import { displayName as toolDisplayName, getToolOAuthStatus } from '@/lib/tool-utils';
import type { AppShellContext } from '@/layouts/AppShell';

// Per-calendar tools that can be individually toggled.
const PER_CALENDAR_TOOLS = [
  'calendar_list_events',
  'calendar_create_event',
  'calendar_update_event',
  'calendar_delete_event',
] as const;

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

  const channelData = useChannelStates();
  const tools = useToolConfig();
  const updateToolConfig = useUpdateToolConfig();
  const oauth = useOAuthStatus();
  const calendarConfig = useCalendarConfig();
  const memory = useMemory();
  const modelConfig = useModelConfig();
  const updateProfile = useUpdateProfile();

  // --- Channels ---
  const channelStates = getVisibleChannels(channelData.channelConfig).flatMap((ch) => {
    const state = channelData.states[ch.key];
    return state ? [{ ...ch, state }] : [];
  });
  const hasAnyActive = channelStates.some((ch) => ch.state === 'active');
  const hasAnyAvailable = channelStates.some(
    (ch) => ch.state === 'available' || ch.state === 'configured' || ch.state === 'active',
  );
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
      <div className="mb-6">
        <h2 className="text-xl font-semibold font-display">Dashboard</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Setup, integrations, and admin live here. For day-to-day, just chat
          with your assistant from your phone. No need to come back to the web
          app for most things.
        </p>
      </div>

      <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-3">
        Setup
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">

        {/* Channels */}
        <DashboardCard
          title="Channels"
          description="Messaging platforms your assistant listens on."
          configured={channelConfigured}
          icon={<ChannelsIcon />}
          onClick={() => navigate('/app/channels')}
          isLoading={channelData.isLoading}
          isError={channelData.isError}
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

        {/* Integrations */}
        <DashboardCard
          title="Integrations"
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
                const { needsOAuth, isConfigured, isConnected } = getToolOAuthStatus(tool.name, oauthMap, tool.configured);
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
                            {needsOAuth ? (isConnected ? 'Connected' : 'Not connected') : 'Available'}
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

      <div className="mb-3">
        <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
          Your assistant's context
        </h3>
        <p className="text-xs text-muted-foreground mt-1">
          All of this is editable from chat too. Come back here when you want a bird's-eye view.
        </p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">

        {/* Knowledge (Memory) */}
        <DashboardCard
          title="Knowledge"
          description="What your assistant knows about your business."
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

        {/* Priorities (Heartbeat) */}
        <DashboardCard
          title="Priorities"
          description="What your assistant should stay aware of."
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
                aria-label="Toggle proactive check-ins"
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
                <p className="text-xs text-muted-foreground">No priorities set yet. Add tasks you're tracking.</p>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Enable to let your assistant proactively follow up on your tasks.</p>
          )}
        </DashboardCard>

        {/* Personality (Soul) */}
        <DashboardCard
          title="Personality"
          description="Guides how your assistant behaves and communicates."
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

        {/* About You (User) */}
        <DashboardCard
          title="About You"
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
