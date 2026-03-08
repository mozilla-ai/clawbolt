import { type InputHTMLAttributes, forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        'w-full px-3 py-2.5 sm:py-2 text-base sm:text-sm bg-card border border-border rounded-[--radius-md] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-colors',
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = 'Input';
export default Input;
