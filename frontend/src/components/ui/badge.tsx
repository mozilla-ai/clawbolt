import { forwardRef, type HTMLAttributes } from 'react';
import { Chip } from '@heroui/chip';

type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'outline';

function mapVariant(v: BadgeVariant) {
  switch (v) {
    case 'default': return { color: 'primary' as const, variant: 'flat' as const };
    case 'success': return { color: 'success' as const, variant: 'flat' as const };
    case 'warning': return { color: 'warning' as const, variant: 'flat' as const };
    case 'danger': return { color: 'danger' as const, variant: 'flat' as const };
    case 'outline': return { color: 'default' as const, variant: 'bordered' as const };
  }
}

type BadgeProps = HTMLAttributes<HTMLSpanElement> & {
  variant?: BadgeVariant;
};

const Badge = forwardRef<HTMLDivElement, BadgeProps>(
  ({ className, variant = 'default', children, color: _color, ...props }, ref) => {
    const mapped = mapVariant(variant);

    return (
      <Chip
        ref={ref}
        color={mapped.color}
        variant={mapped.variant}
        size="sm"
        className={className}
        {...props}
      >
        {children}
      </Chip>
    );
  },
);
Badge.displayName = 'Badge';
export default Badge;
