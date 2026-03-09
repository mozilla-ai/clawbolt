import { Spinner as HeroSpinner } from '@heroui/spinner';

export default function Spinner({ className }: { className?: string }) {
  return (
    <HeroSpinner
      color="primary"
      size="md"
      className={className}
      aria-label="Loading"
    />
  );
}
