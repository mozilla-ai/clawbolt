import { cn } from '@/lib/utils';

export default function Spinner({ className }: { className?: string }) {
  return (
    <div
      className={cn('w-5 h-5 border-2 border-muted-foreground/30 border-t-primary rounded-full animate-spin', className)}
      role="status"
      aria-label="Loading"
    />
  );
}
