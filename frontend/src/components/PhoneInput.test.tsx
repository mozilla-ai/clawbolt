import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import PhoneInput from './PhoneInput';

function Harness({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <PhoneInput value={value} onChange={setValue} label="Phone" />
      <div data-testid="emitted">{value}</div>
    </>
  );
}

describe('PhoneInput', () => {
  it('defaults the country picker to United States with empty value', () => {
    render(<Harness />);
    const picker = screen.getByRole('button', { name: /country code/i });
    expect(picker).toHaveTextContent(/united states/i);
    expect(screen.getByTestId('emitted')).toHaveTextContent('');
  });

  it('prepends +1 when the user types US digits', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const national = screen.getByRole('textbox');
    await user.type(national, '5551234567');
    expect(screen.getByTestId('emitted')).toHaveTextContent('+15551234567');
  });

  it('strips formatting characters but keeps them visible to the user', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const national = screen.getByRole('textbox');
    await user.type(national, '(555) 123-4567');
    expect(national).toHaveValue('(555) 123-4567');
    expect(screen.getByTestId('emitted')).toHaveTextContent('+15551234567');
  });

  it('splits an existing US E.164 value into picker + national digits', () => {
    render(<Harness initial="+15551234567" />);
    const picker = screen.getByRole('button', { name: /country code/i });
    expect(picker).toHaveTextContent(/united states/i);
    const national = screen.getByRole('textbox');
    expect(national).toHaveValue('5551234567');
  });

  it('emits empty string when the national field is empty regardless of picker', () => {
    render(<Harness />);
    expect(screen.getByTestId('emitted')).toHaveTextContent('');
  });

  it('renders the error message when error is set', () => {
    render(
      <PhoneInput
        value=""
        onChange={vi.fn()}
        label="Phone"
        error="Bad number"
        errorId="err-id"
      />,
    );
    const errorEl = screen.getByText('Bad number');
    expect(errorEl).toBeInTheDocument();
    expect(errorEl).toHaveAttribute('id', 'err-id');
  });

  it('renders help text when no error is set', () => {
    render(
      <PhoneInput
        value=""
        onChange={vi.fn()}
        label="Phone"
        helpText="Pick a country"
      />,
    );
    expect(screen.getByText('Pick a country')).toBeInTheDocument();
  });
});
