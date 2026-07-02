import { useEffect, useState } from "react";
import {
  discoverCameraRange,
  discoverCameras,
  discoverDefaultRange,
  getDiscoverySettings,
  probeCamera,
  updateDiscoverySettings,
} from "../api/cameras";
import type { CameraCreatePayload, DiscoveredDevice, ProbeProfile, ProbeResponse } from "../api/types";
import { CameraDetailsForm } from "./CameraDetailsForm";

type Tab = "discover" | "manual";

const BLANK_FORM: CameraCreatePayload = {
  name: "",
  rtsp_main_url: "",
  rtsp_sub_url: "",
  username: "",
  password: "",
  codec: "h264",
  has_audio: false,
  recording_mode: "continuous",
  motion_enabled: true,
  motion_sensitivity: 50,
};

const CAMERA_ICON = (
  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 7h11l3-3h2v14h-2l-3-3H3z" strokeLinejoin="round" />
    <circle cx="9" cy="11" r="2.5" />
  </svg>
);

interface Props {
  onCreate: (payload: CameraCreatePayload) => Promise<void>;
  onClose: () => void;
  submitting: boolean;
  serverError: string | null;
}

export function CameraSetupModal({ onCreate, onClose, submitting, serverError }: Props) {
  const [tab, setTab] = useState<Tab>("discover");

  const [scanning, setScanning] = useState(false);
  const [devices, setDevices] = useState<DiscoveredDevice[] | null>(null);
  const [scanError, setScanError] = useState<string | null>(null);
  // After a quick multicast pass finds nothing, automatically checks common
  // home-router subnets directly (no multicast needed) before giving up -
  // covers the common case (Docker bridge networking blocks multicast
  // replies) with zero typing required.
  const [autoRangeScanning, setAutoRangeScanning] = useState(false);
  const [triedAutoRange, setTriedAutoRange] = useState(false);

  const [selectedDevice, setSelectedDevice] = useState<DiscoveredDevice | null>(null);
  const [onvifUser, setOnvifUser] = useState("admin");
  const [onvifPass, setOnvifPass] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const [manualHost, setManualHost] = useState("");
  const [manualPort, setManualPort] = useState("");

  const [rangeCidr, setRangeCidr] = useState("");
  const [rangeScanning, setRangeScanning] = useState(false);
  const [rangeError, setRangeError] = useState<string | null>(null);
  const [rangeDevices, setRangeDevices] = useState<DiscoveredDevice[] | null>(null);
  const [savedRange, setSavedRange] = useState(false);

  const runRangeScan = async () => {
    if (!rangeCidr) return;
    setRangeScanning(true);
    setRangeError(null);
    setSavedRange(false);
    try {
      setRangeDevices(await discoverCameraRange(rangeCidr));
    } catch (err) {
      setRangeError((err as Error).message);
    } finally {
      setRangeScanning(false);
    }
  };

  const saveRangeForFuture = async () => {
    try {
      const existing = await getDiscoverySettings();
      const next = Array.from(new Set([...existing.custom_subnets, rangeCidr]));
      await updateDiscoverySettings(next);
      setSavedRange(true);
    } catch (err) {
      setRangeError((err as Error).message);
    }
  };

  const [detailsInitial, setDetailsInitial] = useState<CameraCreatePayload | null>(null);
  const [detectedSummary, setDetectedSummary] = useState<string | null>(null);

  const runScan = async () => {
    setScanning(true);
    setScanError(null);
    setTriedAutoRange(false);
    try {
      const found = await discoverCameras();
      if (found.length > 0) {
        setDevices(found);
        return;
      }
      // Multicast found nothing - try common home-router subnets directly
      // before surfacing "not found" to the user.
      setScanning(false);
      setAutoRangeScanning(true);
      setTriedAutoRange(true);
      const fallback = await discoverDefaultRange();
      setDevices(fallback);
    } catch (err) {
      setScanError((err as Error).message);
    } finally {
      setScanning(false);
      setAutoRangeScanning(false);
    }
  };

  // Scanning starts the instant the modal opens - no extra click needed.
  useEffect(() => {
    if (tab === "discover" && devices === null && !scanning) {
      runScan();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const connectToDevice = (device: DiscoveredDevice) => {
    setSelectedDevice(device);
    setConnectError(null);
  };

  const handleConnect = async () => {
    if (!selectedDevice) return;
    setConnecting(true);
    setConnectError(null);
    try {
      const result = await probeCamera({
        host: selectedDevice.host,
        // Pass null when port is unknown — the backend probes common ONVIF
        // ports and picks whichever one responds. Discovered devices already
        // have a confirmed port from the subnet scan, so pass it through.
        port: selectedDevice.port || null,
        username: onvifUser,
        password: onvifPass,
      });
      // Update the selected device with the port that actually worked so that
      // the stream URIs and ONVIF address stored on the camera record are correct.
      setSelectedDevice((prev) => prev && result.detected_port !== prev.port
        ? { ...prev, port: result.detected_port, address: `${prev.host}:${result.detected_port}` }
        : prev
      );
      proceedAutomatically(result);
    } catch (err) {
      setConnectError((err as Error).message);
    } finally {
      setConnecting(false);
    }
  };

  // The whole point of "one-click add": the backend already picked the
  // highest-resolution profile as main and lowest as sub, so there's no
  // dropdown step here - straight from credentials to the confirm screen.
  // If it picked wrong, the URL fields on that screen are editable directly.
  const proceedAutomatically = (probeResult: ProbeResponse) => {
    const mainProfile = probeResult.profiles.find((p) => p.token === probeResult.recommended_main_token);
    const subProfile = probeResult.profiles.find((p) => p.token === probeResult.recommended_sub_token);
    if (!mainProfile) {
      setConnectError("Camera didn't report any usable media profiles");
      return;
    }

    // Use RTSP-validated URLs when available (correct scheme, credentials, no
    // ONVIF-only params). Fall back to the profile URI if validation couldn't run.
    const mainUrl = probeResult.validated_main_url || mainProfile.stream_uri;
    const subUrl = probeResult.validated_sub_url ||
      (subProfile && subProfile.token !== mainProfile.token ? subProfile.stream_uri : "");

    // If the backend found that a different username works for RTSP (e.g. "admin"
    // when the user typed "onvifuser"), apply it to the saved camera record.
    const effectiveUser = probeResult.resolved_username || onvifUser;
    if (probeResult.resolved_username && probeResult.resolved_username !== onvifUser) {
      setOnvifUser(probeResult.resolved_username);
    }

    const describe = (p: ProbeProfile | undefined) => (p?.width && p?.height ? `${p.width}×${p.height}` : p?.name ?? "");
    const codecLabel = probeResult.codec ? ` · ${probeResult.codec.toUpperCase()}` : "";
    const subDesc = subUrl
      ? ` and sub stream ${describe(subProfile?.token !== mainProfile.token ? subProfile : undefined) || "detected"}`
      : " (no sub-stream detected)";
    setDetectedSummary(
      `Detected main stream ${describe(mainProfile)}${codecLabel}${subDesc}.`
    );

    setDetailsInitial({
      ...BLANK_FORM,
      name: probeResult.model !== "unknown" ? probeResult.model : selectedDevice?.host || "New Camera",
      rtsp_main_url: mainUrl,
      rtsp_sub_url: subUrl,
      onvif_address: selectedDevice?.address || null,
      username: effectiveUser,
      password: onvifPass,
      codec: (probeResult.codec === "h265" ? "h265" : "h264") as "h264" | "h265",
      has_audio: probeResult.has_audio ?? false,
    });
  };

  const startManual = () => {
    setDetectedSummary(null);
    setDetailsInitial({ ...BLANK_FORM });
  };

  if (detailsInitial) {
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <h2>Camera details</h2>
            <button className="close-btn" onClick={onClose}>
              ×
            </button>
          </div>
          {detectedSummary && (
            <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: -8 }}>{detectedSummary}</p>
          )}
          <CameraDetailsForm
            initial={detailsInitial}
            submitLabel="Add camera"
            submitting={submitting}
            serverError={serverError}
            onSubmit={(payload) => onCreate(payload)}
            onCancel={() => setDetailsInitial(null)}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Add camera</h2>
          <button className="close-btn" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="tabs">
          <div className={`tab ${tab === "discover" ? "active" : ""}`} onClick={() => setTab("discover")}>
            Auto-discover (ONVIF)
          </div>
          <div className={`tab ${tab === "manual" ? "active" : ""}`} onClick={() => setTab("manual")}>
            Manual RTSP
          </div>
        </div>

        {tab === "discover" ? (
          <div>
            {!selectedDevice && (
              <>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-dim)", fontSize: 13 }}>
                    {scanning
                      ? "Scanning your network for cameras..."
                      : autoRangeScanning
                        ? "Quick scan found nothing - checking common home network ranges (this can take a few minutes)..."
                        : "Discovered cameras"}
                  </span>
                  <button className="btn btn-sm" onClick={runScan} disabled={scanning || autoRangeScanning}>
                    {scanning || autoRangeScanning ? "Scanning..." : "Rescan"}
                  </button>
                </div>
                {scanError && <div className="error-text">{scanError}</div>}

                {devices && devices.length === 0 && !scanning && !autoRangeScanning && (
                  <p style={{ color: "var(--text-dim)", marginTop: 12 }}>
                    No ONVIF cameras found{triedAutoRange ? " - checked common home network ranges too" : ""}. Your
                    network may use an uncommon subnet. Scan it directly below, enter the camera's IP, or use Manual
                    RTSP.
                  </p>
                )}

                {devices && devices.length > 0 && (
                  <div className="discovered-grid" style={{ marginTop: 12 }}>
                    {devices.map((d) => (
                      <div key={d.address} className="discovered-card" onClick={() => connectToDevice(d)}>
                        <div className="thumb-placeholder">{CAMERA_ICON}</div>
                        <div className="device-name">{d.name_hint || d.hardware_hint || d.host}</div>
                        <div className="device-meta">{d.host}</div>
                        {(d.hardware_hint || d.mac_address) && (
                          <div className="device-meta">
                            {d.hardware_hint}
                            {d.hardware_hint && d.mac_address ? " · " : ""}
                            {d.mac_address}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {devices && devices.length === 0 && !scanning && !autoRangeScanning && (
                  <div style={{ marginTop: 14 }}>
                    <label>Or scan a network range directly</label>
                    <div className="form-row">
                      <input
                        type="text"
                        style={{ flex: 1 }}
                        placeholder="e.g. 192.168.68.0/24"
                        value={rangeCidr}
                        onChange={(e) => setRangeCidr(e.target.value)}
                      />
                      <button type="button" className="btn btn-sm" disabled={!rangeCidr || rangeScanning} onClick={runRangeScan}>
                        {rangeScanning ? "Scanning..." : "Scan range"}
                      </button>
                    </div>
                    <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 4 }}>
                      Checks every address in the range directly (no multicast needed) - a /24 takes about 40 seconds,
                      larger ranges longer.
                    </p>
                    {rangeError && <div className="error-text">{rangeError}</div>}
                    {rangeDevices && rangeDevices.length === 0 && !rangeScanning && (
                      <p style={{ color: "var(--text-dim)" }}>No ONVIF devices found in that range.</p>
                    )}
                    {rangeDevices && (
                      <div style={{ marginTop: 8 }}>
                        <button type="button" className="btn btn-sm" disabled={savedRange} onClick={saveRangeForFuture}>
                          {savedRange ? "Saved - will auto-scan this range from now on" : "Save this range for future automatic scans"}
                        </button>
                      </div>
                    )}
                    {rangeDevices && rangeDevices.length > 0 && (
                      <div className="discovered-grid" style={{ marginTop: 12 }}>
                        {rangeDevices.map((d) => (
                          <div key={d.address} className="discovered-card" onClick={() => connectToDevice(d)}>
                            <div className="thumb-placeholder">{CAMERA_ICON}</div>
                            <div className="device-name">{d.name_hint || d.hardware_hint || d.host}</div>
                            <div className="device-meta">{d.host}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                <div style={{ marginTop: 14 }}>
                  <label>Or connect directly by IP</label>
                  <div className="form-row">
                    <input
                      type="text"
                      style={{ flex: 1 }}
                      placeholder="Camera IP, e.g. 192.168.1.50"
                      value={manualHost}
                      onChange={(e) => setManualHost(e.target.value)}
                    />
                    <input
                      type="text"
                      style={{ maxWidth: 90 }}
                      placeholder="Port (auto)"
                      value={manualPort}
                      onChange={(e) => setManualPort(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={!manualHost}
                      onClick={() =>
                        connectToDevice({
                          host: manualHost,
                          // 0 = unknown; handleConnect passes null to backend which
                          // then probes common ONVIF ports to find the right one.
                          port: Number(manualPort) || 0,
                          address: manualPort ? `${manualHost}:${manualPort}` : manualHost,
                          scopes: [],
                          hardware_hint: null,
                          name_hint: null,
                          mac_address: null,
                        })
                      }
                    >
                      Connect
                    </button>
                  </div>
                </div>
              </>
            )}

            {selectedDevice && (
              <div style={{ marginTop: 16 }}>
                <p style={{ color: "var(--text-dim)" }}>
                  Connecting to {selectedDevice.name_hint || selectedDevice.hardware_hint || selectedDevice.address}
                </p>
                <div className="form-row">
                  <div className="field">
                    <label>ONVIF username</label>
                    <input type="text" autoFocus value={onvifUser} onChange={(e) => setOnvifUser(e.target.value)} />
                  </div>
                  <div className="field">
                    <label>ONVIF password</label>
                    <input type="password" value={onvifPass} onChange={(e) => setOnvifPass(e.target.value)} />
                  </div>
                </div>
                {connectError && <div className="error-text">{connectError}</div>}
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="btn" onClick={() => setSelectedDevice(null)}>
                    Back
                  </button>
                  <button className="btn btn-primary" onClick={handleConnect} disabled={connecting}>
                    {connecting ? "Connecting..." : "Add"}
                  </button>
                </div>
              </div>
            )}
          </div>
        ) : (
          <div>
            <p style={{ color: "var(--text-dim)" }}>
              Enter the RTSP URL directly if your camera doesn't support ONVIF or is on a different network.
            </p>
            <button className="btn btn-primary" onClick={startManual}>
              Continue
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
