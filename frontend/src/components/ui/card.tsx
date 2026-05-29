import { forwardRef, type ReactNode } from 'react';
import { Card as HeroCard, CardBody } from '@heroui/card';

interface CardProps {
  className?: string;
  children?: ReactNode;
  onClick?: () => void;
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ className, children, onClick, ...props }, ref) => {
    // A hairline border defines the card against near-equal-luminance surfaces
    // (light mode). Pressable cards add a hover lift + amber border accent.
    const base = 'border border-border';
    const interactive = onClick
      ? 'transition-all duration-150 can-hover:hover:-translate-y-0.5 can-hover:hover:shadow-md can-hover:hover:border-primary/40'
      : '';
    return (
      <HeroCard
        ref={ref}
        shadow="sm"
        radius="lg"
        isPressable={!!onClick}
        onPress={onClick}
        className={[base, interactive, className].filter(Boolean).join(' ')}
        {...props}
      >
        <CardBody className="p-5">{children}</CardBody>
      </HeroCard>
    );
  },
);
Card.displayName = 'Card';
export default Card;
