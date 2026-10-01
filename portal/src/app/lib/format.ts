import type { Timestamp } from "./types";

/** Epoch seconds, epoch milliseconds or an ISO string, as a Date (or null). */
export function toDate(value: Timestamp): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    // The store keeps seconds; tolerate milliseconds too.
    const ms = value > 1e12 ? value : value * 1000;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const n = Number(value);
  if (!Number.isNaN(n) && value.trim() !== "") return toDate(n);
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** The document title: the page's own name, then the brand, never the same name twice. */
export function pageTitle(name: string, brand: string): string {
  const own = name.trim();
  if (!own || own.toLowerCase() === brand.trim().toLowerCase()) return brand;
  return `${own} · ${brand}`;
}

const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

export type DateGroup = "today" | "yesterday" | "week" | "older";

export function dateGroup(value: Timestamp, now = new Date()): DateGroup {
  const d = toDate(value);
  if (!d) return "older";
  const days = Math.round((dayStart(now) - dayStart(d)) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return "week";
  return "older";
}

export function formatDate(value: Timestamp, options: Intl.DateTimeFormatOptions = { dateStyle: "medium" }): string {
  const d = toDate(value);
  if (!d) return "";
  try {
    return new Intl.DateTimeFormat(undefined, options).format(d);
  } catch {
    return d.toDateString();
  }
}

export function formatDateTime(value: Timestamp): string {
  return formatDate(value, { dateStyle: "medium", timeStyle: "short" });
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value >= 10 || Number.isInteger(value) ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** "hubzoid-guide" reads as "Hubzoid Guide"; real names are left alone. */
export function displayName(name: string | null | undefined): string {
  const value = (name || "").trim();
  if (!value) return "";
  if (/^[a-z0-9]+([-_][a-z0-9]+)+$/.test(value) || /^[a-z][a-z0-9]*$/.test(value))
    return value
      .split(/[-_]/)
      .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
      .join(" ");
  return value;
}

/** One or two letters for an avatar. */
export function initials(name: string | null | undefined): string {
  const words = displayName(name).replace(/@.*$/, "").split(/[\s._-]+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

export function fileNameFromUrl(href: string): string {
  try {
    const url = new URL(href, location.origin);
    const last = url.pathname.split("/").filter(Boolean).pop() || href;
    return decodeURIComponent(last);
  } catch {
    return href;
  }
}
