import { useState, useEffect } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import { toast } from '@/lib/toast';
import { useUpdateProfile } from '@/hooks/queries';
import { useAuth } from '@/contexts/AuthContext';
import { getAccessToken } from '@/lib/api-client';
import type { AppShellContext } from '@/layouts/AppShell';

const STEPS = [
  {
    title: 'Set up Telegram',
    description:
      'Connect your Telegram bot so you can chat with your assistant from your phone. This is the main way to use Clawbolt.',
    link: '/app/channels',
    linkLabel: 'Configure Channels',
    icon: ChannelsIcon,
  },
  {
    title: 'Start chatting',
    description:
      'Send your first message. Try asking for an estimate, a reminder, or just say hello.',
    link: '/app/chat',
    linkLabel: 'Open Chat',
    icon: ChatIcon,
  },
] as const;

export default function GetStartedPage() {
  const { reloadProfile } = useOutletContext<AppShellContext>();
  const navigate = useNavigate();
  const updateProfile = useUpdateProfile();
  const { isPremium } = useAuth();
  const [botUsername, setBotUsername] = useState<string | null>(null);

  useEffect(() => {
    if (!isPremium) return;
    const token = getAccessToken();
    const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
    fetch('/api/channels/telegram/bot-info', { headers })
      .then((res) => (res.ok ? (res.json() as Promise<{ bot_username: string }>) : null))
      .then((data) => { if (data) setBotUsername(data.bot_username); })
      .catch(() => {});
  }, [isPremium]);

  const handleDismiss = () => {
    updateProfile.mutate(
      { onboarding_complete: true },
      {
        onSuccess: () => {
          reloadProfile();
          navigate('/app/chat', { replace: true });
        },
        onError: (e) => toast.error(e.message),
      },
    );
  };

  return (
    <div className="max-w-2xl mx-auto">
      <div className="mb-8">
        <h2 className="text-xl font-semibold font-display">Get Started</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Clawbolt is your AI assistant for the trades. Set up a few things here, then head to
          Telegram to run your business from your phone.
        </p>
      </div>

      <div className="grid gap-4">
        {STEPS.map((step, i) => (
          <Card key={step.title}>
            <div className="flex items-start gap-4">
              <div className="flex items-center justify-center w-10 h-10 rounded-full bg-primary-light text-primary shrink-0">
                <step.icon />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-medium text-muted-foreground">
                    Step {i + 1}
                  </span>
                </div>
                <h3 className="text-sm font-semibold font-display">{step.title}</h3>
                <p className="text-sm text-muted-foreground mt-1">
                  {i === 0 && botUsername
                    ? `Message @${botUsername} on Telegram to chat with your assistant from your phone. This is the main way to use Clawbolt.`
                    : step.description}
                </p>
                <Button
                  variant="ghost"
                  size="sm"
                  className="mt-2 -ml-2"
                  onClick={() => navigate(step.link)}
                >
                  {step.linkLabel}
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>

      <div className="mt-8 flex justify-center">
        <Button
          variant="primary"
          onClick={handleDismiss}
          disabled={updateProfile.isPending}
          isLoading={updateProfile.isPending}
        >
          Got it, take me to chat
        </Button>
      </div>
    </div>
  );
}

// --- Step icons (inline SVG) ---

function ChannelsIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
    </svg>
  );
}
