export type UserRole = "admin" | "viewer";

export interface User {
  id: number;
  username: string;
  role: UserRole;
  email: string | null;
}

export type RecordingMode = "continuous" | "motion" | "off";
export type CameraStatus = "online" | "offline" | "unknown";
export type Codec = "h264" | "h265";

export interface MotionZone {
  kind: "include" | "exclude";
  points: number[][]; // [[x, y], ...] normalized to 0-1
}

export interface Camera {
  id: number;
  name: string;
  onvif_address: string | null;
  rtsp_main_url: string;
  rtsp_sub_url: string | null;
  username: string | null;
  codec: Codec;
  has_audio: boolean;
  recording_mode: RecordingMode;
  motion_enabled: boolean;
  motion_sensitivity: number;
  motion_zones: MotionZone[] | null;
  retention_days: number | null;
  is_favorite: boolean;
  enabled: boolean;
  status: CameraStatus;
  last_seen_at: string | null;
  last_error: string | null; // why it's offline (credential-scrubbed server-side)
  hardware_id: string | null; // ONVIF serial; enables self-healing relocation
  created_at: string;
}

export interface CameraCreatePayload {
  name: string;
  rtsp_main_url: string;
  rtsp_sub_url?: string | null;
  onvif_address?: string | null;
  username?: string | null;
  password?: string | null;
  codec?: Codec;
  has_audio?: boolean;
  recording_mode?: RecordingMode;
  motion_enabled?: boolean;
  motion_sensitivity?: number;
  motion_zones?: MotionZone[] | null;
  retention_days?: number | null;
  is_favorite?: boolean;
  hardware_id?: string | null;
}

export type CameraUpdatePayload = Partial<CameraCreatePayload> & { enabled?: boolean };

export interface DiscoveredDevice {
  host: string;
  port: number;
  address: string;
  scopes: string[];
  hardware_hint: string | null;
  name_hint: string | null;
  mac_address: string | null;
}

export interface ProbeProfile {
  token: string;
  name: string;
  stream_uri: string;
  width: number | null;
  height: number | null;
}

export interface ProbeChannel {
  source_token: string;
  label: string;
  main_url: string;
  sub_url: string | null;
  width: number | null;
  height: number | null;
}

export interface ProbeResponse {
  manufacturer: string;
  model: string;
  firmware_version: string;
  profiles: ProbeProfile[];
  recommended_main_token: string | null;
  recommended_sub_token: string | null;
  detected_port: number;
  // RTSP validation fields — present when the backend could reach the stream
  validated_main_url?: string | null;
  validated_sub_url?: string | null;
  resolved_username?: string | null;  // set when admin/root fallback was used
  codec?: string | null;
  has_audio?: boolean | null;
  hardware_id?: string | null;  // ONVIF serial, stored for self-healing relocation
  // Non-empty only for multi-channel devices (NVRs): one entry per channel
  channels?: ProbeChannel[];
}

export interface TestConnectionResult {
  success: boolean;
  codec?: string | null;
  has_audio?: boolean | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  error?: string | null;
}

export type RecordingTrigger = "continuous" | "motion";

export interface Recording {
  id: number;
  camera_id: number;
  file_path: string;
  thumbnail_path: string | null;
  trigger: RecordingTrigger;
  codec: string | null; // "h264" | "h265"; drives browser-playback transcode
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  size_bytes: number | null;
}

export type EventType = "motion" | "camera_offline" | "camera_online" | "low_storage" | "camera_error" | "system";

export interface NvrEvent {
  id: number;
  camera_id: number | null;
  type: EventType;
  message: string;
  created_at: string;
}

export interface LiveSegmentInfo {
  started_at: string;
}

export interface StreamStat {
  camera_id: number;
  quality: "sub" | "main";
  fps: number;
  kbps: number;
  width: number;
  height: number;
}

export interface DashboardData {
  motion_events_today: number;
  recording_failures_today: number;
  cameras_offline: number;
  recordings_today: number;
  person_detections_today: number;
  vehicle_detections_today: number;
  ai_enabled: boolean;
  heatmap: number[][]; // 7 rows (Mon..Sun) x 24 hourly columns
  storage_days_to_full: number | null;
  storage_full_date: string | null;
}

export interface SystemStatus {
  cpu_percent: number;
  memory_percent: number;
  memory_used_bytes: number;
  memory_total_bytes: number;
  storage_used_bytes: number;
  storage_total_bytes: number;
  storage_free_bytes: number;
  cameras_total: number;
  cameras_online: number;
  cameras_offline: number;
  active_workers: number;
  uptime_seconds: number;
}

export interface AlertSettings {
  motion_alerts_enabled: boolean;
  offline_alerts_enabled: boolean;
  low_storage_alerts_enabled: boolean;
  low_storage_threshold_percent: number;
  motion_alert_cooldown_seconds: number;

  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_from: string;
  smtp_use_tls: boolean;
  alert_email_to: string;

  telegram_enabled: boolean;
  telegram_chat_id: string;

  whatsapp_enabled: boolean;
  whatsapp_phone_number_id: string;
  whatsapp_recipient_number: string;
}

export interface AlertSettingsUpdate extends Partial<AlertSettings> {
  smtp_password?: string;
  telegram_bot_token?: string;
  whatsapp_access_token?: string;
}

export interface TestEmailRequest {
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_password: string;
  smtp_from: string;
  smtp_use_tls: boolean;
  alert_email_to: string;
}

export interface TestTelegramRequest {
  bot_token: string;
  chat_id: string;
}

export interface TestWhatsAppRequest {
  phone_number_id: string;
  access_token: string;
  recipient_number: string;
}

export interface TestMessageResult {
  success: boolean;
  message: string;
}

export type StorageType = "local" | "smb" | "nfs";

export interface StorageConfig {
  cache_path: string;
  cache_max_gb: number;
  default_retention_days: number;
  max_storage_gb: number;
  primary_type: StorageType;
  primary_path: string;
  primary_remote_spec: string | null;
  primary_username: string | null;
  backup_enabled: boolean;
  backup_type: StorageType;
  backup_path: string | null;
  backup_remote_spec: string | null;
  backup_username: string | null;
}

export interface StorageConfigUpdate {
  cache_max_gb?: number;
  default_retention_days?: number;
  max_storage_gb?: number;
  primary_type?: StorageType;
  primary_remote_spec?: string | null;
  primary_username?: string | null;
  primary_password?: string;
  backup_enabled?: boolean;
  backup_type?: StorageType;
  backup_remote_spec?: string | null;
  backup_username?: string | null;
  backup_password?: string;
}

export interface SecuritySettings {
  access_token_expire_minutes: number;
  refresh_token_expire_days: number;
}

export type TlsMode = "self_signed" | "custom" | "letsencrypt";

export interface TlsSettings {
  mode: TlsMode;
  domain: string | null;
  email: string | null;
  last_renewal_at: string | null;
  last_renewal_error: string | null;
}

export interface TlsActionResult {
  success: boolean;
  message: string;
}

export interface BackupSettings {
  enabled: boolean;
  interval_hours: number;
  retention_count: number;
  last_backup_at: string | null;
  last_backup_filename: string | null;
  last_backup_location: string | null;
  last_backup_error: string | null;
  last_optimize_at: string | null;
}

export interface BackupSettingsUpdate {
  enabled?: boolean;
  interval_hours?: number;
  retention_count?: number;
}

export interface BackupInfo {
  filename: string;
  location: string;
  size_bytes: number;
  created_at: string;
}

export interface OptimizeResult {
  success: boolean;
  message: string;
  duration_seconds: number;
}

export interface StorageTestRequest {
  target: "primary" | "backup";
  type: StorageType;
  remote_spec?: string | null;
  username?: string | null;
  password?: string | null;
}

export interface StorageTestResult {
  success: boolean;
  message: string;
}

export interface TierHealth {
  available: boolean;
  mounted: boolean;
  free_bytes: number | null;
  total_bytes: number | null;
  used_by_recordings_bytes: number | null;
  detail: string | null;
}

export interface StorageHealth {
  cache: TierHealth;
  primary: TierHealth;
  backup: TierHealth | null;
  pending_migration_count: number;
}

export interface RemoteAccessSettings {
  tailscale_enabled: boolean;
  tailscale_hostname: string;
  has_tailscale_authkey: boolean;

  cloudflare_enabled: boolean;
  has_cloudflare_token: boolean;
}

export type AIBackend = "local" | "remote";
export type VlmProvider = "ollama" | "openai_compatible" | "anthropic";

export interface AISettings {
  enabled: boolean;
  backend: AIBackend;
  remote_url: string;
  has_remote_api_key: boolean;

  detection_enabled: boolean;
  detection_model: string;
  detection_confidence: number;
  detection_classes: string[];
  alert_on_objects_only: boolean;

  search_enabled: boolean;
  embedding_model: string;

  vlm_enabled: boolean;
  vlm_provider: VlmProvider;
  vlm_url: string;
  vlm_model: string;
  has_vlm_api_key: boolean;
  vlm_daily_digest: boolean;

  privacy_ack: boolean;
  face_enabled: boolean;
  face_threshold: number;
  alpr_enabled: boolean;

  detection_retention_days: number;
}

/** Omitted fields are left unchanged server-side. Secrets additionally treat
 *  `undefined` as "keep the stored one" and `""` as "clear it". */
export interface AISettingsUpdate
  extends Partial<Omit<AISettings, "has_remote_api_key" | "has_vlm_api_key">> {
  remote_api_key?: string;
  vlm_api_key?: string;
}

export interface AITestResult {
  success: boolean;
  message: string;
  backend?: string | null;
  latency_ms?: number | null;
}

export interface Detection {
  id: number;
  camera_id: number;
  recording_id: number | null;
  label: string;
  confidence: number;
  bbox_x: number;
  bbox_y: number;
  bbox_w: number;
  bbox_h: number;
  text: string | null;
  description: string | null;
  snapshot_path: string | null;
  created_at: string;
}

export type TailscaleState = "stopped" | "starting" | "needs_login" | "connected";
export type CloudflareState = "stopped" | "starting" | "connected";

export interface TailscaleStatus {
  state: TailscaleState;
  ip: string | null;
  hostname: string | null;
  error: string | null;
}

export interface CloudflareStatus {
  state: CloudflareState;
  error: string | null;
  log_tail: string[];
}

export interface RemoteAccessStatus {
  tailscale: TailscaleStatus;
  cloudflare: CloudflareStatus;
}

export interface KioskView {
  id: number;
  name: string;
  token: string;
  layout: number;
  camera_ids: number[];
  created_at: string;
  last_viewed_at: string | null;
}

export interface KioskCamera {
  id: number;
  name: string;
  status: CameraStatus;
  recording_mode: RecordingMode;
}

export interface KioskPublicView {
  name: string;
  layout: number;
  cameras: KioskCamera[];
}
