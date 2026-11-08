import { useEffect, useRef } from "react";

// What this browser can decode, offered to go2rtc (it sends the first one the
// camera's stream matches). H.264 everywhere; H.265 only where the browser
// has it (Chrome on most phones and Windows, Safari).
const CODECS = ["avc1.640029", "avc1.64002A", "avc1.640033", "hvc1.1.6.L153.B0"];
const START_TIMEOUT_MS = 10_000; // no picture by then: give up (the tile falls back to pictures)
const STALL_MS = 15_000; // no data for this long: the connection is dead
const MAX_LAG_S = 2.5; // further behind live than this (a slow patch): jump to the live edge
const KEEP_BEHIND_S = 10; // how much already-played video to keep buffered

type MediaSourceCtor = typeof MediaSource;

function mediaSourceClass(): MediaSourceCtor | null {
  // iPhone Safari only has ManagedMediaSource.
  const w = window as unknown as { ManagedMediaSource?: MediaSourceCtor; MediaSource?: MediaSourceCtor };
  return w.ManagedMediaSource ?? w.MediaSource ?? null;
}

export const liveVideoSupported = () => mediaSourceClass() !== null;

/** The camera's own video, live, from go2rtc's MSE websocket (see
 *  backend/app/services/live_relay.py): fragmented MP4 appended to a
 *  MediaSource as it arrives, kept at the live edge. `onFail` once if it
 *  can't start or the connection dies - the tile then falls back to the
 *  picture stream. */
export function LiveVideo({
  url,
  className,
  onFail,
  onPlaying,
}: {
  url: string;
  className?: string;
  onFail: (why: string) => void;
  onPlaying?: () => void;
}) {
  const ref = useRef<HTMLVideoElement>(null);
  const failRef = useRef(onFail);
  const playingRef = useRef(onPlaying);
  failRef.current = onFail;
  playingRef.current = onPlaying;

  useEffect(() => {
    const video = ref.current;
    const MS = mediaSourceClass();
    if (!video || !MS) {
      failRef.current("this browser can't play live video");
      return;
    }
    const ms = new MS();
    let ws: WebSocket | null = null;
    let sb: SourceBuffer | null = null;
    let queue: Uint8Array[] = [];
    let done = false;
    let started = false;
    let lastData = Date.now();

    const stop = () => {
      done = true;
      clearTimeout(startTimer);
      clearInterval(watch);
      if (ws) {
        ws.onclose = ws.onerror = ws.onmessage = null;
        ws.close();
      }
    };
    const fail = (why: string) => {
      if (done) return;
      stop();
      failRef.current(why);
    };
    const startTimer = setTimeout(() => !started && fail("no picture"), START_TIMEOUT_MS);
    const watch = setInterval(() => started && Date.now() - lastData > STALL_MS && fail("stalled"), 3000);

    const pump = () => {
      if (done || !sb || sb.updating) return;
      try {
        const b = sb.buffered;
        if (b.length) {
          const end = b.end(b.length - 1);
          if (end - video.currentTime > MAX_LAG_S) video.currentTime = end - 0.3;
          const start = b.start(0);
          if (video.currentTime - start > KEEP_BEHIND_S + 5) {
            sb.remove(start, video.currentTime - KEEP_BEHIND_S);
            return; // carries on at updateend
          }
        }
        if (!queue.length) return;
        const size = queue.reduce((n, q) => n + q.byteLength, 0);
        const data = new Uint8Array(size);
        let at = 0;
        for (const q of queue) {
          data.set(q, at);
          at += q.byteLength;
        }
        queue = [];
        sb.appendBuffer(data);
      } catch (e) {
        fail(e instanceof Error ? e.message : "video error");
      }
    };

    (video as HTMLVideoElement & { disableRemotePlayback?: boolean }).disableRemotePlayback = true;
    const objectUrl = URL.createObjectURL(ms);
    video.src = objectUrl;
    ms.addEventListener(
      "sourceopen",
      () => {
        if (done) return;
        ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
        ws.onopen = () => {
          const offer = CODECS.filter((c) => MS.isTypeSupported(`video/mp4; codecs="${c}"`)).join();
          ws?.send(JSON.stringify({ type: "mse", value: offer }));
        };
        ws.onmessage = (ev) => {
          lastData = Date.now();
          if (typeof ev.data === "string") {
            let msg: { type?: string; value?: string };
            try {
              msg = JSON.parse(ev.data);
            } catch {
              return;
            }
            if (msg.type === "mse" && msg.value && !sb) {
              try {
                sb = ms.addSourceBuffer(msg.value);
                sb.mode = "segments";
                sb.addEventListener("updateend", pump);
              } catch {
                fail(`can't play ${msg.value}`);
              }
            } else if (msg.type === "error") {
              fail(msg.value || "stream error");
            }
            return;
          }
          queue.push(new Uint8Array(ev.data as ArrayBuffer));
          if (!started) {
            started = true;
            video.play().catch(() => {});
            playingRef.current?.();
          }
          pump();
        };
        ws.onerror = () => fail("connection error");
        ws.onclose = () => fail("connection closed");
      },
      { once: true },
    );

    return () => {
      stop();
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  return <video ref={ref} className={className} autoPlay muted playsInline />;
}
