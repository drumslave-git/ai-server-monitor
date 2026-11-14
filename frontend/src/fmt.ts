export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.abs(n);
  let i = 0;
  while (size >= 1000 && i < units.length - 1) {
    size /= 1000;
    i += 1;
  }
  const text = i === 0 ? size.toFixed(0) : size >= 100 ? size.toFixed(0) : size.toFixed(1);
  return `${n < 0 ? "-" : ""}${text} ${units[i]}`;
}

export const rate = (n: number | null | undefined) => (n === null || n === undefined ? "—" : `${bytes(n)}/s`);

export function pct(n: number | null | undefined, digits = 0): string {
  return n === null || n === undefined ? "—" : `${n.toFixed(n < 10 ? Math.max(digits, 1) : digits)}%`;
}

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function until(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "—";
  const s = Math.round(ts - now);
  if (s <= 0) return "expired";
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `in ${Math.round(s / 3600)}h`;
  return `in ${Math.round(s / 86400)}d`;
}

export function duration(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
