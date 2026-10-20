import { apiFetch } from "./client";
import type { NvrEvent } from "./types";

export type SecurityMode = "home" | "away" | "night" | "disarmed";
export type RuleAction = "notify" | "notify_high" | "silent";

export interface ModeInfo {
  mode: SecurityMode;
  source: string;
  manual_mode: SecurityMode;
  hold: boolean;
  set_by: string | null;
  set_at: string | null;
  next_change_at: string | null;
}

export interface ModeSchedule {
  id: number;
  days_mask: number; // bit 0 = Monday ... bit 6 = Sunday
  at_hhmm: string;
  mode: SecurityMode;
  enabled: boolean;
}

export interface AlertRule {
  id: number;
  name: string;
  position: number;
  enabled: boolean;
  modes: SecurityMode[] | null;
  camera_ids: number[] | null;
  kinds: string[] | null;
  objects: string[] | null;
  zones: string[] | null;
  identities: string[] | null;
  action: RuleAction;
  cooldown_s: number | null;
  webhook_url: string | null;
}

export type AlertRuleInput = Omit<AlertRule, "id">;

export const getMode = () => apiFetch<ModeInfo>("/security/mode");
export const setMode = (mode: SecurityMode, hold = false) =>
  apiFetch<ModeInfo>("/security/mode", { method: "PUT", body: JSON.stringify({ mode, hold }) });

export const listSchedules = () => apiFetch<ModeSchedule[]>("/security/schedules");
export const createSchedule = (s: Omit<ModeSchedule, "id">) =>
  apiFetch<ModeSchedule>("/security/schedules", { method: "POST", body: JSON.stringify(s) });
export const updateSchedule = (id: number, s: Omit<ModeSchedule, "id">) =>
  apiFetch<ModeSchedule>(`/security/schedules/${id}`, { method: "PUT", body: JSON.stringify(s) });
export const deleteSchedule = (id: number) => apiFetch<void>(`/security/schedules/${id}`, { method: "DELETE" });

export const listRules = () => apiFetch<AlertRule[]>("/security/rules");
export const createRule = (r: AlertRuleInput) =>
  apiFetch<AlertRule>("/security/rules", { method: "POST", body: JSON.stringify(r) });
export const updateRule = (id: number, r: AlertRuleInput) =>
  apiFetch<AlertRule>(`/security/rules/${id}`, { method: "PUT", body: JSON.stringify(r) });
export const deleteRule = (id: number) => apiFetch<void>(`/security/rules/${id}`, { method: "DELETE" });
export const addRulePresets = () => apiFetch<AlertRule[]>("/security/rules/presets", { method: "POST" });
export const previewRules = (mode?: SecurityMode) =>
  apiFetch<{ mode: string; results: { event_id: number; type: string; message: string; action: string; rule: string | null }[] }>(
    `/security/rules/preview${mode ? `?mode=${mode}` : ""}`,
    { method: "POST" },
  );

export const getEvent = (id: number) => apiFetch<NvrEvent>(`/system/events/${id}`);
export const ackEvent = (id: number) => apiFetch<{ acknowledged: boolean }>(`/system/events/${id}/ack`, { method: "POST" });

export const MODE_LABEL: Record<SecurityMode, string> = {
  home: "Home",
  away: "Away",
  night: "Night",
  disarmed: "Disarmed",
};

export interface AreaLink {
  id: number;
  area_a: string;
  area_b: string;
  min_seconds: number;
  max_seconds: number;
}

export interface Incident {
  id: number;
  started_at: string;
  ended_at: string | null;
  status: "open" | "closed";
  camera_ids: number[] | null;
  areas_path: string[] | null;
  counts: Record<string, number> | null;
  identities: string[] | null;
  summary: string | null;
  snapshot_path: string | null;
  mode: string | null;
}

export const getAreas = () =>
  apiFetch<{ areas: { name: string; cameras: { id: number; name: string }[] }[]; links: AreaLink[] }>("/security/areas");
export const createAreaLink = (l: Omit<AreaLink, "id">) =>
  apiFetch<AreaLink>("/security/areas/links", { method: "POST", body: JSON.stringify(l) });
export const deleteAreaLink = (id: number) => apiFetch<void>(`/security/areas/links/${id}`, { method: "DELETE" });
export const listIncidents = (limit = 50) => apiFetch<Incident[]>(`/security/incidents?limit=${limit}`);
export const getIncident = (id: number) =>
  apiFetch<{ incident: Incident; events: NvrEvent[] }>(`/security/incidents/${id}`);
