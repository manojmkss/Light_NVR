import { useEffect, useRef, useState, type FormEvent } from "react";
import { createCamera, listCameras } from "../api/cameras";
import { restoreFromUpload, waitForServerRestart } from "../api/backup";
import type { Camera, CameraCreatePayload } from "../api/types";
import { CameraSetupModal } from "../components/CameraSetupModal";
import { useAuth } from "../context/AuthContext";
import { AlertsTab, PushNotificationsCard, StorageTab } from "./SettingsPage";

type Mode = "create" | "restore";
type Step = "account" | "storage" | "discovery" | "notifications";

const STEPS: Step[] = ["account", "storage", "discovery", "notifications"];
const STEP_LABELS: Record<Step, string> = {
  account: "Admin account",
  storage: "Storage",
  discovery: "Cameras",
  notifications: "Notifications",
};

function WizardProgress({ step }: { step: Step }) {
  const current = STEPS.indexOf(step);
  return (
    <div style={{ display: "flex", gap: 6, justifyContent: "center", marginBottom: 20 }}>
      {STEPS.map((s, i) => (
        <div
          key={s}
          title={STEP_LABELS[s]}
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: i <= current ? "var(--accent)" : "var(--border)",
          }}
        />
      ))}
    </div>
  );
}

function AccountStep({ onDone }: { onDone: () => void }) {
  const { completeSetup } = useAuth();
  const [mode, setMode] = useState<Mode>("create");

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreStatus, setRestoreStatus] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError("Passwords don't match");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }

    setSubmitting(true);
    try {
      await completeSetup(username, password);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleRestore = async () => {
    const file = fileInputRef.current?.files?.[0];
    if (!file) {
      setError("Choose a backup file first");
      return;
    }
    setError(null);
    setRestoring(true);
    try {
      await restoreFromUpload(file);
      setRestoreStatus("Restoring - waiting for the server to come back up...");
      await waitForServerRestart();
      window.location.reload();
    } catch (err) {
      setError((err as Error).message);
      setRestoring(false);
    }
  };

  if (restoring) {
    return (
      <div style={{ textAlign: "center" }}>
        <h1>Restoring...</h1>
        <p style={{ color: "var(--text-dim)" }}>{restoreStatus || "Working..."}</p>
      </div>
    );
  }

  return (
    <>
      <h1>Welcome to LightNVR</h1>
      <p style={{ color: "var(--text-dim)", textAlign: "center", marginTop: -12, marginBottom: 20 }}>
        {mode === "create" ? "Create your admin account to get started." : "Restore a previous configuration."}
      </p>

      <div className="tabs" style={{ justifyContent: "center" }}>
        <div className={`tab ${mode === "create" ? "active" : ""}`} onClick={() => setMode("create")}>
          New setup
        </div>
        <div className={`tab ${mode === "restore" ? "active" : ""}`} onClick={() => setMode("restore")}>
          Restore from backup
        </div>
      </div>

      {mode === "create" ? (
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label>Username</label>
            <input type="text" required autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Confirm password</label>
            <input
              type="password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </div>
          {error && <div className="error-text">{error}</div>}
          <button type="submit" className="btn btn-primary" style={{ width: "100%", marginTop: 8 }} disabled={submitting}>
            {submitting ? "Creating account..." : "Create admin account"}
          </button>
        </form>
      ) : (
        <div>
          <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
            Upload a backup file downloaded from Settings → Backup on a previous installation. This restores all
            cameras, users, and settings - the server will restart once it's done, and the setup wizard won't run
            again since everything is already configured.
          </p>
          <div className="field">
            <label>Backup file (.db)</label>
            <input ref={fileInputRef} type="file" accept=".db" />
          </div>
          {error && <div className="error-text">{error}</div>}
          <button className="btn btn-primary" style={{ width: "100%", marginTop: 8 }} onClick={handleRestore}>
            Restore
          </button>
        </div>
      )}
    </>
  );
}

function DiscoveryStep() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [showSetup, setShowSetup] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const refresh = () => listCameras().then(setCameras);

  useEffect(() => {
    refresh();
  }, []);

  const handleCreate = async (payload: CameraCreatePayload) => {
    setSubmitting(true);
    setServerError(null);
    try {
      await createCamera(payload);
      setShowSetup(false);
      refresh();
    } catch (err) {
      setServerError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleCreateMany = async (payloads: CameraCreatePayload[]) => {
    setSubmitting(true);
    setServerError(null);
    try {
      for (const p of payloads) await createCamera(p);
      setShowSetup(false);
      refresh();
    } catch (err) {
      setServerError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
        Scan your network for ONVIF-compatible cameras and add them with one click - main/sub streams are
        auto-detected. You can always add or change cameras later from the Cameras page.
      </p>
      {cameras.length > 0 && (
        <p style={{ fontSize: 13 }}>
          <strong>{cameras.length}</strong> camera{cameras.length === 1 ? "" : "s"} added so far.
        </p>
      )}
      <button type="button" className="btn btn-primary" onClick={() => setShowSetup(true)}>
        Discover cameras
      </button>
      {showSetup && (
        <CameraSetupModal onCreate={handleCreate} onCreateMany={handleCreateMany} onClose={() => setShowSetup(false)} submitting={submitting} serverError={serverError} />
      )}
    </div>
  );
}

export function SetupPage() {
  const { finishSetupWizard } = useAuth();
  const [step, setStep] = useState<Step>("account");

  const goTo = (s: Step) => setStep(s);
  const stepIndex = STEPS.indexOf(step);
  const next = () => (stepIndex < STEPS.length - 1 ? goTo(STEPS[stepIndex + 1]) : finishSetupWizard());

  return (
    <div className="login-screen">
      <div className="login-box card" style={{ maxWidth: step === "account" ? 400 : 640 }}>
        {step !== "account" && <WizardProgress step={step} />}

        {step === "account" && <AccountStep onDone={() => goTo("storage")} />}

        {step === "storage" && (
          <>
            <h2 style={{ marginTop: 0 }}>Storage</h2>
            <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: -8 }}>
              Defaults work out of the box (local cache only, 30-day retention). Add a NAS or dedicated drive now, or
              skip and configure it later from Settings → Storage.
            </p>
            <StorageTab />
          </>
        )}

        {step === "discovery" && (
          <>
            <h2 style={{ marginTop: 0 }}>Cameras</h2>
            <DiscoveryStep />
          </>
        )}

        {step === "notifications" && (
          <>
            <h2 style={{ marginTop: 0 }}>Notifications</h2>
            <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: -8 }}>
              Optional - get alerted on motion, offline cameras, and low storage. Skip and configure this anytime
              from Settings → Alerts.
            </p>
            <AlertsTab />
            <PushNotificationsCard />
          </>
        )}

        {step !== "account" && (
          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 24 }}>
            <button type="button" className="btn btn-sm" onClick={next}>
              {step === "notifications" ? "Skip and finish" : "Skip for now"}
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={next}>
              {step === "notifications" ? "Finish" : "Continue"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
