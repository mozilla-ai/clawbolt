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
 * - Otherwise: strip formatting, drop a leading "1" if present (so users can
 *   type "5551234567" or "1-555-123-4567" or "+1 555 123 4567" interchangeably),
 *   and prepend "+1".
 * - Empty input returns empty string (callers gate save).
 */
export function normalizeUsPhone(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return '';
  if (trimmed.startsWith('+')) return stripFormatting(trimmed);
  const digits = stripFormatting(trimmed).replace(/^1/, '');
  return `+1${digits}`;
}

/** True iff value is a syntactically valid E.164 number. */
export function isValidE164(value: string): boolean {
  return E164.test(value);
}

/** User-facing error string for an invalid phone number. Generic, no PII. */
export const PHONE_FORMAT_ERROR =
  'Use a phone number like +15551234567 (10 digits, no spaces).';
