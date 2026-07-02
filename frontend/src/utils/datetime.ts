// Timezone-aware timestamp helpers.
//
// The NVR stores every timestamp in UTC (the server's own reference clock) and
// the API now emits them with an explicit `Z`. These helpers render / reason
// about those instants in a *chosen* timezone - the NVR's configured display
// timezone - so times look identical no matter where the viewer is. Passing an
// empty/undefined `tz` falls back to the browser's local timezone.

type TimeInput = string | number | Date;

// Constructing an Intl.DateTimeFormat is comparatively expensive; cache by
// (timezone + options) since the same handful of formats are reused constantly.
const cache = new Map<string, Intl.DateTimeFormat>();

function fmtr(tz: string | undefined, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${tz ?? ""}|${JSON.stringify(opts)}`;
  let f = cache.get(key);
  if (!f) {
    try {
      f = new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz || undefined });
    } catch {
      // An invalid IANA name would throw - degrade to browser-local instead of
      // breaking every timestamp on the page.
      f = new Intl.DateTimeFormat(undefined, opts);
    }
    cache.set(key, f);
  }
  return f;
}

export function formatDateTime(v: TimeInput, tz?: string): string {
  return fmtr(tz, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(v));
}

export function formatTimeOfDay(v: TimeInput, tz?: string): string {
  return fmtr(tz, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(v));
}

export function formatDateOnly(v: TimeInput, tz?: string): string {
  return fmtr(tz, { dateStyle: "medium" }).format(new Date(v));
}

export interface WallClock {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number; // 0-23
  minute: number;
  second: number;
}

// The wall-clock reading of an instant in `tz` (24h). Used by the timeline and
// the playback clock so their labels match the chosen timezone.
export function partsInTz(ms: number, tz?: string): WallClock {
  const parts = fmtr(tz, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(ms));
  const m: Record<string, number> = {};
  for (const p of parts) {
    if (p.type !== "literal") m[p.type] = Number(p.value);
  }
  return {
    year: m.year,
    month: m.month,
    day: m.day,
    hour: m.hour,
    minute: m.minute,
    second: m.second,
  };
}

// How many milliseconds `tz` is ahead of UTC at instant `ms` (negative if behind).
export function tzOffsetMs(ms: number, tz?: string): number {
  if (!tz) return -new Date(ms).getTimezoneOffset() * 60000;
  const p = partsInTz(ms, tz);
  const asUTC = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUTC - ms;
}

// Epoch ms of local midnight of `dateStr` (YYYY-MM-DD) in `tz`. One refinement
// pass handles DST-transition days; fixed-offset zones (e.g. IST) are exact.
export function startOfDayInTz(dateStr: string, tz?: string): number {
  const [y, mo, d] = dateStr.split("-").map(Number);
  const guessUTC = Date.UTC(y, mo - 1, d, 0, 0, 0);
  let off = tzOffsetMs(guessUTC, tz);
  let epoch = guessUTC - off;
  off = tzOffsetMs(epoch, tz);
  epoch = guessUTC - off;
  return epoch;
}

// Today's date (YYYY-MM-DD) as seen in `tz`.
export function todayInTz(tz?: string): string {
  const p = partsInTz(Date.now(), tz);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

// Convert a wall-clock date (YYYY-MM-DD) + time (HH:MM[:SS]) in `tz` to a UTC
// ISO string, for sending as an API filter bound. DST shifts within a single
// day are negligible for filtering, so this just offsets from local midnight.
export function localDateTimeToUtcIso(dateStr: string, timeStr: string, tz?: string): string {
  const startMs = startOfDayInTz(dateStr, tz);
  const [h = 0, m = 0, s = 0] = timeStr.split(":").map(Number);
  const epoch = startMs + (h * 3600 + m * 60 + s) * 1000;
  return new Date(epoch).toISOString();
}

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

// Compact 24h HH:MM:SS clock in `tz`.
export function clock24(ms: number, tz?: string, withSeconds = true): string {
  const p = partsInTz(ms, tz);
  return withSeconds
    ? `${pad(p.hour)}:${pad(p.minute)}:${pad(p.second)}`
    : `${pad(p.hour)}:${pad(p.minute)}`;
}
