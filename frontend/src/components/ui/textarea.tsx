import { forwardRef, type TextareaHTMLAttributes } from 'react';
import { Textarea as HeroTextarea } from '@heroui/input';

type TextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'size' | 'color'> & {
  maxRows?: number;
  /** HeroUI slot classNames, e.g. { input: 'min-h-[65vh]' } */
  classNames?: Record<string, string>;
};

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, classNames, disabled, onChange, value, placeholder, rows, maxRows, id, ...rest }, ref) => {
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
        maxRows={maxRows}
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
        classNames={{
          inputWrapper: 'bg-card',
          ...classNames,
        }}
      />
    );
  },
);
Textarea.displayName = 'Textarea';
export default Textarea;
