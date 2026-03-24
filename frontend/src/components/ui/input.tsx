import { forwardRef, type InputHTMLAttributes } from 'react';
import { Input as HeroInput } from '@heroui/input';

type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'size' | 'color'>;

const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, disabled, onChange, value, placeholder, type, id, autoComplete, inputMode, ...rest }, ref) => {
    void rest;
    return (
      <HeroInput
        ref={ref}
        variant="bordered"
        size="sm"
        radius="md"
        isDisabled={disabled}
        value={value as string | undefined}
        placeholder={placeholder}
        type={type}
        id={id}
        autoComplete={autoComplete}
        inputMode={inputMode}
        onValueChange={(val) => {
          if (onChange) {
            const syntheticEvent = {
              target: { value: val },
            } as React.ChangeEvent<HTMLInputElement>;
            onChange(syntheticEvent);
          }
        }}
        className={className}
        classNames={{ inputWrapper: 'bg-card' }}
      />
    );
  },
);
Input.displayName = 'Input';
export default Input;
