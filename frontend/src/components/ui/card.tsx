import { type HTMLAttributes, forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Card = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        'bg-card border border-border rounded-[--radius-md] shadow-sm p-4',
        className,
      )}
      {...props}
    />
  ),
);
Card.displayName = 'Card';
export default Card;
