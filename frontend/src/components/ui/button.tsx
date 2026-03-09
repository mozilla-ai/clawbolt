import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Button as HeroButton } from '@heroui/button';

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
type Size = 'sm' | 'md' | 'lg' | 'icon' | 'icon-sm';

function mapVariant(v: Variant) {
  switch (v) {
    case 'primary': return { color: 'primary' as const, variant: 'solid' as const };
    case 'secondary': return { color: 'default' as const, variant: 'bordered' as const };
    case 'danger': return { color: 'danger' as const, variant: 'solid' as const };
    case 'ghost': return { color: 'default' as const, variant: 'light' as const };
  }
}

function mapSize(s: Size) {
  switch (s) {
    case 'sm': return 'sm' as const;
    case 'md': return 'md' as const;
    case 'lg': return 'lg' as const;
    case 'icon': return 'md' as const;
    case 'icon-sm': return 'sm' as const;
  }
}

type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'color'> & {
  variant?: Variant;
  size?: Size;
  children?: ReactNode;
};

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'primary', size = 'md', disabled, onClick, type, children, title, 'aria-label': ariaLabel, 'aria-expanded': ariaExpanded, ...rest }, ref) => {
    const mapped = mapVariant(variant);
    const isIconOnly = size === 'icon' || size === 'icon-sm';
    // Avoid passing rest to prevent incompatible HTML attribute types
    void rest;

    return (
      <HeroButton
        ref={ref}
        color={mapped.color}
        variant={mapped.variant}
        size={mapSize(size)}
        isIconOnly={isIconOnly}
        isDisabled={disabled}
        onPress={onClick as unknown as undefined}
        type={type}
        className={className}
        title={title}
        aria-label={ariaLabel}
        aria-expanded={ariaExpanded}
      >
        {children}
      </HeroButton>
    );
  },
);
Button.displayName = 'Button';
export default Button;
