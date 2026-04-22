/** Timestamp helpers — Alfred is exacting. */

export function clock(iso?: string): string {
  if (!iso) return "";
  // ISO: "2026-04-22T14:32:07Z" → "14:32:07"
  const m = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : iso;
}

export function relativeMinutes(iso?: string, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const delta = Math.max(0, Math.round((now.getTime() - then) / 60_000));
  if (delta === 0) return "just now";
  if (delta === 1) return "1 min ago";
  if (delta < 60) return `${delta} min ago`;
  const hours = Math.floor(delta / 60);
  return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
}
