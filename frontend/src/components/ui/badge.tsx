import { type HTMLAttributes, forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Badge = forwardRef<HTMLSpanElement, HTMLAttributes<HTMLSpanElement>>(
  ({ className, ...props }, ref) => (
    <span
      ref={ref}
      className={cn(
        'inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-[--radius-full] bg-primary-light text-primary',
        className,
      )}
      {...props}
    />
  ),
);
Badge.displayName = 'Badge';
export default Badge;
