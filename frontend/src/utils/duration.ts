/**
 * Format a non-negative number of seconds as a zero-padded HH:MM:SS string.
 *
 * - Hours are not capped (e.g. 100+ hours render as "100:00:00").
 * - Minutes and seconds are always in the 00-59 range.
 * - Fractional/negative inputs are floored / clamped to a sane value.
 */
export function formatDuration(totalSeconds: number): string {
  const safe = Number.isFinite(totalSeconds) ? totalSeconds : 0;
  const total = Math.max(0, Math.floor(safe));

  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;

  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
}
