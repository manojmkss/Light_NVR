import { apiFetch, apiUpload, buildMediaUrl } from "./client";

/** Trust level - shown as Family / Familiar / Stranger / Suspicious. */
export type PersonCategory = "household" | "familiar" | "stranger" | "watchlist";
export type PersonNotify = "always" | "away" | "never";

export interface Person {
  id: number;
  name: string;
  note: string | null; // relation: "milkman", "daughter"...
  category: PersonCategory;
  notify: PersonNotify;
  arrival_alert: boolean;
  arrival_cooldown_s: number;
  samples: number;
  strength: "weak" | "good" | "strong";
  strength_hint: string;
  cover_sample_id: number | null;
  last_seen: string | null;
  created_at: string;
}

export type PersonInput = Pick<Person, "name" | "note" | "category" | "notify" | "arrival_alert" | "arrival_cooldown_s">;

export interface FaceGroup {
  sample_ids: number[];
  cover_sample_id: number;
  sightings: number;
  first_seen: string;
  last_seen: string;
  seen_at: string[];
  camera_ids: number[];
  suggestion: { person_id: number; name: string; score: number; band: string } | null;
}

export interface FaceSampleInfo {
  id: number;
  person_id: number | null;
  camera_id: number | null;
  quality: number;
  score: number | null;
  source: string;
  created_at: string;
  suggestion: { person_id: number; name: string; score: number; band: string } | null;
}

export const listPeople = () => apiFetch<Person[]>("/faces/people");
export const createPerson = (p: PersonInput) => apiFetch<Person>("/faces/people", { method: "POST", body: JSON.stringify(p) });
export const updatePerson = (id: number, p: PersonInput) =>
  apiFetch<Person>(`/faces/people/${id}`, { method: "PUT", body: JSON.stringify(p) });
export const deletePerson = (id: number) => apiFetch<void>(`/faces/people/${id}`, { method: "DELETE" });
export const forgetAllFaces = () => apiFetch<{ deleted_samples: number }>("/faces/forget-all", { method: "POST" });
export const listSamples = (q: { person_id?: number; unassigned?: boolean; limit?: number }) => {
  const p = new URLSearchParams();
  if (q.person_id !== undefined) p.set("person_id", String(q.person_id));
  if (q.unassigned) p.set("unassigned", "true");
  if (q.limit) p.set("limit", String(q.limit));
  return apiFetch<FaceSampleInfo[]>(`/faces/samples?${p.toString()}`);
};
export const assignSample = (id: number, personId: number | null) =>
  apiFetch<{ ok: boolean }>(`/faces/samples/${id}/assign`, { method: "POST", body: JSON.stringify({ person_id: personId }) });
export interface FaceCaptureStats {
  day: string;
  checked: number;
  no_face: number;
  too_small: number;
  low_quality: number;
  usable: number;
  frame_width: number;
  median_face_px: number | null;
}
export const faceCaptureStats = () => apiFetch<Record<string, FaceCaptureStats>>("/faces/capture-stats");
export const listUnknownGroups = () => apiFetch<FaceGroup[]>("/faces/unknown-groups");
export const assignGroup = (sampleIds: number[], target: { person_id: number } | { person: PersonInput }) =>
  apiFetch<Person>("/faces/groups/assign", { method: "POST", body: JSON.stringify({ sample_ids: sampleIds, ...target }) });
export const discardGroup = (sampleIds: number[]) =>
  apiFetch<{ deleted: number }>("/faces/groups/discard", { method: "POST", body: JSON.stringify({ sample_ids: sampleIds }) });
export const deleteSample = (id: number) => apiFetch<void>(`/faces/samples/${id}`, { method: "DELETE" });
export const sampleCropUrl = (id: number) => buildMediaUrl(`/faces/samples/${id}/crop.jpg`);
export const faceModels = () =>
  apiFetch<{ models: { name: string; present: boolean; size_mb: number; license: string }[] }>("/faces/models");
export const downloadFaceModels = () =>
  apiFetch<{ models: { name: string; present: boolean }[] }>("/faces/models/download", { method: "POST" });

export function uploadPhotos(personId: number, files: File[]) {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return apiUpload<{ results: { file: string; ok: boolean; reason?: string; quality?: number }[] }>(
    `/faces/people/${personId}/photos`,
    form,
  );
}
