import { forwardRef, type ReactNode } from 'react';
import { Card as HeroCard, CardBody } from '@heroui/card';

interface CardProps {
  className?: string;
  children?: ReactNode;
  onClick?: () => void;
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ className, children, onClick, ...props }, ref) => {
    // Pressable cards get a clear interactive affordance: hover lift + border
    // accent. Tokens only, hover gated to pointer devices.
    const interactive = onClick
      ? 'border border-transparent transition-all duration-150 can-hover:hover:-translate-y-0.5 can-hover:hover:shadow-md can-hover:hover:border-primary/30'
      : '';
    return (
      <HeroCard
        ref={ref}
        shadow="sm"
        radius="lg"
        isPressable={!!onClick}
        onPress={onClick}
        className={[interactive, className].filter(Boolean).join(' ')}
        {...props}
      >
        <CardBody className="p-5">{children}</CardBody>
      </HeroCard>
    );
  },
);
Card.displayName = 'Card';
export default Card;
