import { render, screen } from '@testing-library/react';
import Button from './button';

describe('Button', () => {
  it('renders with default variant', () => {
    render(<Button>Click me</Button>);
    const btn = screen.getByRole('button', { name: 'Click me' });
    expect(btn).toBeInTheDocument();
  });

  it('renders secondary variant', () => {
    render(<Button variant="secondary">Secondary</Button>);
    const btn = screen.getByRole('button', { name: 'Secondary' });
    expect(btn).toBeInTheDocument();
  });

  it('renders danger variant', () => {
    render(<Button variant="danger">Danger</Button>);
    const btn = screen.getByRole('button', { name: 'Danger' });
    expect(btn).toBeInTheDocument();
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

describe('Button isLoading', () => {
  it('renders spinner when isLoading is true', () => {
    const { container } = render(<Button isLoading>Save</Button>);
    // HeroUI adds aria-label="Loading" to the spinner element
    expect(container.querySelector('[aria-label="Loading"]')).toBeInTheDocument();
  });

  it('disables the button when isLoading', () => {
    render(<Button isLoading>Save</Button>);
    // HeroUI prepends 'Loading' from the spinner's aria-label
    expect(screen.getByRole('button', { name: 'Loading Save' })).toBeDisabled();
  });

  it('does not render spinner when isLoading is false', () => {
    const { container } = render(<Button>Save</Button>);
    expect(container.querySelector('[aria-label="Loading"]')).not.toBeInTheDocument();
  });
});
