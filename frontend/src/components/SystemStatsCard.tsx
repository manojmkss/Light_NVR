function levelClass(percent: number): string {
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warn";
  return "";
}

export function StatCard({
  label,
  value,
  percent,
}: {
  label: string;
  value: string;
  percent?: number;
}) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {percent !== undefined && (
        <div className="progress-bar">
          <div
            className={`progress-bar-fill ${levelClass(percent)}`}
            style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
          />
        </div>
      )}
    </div>
  );
}

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 GB";
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
}

export function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
