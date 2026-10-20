import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { buildMediaUrl } from "../api/client";
import { ackEvent, getEvent } from "../api/security";
import type { NvrEvent } from "../api/types";

/** One event, as opened from a notification: picture, full message, and
 *  "I've seen it" for urgent alerts that would otherwise be sent again. */
export function EventPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const eventId = Number(id);
  const [event, setEvent] = useState<NvrEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [acked, setAcked] = useState(false);

  useEffect(() => {
    if (!eventId) return;
    getEvent(eventId)
      .then((e) => {
        setEvent(e);
        setAcked(!!e.acked_at);
      })
      .catch((e) => setError((e as Error).message));
  }, [eventId]);

  useEffect(() => {
    // Opened from the notification's "I've seen it" action.
    if (params.get("ack") === "1" && eventId && !acked) {
      ackEvent(eventId).then(() => setAcked(true)).catch(() => undefined);
    }
  }, [params, eventId, acked]);

  if (error) return <div className="page"><div className="error-text">{error}</div></div>;
  if (!event) return <div className="page"><p style={{ color: "var(--text-dim)" }}>Loading…</p></div>;

  const urgent = (event.priority ?? 0) >= 2;
  return (
    <div className="page" style={{ maxWidth: 900 }}>
      <div className="page-header">
        <h1 style={{ textTransform: "capitalize" }}>{event.type.replace(/_/g, " ")}</h1>
      </div>
      <div className="card">
        <div style={{ color: "var(--text-dim)", fontSize: 13, marginBottom: 8 }}>
          {new Date(event.created_at).toLocaleString()}
          {urgent && <span style={{ color: "var(--danger, #f85149)", marginLeft: 8 }}>URGENT</span>}
        </div>
        {event.snapshot_path && (
          <img
            src={buildMediaUrl(`/system/events/${event.id}/snapshot.jpg`)}
            alt=""
            style={{ width: "100%", borderRadius: 8, marginBottom: 10 }}
          />
        )}
        <pre style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", margin: 0 }}>{event.message}</pre>
        <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
          {urgent && (
            <button
              className="btn btn-primary"
              disabled={acked}
              onClick={() => ackEvent(event.id).then(() => setAcked(true))}
            >
              {acked ? "Acknowledged" : "I've seen it"}
            </button>
          )}
          {event.camera_id !== null && (
            <Link className="btn" to={`/playback?camera=${event.camera_id}&at=${encodeURIComponent(event.created_at)}`}>
              Watch recording
            </Link>
          )}
          <Link className="btn" to="/">Dashboard</Link>
        </div>
      </div>
    </div>
  );
}
