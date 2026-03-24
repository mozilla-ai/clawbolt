import { QRCodeSVG } from 'qrcode.react';
import Card from '@/components/ui/card';

/**
 * Card showing the Clawbolt phone number and QR code for texting the assistant.
 * Used on both the GetStartedPage and ChannelsPage.
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
  const smsUri = `sms:${fromNumber}`;

  return (
    <Card>
      <div className="flex items-start gap-5">
        <div className="flex-1">
          <h3 className="text-sm font-medium mb-1">Text your assistant</h3>
          <p className="text-xs text-muted-foreground mb-3">
            {subtitle ?? 'Scan the QR code or text this number from your phone.'}
          </p>
          <p className="font-mono text-lg font-medium">{fromNumber}</p>
        </div>
        <a href={smsUri} className="shrink-0">
          <QRCodeSVG value={smsUri} size={qrSize} />
        </a>
      </div>
    </Card>
  );
}
