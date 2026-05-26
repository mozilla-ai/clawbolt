import { describe, it, expect, vi, afterEach } from 'vitest';
import { compressImageIfNeeded, shouldCompressImage } from './imageCompression';

describe('shouldCompressImage', () => {
  it('returns true for a multi-MB JPEG', () => {
    const file = new File([new Uint8Array(2_000_000)], 'photo.jpg', { type: 'image/jpeg' });
    expect(shouldCompressImage(file)).toBe(true);
  });

  it('returns true for a multi-MB PNG', () => {
    const file = new File([new Uint8Array(2_000_000)], 'shot.png', { type: 'image/png' });
    expect(shouldCompressImage(file)).toBe(true);
  });

  it('returns true for HEIC (iPhone default)', () => {
    const file = new File([new Uint8Array(2_000_000)], 'IMG.heic', { type: 'image/heic' });
    expect(shouldCompressImage(file)).toBe(true);
  });

  it('returns false for tiny images below the threshold', () => {
    const file = new File([new Uint8Array(50_000)], 'icon.png', { type: 'image/png' });
    expect(shouldCompressImage(file)).toBe(false);
  });

  it('returns false for GIFs (would lose animation)', () => {
    const file = new File([new Uint8Array(2_000_000)], 'anim.gif', { type: 'image/gif' });
    expect(shouldCompressImage(file)).toBe(false);
  });

  it('returns false for SVGs', () => {
    const file = new File([new Uint8Array(2_000_000)], 'icon.svg', { type: 'image/svg+xml' });
    expect(shouldCompressImage(file)).toBe(false);
  });

  it('returns false for non-image files', () => {
    const file = new File([new Uint8Array(2_000_000)], 'doc.pdf', { type: 'application/pdf' });
    expect(shouldCompressImage(file)).toBe(false);
  });
});

describe('compressImageIfNeeded', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns the original file unchanged when compression is not warranted', async () => {
    const file = new File([new Uint8Array(50_000)], 'icon.png', { type: 'image/png' });
    const out = await compressImageIfNeeded(file);
    expect(out).toBe(file);
  });

  it('returns the original file if createImageBitmap throws (unsupported format)', async () => {
    vi.stubGlobal('createImageBitmap', () => {
      throw new TypeError('Unsupported source');
    });
    const file = new File([new Uint8Array(2_000_000)], 'IMG.heic', { type: 'image/heic' });
    const out = await compressImageIfNeeded(file);
    expect(out).toBe(file);
  });

  it('returns the original file if the re-encoded blob is larger', async () => {
    const fakeBitmap = { width: 100, height: 100, close: vi.fn() };
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue(fakeBitmap));
    // Spy toBlob to return a larger blob than the source.
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (
      this: HTMLCanvasElement,
      cb: BlobCallback,
    ) {
      cb(new Blob([new Uint8Array(5_000_000)], { type: 'image/jpeg' }));
    });
    const file = new File([new Uint8Array(2_000_000)], 'photo.jpg', { type: 'image/jpeg' });
    const out = await compressImageIfNeeded(file);
    expect(out).toBe(file);
  });

  it('returns a smaller JPEG when the round-trip beats the source', async () => {
    const fakeBitmap = { width: 4000, height: 3000, close: vi.fn() };
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue(fakeBitmap));
    // happy-dom's canvas getContext('2d') returns null by default; stub it
    // so the compression path isn't short-circuited.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (
      this: HTMLCanvasElement,
      cb: BlobCallback,
    ) {
      cb(new Blob([new Uint8Array(300_000)], { type: 'image/jpeg' }));
    });
    const file = new File([new Uint8Array(2_000_000)], 'photo.heic', { type: 'image/heic' });
    const out = await compressImageIfNeeded(file);
    expect(out).not.toBe(file);
    expect(out.name).toBe('photo.jpg');
    expect(out.type).toBe('image/jpeg');
    expect(out.size).toBeLessThan(file.size);
  });
});
