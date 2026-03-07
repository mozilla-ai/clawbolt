import { render, screen } from '@testing-library/react';
import Button from './button';

describe('Button', () => {
  it('renders with default variant', () => {
    render(<Button>Click me</Button>);
    const btn = screen.getByRole('button', { name: 'Click me' });
    expect(btn).toBeInTheDocument();
    expect(btn.className).toContain('bg-primary');
  });

  it('renders secondary variant', () => {
    render(<Button variant="secondary">Secondary</Button>);
    const btn = screen.getByRole('button', { name: 'Secondary' });
    expect(btn.className).toContain('bg-card');
  });

  it('renders danger variant', () => {
    render(<Button variant="danger">Danger</Button>);
    const btn = screen.getByRole('button', { name: 'Danger' });
    expect(btn.className).toContain('bg-danger');
  });

  it('applies disabled state', () => {
    render(<Button disabled>Disabled</Button>);
    expect(screen.getByRole('button', { name: 'Disabled' })).toBeDisabled();
  });

  it('accepts custom className', () => {
    render(<Button className="mt-4">Custom</Button>);
    expect(screen.getByRole('button', { name: 'Custom' }).className).toContain('mt-4');
  });
});
