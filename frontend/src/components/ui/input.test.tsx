import { render } from '@testing-library/react';
import Input from './input';

describe('Input', () => {
  it('renders with placeholder', () => {
    const { getByPlaceholderText } = render(<Input placeholder="Type here" />);
    expect(getByPlaceholderText('Type here')).toBeInTheDocument();
  });

  it('forwards inputMode to the underlying input element', () => {
    const { container } = render(<Input inputMode="tel" placeholder="phone" />);
    const input = container.querySelector('input');
    expect(input).toHaveAttribute('inputmode', 'tel');
  });

  it('forwards inputMode="numeric" to the underlying input element', () => {
    const { container } = render(<Input inputMode="numeric" placeholder="number" />);
    const input = container.querySelector('input');
    expect(input).toHaveAttribute('inputmode', 'numeric');
  });
});
