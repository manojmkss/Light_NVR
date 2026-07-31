import { useEffect, useState } from "react";

/**
 * True while the tab/page is visible. Lets components drop expensive work when
 * the user switches away - e.g. the playback rows tear down their <video>
 * elements when hidden, which aborts the in-flight transcode requests (and the
 * server kills the ffmpeg behind them) instead of transcoding a page nobody is
 * looking at.
 */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(() =>
    typeof document === "undefined" ? true : document.visibilityState !== "hidden",
  );
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}
