import { useEffect, useState } from "react";
import { getAIStats, runAIBenchmark, type AIBenchmarkReport, type AIStats, type FeatureLevel } from "../api/ai";

const LEVEL_TEXT: Record<string, { label: string; color: string }> = {
  recommended: { label: "Recommended", color: "var(--ok, #3fb950)" },
  reduced: { label: "Possible (reduced)", color: "#d29922" },
  minimal: { label: "Minimal", color: "#d29922" },
  summary_only: { label: "Summaries only", color: "#d29922" },
  not_recommended: { label: "Not recommended", color: "var(--danger, #f85149)" },
  needs_setup: { label: "Needs setup", color: "var(--text-dim)" },
  unavailable: { label: "Not available", color: "var(--danger, #f85149)" },
};

function Level({ name, f }: { name: string; f?: FeatureLevel }) {
  if (!f) return null;
  const t = LEVEL_TEXT[f.level] ?? { label: f.level, color: "var(--text-dim)" };
  return (
    <div style={{ display: "flex", gap: 8, fontSize: 13, padding: "2px 0" }}>
      <span style={{ minWidth: 150 }}>{name}</span>
      <span style={{ color: t.color, minWidth: 140 }}>{t.label}</span>
      <span style={{ color: "var(--text-dim)" }}>{f.reason}</span>
    </div>
  );
}

/** How hard the AI is working, and what this hardware can sustain. */
export function AIPerformanceCard({ onUseInterval }: { onUseInterval?: (ms: number) => void }) {
  const [stats, setStats] = useState<AIStats | null>(null);
  const [report, setReport] = useState<AIBenchmarkReport | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => getAIStats().then((s) => alive && setStats(s)).catch(() => undefined);
    load();
    const t = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const bench = async () => {
    setRunning(true);
    setError(null);
    try {
      setReport(await runAIBenchmark());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const totals = stats
    ? Object.values(stats.cameras).reduce(
        (a, c) => ({ served: a.served + c.served, skipped: a.skipped + c.superseded + c.expired, failed: a.failed + c.failed }),
        { served: 0, skipped: 0, failed: 0 },
      )
    : null;

  return (
    <div className="card">
      <strong>Performance</strong>
      {stats && totals && (
        <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 6 }}>
          {stats.active_cameras} camera(s) being checked now · average check {stats.avg_infer_ms} ms ·{" "}
          {totals.served} done, {totals.skipped} skipped for a newer frame{totals.failed ? `, ${totals.failed} failed` : ""}
          {totals.skipped > totals.served && totals.served > 10 && (
            <div style={{ color: "#d29922", marginTop: 4 }}>
              Many checks are being skipped: this machine can't keep up with every camera at the current rate. Run the
              measurement below for a suggested interval.
            </div>
          )}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10 }}>
        <button className="btn btn-sm" onClick={bench} disabled={running}>
          {running ? "Measuring…" : "Measure this hardware"}
        </button>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
          Runs a dozen real detections on the saved settings (a few seconds).
        </span>
      </div>
      {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
      {report && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 6 }}>
            {report.cpu.model} · {report.cpu.cores} cores
            {report.memory.container_limit_mb ? ` · ${report.memory.container_limit_mb} MB limit` : ""}
            {report.worker ? ` · AI worker: ${report.worker.reachable ? report.worker.device ?? "reachable" : "unreachable"}` : ""}
            {report.benchmark.ok ? ` · ${report.benchmark.p50_ms} ms per check` : ` · ${report.benchmark.error}`}
          </div>
          <Level name="Object detection" f={report.feasibility.detection} />
          <Level name="Face recognition" f={report.feasibility.faces} />
          <Level name="Search by description" f={report.feasibility.search} />
          <Level name="Scene descriptions" f={report.feasibility.descriptions} />
          {report.feasibility.recommended_interval_ms && onUseInterval && (
            <button
              className="btn btn-sm"
              style={{ marginTop: 8 }}
              onClick={() => onUseInterval(report.feasibility.recommended_interval_ms!)}
            >
              Use suggested interval ({report.feasibility.recommended_interval_ms} ms)
            </button>
          )}
        </div>
      )}
    </div>
  );
}
