import { type TextareaHTMLAttributes, forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        'w-full px-3 py-2.5 sm:py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-colors resize-y min-h-[80px]',
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = 'Textarea';
export default Textarea;
