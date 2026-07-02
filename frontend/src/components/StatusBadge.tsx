import type { CameraStatus } from "../api/types";

export function StatusBadge({ status }: { status: CameraStatus }) {
  const label = status === "online" ? "Online" : status === "offline" ? "Offline" : "Unknown";
  return (
    <span className="status-badge">
      <span className={`status-dot ${status}`} />
      {label}
    </span>
  );
}
