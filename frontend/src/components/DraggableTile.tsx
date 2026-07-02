import { useState, type ReactNode } from "react";

interface DraggableTileProps {
  id: number;
  draggedId: number | null;
  onDragStart: (id: number) => void;
  onDragEnd: () => void;
  onDropOn: (id: number) => void;
  children: ReactNode;
}

// Wraps a grid tile with HTML5 drag-and-drop reordering via a small handle
// overlay (rather than making the whole tile draggable, which would fight
// clicks/drags on the video, scrub slider, and buttons inside it).
export function DraggableTile({ id, draggedId, onDragStart, onDragEnd, onDropOn, children }: DraggableTileProps) {
  const [dragOver, setDragOver] = useState(false);

  return (
    <div
      className={`live-tile-wrap ${draggedId === id ? "dragging" : ""} ${dragOver ? "drag-over" : ""}`}
      onDragOver={(e) => {
        if (draggedId == null || draggedId === id) return;
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        onDropOn(id);
      }}
    >
      <div
        className="live-tile-handle"
        draggable
        title="Drag to rearrange"
        onDragStart={(e) => {
          e.dataTransfer.effectAllowed = "move";
          onDragStart(id);
        }}
        onDragEnd={onDragEnd}
      >
        ⠿
      </div>
      {children}
    </div>
  );
}
