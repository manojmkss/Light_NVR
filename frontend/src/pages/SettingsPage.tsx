import { useEffect, useState, type FormEvent } from "react";
import {
  changeMyPassword,
  createUser,
  deleteUser,
  getSecuritySettings,
  listUsers,
  resetUserPassword,
  updateSecuritySettings,
} from "../api/auth";
import {
  createBackupNow,
  deleteBackup,
  downloadBackupUrl,
  getBackupSettings,
  listBackups,
  optimizeDatabase,
  restoreBackup,
  updateBackupSettings,
  waitForServerRestart,
} from "../api/backup";
import { listCameras } from "../api/cameras";
import {
  createKioskView,
  deleteKioskView,
  kioskShareUrl,
  listKioskViews,
  regenerateKioskToken,
  updateKioskView,
} from "../api/kiosk";
import { getCurrentSubscription, isPushSupported, isRunningAsInstalledApp, subscribeToPush, testPush, unsubscribeFromPush } from "../api/push";
import { getRemoteAccessSettings, getRemoteAccessStatus, updateCloudflare, updateTailscale } from "../api/remoteAccess";
import { getStorageConfig, getStorageHealth, testStorage, updateStorageConfig } from "../api/storage";
import { getAlertSettings, getSystemSettings, getSystemTime, testEmail, testTelegram, testWhatsApp, updateAlertSettings, updateSystemSettings } from "../api/system";
import { getTlsSettings, uploadCustomCert, useLetsEncrypt, useSelfSignedCert } from "../api/tls";
import type {
  AlertSettings,
  BackupInfo,
  BackupSettings,
  Camera,
  KioskView,
  RemoteAccessSettings,
  RemoteAccessStatus,
  SecuritySettings,
  StorageConfig,
  StorageHealth,
  StorageType,
  TlsSettings,
  User,
} from "../api/types";
import { StorageHealthCards } from "../components/StorageHealthCards";
import { useAuth } from "../context/AuthContext";
import { useSettings } from "../context/SettingsContext";

type Tab = "account" | "users" | "alerts" | "storage" | "backup" | "security" | "remote-access" | "kiosk" | "system";

function UsersTab({ currentUser }: { currentUser: User | null }) {
  const [users, setUsers] = useState<User[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [resetUserId, setResetUserId] = useState<number | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [resetError, setResetError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);

  const refresh = () => listUsers().then(setUsers);

  useEffect(() => {
    refresh();
  }, []);

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await createUser({ username, password, role });
      setUsername("");
      setPassword("");
      setRole("viewer");
      refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteUser(id);
      refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const handleResetPassword = async (id: number) => {
    setResetting(true);
    setResetError(null);
    try {
      await resetUserPassword(id, resetPassword);
      setResetUserId(null);
      setResetPassword("");
    } catch (err) {
      setResetError((err as Error).message);
    } finally {
      setResetting(false);
    }
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 320px)", gap: 20 }}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Username</th>
              <th>Role</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>{u.username}</td>
                <td style={{ textTransform: "capitalize" }}>{u.role}</td>
                <td>
                  {resetUserId === u.id ? (
                    <div style={{ display: "flex", gap: 6, alignItems: "center", justifyContent: "flex-end" }}>
                      <input
                        type="password"
                        placeholder="New password"
                        autoFocus
                        value={resetPassword}
                        onChange={(e) => setResetPassword(e.target.value)}
                        style={{ width: 140 }}
                      />
                      <button
                        className="btn btn-sm btn-primary"
                        disabled={resetting}
                        onClick={() => handleResetPassword(u.id)}
                      >
                        Save
                      </button>
                      <button
                        className="btn btn-sm"
                        onClick={() => {
                          setResetUserId(null);
                          setResetPassword("");
                          setResetError(null);
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button className="btn btn-sm" onClick={() => setResetUserId(u.id)}>
                        Reset password
                      </button>
                      {u.id !== currentUser?.id && (
                        <button className="btn btn-sm btn-danger" onClick={() => handleDelete(u.id)}>
                          Delete
                        </button>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {resetError && <div className="error-text" style={{ padding: "8px 12px" }}>{resetError}</div>}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Add account</h3>
        <form onSubmit={handleAdd}>
          <div className="field">
            <label>Username</label>
            <input type="text" required value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="field">
            <label>Password</label>
            <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <div className="field">
            <label>Role</label>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="viewer">Viewer (view-only)</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          {error && <div className="error-text">{error}</div>}
          <button type="submit" className="btn btn-primary" disabled={submitting} style={{ width: "100%" }}>
            {submitting ? "Adding..." : "Add account"}
          </button>
        </form>
      </div>
    </div>
  );
}

export function AlertsTab() {
  const [settings, setSettings] = useState<AlertSettings | null>(null);
  const [smtpPassword, setSmtpPassword] = useState("");
  const [telegramToken, setTelegramToken] = useState("");
  const [whatsappToken, setWhatsappToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testingTelegram, setTestingTelegram] = useState(false);
  const [telegramTestResult, setTelegramTestResult] = useState<string | null>(null);
  const [testingWhatsapp, setTestingWhatsapp] = useState(false);
  const [whatsappTestResult, setWhatsappTestResult] = useState<string | null>(null);

  useEffect(() => {
    getAlertSettings().then(setSettings);
  }, []);

  if (!settings) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const update = <K extends keyof AlertSettings>(key: K, value: AlertSettings[K]) => {
    setSettings((s) => (s ? { ...s, [key]: value } : s));
    setSaved(false);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await updateAlertSettings({
        ...settings,
        ...(smtpPassword ? { smtp_password: smtpPassword } : {}),
        ...(telegramToken ? { telegram_bot_token: telegramToken } : {}),
        ...(whatsappToken ? { whatsapp_access_token: whatsappToken } : {}),
      });
      setSettings(result);
      setSmtpPassword("");
      setTelegramToken("");
      setWhatsappToken("");
      setSaved(true);
    } finally {
      setSaving(false);
    }
  };

  const handleTestEmail = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testEmail({
        smtp_host: settings.smtp_host,
        smtp_port: settings.smtp_port,
        smtp_username: settings.smtp_username,
        smtp_password: smtpPassword,
        smtp_from: settings.smtp_from,
        smtp_use_tls: settings.smtp_use_tls,
        alert_email_to: settings.alert_email_to,
      });
      setTestResult(result.message);
    } catch (err) {
      setTestResult((err as Error).message);
    } finally {
      setTesting(false);
    }
  };

  const handleTestTelegram = async () => {
    setTestingTelegram(true);
    setTelegramTestResult(null);
    try {
      const result = await testTelegram({ bot_token: telegramToken, chat_id: settings.telegram_chat_id });
      setTelegramTestResult(result.message);
    } catch (err) {
      setTelegramTestResult((err as Error).message);
    } finally {
      setTestingTelegram(false);
    }
  };

  const handleTestWhatsapp = async () => {
    setTestingWhatsapp(true);
    setWhatsappTestResult(null);
    try {
      const result = await testWhatsApp({
        phone_number_id: settings.whatsapp_phone_number_id,
        access_token: whatsappToken,
        recipient_number: settings.whatsapp_recipient_number,
      });
      setWhatsappTestResult(result.message);
    } catch (err) {
      setWhatsappTestResult((err as Error).message);
    } finally {
      setTestingWhatsapp(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 480 }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Email (SMTP)</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Configured entirely here - no server files to edit. Leave the host blank to disable email alerts.
        </p>

        <div className="field">
          <label>SMTP host</label>
          <input type="text" value={settings.smtp_host} onChange={(e) => update("smtp_host", e.target.value)} />
        </div>
        <div className="form-row">
          <div className="field">
            <label>Port</label>
            <input
              type="number"
              value={settings.smtp_port}
              onChange={(e) => update("smtp_port", Number(e.target.value))}
            />
          </div>
          <div className="field checkbox-field" style={{ alignSelf: "center", marginTop: 18 }}>
            <input
              type="checkbox"
              id="smtp_tls"
              checked={settings.smtp_use_tls}
              onChange={(e) => update("smtp_use_tls", e.target.checked)}
            />
            <label htmlFor="smtp_tls">Use TLS</label>
          </div>
        </div>
        <div className="form-row">
          <div className="field">
            <label>Username</label>
            <input type="text" value={settings.smtp_username} onChange={(e) => update("smtp_username", e.target.value)} />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              type="password"
              value={smtpPassword}
              onChange={(e) => setSmtpPassword(e.target.value)}
              placeholder="Leave blank to keep current"
            />
          </div>
        </div>
        <div className="field">
          <label>From address</label>
          <input type="text" value={settings.smtp_from} onChange={(e) => update("smtp_from", e.target.value)} />
        </div>
        <div className="field">
          <label>Send alerts to</label>
          <input type="text" value={settings.alert_email_to} onChange={(e) => update("alert_email_to", e.target.value)} />
        </div>

        <div className="field">
          <button type="button" className="btn btn-sm" onClick={handleTestEmail} disabled={testing || !settings.smtp_host}>
            {testing ? "Sending..." : "Send test email"}
          </button>
          {testResult && <div style={{ marginTop: 8, fontSize: 13 }}>{testResult}</div>}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Telegram</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Free, no business verification needed. Message <strong>@BotFather</strong> on Telegram to create a bot and
          get its token, then message your new bot once and check{" "}
          <code>https://api.telegram.org/bot&lt;token&gt;/getUpdates</code> to find your chat ID.
        </p>
        <div className="field checkbox-field">
          <input
            type="checkbox"
            id="telegram_enabled"
            checked={settings.telegram_enabled}
            onChange={(e) => update("telegram_enabled", e.target.checked)}
          />
          <label htmlFor="telegram_enabled">Enable Telegram alerts</label>
        </div>
        <div className="form-row">
          <div className="field">
            <label>Bot token</label>
            <input
              type="password"
              value={telegramToken}
              onChange={(e) => setTelegramToken(e.target.value)}
              placeholder="Leave blank to keep current"
            />
          </div>
          <div className="field">
            <label>Chat ID</label>
            <input type="text" value={settings.telegram_chat_id} onChange={(e) => update("telegram_chat_id", e.target.value)} />
          </div>
        </div>
        <div className="field">
          <button type="button" className="btn btn-sm" onClick={handleTestTelegram} disabled={testingTelegram || !telegramToken}>
            {testingTelegram ? "Sending..." : "Send test message"}
          </button>
          {telegramTestResult && <div style={{ marginTop: 8, fontSize: 13 }}>{telegramTestResult}</div>}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>WhatsApp</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Uses the official WhatsApp Business Cloud API - this requires a Meta Developer app with a verified
          WhatsApp Business Account already set up (outside LightNVR) to get a phone number ID and access token.
          There's no simpler official option; unofficial automation risks your WhatsApp account being banned, so
          it's not supported here.
        </p>
        <div className="field checkbox-field">
          <input
            type="checkbox"
            id="whatsapp_enabled"
            checked={settings.whatsapp_enabled}
            onChange={(e) => update("whatsapp_enabled", e.target.checked)}
          />
          <label htmlFor="whatsapp_enabled">Enable WhatsApp alerts</label>
        </div>
        <div className="form-row">
          <div className="field">
            <label>Phone number ID</label>
            <input
              type="text"
              value={settings.whatsapp_phone_number_id}
              onChange={(e) => update("whatsapp_phone_number_id", e.target.value)}
            />
          </div>
          <div className="field">
            <label>Access token</label>
            <input
              type="password"
              value={whatsappToken}
              onChange={(e) => setWhatsappToken(e.target.value)}
              placeholder="Leave blank to keep current"
            />
          </div>
        </div>
        <div className="field">
          <label>Recipient number (with country code)</label>
          <input
            type="text"
            placeholder="15551234567"
            value={settings.whatsapp_recipient_number}
            onChange={(e) => update("whatsapp_recipient_number", e.target.value)}
          />
        </div>
        <div className="field">
          <button
            type="button"
            className="btn btn-sm"
            onClick={handleTestWhatsapp}
            disabled={testingWhatsapp || !settings.whatsapp_phone_number_id}
          >
            {testingWhatsapp ? "Sending..." : "Send test message"}
          </button>
          {whatsappTestResult && <div style={{ marginTop: 8, fontSize: 13 }}>{whatsappTestResult}</div>}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Alert types</h3>

        <div className="field checkbox-field">
          <input
            type="checkbox"
            id="motion"
            checked={settings.motion_alerts_enabled}
            onChange={(e) => update("motion_alerts_enabled", e.target.checked)}
          />
          <label htmlFor="motion">Motion alerts</label>
        </div>

        <div className="field">
          <label>Motion alert cooldown (seconds)</label>
          <input
            type="number"
            min={0}
            value={settings.motion_alert_cooldown_seconds}
            onChange={(e) => update("motion_alert_cooldown_seconds", Number(e.target.value))}
          />
        </div>

        <div className="field checkbox-field">
          <input
            type="checkbox"
            id="offline"
            checked={settings.offline_alerts_enabled}
            onChange={(e) => update("offline_alerts_enabled", e.target.checked)}
          />
          <label htmlFor="offline">Camera offline alerts</label>
        </div>

        <div className="field checkbox-field">
          <input
            type="checkbox"
            id="storage"
            checked={settings.low_storage_alerts_enabled}
            onChange={(e) => update("low_storage_alerts_enabled", e.target.checked)}
          />
          <label htmlFor="storage">Low storage alerts</label>
        </div>

        <div className="field">
          <label>Low storage threshold (% free)</label>
          <input
            type="number"
            min={1}
            max={50}
            value={settings.low_storage_threshold_percent}
            onChange={(e) => update("low_storage_threshold_percent", Number(e.target.value))}
          />
        </div>
      </div>

      <button className="btn btn-primary" onClick={handleSave} disabled={saving} style={{ justifySelf: "start" }}>
        {saving ? "Saving..." : saved ? "Saved" : "Save changes"}
      </button>
    </div>
  );
}

export function StorageTab() {
  const [config, setConfig] = useState<StorageConfig | null>(null);
  const [health, setHealth] = useState<StorageHealth | null>(null);
  const [primaryPassword, setPrimaryPassword] = useState("");
  const [backupPassword, setBackupPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [testingPrimary, setTestingPrimary] = useState(false);
  const [testingBackup, setTestingBackup] = useState(false);
  const [primaryTestResult, setPrimaryTestResult] = useState<string | null>(null);
  const [backupTestResult, setBackupTestResult] = useState<string | null>(null);

  useEffect(() => {
    getStorageConfig().then(setConfig);
    getStorageHealth().then(setHealth);
    const interval = setInterval(() => getStorageHealth().then(setHealth), 10000);
    return () => clearInterval(interval);
  }, []);

  if (!config) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const update = <K extends keyof StorageConfig>(key: K, value: StorageConfig[K]) => {
    setConfig((c) => (c ? { ...c, [key]: value } : c));
    setSaved(false);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await updateStorageConfig({
        cache_max_gb: config.cache_max_gb,
        default_retention_days: config.default_retention_days,
        max_storage_gb: config.max_storage_gb,
        primary_type: config.primary_type,
        primary_remote_spec: config.primary_remote_spec,
        primary_username: config.primary_username,
        backup_enabled: config.backup_enabled,
        backup_type: config.backup_type,
        backup_remote_spec: config.backup_remote_spec,
        backup_username: config.backup_username,
        ...(primaryPassword ? { primary_password: primaryPassword } : {}),
        ...(backupPassword ? { backup_password: backupPassword } : {}),
      });
      setConfig(result);
      setPrimaryPassword("");
      setBackupPassword("");
      setSaved(true);
      getStorageHealth().then(setHealth);
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async (target: "primary" | "backup") => {
    const setTesting = target === "primary" ? setTestingPrimary : setTestingBackup;
    const setResult = target === "primary" ? setPrimaryTestResult : setBackupTestResult;
    setTesting(true);
    setResult(null);
    try {
      const result = await testStorage({
        target,
        type: target === "primary" ? config.primary_type : config.backup_type,
        remote_spec: target === "primary" ? config.primary_remote_spec : config.backup_remote_spec,
        username: target === "primary" ? config.primary_username : config.backup_username,
        password: target === "primary" ? primaryPassword : backupPassword,
      });
      setResult(result.message);
    } catch (err) {
      setResult((err as Error).message);
    } finally {
      setTesting(false);
    }
  };

  const typeOptions = (
    <>
      <option value="local">Local dedicated drive</option>
      <option value="smb">Network share (SMB/CIFS)</option>
      <option value="nfs">Network share (NFS)</option>
    </>
  );

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 640 }}>
      {health && <StorageHealthCards health={health} />}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Local cache</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Recordings always write here first, so a flaky NAS can never stall or corrupt an active recording. A
          background job moves finished recordings to Primary (or Backup, if Primary is unreachable) within seconds.
        </p>
        <div className="field">
          <label>Cache size cap (GB)</label>
          <input
            type="number"
            min={1}
            value={config.cache_max_gb}
            onChange={(e) => update("cache_max_gb", Number(e.target.value))}
          />
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Retention &amp; rotation</h3>
        <div className="field">
          <label>Default retention (days)</label>
          <input
            type="number"
            min={1}
            value={config.default_retention_days}
            onChange={(e) => update("default_retention_days", Number(e.target.value))}
          />
          <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 6, marginBottom: 0 }}>
            Applies to any camera without its own override (set per-camera under Cameras → Edit).
          </p>
        </div>
        <div className="field">
          <label>Total storage cap (GB, 0 = unlimited)</label>
          <input
            type="number"
            min={0}
            value={config.max_storage_gb}
            onChange={(e) => update("max_storage_gb", Number(e.target.value))}
          />
          <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 6, marginBottom: 0 }}>
            Safety backstop across all cameras combined - oldest recordings are deleted first if exceeded, regardless
            of individual retention settings.
          </p>
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Primary storage</h3>
        <div className="field">
          <label>Type</label>
          <select value={config.primary_type} onChange={(e) => update("primary_type", e.target.value as StorageType)}>
            {typeOptions}
          </select>
        </div>
        {config.primary_type === "local" ? (
          <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
            Maps to <code>/mnt/primary</code> in the container. Point that at your dedicated drive by editing the
            volume in docker-compose.yml, then restart - Docker can't attach a new host path without it.
          </p>
        ) : (
          <>
            <div className="field">
              <label>{config.primary_type === "smb" ? "Share path (//server/share)" : "Export (server:/export/path)"}</label>
              <input
                type="text"
                value={config.primary_remote_spec ?? ""}
                onChange={(e) => update("primary_remote_spec", e.target.value)}
              />
            </div>
            {config.primary_type === "smb" && (
              <div className="form-row">
                <div className="field">
                  <label>Username</label>
                  <input
                    type="text"
                    value={config.primary_username ?? ""}
                    onChange={(e) => update("primary_username", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label>Password</label>
                  <input
                    type="password"
                    value={primaryPassword}
                    onChange={(e) => setPrimaryPassword(e.target.value)}
                    placeholder="Leave blank to keep current"
                  />
                </div>
              </div>
            )}
          </>
        )}
        <div className="field">
          <button type="button" className="btn btn-sm" onClick={() => handleTest("primary")} disabled={testingPrimary}>
            {testingPrimary ? "Testing..." : "Test connection"}
          </button>
          {primaryTestResult && <div style={{ marginTop: 8, fontSize: 13 }}>{primaryTestResult}</div>}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Backup storage</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Used only when Primary is unreachable, so nothing gets missed during an outage. Once a recording lands on
          Backup it stays there - it is not migrated to Primary afterward.
        </p>
        <div className="field checkbox-field">
          <input
            type="checkbox"
            id="backup_enabled"
            checked={config.backup_enabled}
            onChange={(e) => update("backup_enabled", e.target.checked)}
          />
          <label htmlFor="backup_enabled">Enable backup storage</label>
        </div>
        {config.backup_enabled && (
          <>
            <div className="field">
              <label>Type</label>
              <select value={config.backup_type} onChange={(e) => update("backup_type", e.target.value as StorageType)}>
                {typeOptions}
              </select>
            </div>
            {config.backup_type === "local" ? (
              <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
                Maps to <code>/mnt/backup</code> in the container - point that at a drive in docker-compose.yml.
              </p>
            ) : (
              <>
                <div className="field">
                  <label>{config.backup_type === "smb" ? "Share path (//server/share)" : "Export (server:/export/path)"}</label>
                  <input
                    type="text"
                    value={config.backup_remote_spec ?? ""}
                    onChange={(e) => update("backup_remote_spec", e.target.value)}
                  />
                </div>
                {config.backup_type === "smb" && (
                  <div className="form-row">
                    <div className="field">
                      <label>Username</label>
                      <input
                        type="text"
                        value={config.backup_username ?? ""}
                        onChange={(e) => update("backup_username", e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label>Password</label>
                      <input
                        type="password"
                        value={backupPassword}
                        onChange={(e) => setBackupPassword(e.target.value)}
                        placeholder="Leave blank to keep current"
                      />
                    </div>
                  </div>
                )}
              </>
            )}
            <div className="field">
              <button type="button" className="btn btn-sm" onClick={() => handleTest("backup")} disabled={testingBackup}>
                {testingBackup ? "Testing..." : "Test connection"}
              </button>
              {backupTestResult && <div style={{ marginTop: 8, fontSize: 13 }}>{backupTestResult}</div>}
            </div>
          </>
        )}
      </div>

      <button className="btn btn-primary" onClick={handleSave} disabled={saving} style={{ justifySelf: "start" }}>
        {saving ? "Saving..." : saved ? "Saved" : "Save changes"}
      </button>
    </div>
  );
}

function formatBackupSize(bytes: number): string {
  const mb = bytes / 1024 ** 2;
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
}

function BackupTab() {
  const { fmtDateTime } = useSettings();
  const [settings, setSettings] = useState<BackupSettings | null>(null);
  const [backups, setBackups] = useState<BackupInfo[]>([]);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [restoringFilename, setRestoringFilename] = useState<string | null>(null);
  const [restoreStatus, setRestoreStatus] = useState<string | null>(null);
  const [optimizing, setOptimizing] = useState(false);
  const [optimizeResult, setOptimizeResult] = useState<string | null>(null);

  const refresh = () => {
    getBackupSettings().then(setSettings);
    listBackups().then(setBackups);
  };

  useEffect(() => {
    refresh();
  }, []);

  if (!settings) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const update = <K extends keyof BackupSettings>(key: K, value: BackupSettings[K]) => {
    setSettings((s) => (s ? { ...s, [key]: value } : s));
    setSaved(false);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await updateBackupSettings({
        enabled: settings.enabled,
        interval_hours: settings.interval_hours,
        retention_count: settings.retention_count,
      });
      setSettings(result);
      setSaved(true);
    } finally {
      setSaving(false);
    }
  };

  const handleCreateNow = async () => {
    setCreating(true);
    setActionError(null);
    try {
      await createBackupNow();
      refresh();
    } catch (err) {
      setActionError((err as Error).message);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (filename: string) => {
    try {
      await deleteBackup(filename);
      refresh();
    } catch (err) {
      setActionError((err as Error).message);
    }
  };

  const handleRestore = async (filename: string) => {
    if (!window.confirm(`Restore "${filename}"? This replaces all current cameras, users, and settings, and the server will restart.`)) {
      return;
    }
    setRestoringFilename(filename);
    setActionError(null);
    try {
      await restoreBackup(filename);
      setRestoreStatus("Restoring - waiting for the server to come back up...");
      await waitForServerRestart();
      window.location.reload();
    } catch (err) {
      setActionError((err as Error).message);
      setRestoringFilename(null);
    }
  };

  const handleOptimize = async () => {
    setOptimizing(true);
    setOptimizeResult(null);
    try {
      const result = await optimizeDatabase();
      setOptimizeResult(result.success ? `Optimized in ${result.duration_seconds.toFixed(1)}s` : result.message);
      refresh();
    } catch (err) {
      setOptimizeResult((err as Error).message);
    } finally {
      setOptimizing(false);
    }
  };

  if (restoringFilename) {
    return (
      <div className="card" style={{ maxWidth: 480 }}>
        <h3 style={{ marginTop: 0 }}>Restoring...</h3>
        <p style={{ color: "var(--text-dim)" }}>{restoreStatus || "Working..."}</p>
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
          The page will reload automatically once the server is back. You'll need to log in again.
        </p>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 640 }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Configuration backup</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Backs up cameras, users, and all settings (not recorded video) to Primary storage automatically, falling
          back to local storage if Primary is unreachable when a backup runs.
        </p>

        <div className="field checkbox-field">
          <input type="checkbox" id="backup_enabled_toggle" checked={settings.enabled} onChange={(e) => update("enabled", e.target.checked)} />
          <label htmlFor="backup_enabled_toggle">Automatic backups</label>
        </div>
        <div className="form-row">
          <div className="field">
            <label>Every (hours)</label>
            <input
              type="number"
              min={1}
              value={settings.interval_hours}
              onChange={(e) => update("interval_hours", Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label>Keep last N backups</label>
            <input
              type="number"
              min={1}
              value={settings.retention_count}
              onChange={(e) => update("retention_count", Number(e.target.value))}
            />
          </div>
        </div>

        <div style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 12 }}>
          {settings.last_backup_at ? (
            <>
              Last backup: {fmtDateTime(settings.last_backup_at)} ({settings.last_backup_location})
            </>
          ) : (
            "No backup yet"
          )}
          {settings.last_backup_error && <div className="error-text">{settings.last_backup_error}</div>}
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : saved ? "Saved" : "Save changes"}
          </button>
          <button className="btn" onClick={handleCreateNow} disabled={creating}>
            {creating ? "Backing up..." : "Back up now"}
          </button>
        </div>
        {actionError && <div className="error-text">{actionError}</div>}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Available backups</h3>
        {backups.length === 0 ? (
          <p style={{ color: "var(--text-dim)" }}>No backups yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Created</th>
                  <th>Location</th>
                  <th>Size</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {backups.map((b) => (
                  <tr key={b.filename}>
                    <td>{fmtDateTime(b.created_at)}</td>
                    <td style={{ textTransform: "capitalize" }}>{b.location}</td>
                    <td>{formatBackupSize(b.size_bytes)}</td>
                    <td>
                      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                        <a className="btn btn-sm" href={downloadBackupUrl(b.filename)}>
                          Download
                        </a>
                        <button className="btn btn-sm" onClick={() => handleRestore(b.filename)}>
                          Restore
                        </button>
                        <button className="btn btn-sm btn-danger" onClick={() => handleDelete(b.filename)}>
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Database maintenance</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Runs automatically once a day - reclaims space left behind by retention's deletes and refreshes query
          performance statistics. No action needed, but you can run it on demand here.
        </p>
        <p style={{ fontSize: 13, color: "var(--text-dim)" }}>
          {settings.last_optimize_at ? `Last optimized: ${fmtDateTime(settings.last_optimize_at)}` : "Not run yet"}
        </p>
        <button className="btn btn-sm" onClick={handleOptimize} disabled={optimizing}>
          {optimizing ? "Optimizing..." : "Optimize now"}
        </button>
        {optimizeResult && <div style={{ marginTop: 8, fontSize: 13 }}>{optimizeResult}</div>}
      </div>
    </div>
  );
}

function TlsTab() {
  const { fmtDateTime } = useSettings();
  const [tls, setTls] = useState<TlsSettings | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [certFile, setCertFile] = useState<File | null>(null);
  const [keyFile, setKeyFile] = useState<File | null>(null);
  const [domain, setDomain] = useState("");
  const [email, setEmail] = useState("");

  const refresh = () => getTlsSettings().then(setTls);

  useEffect(() => {
    refresh();
  }, []);

  if (!tls) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const runAction = async (fn: () => Promise<{ success: boolean; message: string }>) => {
    setBusy(true);
    setActionError(null);
    setActionResult(null);
    try {
      const result = await fn();
      setActionResult(result.message);
      refresh();
    } catch (err) {
      setActionError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 560 }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>HTTPS certificate</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Required for installing the app to your home screen and for push notifications - browsers refuse both over
          plain HTTP. Access the app at the HTTPS port (default <code>8443</code>) to use whichever certificate is
          active below.
        </p>
        <div style={{ fontSize: 13, marginBottom: 4 }}>
          Current mode: <strong style={{ textTransform: "capitalize" }}>{tls.mode.replace("_", " ")}</strong>
          {tls.domain && ` (${tls.domain})`}
        </div>
        {tls.last_renewal_at && (
          <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
            Last renewed: {fmtDateTime(tls.last_renewal_at)}
          </div>
        )}
        {tls.last_renewal_error && <div className="error-text">{tls.last_renewal_error}</div>}
        {actionResult && <div style={{ color: "var(--success)", fontSize: 13, marginTop: 8 }}>{actionResult}</div>}
        {actionError && <div className="error-text">{actionError}</div>}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Self-signed (default)</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Works immediately with zero setup. Browsers show a one-time "not secure" warning to click through on each
          new device, since nothing but this server signs it - the same default experience as Proxmox's own web UI.
        </p>
        <button className="btn btn-sm" disabled={busy} onClick={() => runAction(useSelfSignedCert)}>
          Use self-signed certificate
        </button>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Custom certificate</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Upload a certificate (PEM, including any intermediate chain) and its private key from your own CA.
        </p>
        <div className="form-row">
          <div className="field">
            <label>Certificate file</label>
            <input type="file" accept=".pem,.crt,.cer" onChange={(e) => setCertFile(e.target.files?.[0] ?? null)} />
          </div>
          <div className="field">
            <label>Private key file</label>
            <input type="file" accept=".pem,.key" onChange={(e) => setKeyFile(e.target.files?.[0] ?? null)} />
          </div>
        </div>
        <button
          className="btn btn-sm"
          disabled={busy || !certFile || !keyFile}
          onClick={() => certFile && keyFile && runAction(() => uploadCustomCert(certFile, keyFile))}
        >
          Upload and use
        </button>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Let's Encrypt</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Requires a real domain name pointed at your public IP, with your router forwarding external port 80 to
          this server (Let's Encrypt always validates over port 80, regardless of which port you normally browse to
          this app on). Renews automatically every 12 hours when due.
        </p>
        <div className="form-row">
          <div className="field">
            <label>Domain</label>
            <input type="text" placeholder="nvr.example.com" value={domain} onChange={(e) => setDomain(e.target.value)} />
          </div>
          <div className="field">
            <label>Email (for renewal notices)</label>
            <input type="text" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
        </div>
        <button
          className="btn btn-sm"
          disabled={busy || !domain || !email}
          onClick={() => runAction(() => useLetsEncrypt(domain, email))}
        >
          {busy ? "Requesting..." : "Request certificate"}
        </button>
      </div>
    </div>
  );
}

function RemoteAccessTab() {
  const [settings, setSettings] = useState<RemoteAccessSettings | null>(null);
  const [status, setStatus] = useState<RemoteAccessStatus | null>(null);

  const [tailscaleKey, setTailscaleKey] = useState("");
  const [tailscaleBusy, setTailscaleBusy] = useState(false);
  const [tailscaleError, setTailscaleError] = useState<string | null>(null);

  const [cloudflareToken, setCloudflareToken] = useState("");
  const [cloudflareBusy, setCloudflareBusy] = useState(false);
  const [cloudflareError, setCloudflareError] = useState<string | null>(null);

  const refresh = () => {
    getRemoteAccessSettings().then(setSettings);
    getRemoteAccessStatus().then(setStatus);
  };

  useEffect(() => {
    refresh();
    const interval = setInterval(() => getRemoteAccessStatus().then(setStatus), 5000);
    return () => clearInterval(interval);
  }, []);

  if (!settings) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const handleTailscaleToggle = async (enabled: boolean) => {
    setTailscaleBusy(true);
    setTailscaleError(null);
    try {
      const result = await updateTailscale(enabled, tailscaleKey || undefined);
      setSettings(result);
      setTailscaleKey("");
      refresh();
    } catch (err) {
      setTailscaleError((err as Error).message);
    } finally {
      setTailscaleBusy(false);
    }
  };

  const handleCloudflareToggle = async (enabled: boolean) => {
    setCloudflareBusy(true);
    setCloudflareError(null);
    try {
      const result = await updateCloudflare(enabled, cloudflareToken || undefined);
      setSettings(result);
      setCloudflareToken("");
      refresh();
    } catch (err) {
      setCloudflareError((err as Error).message);
    } finally {
      setCloudflareBusy(false);
    }
  };

  const tailscaleStateLabel: Record<string, string> = {
    stopped: "Disconnected",
    starting: "Connecting...",
    needs_login: "Auth key rejected - check the key",
    connected: "Connected",
  };
  const cloudflareStateLabel: Record<string, string> = {
    stopped: "Disconnected",
    starting: "Connecting...",
    connected: "Connected",
  };

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 560 }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Tailscale</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Reach this NVR from anywhere via your private Tailscale network, with a real trusted HTTPS certificate (no
          browser warning) and no router port-forwarding. Generate a key at{" "}
          <code>login.tailscale.com/admin/settings/keys</code> and paste it below.
        </p>
        <div style={{ fontSize: 13, marginBottom: 8 }}>
          Status: <strong>{status ? tailscaleStateLabel[status.tailscale.state] ?? status.tailscale.state : "..."}</strong>
          {status?.tailscale.hostname && (
            <>
              {" - "}
              <code>https://{status.tailscale.hostname}</code>
            </>
          )}
        </div>
        {status?.tailscale.error && <div className="error-text">{status.tailscale.error}</div>}
        <div className="field">
          <label>Auth key</label>
          <input
            type="password"
            placeholder={settings.has_tailscale_authkey ? "Leave blank to keep current" : "tskey-auth-..."}
            value={tailscaleKey}
            onChange={(e) => setTailscaleKey(e.target.value)}
          />
        </div>
        {tailscaleError && <div className="error-text">{tailscaleError}</div>}
        <div style={{ display: "flex", gap: 8 }}>
          {!settings.tailscale_enabled ? (
            <button
              className="btn btn-sm"
              disabled={tailscaleBusy || (!tailscaleKey && !settings.has_tailscale_authkey)}
              onClick={() => handleTailscaleToggle(true)}
            >
              {tailscaleBusy ? "Connecting..." : "Connect"}
            </button>
          ) : (
            <button className="btn btn-sm" disabled={tailscaleBusy} onClick={() => handleTailscaleToggle(false)}>
              {tailscaleBusy ? "Disconnecting..." : "Disconnect"}
            </button>
          )}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Cloudflare Tunnel</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Reach this NVR over the public internet via a Cloudflare-hosted hostname, with no router port-forwarding.
          Create a tunnel in the Cloudflare Zero Trust dashboard (Networks -&gt; Tunnels), set its public hostname's
          origin service to <code>https://nginx:443</code> with "No TLS Verify" enabled, then paste the tunnel token
          here.
        </p>
        <div style={{ fontSize: 13, marginBottom: 8 }}>
          Status: <strong>{status ? cloudflareStateLabel[status.cloudflare.state] ?? status.cloudflare.state : "..."}</strong>
        </div>
        {status?.cloudflare.error && <div className="error-text">{status.cloudflare.error}</div>}
        <div className="field">
          <label>Tunnel token</label>
          <input
            type="password"
            placeholder={settings.has_cloudflare_token ? "Leave blank to keep current" : "Paste tunnel token"}
            value={cloudflareToken}
            onChange={(e) => setCloudflareToken(e.target.value)}
          />
        </div>
        {cloudflareError && <div className="error-text">{cloudflareError}</div>}
        <div style={{ display: "flex", gap: 8 }}>
          {!settings.cloudflare_enabled ? (
            <button
              className="btn btn-sm"
              disabled={cloudflareBusy || (!cloudflareToken && !settings.has_cloudflare_token)}
              onClick={() => handleCloudflareToggle(true)}
            >
              {cloudflareBusy ? "Connecting..." : "Connect"}
            </button>
          ) : (
            <button className="btn btn-sm" disabled={cloudflareBusy} onClick={() => handleCloudflareToggle(false)}>
              {cloudflareBusy ? "Disconnecting..." : "Disconnect"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function KioskCameraPicker({
  cameras,
  selected,
  onChange,
}: {
  cameras: Camera[];
  selected: number[];
  onChange: (ids: number[]) => void;
}) {
  const toggle = (id: number) => {
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  };
  return (
    <div
      style={{
        display: "grid",
        gap: 4,
        maxHeight: 160,
        overflowY: "auto",
        border: "1px solid var(--border)",
        borderRadius: 6,
        padding: 8,
      }}
    >
      {cameras.map((cam) => (
        <label key={cam.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
          <input type="checkbox" checked={selected.includes(cam.id)} onChange={() => toggle(cam.id)} />
          {cam.name}
        </label>
      ))}
      {cameras.length === 0 && <div style={{ color: "var(--text-dim)", fontSize: 13 }}>No cameras yet.</div>}
    </div>
  );
}

function KioskViewForm({
  cameras,
  initialName = "",
  initialLayout = 4,
  initialCameraIds = [],
  onSubmit,
  onCancel,
  submitLabel,
}: {
  cameras: Camera[];
  initialName?: string;
  initialLayout?: number;
  initialCameraIds?: number[];
  onSubmit: (payload: { name: string; layout: number; camera_ids: number[] }) => Promise<void>;
  onCancel?: () => void;
  submitLabel: string;
}) {
  const [name, setName] = useState(initialName);
  const [layout, setLayout] = useState(initialLayout);
  const [cameraIds, setCameraIds] = useState<number[]>(initialCameraIds);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({ name, layout, camera_ids: cameraIds });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 10, marginTop: 12 }}>
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Living room tablet" />
      </div>
      <div className="field">
        <label>Layout (cameras per page)</label>
        <select value={layout} onChange={(e) => setLayout(Number(e.target.value))}>
          <option value={1}>1</option>
          <option value={4}>4 (2x2)</option>
          <option value={6}>6 (1 large + 5 small)</option>
          <option value={9}>9 (3x3)</option>
          <option value={16}>16 (4x4)</option>
        </select>
        <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
          Just the starting layout - viewers can switch it live from the kiosk display itself.
        </p>
      </div>
      <div className="field">
        <label>Cameras</label>
        <KioskCameraPicker cameras={cameras} selected={cameraIds} onChange={setCameraIds} />
      </div>
      {error && <div className="error-text">{error}</div>}
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" className="btn btn-primary btn-sm" disabled={submitting} onClick={handleSubmit}>
          {submitting ? "Saving..." : submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="btn btn-sm" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}

export function KioskTab() {
  const { fmtDateTime } = useSettings();
  const [views, setViews] = useState<KioskView[]>([]);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [copiedId, setCopiedId] = useState<number | null>(null);

  const refresh = () => {
    listKioskViews().then(setViews);
  };

  useEffect(() => {
    Promise.all([listKioskViews(), listCameras()]).then(([v, c]) => {
      setViews(v);
      setCameras(c);
      setLoading(false);
    });
  }, []);

  if (loading) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const cameraName = (id: number) => cameras.find((c) => c.id === id)?.name ?? `Camera #${id}`;

  const handleCopy = (view: KioskView) => {
    navigator.clipboard.writeText(kioskShareUrl(view.token)).then(() => {
      setCopiedId(view.id);
      setTimeout(() => setCopiedId(null), 2000);
    });
  };

  const handleRegenerate = async (id: number) => {
    if (
      !window.confirm(
        "This invalidates the current link - any tablet/monitor using it will stop working until you update it with the new link. Continue?"
      )
    ) {
      return;
    }
    await regenerateKioskToken(id);
    refresh();
  };

  const handleDelete = async (id: number) => {
    if (!window.confirm("Delete this kiosk display? Its link will stop working immediately.")) return;
    await deleteKioskView(id);
    refresh();
  };

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 640 }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Kiosk displays</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          A view-only link for a tablet or monitor - no login, no editing, just a live camera grid. Anyone with the
          link can view it, so treat it like a password; regenerate the link any time to revoke access.
        </p>

        {!creating ? (
          <button type="button" className="btn btn-sm" onClick={() => setCreating(true)}>
            + New kiosk display
          </button>
        ) : (
          <KioskViewForm
            cameras={cameras}
            submitLabel="Create"
            onCancel={() => setCreating(false)}
            onSubmit={async (payload) => {
              await createKioskView(payload);
              setCreating(false);
              refresh();
            }}
          />
        )}
      </div>

      {views.map((view) => (
        <div key={view.id} className="card">
          {editingId === view.id ? (
            <KioskViewForm
              cameras={cameras}
              initialName={view.name}
              initialLayout={view.layout}
              initialCameraIds={view.camera_ids}
              submitLabel="Save"
              onCancel={() => setEditingId(null)}
              onSubmit={async (payload) => {
                await updateKioskView(view.id, payload);
                setEditingId(null);
                refresh();
              }}
            />
          ) : (
            <>
              <h3 style={{ marginTop: 0 }}>{view.name}</h3>
              <div style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 8 }}>
                {view.camera_ids.length} camera{view.camera_ids.length === 1 ? "" : "s"} (
                {view.camera_ids.map(cameraName).join(", ") || "none"}) - layout {view.layout}
              </div>
              {view.last_viewed_at && (
                <div style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 8 }}>
                  Last viewed: {fmtDateTime(view.last_viewed_at)}
                </div>
              )}
              <div className="field">
                <label>Shareable link</label>
                <input
                  type="text"
                  readOnly
                  value={kioskShareUrl(view.token)}
                  onClick={(e) => (e.target as HTMLInputElement).select()}
                />
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button type="button" className="btn btn-sm" onClick={() => handleCopy(view)}>
                  {copiedId === view.id ? "Copied!" : "Copy link"}
                </button>
                <button type="button" className="btn btn-sm" onClick={() => setEditingId(view.id)}>
                  Edit
                </button>
                <button type="button" className="btn btn-sm" onClick={() => handleRegenerate(view.id)}>
                  Regenerate link
                </button>
                <button type="button" className="btn btn-sm btn-danger" onClick={() => handleDelete(view.id)}>
                  Delete
                </button>
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function SecuritySettingsCard() {
  const [settings, setSettings] = useState<SecuritySettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getSecuritySettings().then(setSettings);
  }, []);

  if (!settings) return null;

  const update = <K extends keyof SecuritySettings>(key: K, value: SecuritySettings[K]) => {
    setSettings((s) => (s ? { ...s, [key]: value } : s));
    setSaved(false);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await updateSecuritySettings(settings);
      setSettings(result);
      setSaved(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card" style={{ maxWidth: 400, marginTop: 20 }}>
      <h3 style={{ marginTop: 0 }}>Session settings</h3>
      <div className="field">
        <label>Sign-in session length (minutes)</label>
        <input
          type="number"
          min={5}
          value={settings.access_token_expire_minutes}
          onChange={(e) => update("access_token_expire_minutes", Number(e.target.value))}
        />
      </div>
      <div className="field">
        <label>"Remember me" duration (days)</label>
        <input
          type="number"
          min={1}
          value={settings.refresh_token_expire_days}
          onChange={(e) => update("refresh_token_expire_days", Number(e.target.value))}
        />
      </div>
      <button className="btn btn-primary" onClick={handleSave} disabled={saving} style={{ width: "100%" }}>
        {saving ? "Saving..." : saved ? "Saved" : "Save changes"}
      </button>
    </div>
  );
}

export function PushNotificationsCard() {
  const supported = isPushSupported();
  const installed = isRunningAsInstalledApp();
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);

  useEffect(() => {
    if (!supported) return;
    getCurrentSubscription().then((sub) => setSubscribed(!!sub));
  }, [supported]);

  const handleEnable = async () => {
    setBusy(true);
    setError(null);
    try {
      await subscribeToPush();
      setSubscribed(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleDisable = async () => {
    setBusy(true);
    setError(null);
    try {
      await unsubscribeFromPush();
      setSubscribed(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleTest = async () => {
    setError(null);
    setTestResult(null);
    try {
      const result = await testPush();
      setTestResult(result.message);
    } catch (err) {
      setTestResult((err as Error).message);
    }
  };

  if (!supported) {
    return (
      <div className="card" style={{ maxWidth: 400, marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Push notifications</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
          Not available - this needs HTTPS (see Settings → Security) and a browser that supports push.
        </p>
      </div>
    );
  }

  const iosNeedsInstall = /iphone|ipad|ipod/i.test(navigator.userAgent) && !installed;

  return (
    <div className="card" style={{ maxWidth: 400, marginTop: 20 }}>
      <h3 style={{ marginTop: 0 }}>Push notifications</h3>
      <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
        Get motion/offline/storage alerts as notifications on this device, even when LightNVR isn't open.
      </p>
      {iosNeedsInstall ? (
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
          On iPhone/iPad, push only works for an installed app: tap Share → "Add to Home Screen", then open LightNVR
          from the home screen icon and come back here.
        </p>
      ) : (
        <>
          {error && <div className="error-text">{error}</div>}
          {subscribed ? (
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn btn-sm" onClick={handleTest}>
                Send test
              </button>
              <button className="btn btn-sm btn-danger" onClick={handleDisable} disabled={busy}>
                Disable on this device
              </button>
            </div>
          ) : (
            <button className="btn btn-sm btn-primary" onClick={handleEnable} disabled={busy}>
              {busy ? "Enabling..." : "Enable on this device"}
            </button>
          )}
          {testResult && <div style={{ marginTop: 8, fontSize: 13 }}>{testResult}</div>}
        </>
      )}
    </div>
  );
}

function ChangePasswordCard() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(false);

    if (newPassword !== confirmPassword) {
      setError("New passwords don't match");
      return;
    }

    setSubmitting(true);
    try {
      await changeMyPassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setSuccess(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="card" style={{ maxWidth: 400, marginTop: 20 }}>
      <h3 style={{ marginTop: 0 }}>Change password</h3>
      <form onSubmit={handleSubmit}>
        <div className="field">
          <label>Current password</label>
          <input
            type="password"
            required
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
          />
        </div>
        <div className="field">
          <label>New password</label>
          <input type="password" required minLength={8} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
        </div>
        <div className="field">
          <label>Confirm new password</label>
          <input
            type="password"
            required
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
          />
        </div>
        {error && <div className="error-text">{error}</div>}
        {success && <div style={{ color: "var(--success)", fontSize: 13, marginTop: 8 }}>Password changed.</div>}
        <button type="submit" className="btn btn-primary" disabled={submitting} style={{ width: "100%", marginTop: 8 }}>
          {submitting ? "Saving..." : "Change password"}
        </button>
      </form>
    </div>
  );
}

const SUGGESTED_TIMEZONES = typeof (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf === "function"
  ? (Intl as unknown as { supportedValuesOf: (k: string) => string[] }).supportedValuesOf("timeZone")
  : ["UTC", "America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London", "Europe/Paris",
     "Asia/Kolkata", "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney"];

function SystemTab() {
  const { reload: reloadSettings, fmtDateTime } = useSettings();
  const [timezone, setTimezone] = useState("");
  const [ntpServer, setNtpServer] = useState("pool.ntp.org");
  const [serverTime, setServerTime] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSystemSettings().then((s) => {
      setTimezone(s.timezone);
      setNtpServer(s.ntp_server);
    });
    getSystemTime().then((t) => setServerTime(t.server_utc));
  }, []);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateSystemSettings({ timezone, ntp_server: ntpServer });
      // Refresh the app-wide timezone so every timestamp re-renders in the new
      // zone immediately, without a manual page reload. (The context also
      // mirrors the value into localStorage.)
      reloadSettings();
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const fmtTime = (iso: string | null) => (iso ? fmtDateTime(iso) : "—");

  return (
    <div className="card" style={{ maxWidth: 520 }}>
      <h2 style={{ marginTop: 0 }}>System</h2>

      {serverTime && (
        <div style={{ marginBottom: 20, fontSize: 13, color: "var(--text-dim)" }}>
          <div>Server time: <strong style={{ color: "var(--text)" }}>{fmtTime(serverTime)}</strong></div>
          <div>Client time: <strong style={{ color: "var(--text)" }}>{fmtTime(new Date().toISOString())}</strong></div>
          <div style={{ fontSize: 11, marginTop: 4 }}>
            Offset: {Math.round((new Date().getTime() - new Date(serverTime).getTime()) / 1000)}s
          </div>
        </div>
      )}

      <div className="field">
        <label>Display timezone</label>
        <input
          type="text"
          list="tz-list"
          placeholder="Leave blank to use browser timezone"
          value={timezone}
          onChange={(e) => setTimezone(e.target.value)}
        />
        <datalist id="tz-list">
          {SUGGESTED_TIMEZONES.map((tz) => (
            <option key={tz} value={tz} />
          ))}
        </datalist>
        <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
          IANA timezone (e.g. Asia/Kolkata, America/New_York). Blank = use browser timezone.
          Timestamps throughout the app will display in this timezone.
        </p>
      </div>

      <div className="field">
        <label>NTP server</label>
        <input
          type="text"
          value={ntpServer}
          onChange={(e) => setNtpServer(e.target.value)}
          placeholder="pool.ntp.org"
        />
        <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
          Reference server for time sync. The NVR host syncs automatically via the OS;
          this setting is used when pushing NTP configuration to cameras via ONVIF.
        </p>
      </div>

      {error && <div className="error-text">{error}</div>}
      <button className="btn btn-primary" disabled={saving} onClick={save}>
        {saving ? "Saving…" : saved ? "Saved" : "Save"}
      </button>
    </div>
  );
}

export function SettingsPage() {
  const { user, isAdmin } = useAuth();
  const [tab, setTab] = useState<Tab>("account");

  return (
    <div className="page">
      <div className="page-header">
        <h1>Settings</h1>
      </div>

      <div className="tabs">
        <div className={`tab ${tab === "account" ? "active" : ""}`} onClick={() => setTab("account")}>
          Account
        </div>
        {isAdmin && (
          <>
            <div className={`tab ${tab === "users" ? "active" : ""}`} onClick={() => setTab("users")}>
              Users
            </div>
            <div className={`tab ${tab === "alerts" ? "active" : ""}`} onClick={() => setTab("alerts")}>
              Alerts
            </div>
            <div className={`tab ${tab === "storage" ? "active" : ""}`} onClick={() => setTab("storage")}>
              Storage
            </div>
            <div className={`tab ${tab === "backup" ? "active" : ""}`} onClick={() => setTab("backup")}>
              Backup
            </div>
            <div className={`tab ${tab === "security" ? "active" : ""}`} onClick={() => setTab("security")}>
              Security
            </div>
            <div className={`tab ${tab === "remote-access" ? "active" : ""}`} onClick={() => setTab("remote-access")}>
              Remote Access
            </div>
            <div className={`tab ${tab === "kiosk" ? "active" : ""}`} onClick={() => setTab("kiosk")}>
              Kiosk Displays
            </div>
            <div className={`tab ${tab === "system" ? "active" : ""}`} onClick={() => setTab("system")}>
              System
            </div>
          </>
        )}
      </div>

      {tab === "account" && (
        <>
          <div className="card" style={{ maxWidth: 400 }}>
            <div className="field">
              <label>Username</label>
              <div>{user?.username}</div>
            </div>
            <div className="field">
              <label>Role</label>
              <div style={{ textTransform: "capitalize" }}>{user?.role}</div>
            </div>
          </div>
          <PushNotificationsCard />
          <ChangePasswordCard />
          {isAdmin && <SecuritySettingsCard />}
        </>
      )}
      {tab === "users" && isAdmin && <UsersTab currentUser={user} />}
      {tab === "alerts" && isAdmin && <AlertsTab />}
      {tab === "storage" && isAdmin && <StorageTab />}
      {tab === "backup" && isAdmin && <BackupTab />}
      {tab === "security" && isAdmin && <TlsTab />}
      {tab === "remote-access" && isAdmin && <RemoteAccessTab />}
      {tab === "kiosk" && isAdmin && <KioskTab />}
      {tab === "system" && isAdmin && <SystemTab />}
    </div>
  );
}
