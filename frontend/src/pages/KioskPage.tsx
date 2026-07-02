import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { createKioskEndpoints, getKioskStreamStats, getPublicKioskView } from "../api/kiosk";
import type { KioskPublicView, StreamStat } from "../api/types";
import { CameraGrid } from "../components/CameraGrid";
import { GridLayoutSwitcher } from "../components/GridLayoutSwitcher";
import { useGridLayout } from "../hooks/useGridLayout";

// Long-running kiosk tabs can drift into a bad state over days/weeks (memory
// growth, a stuck media decoder, etc.) - a scheduled full reload is the
// standard, simple fix digital-signage players use, cheaper than trying to
// detect and recover from every possible failure mode in JS.
const FULL_RELOAD_INTERVAL_MS = 6 * 60 * 60 * 1000;
const RECONNECT_INTERVAL_MS = 10000;
const STATS_POLL_MS = 2000;

// Mounted only once `view` has actually loaded, so useGridLayout's one-time
// localStorage seed correctly falls back to the admin's configured default
// layout on a device that's never customised it - the parent can't do this
// itself since it re-renders (rather than remounts) every time the 10s poll
// below refreshes `view`.
function KioskContent({ token, view }: { token: string; view: KioskPublicView }) {
  const { layout, layoutId, setLayoutId, page, setPage } = useGridLayout(`lightnvr_kiosk_layout_${token}`);
  const [stats, setStats] = useState<StreamStat[]>([]);
  const endpoints = useMemo(() => createKioskEndpoints(token), [token]);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getKioskStreamStats(token)
        .then((r) => {
          if (!cancelled) setStats(r.stats);
        })
        .catch(() => {});
    load();
    const t = setInterval(load, STATS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [token]);

  return (
    <div className="kiosk-page live-view-page">
      <div className="page-header kiosk-header">
        <h1>{view.name}</h1>
        <GridLayoutSwitcher activeId={layoutId} onChange={setLayoutId} />
      </div>
      <CameraGrid
        cameras={view.cameras}
        stats={stats}
        endpoints={endpoints}
        layout={layout}
        page={page}
        onPageChange={setPage}
        orderStorageKey={`lightnvr_kiosk_order_${token}`}
        emptyMessage="No cameras configured for this display."
      />
    </div>
  );
}

export function KioskPage() {
  const { token } = useParams<{ token: string }>();
  const [view, setView] = useState<KioskPublicView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    const load = () => {
      getPublicKioskView(token)
        .then((v) => {
          if (cancelled) return;
          setView(v);
          setError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          setError((err as Error).message);
        })
        .finally(() => {
          if (!cancelled) timer = setTimeout(load, RECONNECT_INTERVAL_MS);
        });
    };
    load();

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [token]);

  // Prevents the tablet/monitor from sleeping. Not supported on every
  // browser (notably older iOS Safari) - fails silently there since there's
  // no good fallback short of the device's own display settings.
  const wakeLockRef = useRef<WakeLockSentinel | null>(null);
  useEffect(() => {
    const acquire = async () => {
      try {
        wakeLockRef.current = await navigator.wakeLock?.request("screen");
      } catch {
        // unsupported or denied - nothing more to do here
      }
    };
    acquire();

    const handleVisibility = () => {
      // Wake locks are released automatically when the tab is hidden -
      // re-acquire once it's visible again (e.g. after a brief OS overlay).
      if (document.visibilityState === "visible" && !wakeLockRef.current) acquire();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => window.location.reload(), FULL_RELOAD_INTERVAL_MS);
    return () => clearTimeout(timer);
  }, []);

  if (!token) return null;

  if (error && !view) {
    return (
      <div className="kiosk-page kiosk-message">
        <p>{error}</p>
      </div>
    );
  }

  if (!view) {
    return (
      <div className="kiosk-page kiosk-message">
        <p>Loading...</p>
      </div>
    );
  }

  return <KioskContent token={token} view={view} />;
}
