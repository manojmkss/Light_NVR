import { useEffect, useState } from "react";
import { createCamera, deleteCamera, listCameras, redetectCamera, updateCamera } from "../api/cameras";
import type { Camera, CameraCreatePayload } from "../api/types";
import { CameraDetailsForm } from "../components/CameraDetailsForm";
import { CameraSetupModal } from "../components/CameraSetupModal";
import { StatusBadge } from "../components/StatusBadge";
import { useAuth } from "../context/AuthContext";

export function CamerasPage() {
  const { isAdmin } = useAuth();
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [loading, setLoading] = useState(true);
  const [showSetup, setShowSetup] = useState(false);
  const [editingCamera, setEditingCamera] = useState<Camera | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [redetectingId, setRedetectingId] = useState<number | null>(null);
  const [redetectNotice, setRedetectNotice] = useState<string | null>(null);

  const refresh = () => {
    listCameras()
      .then(setCameras)
      .finally(() => setLoading(false));
  };

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

  const handleUpdate = async (payload: CameraCreatePayload) => {
    if (!editingCamera) return;
    setSubmitting(true);
    setServerError(null);
    const cleaned = { ...payload };
    if (!cleaned.password) delete cleaned.password;
    try {
      await updateCamera(editingCamera.id, cleaned);
      setEditingCamera(null);
      refresh();
    } catch (err) {
      setServerError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggleEnabled = async (camera: Camera) => {
    await updateCamera(camera.id, { enabled: !camera.enabled });
    refresh();
  };

  const handleToggleFavorite = async (camera: Camera) => {
    await updateCamera(camera.id, { is_favorite: !camera.is_favorite });
    refresh();
  };

  const handleDelete = async (id: number) => {
    await deleteCamera(id);
    setConfirmDeleteId(null);
    refresh();
  };

  const handleRedetect = async (camera: Camera) => {
    setRedetectingId(camera.id);
    setRedetectNotice(null);
    try {
      const updated = await redetectCamera(camera.id);
      setRedetectNotice(
        `${updated.name}: streams re-detected (${updated.codec.toUpperCase()}${updated.rtsp_sub_url ? ", sub-stream found" : ", no sub-stream"})`
      );
      refresh();
    } catch (err) {
      setRedetectNotice(`${camera.name}: ${(err as Error).message}`);
    } finally {
      setRedetectingId(null);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h1>Cameras</h1>
        {isAdmin && (
          <button className="btn btn-primary" onClick={() => setShowSetup(true)}>
            + Add camera
          </button>
        )}
      </div>

      {redetectNotice && (
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: -8 }}>{redetectNotice}</p>
      )}

      {loading ? (
        <p style={{ color: "var(--text-dim)" }}>Loading...</p>
      ) : cameras.length === 0 ? (
        <div className="empty-state">No cameras yet. Click "Add camera" to discover or configure one.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th></th>
                <th>Name</th>
                <th>Status</th>
                <th>Codec</th>
                <th>Mode</th>
                <th>Enabled</th>
                {isAdmin && <th></th>}
              </tr>
            </thead>
            <tbody>
              {cameras.map((cam) => (
                <tr key={cam.id}>
                  <td>
                    <button
                      className={`star-toggle${cam.is_favorite ? " active" : ""}`}
                      title={cam.is_favorite ? "Remove from dashboard favorites" : "Pin to dashboard favorites"}
                      onClick={() => handleToggleFavorite(cam)}
                      disabled={!isAdmin}
                      aria-pressed={cam.is_favorite}
                    >
                      {cam.is_favorite ? "★" : "☆"}
                    </button>
                  </td>
                  <td>{cam.name}</td>
                  <td>
                    <StatusBadge status={cam.status} />
                  </td>
                  <td>{cam.codec.toUpperCase()}</td>
                  <td style={{ textTransform: "capitalize" }}>{cam.recording_mode}</td>
                  <td>
                    {isAdmin ? (
                      <input type="checkbox" checked={cam.enabled} onChange={() => handleToggleEnabled(cam)} />
                    ) : cam.enabled ? (
                      "Yes"
                    ) : (
                      "No"
                    )}
                  </td>
                  {isAdmin && (
                    <td>
                      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                        {cam.onvif_address && (
                          <button
                            className="btn btn-sm"
                            disabled={redetectingId !== null}
                            title="Re-run stream auto-detection (URLs, sub-stream, codec) using the camera's saved credentials"
                            onClick={() => handleRedetect(cam)}
                          >
                            {redetectingId === cam.id ? "Detecting..." : "Re-detect"}
                          </button>
                        )}
                        <button className="btn btn-sm" onClick={() => setEditingCamera(cam)}>
                          Edit
                        </button>
                        {confirmDeleteId === cam.id ? (
                          <>
                            <button className="btn btn-sm btn-danger" onClick={() => handleDelete(cam.id)}>
                              Confirm
                            </button>
                            <button className="btn btn-sm" onClick={() => setConfirmDeleteId(null)}>
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button className="btn btn-sm btn-danger" onClick={() => setConfirmDeleteId(cam.id)}>
                            Delete
                          </button>
                        )}
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showSetup && (
        <CameraSetupModal
          onCreate={handleCreate}
          onClose={() => {
            setShowSetup(false);
            setServerError(null);
          }}
          submitting={submitting}
          serverError={serverError}
        />
      )}

      {editingCamera && (
        <div className="modal-backdrop" onClick={() => setEditingCamera(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Edit {editingCamera.name}</h2>
              <button className="close-btn" onClick={() => setEditingCamera(null)}>
                ×
              </button>
            </div>
            <CameraDetailsForm
              initial={{
                name: editingCamera.name,
                rtsp_main_url: editingCamera.rtsp_main_url,
                rtsp_sub_url: editingCamera.rtsp_sub_url,
                username: editingCamera.username ?? "",
                password: "",
                codec: editingCamera.codec,
                has_audio: editingCamera.has_audio,
                recording_mode: editingCamera.recording_mode,
                motion_enabled: editingCamera.motion_enabled,
                motion_sensitivity: editingCamera.motion_sensitivity,
                retention_days: editingCamera.retention_days,
              }}
              submitLabel="Save changes"
              submitting={submitting}
              serverError={serverError}
              onSubmit={handleUpdate}
              onCancel={() => setEditingCamera(null)}
            />
          </div>
        </div>
      )}
    </div>
  );
}
