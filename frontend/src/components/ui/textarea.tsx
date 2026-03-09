import { forwardRef, type TextareaHTMLAttributes } from 'react';
import { Textarea as HeroTextarea } from '@heroui/input';

type TextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'size' | 'color'>;

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, disabled, onChange, value, placeholder, rows, id, ...rest }, ref) => {
    void rest;
    return (
      <HeroTextarea
        ref={ref}
        variant="bordered"
        size="sm"
        radius="md"
        isDisabled={disabled}
        value={value as string | undefined}
        placeholder={placeholder}
        minRows={rows ?? 3}
        id={id}
        onValueChange={(val) => {
          if (onChange) {
            const syntheticEvent = {
              target: { value: val },
            } as React.ChangeEvent<HTMLTextAreaElement>;
            onChange(syntheticEvent);
          }
        }}
        className={className}
      />
    );
  },
);
Textarea.displayName = 'Textarea';
export default Textarea;
