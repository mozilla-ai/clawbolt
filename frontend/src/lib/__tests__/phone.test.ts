import { describe, it, expect } from 'vitest';
import { normalizeUsPhone, isValidE164, PHONE_FORMAT_ERROR } from '../phone';

describe('normalizeUsPhone', () => {
  it('returns empty for empty input', () => {
    expect(normalizeUsPhone('')).toBe('');
    expect(normalizeUsPhone('   ')).toBe('');
  });

  it('prepends +1 to bare 10 digits', () => {
    expect(normalizeUsPhone('5551234567')).toBe('+15551234567');
  });

  it('strips formatting from US numbers', () => {
    expect(normalizeUsPhone('(555) 123-4567')).toBe('+15551234567');
    expect(normalizeUsPhone('555.123.4567')).toBe('+15551234567');
    expect(normalizeUsPhone('555-123-4567')).toBe('+15551234567');
  });

  it('drops a leading 1 before prepending +1', () => {
    expect(normalizeUsPhone('1-555-123-4567')).toBe('+15551234567');
    expect(normalizeUsPhone('15551234567')).toBe('+15551234567');
  });

  it('respects an explicit + prefix without prepending US', () => {
    expect(normalizeUsPhone('+447911123456')).toBe('+447911123456');
    expect(normalizeUsPhone('+44 7911 123456')).toBe('+447911123456');
  });

  it('keeps a +1 prefix as-is when the user enters it themselves', () => {
    expect(normalizeUsPhone('+15551234567')).toBe('+15551234567');
    expect(normalizeUsPhone('+1 555 123 4567')).toBe('+15551234567');
  });
});

describe('isValidE164', () => {
  it('accepts valid E.164 numbers', () => {
    expect(isValidE164('+15551234567')).toBe(true);
    expect(isValidE164('+447911123456')).toBe(true);
  });

  it('rejects malformed numbers', () => {
    expect(isValidE164('5551234567')).toBe(false);
    expect(isValidE164('+0123')).toBe(false);
    expect(isValidE164('+1abc1234567')).toBe(false);
    expect(isValidE164('')).toBe(false);
  });
});

describe('PHONE_FORMAT_ERROR', () => {
  it('is a non-empty user-facing string', () => {
    expect(typeof PHONE_FORMAT_ERROR).toBe('string');
    expect(PHONE_FORMAT_ERROR.length).toBeGreaterThan(0);
  });
});
