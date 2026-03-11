import { Divider as HeroDivider } from '@heroui/divider';

type DividerProps = {
  className?: string;
  orientation?: 'horizontal' | 'vertical';
};

export default function Divider({ className, orientation = 'horizontal' }: DividerProps) {
  return <HeroDivider className={className} orientation={orientation} />;
}
