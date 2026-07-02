import { useEffect, useState } from "react";

// Each layout is a grid preset. `cells` is how many tiles a page holds;
// `asymmetric` marks the 1-large-plus-5-small mosaic where the first tile is
// promoted to the main stream. `label` is what the switcher button shows.
export interface LayoutDef {
  id: number;
  cells: number;
  label: string;
  asymmetric?: boolean;
}

export const GRID_LAYOUTS: LayoutDef[] = [
  { id: 1, cells: 1, label: "1" },
  { id: 4, cells: 4, label: "2×2" },
  { id: 6, cells: 6, label: "1+5", asymmetric: true },
  { id: 9, cells: 9, label: "3×3" },
  { id: 16, cells: 16, label: "4×4" },
];

// Shared by Live View and Kiosk so both get the same dynamic grid engine
// (persisted per storageKey, so each surface remembers its own choice).
export function useGridLayout(storageKey: string) {
  const [layoutId, setLayoutIdState] = useState<number>(() => {
    const saved = Number(localStorage.getItem(storageKey));
    return GRID_LAYOUTS.some((l) => l.id === saved) ? saved : 4;
  });
  const [page, setPage] = useState(0);

  const setLayoutId = (id: number) => {
    setLayoutIdState(id);
    setPage(0);
  };

  useEffect(() => {
    localStorage.setItem(storageKey, String(layoutId));
  }, [layoutId, storageKey]);

  const layout = GRID_LAYOUTS.find((l) => l.id === layoutId) ?? GRID_LAYOUTS[1];

  return { layout, layoutId, setLayoutId, page, setPage };
}
