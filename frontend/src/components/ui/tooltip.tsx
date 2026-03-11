import { Tooltip as HeroTooltip } from '@heroui/tooltip';
import type { ReactNode } from 'react';

type TooltipProps = {
  content: string;
  children: ReactNode;
};

export default function Tooltip({ content, children }: TooltipProps) {
  return (
    <HeroTooltip content={content} delay={400} closeDelay={0}>
      {children}
    </HeroTooltip>
  );
}
