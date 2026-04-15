import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import api from '@/api';

interface ChatActivityValue {
  agentBusy: boolean;
  activityTool: string | null;
  // Monotonically-increasing counter, incremented once per 'done' event. Lets
  // consumers fire an effect whenever the agent finishes without re-running on
  // unrelated activity events.
  doneTick: number;
}

const ChatActivityContext = createContext<ChatActivityValue | null>(null);

export function ChatActivityProvider({ children }: { children: ReactNode }) {
  const [agentBusy, setAgentBusy] = useState(false);
  const [activityTool, setActivityTool] = useState<string | null>(null);
  const [doneTick, setDoneTick] = useState(0);

  useEffect(() => {
    const controller = api.subscribeToActivity((event) => {
      if (event.type === 'thinking') {
        setAgentBusy(true);
        setActivityTool(null);
      } else if (event.type === 'tool_call') {
        setAgentBusy(true);
        setActivityTool(event.tool_name ?? null);
      } else if (event.type === 'done') {
        setAgentBusy(false);
        setActivityTool(null);
        setDoneTick((n) => n + 1);
      }
    });
    return () => controller.abort();
  }, []);

  return (
    <ChatActivityContext.Provider value={{ agentBusy, activityTool, doneTick }}>
      {children}
    </ChatActivityContext.Provider>
  );
}

export function useChatActivity(): ChatActivityValue {
  const ctx = useContext(ChatActivityContext);
  if (!ctx) {
    throw new Error('useChatActivity must be used within a ChatActivityProvider');
  }
  return ctx;
}
