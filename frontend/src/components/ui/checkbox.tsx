import { forwardRef, type InputHTMLAttributes } from 'react';
import { Checkbox as HeroCheckbox } from '@heroui/checkbox';

type CheckboxProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'color'>;

const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(
  ({ className, checked, disabled, onChange, id, children }) => (
    <HeroCheckbox
      isSelected={checked}
      isDisabled={disabled}
      onValueChange={(val: boolean) => {
        if (onChange) {
          const syntheticEvent = {
            target: { checked: val },
          } as React.ChangeEvent<HTMLInputElement>;
          onChange(syntheticEvent);
        }
      }}
      color={'primary' as const}
      id={id}
      className={className}
    >
      {children}
    </HeroCheckbox>
  ),
);
Checkbox.displayName = 'Checkbox';
export default Checkbox;
