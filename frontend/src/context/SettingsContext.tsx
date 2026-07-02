import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getSystemSettings } from "../api/system";
import { formatDateOnly, formatDateTime, formatTimeOfDay } from "../utils/datetime";
import { useAuth } from "./AuthContext";

const TZ_STORAGE_KEY = "lightnvr_timezone";

interface SettingsContextValue {
  /** IANA display timezone, or "" to use the viewer's local timezone. */
  timezone: string;
  /** Re-fetch the timezone from the server (call after saving it in Settings). */
  reload: () => void;
  fmtDateTime: (v: string | number | Date) => string;
  fmtTime: (v: string | number | Date) => string;
  fmtDate: (v: string | number | Date) => string;
}

const SettingsContext = createContext<SettingsContextValue | undefined>(undefined);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  // Seed synchronously from localStorage so the first paint already uses the
  // right zone (SettingsPage mirrors the saved value there); the API fetch
  // below keeps it correct across devices where localStorage isn't primed yet.
  const [timezone, setTimezone] = useState<string>(() => localStorage.getItem(TZ_STORAGE_KEY) || "");

  const reload = useCallback(() => {
    getSystemSettings()
      .then((s) => {
        const tz = s.timezone || "";
        setTimezone(tz);
        if (tz) localStorage.setItem(TZ_STORAGE_KEY, tz);
        else localStorage.removeItem(TZ_STORAGE_KEY);
      })
      .catch(() => {
        /* not authenticated yet, or endpoint unreachable - keep the cached value */
      });
  }, []);

  // The settings endpoint requires auth, so fetch once the user is present.
  useEffect(() => {
    if (user) reload();
  }, [user, reload]);

  const value = useMemo<SettingsContextValue>(
    () => ({
      timezone,
      reload,
      fmtDateTime: (v) => formatDateTime(v, timezone),
      fmtTime: (v) => formatTimeOfDay(v, timezone),
      fmtDate: (v) => formatDateOnly(v, timezone),
    }),
    [timezone, reload],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within SettingsProvider");
  return ctx;
}
