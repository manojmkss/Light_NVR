import { GRID_LAYOUTS } from "../hooks/useGridLayout";

export function GridLayoutSwitcher({ activeId, onChange }: { activeId: number; onChange: (id: number) => void }) {
  return (
    <div className="grid-layout-switcher">
      {GRID_LAYOUTS.map((l) => (
        <button
          key={l.id}
          className={`btn btn-sm ${activeId === l.id ? "btn-primary" : ""}`}
          onClick={() => onChange(l.id)}
          title={`${l.cells} camera${l.cells > 1 ? "s" : ""} per page`}
        >
          {l.label}
        </button>
      ))}
    </div>
  );
}
