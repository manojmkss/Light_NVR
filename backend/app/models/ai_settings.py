from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AISettings(Base):
    """Singleton (id=1) config for the optional AI layer.

    Everything here is off by default and inert until `enabled` is set: a
    stock install runs exactly as it did before, with no AI dependency loaded,
    no model downloaded, and no extra CPU used. That matters because this
    project targets low-power boxes (Pi/mini-PC) where an always-on inference
    pass would be the single most expensive thing running.
    """

    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)

    # ── Master switch ───────────────────────────────────────────────────────
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Where inference actually runs ───────────────────────────────────────
    # "local"  - ONNX Runtime in the backend process, CPU. No extra infra, but
    #            costs CPU on the NVR box itself.
    # "remote" - POST frames to an ai-worker on another machine (e.g. a PC with
    #            a GPU on the LAN). The NVR box then does almost no AI work.
    backend: Mapped[str] = mapped_column(String(16), default="local")
    remote_url: Mapped[str] = mapped_column(String(256), default="")
    remote_api_key: Mapped[str] = mapped_column(String(256), default="")

    # ── Tier 1: object detection ────────────────────────────────────────────
    detection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    detection_model: Mapped[str] = mapped_column(String(64), default="yolov8n")
    # 0-100 rather than a 0-1 float: it's a UI slider, and integers keep the
    # SQLite schema-sync defaults trivial.
    detection_confidence: Mapped[int] = mapped_column(Integer, default=50)
    # CSV of COCO class names to keep. Empty = keep everything the model emits.
    detection_classes: Mapped[str] = mapped_column(
        String(512), default="person,car,truck,bus,motorcycle,bicycle,dog,cat"
    )
    # The whole point of Tier 1: when on, a motion event that contains none of
    # the classes above raises no alert. This is what kills the tree/shadow/
    # rain false positives that make motion-only alerting untrustworthy.
    alert_on_objects_only: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Tier 2: semantic search ─────────────────────────────────────────────
    search_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding_model: Mapped[str] = mapped_column(String(64), default="clip-vit-b32")

    # ── Tier 3: natural-language descriptions (VLM) ─────────────────────────
    vlm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # "ollama" is the default and gets first-class treatment (native API, no
    # key, model auto-discovery): an Ollama box on the same LAN gives you
    # descriptions with no footage leaving the house and no per-call cost.
    # "openai_compatible" covers LM Studio / vLLM / OpenAI; "anthropic" covers
    # Claude directly, for people who want the best descriptions and accept
    # sending snapshots to a third party.
    vlm_provider: Mapped[str] = mapped_column(String(32), default="ollama")
    vlm_url: Mapped[str] = mapped_column(String(256), default="")
    vlm_model: Mapped[str] = mapped_column(String(64), default="")
    vlm_api_key: Mapped[str] = mapped_column(String(256), default="")
    vlm_daily_digest: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Tier 4: recognition (privacy-sensitive) ─────────────────────────────
    # Face recognition and ALPR process other people's biometric/identifying
    # data (visitors, neighbours, delivery staff) and carry real obligations
    # under laws like India's DPDP Act and the GDPR. They stay hard-disabled
    # until the operator explicitly acknowledges that in the UI - a deliberate
    # speed bump, not a dark pattern.
    privacy_ack: Mapped[bool] = mapped_column(Boolean, default=False)
    face_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    face_threshold: Mapped[int] = mapped_column(Integer, default=60)
    alpr_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Detections are metadata about footage; they'd otherwise outlive the clips
    # they describe and grow forever. Pruned by the existing retention loop.
    detection_retention_days: Mapped[int] = mapped_column(Integer, default=30)
