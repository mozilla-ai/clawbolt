import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Modal,
  ModalContent,
  ModalBody,
} from '@heroui/modal';
import { Input } from '@heroui/input';
import Badge from '@/components/ui/badge';
import type { SessionSummary } from '@/types';

interface SearchResult {
  type: 'conversation' | 'memory';
  label: string;
  detail: string;
  id: string;
}

interface SearchOverlayProps {
  isOpen: boolean;
  onClose: () => void;
  sessions: SessionSummary[];
  memoryContent: string;
}

function fuzzyMatch(query: string, text: string): boolean {
  return text.toLowerCase().includes(query.toLowerCase());
}

export default function SearchOverlay({ isOpen, onClose, sessions, memoryContent }: SearchOverlayProps) {
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  // Debounce the search query
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQuery(query);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  // Reset state when overlay opens
  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setDebouncedQuery('');
      setSelectedIndex(0);
    }
  }, [isOpen]);

  // Focus input when opened
  useEffect(() => {
    if (isOpen) {
      // Small delay to allow modal to render
      const timer = setTimeout(() => {
        inputRef.current?.focus();
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [isOpen]);

  const results: SearchResult[] = useMemo(() => {
    if (!debouncedQuery.trim()) return [];

    const matched: SearchResult[] = [];

    for (const session of sessions) {
      if (fuzzyMatch(debouncedQuery, session.last_message_preview)) {
        matched.push({
          type: 'conversation',
          label: session.last_message_preview || 'No preview',
          detail: new Date(session.start_time).toLocaleDateString(),
          id: session.id,
        });
      }
    }

    // Search memory content by matching lines
    if (memoryContent) {
      const lines = memoryContent.split('\n');
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith('#') && fuzzyMatch(debouncedQuery, trimmed)) {
          matched.push({
            type: 'memory',
            label: trimmed.length > 60 ? trimmed.slice(0, 60) + '...' : trimmed,
            detail: 'Memory',
            id: `memory-${trimmed.slice(0, 30)}`,
          });
        }
      }
    }

    return matched;
  }, [debouncedQuery, sessions, memoryContent]);

  // Reset selected index when results change
  useEffect(() => {
    setSelectedIndex(0);
  }, [results]);

  const navigateToResult = useCallback(
    (result: SearchResult) => {
      onClose();
      if (result.type === 'conversation') {
        navigate(`/app/chat?session=${encodeURIComponent(result.id)}`);
      } else {
        navigate('/app/memory');
      }
    },
    [navigate, onClose],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex((prev) => Math.min(prev + 1, results.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
      } else if (e.key === 'Enter' && results.length > 0) {
        e.preventDefault();
        const result = results[selectedIndex];
        if (result) {
          navigateToResult(result);
        }
      }
    },
    [results, selectedIndex, navigateToResult],
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement="top"
      size="lg"
      hideCloseButton
      classNames={{
        backdrop: 'bg-black/50',
        base: 'mt-[15vh]',
      }}
    >
      <ModalContent>
        <ModalBody className="p-3">
          <Input
            ref={inputRef}
            placeholder="Search conversations and memory..."
            value={query}
            onValueChange={setQuery}
            onKeyDown={handleKeyDown}
            variant="bordered"
            size="lg"
            autoFocus
            startContent={<SearchIcon />}
            classNames={{
              inputWrapper: 'border-default-300',
            }}
          />

          {debouncedQuery.trim() && (
            <div className="max-h-80 overflow-y-auto">
              {results.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-6">
                  No results found
                </p>
              ) : (
                <ul className="space-y-1" role="listbox">
                  {results.map((result, index) => (
                    <li
                      key={`${result.type}-${result.id}`}
                      role="option"
                      aria-selected={index === selectedIndex}
                      className={`flex items-center gap-3 px-3 py-2 rounded-md cursor-pointer transition-colors text-sm ${
                        index === selectedIndex
                          ? 'bg-primary/10 text-foreground'
                          : 'hover:bg-secondary text-foreground'
                      }`}
                      onClick={() => navigateToResult(result)}
                      onMouseEnter={() => setSelectedIndex(index)}
                    >
                      <Badge variant={result.type === 'conversation' ? 'default' : 'success'}>
                        {result.type === 'conversation' ? 'Conversation' : 'Memory'}
                      </Badge>
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">{result.label}</p>
                        <p className="truncate text-xs text-muted-foreground">{result.detail}</p>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="flex items-center justify-between text-xs text-muted-foreground px-1 pb-1">
            <span>
              <kbd className="px-1.5 py-0.5 rounded bg-default-100 text-[10px] font-mono">Esc</kbd>
              {' '}to close
            </span>
            <span>
              <kbd className="px-1.5 py-0.5 rounded bg-default-100 text-[10px] font-mono">↑↓</kbd>
              {' '}to navigate
              {' '}
              <kbd className="px-1.5 py-0.5 rounded bg-default-100 text-[10px] font-mono">↵</kbd>
              {' '}to select
            </span>
          </div>
        </ModalBody>
      </ModalContent>
    </Modal>
  );
}

function SearchIcon() {
  return (
    <svg className="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
    </svg>
  );
}
