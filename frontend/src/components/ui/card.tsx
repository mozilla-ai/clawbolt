import { forwardRef, type ReactNode } from 'react';
import { Card as HeroCard, CardBody } from '@heroui/card';

interface CardProps {
  className?: string;
  children?: ReactNode;
  onClick?: () => void;
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ className, children, onClick, ...props }, ref) => (
    <HeroCard
      ref={ref}
      shadow="sm"
      radius="lg"
      isPressable={!!onClick}
      onPress={onClick}
      className={className}
      {...props}
    >
      <CardBody className="p-5">{children}</CardBody>
    </HeroCard>
  ),
);
Card.displayName = 'Card';
export default Card;
