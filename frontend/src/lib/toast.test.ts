import { toast, _resetActiveToasts } from './toast';

const mockAddToast = vi.fn();

vi.mock('@heroui/toast', () => ({
  addToast: (...args: unknown[]) => mockAddToast(...args),
}));

beforeEach(() => {
  vi.useFakeTimers();
  mockAddToast.mockClear();
  _resetActiveToasts();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('toast deduplication', () => {
  it('shows a success toast', () => {
    toast.success('Saved');
    expect(mockAddToast).toHaveBeenCalledWith({
      title: 'Saved',
      color: 'success',
      timeout: 4000,
    });
  });

  it('shows an error toast', () => {
    toast.error('Network error');
    expect(mockAddToast).toHaveBeenCalledWith({
      title: 'Network error',
      color: 'danger',
      timeout: 8000,
    });
  });

  it('suppresses duplicate success toasts', () => {
    toast.success('Saved');
    toast.success('Saved');
    toast.success('Saved');
    expect(mockAddToast).toHaveBeenCalledTimes(1);
  });

  it('suppresses duplicate error toasts', () => {
    toast.error('Oops');
    toast.error('Oops');
    expect(mockAddToast).toHaveBeenCalledTimes(1);
  });

  it('allows the same message again after the success duration elapses', () => {
    toast.success('Saved');
    expect(mockAddToast).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(4000);

    toast.success('Saved');
    expect(mockAddToast).toHaveBeenCalledTimes(2);
  });

  it('allows the same message again after the error duration elapses', () => {
    toast.error('Oops');
    expect(mockAddToast).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(8000);

    toast.error('Oops');
    expect(mockAddToast).toHaveBeenCalledTimes(2);
  });

  it('still suppresses before the duration elapses', () => {
    toast.success('Saved');
    vi.advanceTimersByTime(3999);
    toast.success('Saved');
    expect(mockAddToast).toHaveBeenCalledTimes(1);
  });

  it('allows different messages at the same time', () => {
    toast.success('Saved');
    toast.success('Updated');
    expect(mockAddToast).toHaveBeenCalledTimes(2);
  });

  it('treats success and error with the same text as different toasts', () => {
    toast.success('Done');
    toast.error('Done');
    expect(mockAddToast).toHaveBeenCalledTimes(2);
  });
});
