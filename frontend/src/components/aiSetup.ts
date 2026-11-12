import type { DetectionModels, EngineDevice, EngineState, EngineStatus } from "../api/ai";

/** What a person can tick in plain words, and the detector classes behind it.
 *  Shared by the setup wizard and the AI settings page so both speak the same
 *  language ("Vehicles", not "car, truck, bus, motorcycle, bicycle"). */
export interface DetectGroup {
  key: string;
  label: string;
  hint: string;
  classes: string[];
  defaultOn: boolean;
}

export const DETECT_GROUPS: DetectGroup[] = [
  { key: "people", label: "People", hint: "Anyone walking, standing or at the door", classes: ["person"], defaultOn: true },
  {
    key: "vehicles",
    label: "Vehicles",
    hint: "Cars, trucks, buses, motorbikes and bicycles",
    classes: ["car", "truck", "bus", "motorcycle", "bicycle"],
    defaultOn: true,
  },
  { key: "animals", label: "Animals", hint: "Dogs, cats and birds", classes: ["dog", "cat", "bird"], defaultOn: true },
  {
    key: "bags",
    label: "Bags & parcels",
    hint: "Backpacks, handbags and suitcases left or picked up",
    classes: ["backpack", "handbag", "suitcase"],
    defaultOn: false,
  },
];

/** Which groups a saved class list covers (a group counts once any of its classes is on). */
export function groupsFromClasses(classes: string[]): Record<string, boolean> {
  const set = new Set(classes.map((c) => c.toLowerCase()));
  return Object.fromEntries(DETECT_GROUPS.map((g) => [g.key, g.classes.some((c) => set.has(c))]));
}

/** The class list for a set of ticked groups. Classes outside the groups (picked
 *  by hand in Advanced) are kept, so the simple view never silently drops them. */
export function classesFromGroups(on: Record<string, boolean>, previous: string[] = []): string[] {
  const grouped = new Set(DETECT_GROUPS.flatMap((g) => g.classes));
  const extra = previous.filter((c) => !grouped.has(c));
  return [...DETECT_GROUPS.filter((g) => on[g.key]).flatMap((g) => g.classes), ...extra];
}

export const DEVICE_LABEL: Record<EngineDevice, string> = {
  cpu: "CPU",
  intel_gpu: "Intel GPU",
  nvidia_gpu: "NVIDIA GPU",
};

/** The engine this NVR actually uses: its own AI service, or another PC's
 *  (the server already resolves which one is in use into `engine`). */
export function engineInUse(st: EngineStatus | null): EngineState | null {
  return st?.engine ?? null;
}

export interface Recommendation {
  device: EngineDevice;
  deviceName: string;
  model: string;
  modelLabel: string;
  intervalMs: number;
  confidence: number;
  why: string[];
}

const ORDER: EngineDevice[] = ["nvidia_gpu", "intel_gpu", "cpu"];

/** Pick the setup this hardware handles best. A device only counts when the AI
 *  service can use it and its speed test (if one ran) passed; a GPU gets the
 *  balanced model, a CPU the light one, and a slow device checks less often so
 *  it keeps up instead of skipping frames. */
export function recommend(engine: EngineState, models: DetectionModels | null): Recommendation {
  const usable = (id: EngineDevice) => {
    const d = engine.devices.find((x) => x.id === id);
    return !!d && d.available && (!d.result || d.result.ok);
  };
  const device = ORDER.find(usable) ?? "cpu";
  const info = engine.devices.find((d) => d.id === device);
  const gpu = device !== "cpu";
  const wanted = gpu ? "yolo26s" : "yolo26n";
  const offered = models?.models ?? [];
  const model = offered.some((m) => m.name === wanted && (m.downloadable || m.present))
    ? wanted
    : (offered.find((m) => m.downloadable || m.present)?.name ?? wanted);
  const modelLabel = offered.find((m) => m.name === model)?.label ?? model;

  const why: string[] = [];
  if (gpu) why.push(`${info?.name ?? DEVICE_LABEL[device]} found - it checks pictures much faster than the CPU.`);
  else why.push("No usable graphics card was found, so the CPU runs it.");
  why.push(
    gpu
      ? `"${modelLabel}" finds far-away and partly hidden people more reliably, and the graphics card keeps it fast.`
      : `"${modelLabel}" is light enough for a CPU.`,
  );

  // The device's own speed test ran with whichever model was loaded; use it as
  // a rough guide to how often each camera can be checked during motion.
  const p50 = info?.result?.ok ? info.result.p50_ms ?? 0 : 0;
  let intervalMs = 500;
  if (!gpu && p50 > 200) intervalMs = 1000;
  if (!gpu && p50 > 450) intervalMs = 1500;
  if (intervalMs > 500) why.push(`This ${DEVICE_LABEL[device]} needs about ${p50} ms per check, so cameras are checked every ${intervalMs / 1000} s during motion.`);

  return { device, deviceName: info?.name ?? DEVICE_LABEL[device], model, modelLabel, intervalMs, confidence: 50, why };
}
