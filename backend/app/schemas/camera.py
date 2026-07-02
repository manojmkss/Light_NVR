from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class CameraCreate(BaseModel):
    name: str
    rtsp_main_url: str
    rtsp_sub_url: str | None = None
    onvif_address: str | None = None
    username: str | None = None
    password: str | None = None
    codec: str = "h264"
    has_audio: bool = False
    recording_mode: str = "continuous"
    motion_enabled: bool = True
    motion_sensitivity: int = 50
    retention_days: int | None = None  # overrides the global retention setting when set
    is_favorite: bool = False


class CameraUpdate(BaseModel):
    name: str | None = None
    rtsp_main_url: str | None = None
    rtsp_sub_url: str | None = None
    username: str | None = None
    password: str | None = None
    codec: str | None = None
    has_audio: bool | None = None
    recording_mode: str | None = None
    motion_enabled: bool | None = None
    motion_sensitivity: int | None = None
    retention_days: int | None = None
    enabled: bool | None = None
    is_favorite: bool | None = None


class CameraOut(BaseModel):
    id: int
    name: str
    onvif_address: str | None
    rtsp_main_url: str
    rtsp_sub_url: str | None
    username: str | None
    codec: str
    has_audio: bool
    recording_mode: str
    motion_enabled: bool
    motion_sensitivity: int
    retention_days: int | None
    is_favorite: bool
    enabled: bool
    status: str
    last_seen_at: UtcDatetime | None
    created_at: UtcDatetime

    class Config:
        from_attributes = True


class DiscoveredDeviceOut(BaseModel):
    host: str
    port: int
    address: str
    scopes: list[str]
    hardware_hint: str | None = None
    name_hint: str | None = None
    mac_address: str | None = None


class RangeScanRequest(BaseModel):
    cidr: str


class DiscoverySettingsOut(BaseModel):
    custom_subnets: list[str]


class DiscoverySettingsUpdate(BaseModel):
    custom_subnets: list[str]


class ProbeRequest(BaseModel):
    host: str
    port: int | None = None  # None = auto-detect from common ONVIF ports
    username: str
    password: str


class ProbeProfileOut(BaseModel):
    token: str
    name: str
    stream_uri: str
    width: int | None = None
    height: int | None = None


class ProbeResponseOut(BaseModel):
    manufacturer: str
    model: str
    firmware_version: str
    profiles: list[ProbeProfileOut]
    recommended_main_token: str | None = None
    recommended_sub_token: str | None = None
    detected_port: int
    # RTSP validation results — populated when the backend could reach the stream
    validated_main_url: str | None = None
    validated_sub_url: str | None = None
    resolved_username: str | None = None  # differs from input when admin fallback was used
    codec: str | None = None
    has_audio: bool | None = None


class TestConnectionRequest(BaseModel):
    rtsp_url: str


class TestConnectionResponse(BaseModel):
    success: bool
    codec: str | None = None
    has_audio: bool | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    error: str | None = None
