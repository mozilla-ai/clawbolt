import { forwardRef, type SelectHTMLAttributes, Children, isValidElement, type ReactElement } from 'react';
import { Select as HeroSelect, SelectItem } from '@heroui/select';

type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size' | 'color'>;

/**
 * Wrapper around HeroUI Select that accepts native <option> children
 * and converts them to <SelectItem> elements for backward compatibility.
 */
const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, disabled, onChange, value, children, 'aria-label': ariaLabel }, ref) => {
    // Extract option data from <option> children
    const items: { key: string; label: string }[] = [];
    Children.forEach(children, (child) => {
      if (isValidElement(child) && child.type === 'option') {
        const option = child as ReactElement<{ value?: string; children?: string }>;
        const optionValue = String(option.props.value ?? option.props.children ?? '');
        const label = String(option.props.children ?? optionValue);
        items.push({ key: optionValue, label });
      }
    });

    return (
      <HeroSelect
        ref={ref}
        variant="bordered"
        size="sm"
        radius="md"
        isDisabled={disabled}
        selectedKeys={value !== undefined ? [String(value)] : undefined}
        onSelectionChange={(keys) => {
          if (onChange) {
            const selected = Array.from(keys as Iterable<string>)[0] ?? '';
            const syntheticEvent = {
              target: { value: selected },
            } as React.ChangeEvent<HTMLSelectElement>;
            onChange(syntheticEvent);
          }
        }}
        className={className}
        aria-label={ariaLabel ?? 'Select'}
        disallowEmptySelection
      >
        {items.map((item) => (
          <SelectItem key={item.key}>{item.label}</SelectItem>
        ))}
      </HeroSelect>
    );
  },
);
Select.displayName = 'Select';
export default Select;
