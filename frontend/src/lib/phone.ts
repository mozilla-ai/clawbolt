// US-first phone number handling. Most clawbolt users today are US-based
// trades professionals; the auto-prefix shaves a real friction point off
// the onboarding flow. Anyone outside the US can enter their own country
// code by typing a leading "+" first; we never override that. If demand
// for non-US users grows enough to justify a country selector, replace
// the auto-prefix with one. Tracking issue: TBD.

const E164 = /^\+[1-9]\d{6,14}$/;

/** Strip whitespace, dashes, parens, and other formatting characters. */
function stripFormatting(raw: string): string {
  return raw.replace(/[\s\-().]/g, '');
}

/** Normalize user-entered text to E.164.
 *
 * Rules:
 * - Leading "+": treat as a complete international number; only strip formatting.
 * - Otherwise: strip formatting and prepend "+1". A leading "1" is dropped
 *   ONLY when the digit string has 11 characters (so "15551234567" becomes
 *   "+15551234567"); a 10-character bare-digit input is taken at face value
 *   so "1234567890" produces a clearly-malformed "+11234567890" that the
 *   E.164 validator catches, instead of silently shifting it to "+1234567890".
 * - Empty input returns empty string (callers gate save).
 */
export function normalizeUsPhone(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return '';
  if (trimmed.startsWith('+')) return stripFormatting(trimmed);
  const digits = stripFormatting(trimmed);
  const body = digits.length === 11 && digits.startsWith('1') ? digits.slice(1) : digits;
  return `+1${body}`;
}

/** True iff value is a syntactically valid E.164 number. */
export function isValidE164(value: string): boolean {
  return E164.test(value);
}

/** User-facing error string for an invalid phone number. Generic, no PII. */
export const PHONE_FORMAT_ERROR =
  'Use a phone number like +15551234567 (10 digits, no spaces).';
