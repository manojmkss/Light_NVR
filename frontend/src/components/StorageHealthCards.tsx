import type { StorageHealth, TierHealth } from "../api/types";
import { formatBytes } from "./SystemStatsCard";

function TierCard({ label, health }: { label: string; health: TierHealth }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ fontSize: 18 }}>
        {health.available ? (
          <span style={{ color: "var(--success)" }}>Online</span>
        ) : (
          <span style={{ color: "var(--danger)" }}>Unreachable</span>
        )}
      </div>
      {health.free_bytes != null && health.total_bytes != null ? (
        <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
          {formatBytes(health.free_bytes)} free of {formatBytes(health.total_bytes)}
        </div>
      ) : (
        health.detail && <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>{health.detail}</div>
      )}
    </div>
  );
}

export function StorageHealthCards({ health }: { health: StorageHealth }) {
  return (
    <div className="card-grid" style={{ marginBottom: 0 }}>
      <TierCard label="Cache" health={health.cache} />
      <TierCard label="Primary" health={health.primary} />
      {health.backup && <TierCard label="Backup" health={health.backup} />}
      <div className="card">
        <div className="stat-label">Pending migration</div>
        <div className="stat-value" style={{ fontSize: 18 }}>
          {health.pending_migration_count}
        </div>
      </div>
    </div>
  );
}
