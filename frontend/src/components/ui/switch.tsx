import { forwardRef } from 'react';
import { Switch as HeroSwitch } from '@heroui/switch';

interface SwitchProps {
  checked?: boolean;
  disabled?: boolean;
  onChange?: (checked: boolean) => void;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  id?: string;
  children?: React.ReactNode;
}

const Switch = forwardRef<HTMLInputElement, SwitchProps>(
  ({ checked, disabled, onChange, size = 'sm', className, id, children }) => (
    <HeroSwitch
      isSelected={checked}
      isDisabled={disabled}
      onValueChange={onChange}
      size={size}
      color={'primary' as const}
      id={id}
      className={className}
    >
      {children}
    </HeroSwitch>
  ),
);
Switch.displayName = 'Switch';
export default Switch;
