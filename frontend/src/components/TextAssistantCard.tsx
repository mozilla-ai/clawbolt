import { useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import Card from '@/components/ui/card';
import Button from '@/components/ui/button';
import { useIsMobile } from '@/hooks/useIsMobile';

/**
 * Card showing the Clawbolt phone number and QR code for texting the assistant.
 * Used on the GetStartedPage and ChannelsPage.
 *
 * On mobile the QR is useless because the phone is the device that would
 * scan it. Render a prominent "Open Messages" deep link instead and keep
 * the number copy-able. On desktop the QR is the natural cross-device
 * pairing affordance.
 */
export default function TextAssistantCard({
  fromNumber,
  subtitle,
  qrSize = 96,
}: {
  fromNumber: string;
  subtitle?: string;
  qrSize?: number;
}) {
  const isMobile = useIsMobile();
  // BlueBubbles can be configured with an iCloud email; an
  // ``sms:user@icloud.com`` deep-link is malformed and most OS handlers
  // reject it. Email shape gets a copy-the-address UX instead.
  const isEmail = fromNumber.includes('@');
  const smsUri = `sms:${fromNumber}`;
  const [copied, setCopied] = useState(false);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(fromNumber);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API may be blocked (insecure context, permissions). User
      // can still long-press the number to copy via the OS menu.
    }
  };

  if (isMobile) {
    return (
      <Card>
        <div className="flex flex-col gap-3">
          <div>
            <h3 className="text-sm font-medium mb-1">Text your assistant</h3>
            <p className="text-xs text-muted-foreground">
              {subtitle
                ?? (isEmail
                  ? 'Open Messages on your iCloud-connected device and send a note to this address.'
                  : 'Tap below to open Messages with this number prefilled.')}
            </p>
          </div>
          {!isEmail && (
            <a href={smsUri} className="block">
              <Button variant="primary" className="w-full">
                Open Messages
              </Button>
            </a>
          )}
          <button
            type="button"
            onClick={onCopy}
            className="text-center text-sm font-mono py-2 rounded-md hover:bg-secondary-hover focus:outline-none focus:ring-2 focus:ring-primary/30"
            aria-label={`Copy ${isEmail ? 'address' : 'phone number'} ${fromNumber}`}
          >
            {fromNumber}
            <span className="ml-2 text-xs text-muted-foreground">
              {copied ? '(copied)' : '(tap to copy)'}
            </span>
          </button>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-start gap-5">
        <div className="flex-1">
          <h3 className="text-sm font-medium mb-1">Text your assistant</h3>
          <p className="text-xs text-muted-foreground mb-3">
            {subtitle
              ?? (isEmail
                ? 'Send a note to this address from your iCloud-connected device.'
                : 'Scan the QR code from your phone, or text this number directly.')}
          </p>
          <button
            type="button"
            onClick={onCopy}
            className="font-mono text-lg font-medium hover:underline focus:outline-none focus:ring-2 focus:ring-primary/30 rounded"
            aria-label={`Copy ${isEmail ? 'address' : 'phone number'} ${fromNumber}`}
          >
            {fromNumber}
            <span className="ml-2 text-xs text-muted-foreground font-sans">
              {copied ? '(copied)' : '(click to copy)'}
            </span>
          </button>
        </div>
        {!isEmail && (
          <a href={smsUri} className="shrink-0">
            <QRCodeSVG value={smsUri} size={qrSize} />
          </a>
        )}
      </div>
    </Card>
  );
}
