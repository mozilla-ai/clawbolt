import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';

export default function OAuthCallbackPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const status = params.get('status');
  const integration = params.get('integration') ?? 'unknown';
  const error = params.get('error');
  const [countdown, setCountdown] = useState(5);

  useEffect(() => {
    if (status !== 'success') return;
    const timer = setInterval(() => {
      setCountdown((c) => {
        if (c <= 1) {
          clearInterval(timer);
          window.location.href = '/app/tools';
          return 0;
        }
        return c - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [status]);

  if (status === 'success') {
    return (
      <div className="flex flex-col items-center justify-center min-h-dvh gap-4 px-4">
        <Card className="max-w-md w-full text-center p-6">
          <div className="text-4xl mb-4" aria-hidden>&#10003;</div>
          <h1 className="text-xl font-semibold font-display mb-2">Connected</h1>
          <p className="text-sm text-muted-foreground mb-4">
            <span className="capitalize">{integration}</span> has been connected successfully.
            You can now use {integration} tools.
          </p>
          <Button
            variant="primary"
            className="w-full sm:w-auto"
            onClick={() => navigate('/app/tools')}
          >
            Back to Tools
          </Button>
          <p className="text-xs text-muted-foreground mt-3">
            Redirecting in {countdown}s...
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-dvh gap-4 px-4">
      <Card className="max-w-md w-full text-center p-6">
        <div className="text-4xl mb-4" aria-hidden>&#10007;</div>
        <h1 className="text-xl font-semibold font-display mb-2">Connection Failed</h1>
        <p className="text-sm text-muted-foreground mb-4">
          {error || 'Something went wrong during authorization. Please try again.'}
        </p>
        <Button
          variant="primary"
          className="w-full sm:w-auto"
          onClick={() => navigate('/app/tools')}
        >
          Back to Tools
        </Button>
      </Card>
    </div>
  );
}
