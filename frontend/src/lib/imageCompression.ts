/**
 * Client-side image compression before upload.
 *
 * Phone photos are routinely 5-10 MB at full resolution, but the agent's
 * vision pipeline downsamples them to ~1600px anyway (see backend
 * media/download.py compression step). Shipping the original over the
 * wire is pure overhead, and on a typical home connection it dominates
 * end-to-end latency. Resizing to ``MAX_DIMENSION`` and re-encoding as
 * JPEG at ``JPEG_QUALITY`` typically cuts size by 5-10x with no
 * visible quality loss at the size the user sees in the chat. #1368.
 *
 * The compressed file is always JPEG (we re-encode through a canvas),
 * regardless of the input format. EXIF is stripped as a side effect,
 * which is desirable for a chat upload anyway (drops GPS, camera serial,
 * etc.).
 */

const MAX_DIMENSION = 1600;
const JPEG_QUALITY = 0.85;
// Don't bother compressing files this small: the canvas round-trip would
// likely produce a similar or larger blob and we'd add latency for nothing.
const MIN_COMPRESS_BYTES = 200 * 1024;

const COMPRESSIBLE_TYPES = new Set([
  'image/jpeg',
  'image/jpg',
  'image/png',
  'image/webp',
  'image/heic',
  'image/heif',
]);

/** Return true when the file is worth attempting to compress. */
export function shouldCompressImage(file: File): boolean {
  if (!COMPRESSIBLE_TYPES.has(file.type)) return false;
  if (file.size < MIN_COMPRESS_BYTES) return false;
  return true;
}

/**
 * Compress *file* if it's an image worth shrinking; otherwise return the
 * original. Never throws: any failure (unsupported format, OOM, canvas
 * blocked, …) returns the original so the upload still goes through.
 */
export async function compressImageIfNeeded(file: File): Promise<File> {
  if (!shouldCompressImage(file)) return file;
  try {
    return await _compressImage(file);
  } catch {
    return file;
  }
}

async function _compressImage(file: File): Promise<File> {
  const bitmap = await createImageBitmap(file);
  try {
    const { width, height } = bitmap;
    const maxSide = Math.max(width, height);
    // Already at or below the target resolution: only worth re-encoding if
    // a high-quality JPEG round-trip noticeably beats the source bytes.
    // For PNG screenshots and HEIC this can still help, so we don't bail
    // early on dimensions alone.
    const scale = maxSide > MAX_DIMENSION ? MAX_DIMENSION / maxSide : 1;
    const targetW = Math.max(1, Math.round(width * scale));
    const targetH = Math.max(1, Math.round(height * scale));

    const canvas = document.createElement('canvas');
    canvas.width = targetW;
    canvas.height = targetH;
    const ctx = canvas.getContext('2d');
    if (!ctx) return file;
    ctx.drawImage(bitmap, 0, 0, targetW, targetH);

    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY);
    });
    if (!blob) return file;
    // Never substitute a larger file: simple PNGs of solid color and
    // already-aggressively-compressed JPEGs sometimes round-trip larger
    // than the source.
    if (blob.size >= file.size) return file;

    const baseName = file.name.replace(/\.[^.]+$/, '');
    return new File([blob], `${baseName}.jpg`, { type: 'image/jpeg' });
  } finally {
    bitmap.close();
  }
}
