import { useEffect, useRef, useState } from 'react';
import Input from '@/components/ui/input';
import Select from '@/components/ui/select';
import Field from '@/components/ui/field';

interface Country {
  /** Stable key used in the picker. */
  code: string;
  /** Human label shown in the dropdown, including the dial code. */
  name: string;
  /** E.164 dial code, e.g. "+1". Empty for the "Other" escape hatch. */
  dialCode: string;
}

// US first. Picker stays short and US-centric (most users today are US
// trades) with "Other" for the long tail. Add a country here to expand.
const OTHER: Country = { code: 'OTHER', name: 'Other', dialCode: '' };

const COUNTRIES: readonly Country[] = [
  { code: 'US', name: 'United States (+1)', dialCode: '+1' },
  { code: 'CA', name: 'Canada (+1)', dialCode: '+1' },
  { code: 'MX', name: 'Mexico (+52)', dialCode: '+52' },
  { code: 'GB', name: 'United Kingdom (+44)', dialCode: '+44' },
  { code: 'IE', name: 'Ireland (+353)', dialCode: '+353' },
  { code: 'AU', name: 'Australia (+61)', dialCode: '+61' },
  { code: 'NZ', name: 'New Zealand (+64)', dialCode: '+64' },
  { code: 'DE', name: 'Germany (+49)', dialCode: '+49' },
  { code: 'FR', name: 'France (+33)', dialCode: '+33' },
  OTHER,
] as const;

// US is the default; the picker is rendered with it preselected. A copy
// lives here so TypeScript can narrow ``COUNTRIES[0]`` (which it sees as
// possibly ``undefined``) without an unsafe assertion.
const DEFAULT_COUNTRY: Country = { code: 'US', name: 'United States (+1)', dialCode: '+1' };

function findCountry(code: string): Country {
  return COUNTRIES.find((c) => c.code === code) ?? DEFAULT_COUNTRY;
}

// US wins ties at +1 over CA because it sits first in COUNTRIES; the sort
// here is stable for equal-length dial codes.
function matchByDialCode(value: string): Country | undefined {
  if (!value.startsWith('+')) return undefined;
  return [...COUNTRIES]
    .filter((c) => c.dialCode)
    .sort((a, b) => b.dialCode.length - a.dialCode.length)
    .find((c) => value.startsWith(c.dialCode));
}

function deriveFromValue(value: string): { country: Country; national: string } {
  if (!value) return { country: DEFAULT_COUNTRY, national: '' };
  const match = matchByDialCode(value);
  if (match) return { country: match, national: value.slice(match.dialCode.length) };
  if (value.startsWith('+')) return { country: OTHER, national: value };
  return { country: DEFAULT_COUNTRY, national: value };
}

/** Compose a canonical E.164-shaped value from picker + national input. */
function compose(country: Country, national: string): string {
  if (country.code === 'OTHER') {
    const trimmed = national.trim();
    if (!trimmed) return '';
    const stripped = trimmed.replace(/[\s\-().]/g, '');
    return stripped.startsWith('+') ? stripped : `+${stripped.replace(/^\+*/, '')}`;
  }
  const digits = national.replace(/\D/g, '');
  if (!digits) return '';
  return country.dialCode + digits;
}

interface PhoneInputProps {
  /** Canonical E.164 value, e.g. "+15551234567". Empty string means unset. */
  value: string;
  /** Called with the new canonical value on every keystroke or country change. */
  onChange: (value: string) => void;
  label?: string;
  placeholder?: string;
  /** When set, the helper text is replaced with this error and the input is flagged. */
  error?: string | null;
  helpText?: string;
  /** Optional id for the error/help text; used by aria-describedby. */
  errorId?: string;
}

/** Phone-number entry with a country picker that defaults to US (+1).
 *
 * The parent owns the canonical E.164 value and receives updates via
 * ``onChange``. The component keeps a local copy of the typed national
 * digits so the user's in-progress formatting (parens, dashes) is not
 * stripped on every keystroke; ``compose`` strips formatting only when
 * emitting upward. External updates to ``value`` (e.g. async data loads)
 * re-sync the local state.
 */
export default function PhoneInput({
  value,
  onChange,
  label = 'Phone number',
  placeholder = '(555) 123-4567',
  error,
  helpText,
  errorId,
}: PhoneInputProps) {
  const initial = deriveFromValue(value);
  const [country, setCountry] = useState<Country>(initial.country);
  const [national, setNational] = useState<string>(initial.national);
  const lastEmittedRef = useRef<string>(value);

  // Re-sync from external value when the parent supplies one we did not
  // emit ourselves (e.g. async data load). Skipping when value matches the
  // last emission keeps the component from clobbering the user's typed
  // formatting on echo.
  useEffect(() => {
    if (value === lastEmittedRef.current) return;
    if (value === compose(country, national)) return;
    const next = deriveFromValue(value);
    setCountry(next.country);
    setNational(next.national);
    lastEmittedRef.current = value;
  }, [value, country, national]);

  const emit = (c: Country, n: string): void => {
    const next = compose(c, n);
    lastEmittedRef.current = next;
    onChange(next);
  };

  const handleCountryChange = (newCode: string): void => {
    const next = findCountry(newCode);
    setCountry(next);
    emit(next, national);
  };

  const handleNationalChange = (raw: string): void => {
    setNational(raw);
    emit(country, raw);
  };

  const isOther = country.code === 'OTHER';

  return (
    <Field label={label}>
      <div className="flex gap-2 items-start">
        <div className="w-44 shrink-0">
          <Select
            value={country.code}
            onChange={(e) => handleCountryChange(e.target.value)}
            aria-label="Country code"
          >
            {COUNTRIES.map((c) => (
              <option key={c.code} value={c.code}>{c.name}</option>
            ))}
          </Select>
        </div>
        <div className="flex-1 min-w-0">
          <Input
            value={national}
            onChange={(e) => handleNationalChange(e.target.value)}
            placeholder={isOther ? '+447911123456' : placeholder}
            inputMode="tel"
            autoComplete={isOther ? 'tel' : 'tel-national'}
            aria-invalid={error ? true : undefined}
            aria-describedby={error && errorId ? errorId : undefined}
          />
        </div>
      </div>
      {error ? (
        <p id={errorId} className="text-xs text-danger mt-1">{error}</p>
      ) : helpText ? (
        <p className="text-xs text-muted-foreground mt-1">{helpText}</p>
      ) : null}
    </Field>
  );
}
