import { useEffect, useState } from "react";
import { updateCamera } from "../api/cameras";
import { getCameraGroups, setCameraGroupsSettings, type CameraGroups } from "../api/security";

const NEW = "__new__";

/** Which cameras watch the same place. Their events are one visit; cameras in
 *  different groups are separate visits, unless the same person turns up on
 *  the other group's camera within a minute or so. */
export function CameraGroupsCard() {
  const [data, setData] = useState<CameraGroups | null>(null);
  const [naming, setNaming] = useState<number | null>(null);
  const [newName, setNewName] = useState("");
  const [seconds, setSeconds] = useState(60);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = () =>
    getCameraGroups()
      .then((d) => {
        setData(d);
        setSeconds(d.join_seconds);
      })
      .catch((e) => setErr((e as Error).message));
  useEffect(() => {
    load();
  }, []);

  const setGroup = async (cameraId: number, group: string | null) => {
    setErr(null);
    setMsg(null);
    try {
      await updateCamera(cameraId, { area: group });
      setNaming(null);
      setNewName("");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  if (!data) return err ? <div className="card error-text">{err}</div> : null;

  return (
    <div className="card">
      <strong>Camera groups</strong>
      <p style={{ color: "var(--text-dim)", fontSize: 13, margin: "4px 0 10px" }}>
        Put cameras that watch the <strong>same place</strong> in one group - their events become one visit. Cameras in
        different groups make separate visits, unless the <strong>same person</strong> (a recognised face, or matching
        clothes) shows up on the other group&apos;s camera soon after - then it&apos;s one visit with a route, e.g.
        &quot;Front porch → Back yard&quot;. A camera on its own is never lumped in with the others.
      </p>
      <div className="table-wrap" style={{ border: "none" }}>
        <table>
          <thead>
            <tr>
              <th>Camera</th>
              <th>Group</th>
            </tr>
          </thead>
          <tbody>
            {data.cameras.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>
                  {naming === c.id ? (
                    <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <input
                        autoFocus
                        placeholder="e.g. Front porch"
                        maxLength={64}
                        value={newName}
                        style={{ width: 160 }}
                        onChange={(e) => setNewName(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && newName.trim() && setGroup(c.id, newName.trim())}
                      />
                      <button className="btn btn-sm btn-primary" disabled={!newName.trim()} onClick={() => setGroup(c.id, newName.trim())}>
                        Save
                      </button>
                      <button className="btn btn-sm" onClick={() => setNaming(null)}>Cancel</button>
                    </span>
                  ) : (
                    <select
                      value={c.group ?? ""}
                      style={{ width: "auto" }}
                      onChange={(e) => {
                        if (e.target.value === NEW) {
                          setNaming(c.id);
                          setNewName("");
                        } else {
                          setGroup(c.id, e.target.value || null);
                        }
                      }}
                    >
                      <option value="">On its own</option>
                      {data.groups.map((g) => <option key={g} value={g}>{g}</option>)}
                      <option value={NEW}>New group…</option>
                    </select>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 10, fontSize: 13 }}>
        <span>The same person on a camera of another group within</span>
        <input
          type="number"
          min={10}
          max={600}
          value={seconds}
          style={{ width: 80 }}
          onChange={(e) => setSeconds(Number(e.target.value))}
        />
        <span>seconds joins the visit.</span>
        <button
          className="btn btn-sm"
          disabled={seconds === data.join_seconds || seconds < 10 || seconds > 600}
          onClick={() =>
            setCameraGroupsSettings(seconds)
              .then(() => {
                setMsg("Saved.");
                load();
              })
              .catch((e) => setErr((e as Error).message))
          }
        >
          Save
        </button>
        {msg && <span style={{ color: "var(--success)" }}>{msg}</span>}
      </div>
      <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
        Changing a camera&apos;s group takes effect for its next event - recording isn&apos;t interrupted.
      </div>
      {err && <div className="error-text" style={{ marginTop: 6 }}>{err}</div>}
    </div>
  );
}
