import { useEffect, useMemo, useState } from "react";

interface OrderedList<T> {
  ordered: T[];
  /** Move `draggedId` to just before `targetId` in the saved order. */
  moveBefore: (draggedId: number, targetId: number) => void;
}

// Backs the drag-to-rearrange grids (Live View and Kiosk): persists a plain
// array of ids to localStorage under `storageKey` and reorders `items` to
// match it. Ids no longer present in `items` are dropped silently; items not
// yet in the saved order are appended at the end in their given order - so a
// newly added camera just shows up at the end instead of the whole
// arrangement being invalidated.
export function useOrderedList<T extends { id: number }>(storageKey: string, items: T[]): OrderedList<T> {
  const [order, setOrder] = useState<number[]>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(order));
    } catch {
      /* storage full/unavailable - the order just won't persist this time */
    }
  }, [order, storageKey]);

  const ordered = useMemo(() => {
    const byId = new Map(items.map((i) => [i.id, i]));
    const known = order.filter((id) => byId.has(id)).map((id) => byId.get(id)!);
    const knownSet = new Set(order);
    const rest = items.filter((i) => !knownSet.has(i.id));
    return [...known, ...rest];
  }, [items, order]);

  const moveBefore = (draggedId: number, targetId: number) => {
    if (draggedId === targetId) return;
    const ids = ordered.map((i) => i.id);
    const from = ids.indexOf(draggedId);
    if (from === -1) return;
    ids.splice(from, 1);
    const to = ids.indexOf(targetId);
    ids.splice(to === -1 ? ids.length : to, 0, draggedId);
    setOrder(ids);
  };

  return { ordered, moveBefore };
}
