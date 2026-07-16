from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.schemas.common import UtcDatetime


def _check_http_url(v: str | None) -> str | None:
    """Endpoints are fed to an HTTP client, so anything that isn't http(s) is
    rejected here rather than failing confusingly at request time."""
    if v is None or v == "":
        return v
    if not v.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    return v


OptionalHttpUrl = Annotated[str | None, AfterValidator(_check_http_url)]


class AISettingsOut(BaseModel):
    enabled: bool
    backend: str
    remote_url: str
    # Secrets are never echoed back - only whether one is stored, so the UI can
    # show "configured" without the value ever leaving the box again.
    has_remote_api_key: bool

    detection_enabled: bool
    detection_model: str
    detection_confidence: int
    detection_classes: list[str]
    alert_on_objects_only: bool

    search_enabled: bool
    embedding_model: str

    vlm_enabled: bool
    vlm_provider: str
    vlm_url: str
    vlm_model: str
    has_vlm_api_key: bool
    vlm_daily_digest: bool

    privacy_ack: bool
    face_enabled: bool
    face_threshold: int
    alpr_enabled: bool

    detection_retention_days: int


class AISettingsUpdate(BaseModel):
    enabled: bool | None = None
    backend: Literal["local", "remote"] | None = None
    remote_url: OptionalHttpUrl = None
    remote_api_key: str | None = None

    detection_enabled: bool | None = None
    detection_model: str | None = None
    detection_confidence: Annotated[int, Field(ge=1, le=99)] | None = None
    detection_classes: list[str] | None = None
    alert_on_objects_only: bool | None = None

    search_enabled: bool | None = None
    embedding_model: str | None = None

    vlm_enabled: bool | None = None
    vlm_provider: Literal["ollama", "openai_compatible", "anthropic"] | None = None
    vlm_url: OptionalHttpUrl = None
    vlm_model: str | None = None
    vlm_api_key: str | None = None
    vlm_daily_digest: bool | None = None

    privacy_ack: bool | None = None
    face_enabled: bool | None = None
    face_threshold: Annotated[int, Field(ge=1, le=99)] | None = None
    alpr_enabled: bool | None = None

    detection_retention_days: Annotated[int, Field(ge=1, le=3650)] | None = None


class DetectionOut(BaseModel):
    id: int
    camera_id: int
    recording_id: int | None
    label: str
    confidence: int
    bbox_x: float
    bbox_y: float
    bbox_w: float
    bbox_h: float
    text: str | None
    description: str | None
    snapshot_path: str | None
    created_at: UtcDatetime

    class Config:
        from_attributes = True


class AITestResult(BaseModel):
    success: bool
    message: str
    backend: str | None = None
    latency_ms: int | None = None
    detections: int | None = None
