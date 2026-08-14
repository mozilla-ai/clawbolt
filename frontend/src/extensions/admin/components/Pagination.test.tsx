import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import Pagination from './Pagination';

describe('Pagination', () => {
  it('disables Prev on first page and Next on last page', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <Pagination page={0} total={100} pageSize={50} onChange={onChange} />,
    );
    expect(screen.getByRole('button', { name: 'Prev' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).not.toBeDisabled();

    rerender(<Pagination page={1} total={100} pageSize={50} onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'Prev' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('renders the count label and total page count', () => {
    render(
      <Pagination
        page={0}
        total={120}
        pageSize={50}
        onChange={() => {}}
        countLabel="120 users"
      />,
    );
    expect(screen.getByText('120 users')).toBeInTheDocument();
    // 120 / 50 = 3 pages
    expect(screen.getByText(/Page 1 of 3/)).toBeInTheDocument();
  });

  it('calls onChange with the next/prev page', () => {
    const onChange = vi.fn();
    render(<Pagination page={1} total={300} pageSize={50} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(onChange).toHaveBeenCalledWith(2);
    fireEvent.click(screen.getByRole('button', { name: 'Prev' }));
    expect(onChange).toHaveBeenCalledWith(0);
  });

  it('renders a Go-to form only when showGoTo and totalPages > 3', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <Pagination page={0} total={100} pageSize={50} onChange={onChange} showGoTo />,
    );
    // 2 pages: no go-to form
    expect(screen.queryByRole('button', { name: 'Go' })).not.toBeInTheDocument();

    rerender(<Pagination page={0} total={300} pageSize={50} onChange={onChange} showGoTo />);
    expect(screen.getByRole('button', { name: 'Go' })).toBeInTheDocument();
  });

  it('jumps to a valid 1-indexed page from the Go-to form', () => {
    const onChange = vi.fn();
    render(<Pagination page={0} total={300} pageSize={50} onChange={onChange} showGoTo />);
    const input = screen.getByPlaceholderText('Go to') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Go' }));
    // 4 -> page index 3 (0-indexed)
    expect(onChange).toHaveBeenCalledWith(3);
  });

  it('ignores out-of-range Go-to values', () => {
    const onChange = vi.fn();
    // 200 / 50 = 4 pages, > 3 threshold so the form renders
    render(<Pagination page={0} total={200} pageSize={50} onChange={onChange} showGoTo />);
    const input = screen.getByPlaceholderText('Go to') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '99' } });
    fireEvent.click(screen.getByRole('button', { name: 'Go' }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
