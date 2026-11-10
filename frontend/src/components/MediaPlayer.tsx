import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";

/** What the player should show: the camera live, or its recordings from a
 *  moment on. `nonce` makes the same moment playable again (a re-click). */
export type MediaMode =
  | { kind: "live"; camera: number; quality: "sub" | "main"; audio: boolean }
  | { kind: "play"; camera: number; at: number; audio: boolean; nonce: number };

export interface MediaPlayerHandle {
  /** Jump within what's already loaded (no round trip); false if it isn't. */
  seekLocal(wallMs: number): boolean;
  video(): HTMLVideoElement | null;
}

interface Props {
  /** A fresh websocket URL (with the current login) for every connection. */
  url: () => string;
  mode: MediaMode;
  muted: boolean;
  paused?: boolean;
  className?: string;
  onPlaying?: (width: number, height: number) => void;
  /** Live couldn't start / the device can't play it / playback failed. */
  onFail?: (why: string) => void;
  /** Playback: no footage from `at` (unix s); where it resumes, if anywhere. */
  onGap?: (at: number, nextAt: number | null) => void;
  /** Playback: wall time (ms) of the frame on screen. */
  onTime?: (wallMs: number) => void;
  /** The browser wouldn't start sound without a tap: playing muted. */
  onSoundBlocked?: () => void;
}

type MediaSourceCtor = typeof MediaSource;

function mediaSourceClass(): MediaSourceCtor | null {
  // iPhone Safari has only ManagedMediaSource.
  const w = window as unknown as { ManagedMediaSource?: MediaSourceCtor; MediaSource?: MediaSourceCtor };
  return w.ManagedMediaSource ?? w.MediaSource ?? null;
}

export const mediaEngineSupported = () => mediaSourceClass() !== null;

const LIVE_START_MS = 12_000; // no picture by then: give up (the tile shows pictures instead)
const LIVE_STALL_MS = 15_000; // no data for this long: reconnect
const LIVE_MAX_LAG_S = 2.5; // further behind live than this: jump to the live edge
const LIVE_KEEP_S = 10; // played video kept buffered (live)
const PLAY_KEEP_S = 30; // ... (playback: room to step back a little)
const MAX_QUEUED_BYTES = 24 * 1024 * 1024;

type Bytes = Uint8Array<ArrayBuffer>;
type Item = { kind: "init"; data: Bytes; offset: number } | { kind: "frag"; data: Bytes };

/**
 * The camera's own video (H.264/H.265), live or recorded, from the NVR's
 * media engine (backend/app/services/media_engine.py) over one websocket:
 * fragmented MP4 appended to a MediaSource as it arrives. Live is kept at the
 * live edge; playback reports its position so the NVR sends only ~20 s ahead.
 */
export const MediaPlayer = forwardRef<MediaPlayerHandle, Props>(function MediaPlayer(props, ref) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const propsRef = useRef(props);
  propsRef.current = props;
  const engine = useRef<{ setMode: (m: MediaMode) => void; seekLocal: (ms: number) => boolean } | null>(null);

  useImperativeHandle(ref, () => ({
    seekLocal: (ms) => engine.current?.seekLocal(ms) ?? false,
    video: () => videoRef.current,
  }));

  // One engine connection for the life of the player; modes switch over it.
  useEffect(() => {
    const video = videoRef.current;
    const MS = mediaSourceClass();
    if (!video || !MS) {
      propsRef.current.onFail?.("This browser can't play video from the NVR");
      return;
    }
    (video as HTMLVideoElement & { disableRemotePlayback?: boolean }).disableRemotePlayback = true;

    let disposed = false;
    let ws: WebSocket | null = null;
    let backoff = 1000;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let mode: MediaMode = propsRef.current.mode;
    let ms: MediaSource | null = null;
    let sb: SourceBuffer | null = null;
    let objectUrl = "";
    let mime = "";
    let queue: Item[] = [];
    let queued = 0;
    let pendingInit: { codecs: string; reset: boolean; offset?: number; origin?: number; at?: number } | null = null;
    let origin = 0; // playback: wall time (s) of media time 0
    let seekTo: number | null = null; // playback: media time to start at once data is in
    let started = false;
    let lastData = Date.now();
    let liveDeadline = 0;
    let failed = false;
    let resumeAt: number | null = null; // playback: carry on from here after a reconnect

    const fail = (why: string) => {
      if (failed || disposed) return;
      failed = true;
      propsRef.current.onFail?.(why);
    };

    const teardownSource = () => {
      sb = null;
      ms = null;
      queue = [];
      queued = 0;
      if (objectUrl) {
        video.removeAttribute("src");
        video.load();
        URL.revokeObjectURL(objectUrl);
        objectUrl = "";
      }
    };

    const play = () => {
      if (propsRef.current.paused) return;
      video.play().catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "NotAllowedError" && !video.muted) {
          video.muted = true; // sound needs a tap first
          propsRef.current.onSoundBlocked?.();
          video.play().catch(() => {});
        }
      });
    };

    const housekeep = (): boolean => {
      // Returns true when it started a SourceBuffer operation.
      if (!sb || !sb.buffered.length) return false;
      const b = sb.buffered;
      const end = b.end(b.length - 1);
      const start = b.start(0);
      if (mode.kind === "live") {
        if (end - video.currentTime > LIVE_MAX_LAG_S || video.currentTime < start) video.currentTime = Math.max(start, end - 0.3);
        if (video.currentTime - start > LIVE_KEEP_S + 5) {
          sb.remove(start, video.currentTime - LIVE_KEEP_S);
          return true;
        }
      } else if (video.currentTime - start > PLAY_KEEP_S + 10) {
        sb.remove(start, video.currentTime - PLAY_KEEP_S);
        return true;
      }
      return false;
    };

    const pump = () => {
      if (disposed || !sb || !ms || sb.updating || ms.readyState !== "open") return;
      if ((ms as MediaSource & { streaming?: boolean }).streaming === false) return; // ManagedMediaSource: not now
      try {
        if (housekeep()) return; // carries on at updateend
        const item = queue.shift();
        if (!item) return;
        if (item.kind === "init") {
          sb.timestampOffset = item.offset;
          sb.appendBuffer(item.data);
          return;
        }
        // Several fragments in one append.
        let size = item.data.byteLength;
        const parts = [item.data];
        while (queue.length && queue[0].kind === "frag" && size < 2 * 1024 * 1024) {
          const next = queue.shift()!;
          parts.push(next.data);
          size += next.data.byteLength;
        }
        queued -= size;
        const data = parts.length === 1 ? parts[0] : new Uint8Array(size);
        if (parts.length > 1) {
          let at = 0;
          for (const p of parts) {
            data.set(p, at);
            at += p.byteLength;
          }
        }
        sb.appendBuffer(data);
      } catch (e) {
        if (e instanceof DOMException && e.name === "QuotaExceededError" && sb && sb.buffered.length) {
          // Full: drop played video, try again.
          try {
            sb.remove(sb.buffered.start(0), Math.max(sb.buffered.start(0) + 0.1, video.currentTime - 2));
          } catch {
            /* next updateend */
          }
          return;
        }
        if (mode.kind === "live") reconnect();
        else fail(e instanceof Error ? e.message : "video error");
      }
    };

    const newSource = (codecMime: string, init: Item) => {
      teardownSource();
      mime = codecMime;
      const source = new MS();
      ms = source;
      objectUrl = URL.createObjectURL(source);
      video.src = objectUrl;
      queue = [init];
      source.addEventListener("startstreaming", pump);
      source.addEventListener(
        "sourceopen",
        () => {
          if (disposed || ms !== source) return;
          try {
            sb = source.addSourceBuffer(mime);
            sb.mode = "segments";
            sb.addEventListener("updateend", () => {
              if (seekTo != null && seekTo >= 0 && sb && sb.buffered.length) {
                const b = sb.buffered;
                // Footage from the asked moment is in: start there. (Data that
                // starts later than asked: the NVR skipped a gap - start where
                // it begins.)
                if (seekTo <= b.end(b.length - 1)) {
                  video.currentTime = Math.max(seekTo, b.start(0));
                  seekTo = null;
                  play();
                }
              }
              pump();
            });
            pump();
          } catch {
            fail("This device can't play this camera's video");
          }
        },
        { once: true },
      );
    };

    const onInit = (msg: NonNullable<typeof pendingInit>, data: Bytes) => {
      const codecMime = `video/mp4; codecs="${msg.codecs}"`;
      if (!MS.isTypeSupported(codecMime)) {
        const h265 = msg.codecs.startsWith("hvc1") || msg.codecs.startsWith("hev1");
        fail(h265 ? "This device can't play H.265 video" : `This device can't play ${msg.codecs}`);
        return;
      }
      const offset = mode.kind === "play" ? msg.offset ?? 0 : 0;
      // `ms`, not `sb`: the next file's init can arrive before the browser has
      // opened the source for this one - it must queue behind it, not start over.
      if (msg.reset || !ms || codecMime !== mime) {
        if (mode.kind === "play") {
          origin = msg.origin ?? 0;
          seekTo = (msg.at ?? origin) - origin;
        }
        newSource(codecMime, { kind: "init", data, offset });
      } else {
        queue.push({ kind: "init", data, offset });
        pump();
      }
    };

    const send = (msg: object) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
    };

    const sendMode = () => {
      started = false;
      failed = false;
      lastData = Date.now();
      if (mode.kind === "live") {
        liveDeadline = Date.now() + LIVE_START_MS;
        send({ op: "live", camera: mode.camera, quality: mode.quality, audio: mode.audio });
      } else {
        liveDeadline = 0;
        send({ op: "play", camera: mode.camera, at: resumeAt ?? mode.at, audio: mode.audio });
        resumeAt = null;
      }
    };

    const connect = () => {
      if (disposed) return;
      const sock = new WebSocket(propsRef.current.url());
      ws = sock;
      sock.binaryType = "arraybuffer";
      sock.onopen = () => {
        backoff = 1000;
        sendMode();
      };
      sock.onmessage = (ev) => {
        if (ws !== sock) return;
        if (typeof ev.data === "string") {
          let msg: Record<string, unknown>;
          try {
            msg = JSON.parse(ev.data);
          } catch {
            return;
          }
          if (msg.type === "init") pendingInit = msg as unknown as NonNullable<typeof pendingInit>;
          else if (msg.type === "gap") {
            if (seekTo != null) video.pause(); // nothing of the asked moment: don't play on what was before
            propsRef.current.onGap?.(msg.at as number, (msg.next_at as number | null) ?? null);
          }
          else if (msg.type === "error") fail(String(msg.message || "error"));
          return;
        }
        const data = new Uint8Array(ev.data as ArrayBuffer);
        lastData = Date.now();
        if (pendingInit) {
          const init = pendingInit;
          pendingInit = null;
          onInit(init, data);
          return;
        }
        if (!ms) return;
        queue.push({ kind: "frag", data });
        queued += data.byteLength;
        if (queued > MAX_QUEUED_BYTES) {
          // The decoder can't keep up: start over at the next keyframe.
          if (mode.kind === "live") reconnect();
          return;
        }
        if (!started) {
          started = true;
          liveDeadline = 0;
          play();
          setTimeout(() => propsRef.current.onPlaying?.(video.videoWidth, video.videoHeight), 300);
        }
        pump();
      };
      sock.onclose = () => {
        if (ws !== sock || disposed) return;
        noteResume();
        ws = null;
        reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 10_000);
      };
    };

    // A dropped connection mid-playback resumes from the frame on screen, not
    // from the moment first asked for.
    const noteResume = () => {
      if (mode.kind === "play" && origin && seekTo == null && video.readyState >= 2) resumeAt = origin + video.currentTime;
    };

    const reconnect = () => {
      noteResume();
      const sock = ws;
      ws = null;
      if (sock) {
        sock.onclose = null;
        sock.close();
      }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 500);
    };

    const watch = setInterval(() => {
      const now = Date.now();
      if (mode.kind === "live") {
        if (liveDeadline && now > liveDeadline) fail("no picture");
        else if (started && now - lastData > LIVE_STALL_MS) {
          started = false;
          liveDeadline = now + LIVE_START_MS;
          reconnect();
        }
        return;
      }
      // Playback: tell the NVR where we are (it stays ~20 s ahead of this),
      // and step over the gap between two files.
      if (origin) send({ op: "pos", wall: origin + video.currentTime });
      if (sb && !video.paused && video.readyState < 3) {
        const b = sb.buffered;
        for (let i = 0; i < b.length; i++) {
          if (b.start(i) > video.currentTime + 0.05 && b.start(i) - video.currentTime < 20) {
            const inside = Array.from({ length: b.length }, (_, j) => j).some(
              (j) => b.start(j) <= video.currentTime && video.currentTime < b.end(j) - 0.1,
            );
            if (!inside) video.currentTime = b.start(i) + 0.01;
            break;
          }
        }
      }
    }, 1000);

    const onTimeUpdate = () => {
      // Only real positions: not while a new source has no picture yet
      // (currentTime 0) or a jump is under way.
      if (mode.kind === "play" && origin && seekTo == null && video.readyState >= 2 && !video.seeking) {
        propsRef.current.onTime?.((origin + video.currentTime) * 1000);
      }
    };
    video.addEventListener("timeupdate", onTimeUpdate);

    engine.current = {
      setMode: (m) => {
        mode = m;
        resumeAt = null;
        pendingInit = null;
        // Playback of a new moment: the old picture stops now, not when the
        // new one arrives (seekTo != null also keeps timeupdate quiet).
        seekTo = m.kind === "play" ? -1 : null;
        if (m.kind === "play") video.pause();
        sendMode();
      },
      seekLocal: (wallMs) => {
        if (mode.kind !== "play" || !origin || !sb) return false;
        const t = wallMs / 1000 - origin;
        const b = sb.buffered;
        for (let i = 0; i < b.length; i++) {
          if (t >= b.start(i) && t <= b.end(i) - 0.5) {
            video.currentTime = t;
            send({ op: "pos", wall: wallMs / 1000 });
            return true;
          }
        }
        return false;
      },
    };

    connect();
    return () => {
      disposed = true;
      engine.current = null;
      clearInterval(watch);
      clearTimeout(reconnectTimer);
      video.removeEventListener("timeupdate", onTimeUpdate);
      if (ws) {
        ws.onclose = ws.onmessage = null;
        ws.close();
      }
      teardownSource();
    };
  }, []);

  // Mode changes go over the same connection.
  const modeKey = JSON.stringify(props.mode);
  const firstMode = useRef(true);
  useEffect(() => {
    if (firstMode.current) {
      firstMode.current = false;
      return;
    }
    engine.current?.setMode(props.mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modeKey]);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.muted = props.muted;
    if (!props.muted && !v.paused) {
      v.play().catch(() => {
        v.muted = true;
        propsRef.current.onSoundBlocked?.();
      });
    }
  }, [props.muted]);

  useEffect(() => {
    const v = videoRef.current;
    if (!v || props.mode.kind !== "play") return;
    if (props.paused) v.pause();
    else if (v.src) v.play().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.paused]);

  return <video ref={videoRef} className={props.className} autoPlay muted={props.muted} playsInline />;
});
