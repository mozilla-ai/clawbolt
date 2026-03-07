import { type SelectHTMLAttributes, forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        'w-full px-3 py-2.5 sm:py-2 text-sm bg-card border border-border rounded-[--radius-md] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-colors',
        className,
      )}
      {...props}
    />
  ),
);
Select.displayName = 'Select';
export default Select;
