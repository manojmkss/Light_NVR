import { useState } from "react";
import type { CameraTileEndpoints, TileCamera } from "../api/liveEndpoints";
import type { StreamStat } from "../api/types";
import type { LayoutDef } from "../hooks/useGridLayout";
import { useOrderedList } from "../hooks/useOrderedList";
import { CameraTile } from "./CameraTile";
import { DraggableTile } from "./DraggableTile";

interface CameraGridProps {
  cameras: TileCamera[];
  stats: StreamStat[];
  endpoints: CameraTileEndpoints;
  layout: LayoutDef;
  page: number;
  onPageChange: (page: number) => void;
  /** localStorage key for the saved drag-to-rearrange order - scope this per
   *  surface (Live View vs. a specific kiosk token) so each remembers its own. */
  orderStorageKey: string;
  emptyMessage?: string;
}

// The dynamic grid engine shared by Live View and Kiosk: adaptive stream
// quality per cell, drag-to-rearrange with a saved order, and pagination when
// there are more cameras than the current layout holds.
export function CameraGrid({
  cameras,
  stats,
  endpoints,
  layout,
  page,
  onPageChange,
  orderStorageKey,
  emptyMessage,
}: CameraGridProps) {
  const [draggedId, setDraggedId] = useState<number | null>(null);
  const { ordered, moveBefore } = useOrderedList(orderStorageKey, cameras);

  const pageCount = Math.max(1, Math.ceil(ordered.length / layout.cells));
  const visible = ordered.slice(page * layout.cells, page * layout.cells + layout.cells);

  // Which stream a given tile pulls: the single-cell view and the big tile in
  // the 1+5 mosaic upgrade to the main stream; everything else stays on the
  // lighter substream so a dense grid doesn't hammer the host.
  const tileQuality = (index: number): "sub" | "main" => {
    if (layout.cells === 1) return "main";
    if (layout.asymmetric && index === 0) return "main";
    return "sub";
  };

  const statFor = (cameraId: number, quality: "sub" | "main") =>
    stats.find((s) => s.camera_id === cameraId && s.quality === quality) ?? null;

  if (cameras.length === 0) {
    return <div className="empty-state">{emptyMessage ?? "No cameras to show."}</div>;
  }

  return (
    <>
      <div className={`live-grid layout-${layout.id}`}>
        {visible.map((cam, i) => {
          const q = tileQuality(i);
          return (
            <DraggableTile
              key={cam.id}
              id={cam.id}
              draggedId={draggedId}
              onDragStart={setDraggedId}
              onDragEnd={() => setDraggedId(null)}
              onDropOn={(targetId) => {
                if (draggedId != null) moveBefore(draggedId, targetId);
                setDraggedId(null);
              }}
            >
              <CameraTile camera={cam} quality={q} stat={statFor(cam.id, q)} endpoints={endpoints} />
            </DraggableTile>
          );
        })}
      </div>

      {pageCount > 1 && (
        <div className="live-view-pager" style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 16 }}>
          <button className="btn btn-sm" disabled={page === 0} onClick={() => onPageChange(page - 1)}>
            Prev
          </button>
          <span style={{ color: "var(--text-dim)", fontSize: 13, alignSelf: "center" }}>
            Page {page + 1} / {pageCount}
          </span>
          <button className="btn btn-sm" disabled={page >= pageCount - 1} onClick={() => onPageChange(page + 1)}>
            Next
          </button>
        </div>
      )}
    </>
  );
}
