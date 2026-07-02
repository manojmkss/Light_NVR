import { useEffect, useMemo, useState } from "react";
import { authenticatedEndpoints } from "../api/liveEndpoints";
import { getStreamStats, listCameras } from "../api/cameras";
import type { StreamStat } from "../api/types";
import { CameraGrid } from "../components/CameraGrid";
import { GridLayoutSwitcher } from "../components/GridLayoutSwitcher";
import { useSettings } from "../context/SettingsContext";
import { useGridLayout } from "../hooks/useGridLayout";
import { usePolling } from "../hooks/usePolling";

function LiveClock() {
  const { fmtDateTime } = useSettings();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span style={{ color: "var(--text-dim)", fontSize: 13, fontVariantNumeric: "tabular-nums" }}>
      {fmtDateTime(now)}
    </span>
  );
}

export function LiveViewPage() {
  const { data: cameras } = usePolling(listCameras, 15000);
  const { data: statsResp } = usePolling(getStreamStats, 2000);
  const { layout, layoutId, setLayoutId, page, setPage } = useGridLayout("lightnvr_layout");

  const enabledCameras = useMemo(() => (cameras ?? []).filter((c) => c.enabled), [cameras]);
  const stats: StreamStat[] = statsResp?.stats ?? [];

  return (
    <div className="page live-view-page">
      <div className="page-header">
        <h1>Live View</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <LiveClock />
          <GridLayoutSwitcher activeId={layoutId} onChange={setLayoutId} />
        </div>
      </div>

      {cameras === null ? (
        <div className="empty-state">Loading cameras…</div>
      ) : (
        <CameraGrid
          cameras={enabledCameras}
          stats={stats}
          endpoints={authenticatedEndpoints}
          layout={layout}
          page={page}
          onPageChange={setPage}
          orderStorageKey="lightnvr_live_order"
          emptyMessage="No enabled cameras yet. Add one from the Cameras page."
        />
      )}
    </div>
  );
}
